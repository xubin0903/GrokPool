package xai

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

const (
	// SSOBuildScope must include conversations:* so chat works on cli-chat-proxy.
	SSOBuildScope = "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write"
	// SSOBuildReferrer is REQUIRED in authorize + consent. Device-flow tokens lack
	// this claim and cli-chat-proxy returns permission-denied / chat endpoint denied.
	SSOBuildReferrer = "grok-build"
	SSOBuildPlan     = "generic"
	SSOAccountsURL   = "https://accounts.x.ai/"
	SSOTokenURL      = OAuthIssuer + "/oauth2/token"
	// Legacy device endpoints kept for name compatibility with older diagnostics.
	SSODeviceURL         = OAuthIssuer + "/oauth2/device/code"
	SSOVerifyURL         = OAuthIssuer + "/oauth2/device/verify"
	SSOApproveURL        = OAuthIssuer + "/oauth2/device/approve"
	SSOConversionTimeout = 90 * time.Second

	ssoMaxAuthBody     = 2 << 20
	ssoDefaultUA       = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
	ssoDefaultTokenTTL = 6 * time.Hour
	ssoTokenUA         = "grok-pager/0.2.93 grok-shell/0.2.93 (linux; x86_64)"
	ssoGrokVersion     = "0.2.93"
)

var (
	ErrSSOUnauthorized        = errors.New("xai sso unauthorized")
	ErrSSOAuthorizationDenied = errors.New("xai authorization denied")
	ErrSSOMissingReferrer     = errors.New("xai access_token missing referrer=grok-build claim")
)

type SSOHTTPError struct{ Status int }

func (e SSOHTTPError) Error() string { return fmt.Sprintf("xAI OAuth HTTP %d", e.Status) }

type SSODeviceHTTPClient interface {
	Do(*http.Request) (*http.Response, error)
}

// SSODeviceOptions retains the historical name used by callers/tests.
// ConvertSSOToBuild now performs Authorization Code + PKCE with referrer=grok-build
// (NOT device flow). Device flow cannot inject the grok-build claim.
type SSODeviceOptions struct {
	HTTPClient SSODeviceHTTPClient
	UserAgent  string
	Sleep      func(context.Context, time.Duration) error
}

type ssoDeviceFlow struct {
	client    SSODeviceHTTPClient
	userAgent string
	cookies   map[string]string
	sleep     func(context.Context, time.Duration) error
}

func ConvertSSOToBuild(ctx context.Context, ssoToken string, opts *SSODeviceOptions) (*TokenResponse, error) {
	ssoToken = NormalizeSSOToken(ssoToken)
	if ssoToken == "" {
		return nil, ErrSSOUnauthorized
	}
	if opts == nil {
		opts = &SSODeviceOptions{}
	}
	client := opts.HTTPClient
	if client == nil {
		client = &http.Client{
			Timeout: SSOConversionTimeout,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		}
	}
	userAgent := strings.TrimSpace(opts.UserAgent)
	if userAgent == "" {
		userAgent = ssoDefaultUA
	}
	sleep := opts.Sleep
	if sleep == nil {
		sleep = sleepContext
	}

	flow := &ssoDeviceFlow{
		client:    client,
		userAgent: userAgent,
		cookies:   map[string]string{"sso": ssoToken, "sso-rw": ssoToken},
		sleep:     sleep,
	}
	return flow.convertAuthCode(ctx)
}

func (f *ssoDeviceFlow) convertAuthCode(ctx context.Context) (*TokenResponse, error) {
	// 0) Validate SSO session
	status, finalURL, _, err := f.do(ctx, http.MethodGet, SSOAccountsURL, nil)
	if err != nil {
		return nil, err
	}
	if status == http.StatusUnauthorized || strings.Contains(finalURL, "sign-in") || strings.Contains(finalURL, "sign-up") {
		return nil, ErrSSOUnauthorized
	}
	if status < 200 || status >= 400 {
		return nil, fmt.Errorf("validate Grok Web SSO: %w", SSOHTTPError{Status: status})
	}

	// 1) PKCE authorize with referrer=grok-build + plan=generic
	verifier, err := GenerateCodeVerifier()
	if err != nil {
		return nil, err
	}
	challenge := GenerateCodeChallenge(verifier)
	state, err := GenerateState()
	if err != nil {
		return nil, err
	}
	nonce, err := GenerateNonce()
	if err != nil {
		return nil, err
	}
	clientID := EffectiveClientID()
	redirectURI := EffectiveRedirectURI("")
	authorizeURL, err := ValidatedAuthorizeURL()
	if err != nil {
		return nil, fmt.Errorf("invalid authorize url: %w", err)
	}

	params := url.Values{}
	params.Set("response_type", "code")
	params.Set("client_id", clientID)
	params.Set("redirect_uri", redirectURI)
	params.Set("scope", SSOBuildScope)
	params.Set("state", state)
	params.Set("nonce", nonce)
	params.Set("code_challenge", challenge)
	params.Set("code_challenge_method", "S256")
	params.Set("plan", SSOBuildPlan)
	params.Set("referrer", SSOBuildReferrer)
	authURL := authorizeURL + "?" + params.Encode()

	status, finalURL, body, err := f.do(ctx, http.MethodGet, authURL, nil)
	if err != nil {
		return nil, err
	}
	if status == http.StatusUnauthorized || strings.Contains(finalURL, "sign-in") || strings.Contains(finalURL, "sign-up") {
		return nil, ErrSSOUnauthorized
	}
	if code := extractOAuthCode(finalURL, string(body)); code != "" && !strings.Contains(finalURL, "/oauth2/consent") {
		// Rare auto-approve path.
		token, err := f.exchangeCode(ctx, code, verifier, redirectURI, clientID)
		if err != nil {
			return nil, err
		}
		return f.requireGrokBuildReferrer(token)
	}
	if !strings.Contains(finalURL, "/oauth2/consent") {
		return nil, fmt.Errorf("authorize did not reach consent page: status=%d url=%s", status, truncateForErr(finalURL, 180))
	}

	// 2) Submit consent with referrer=grok-build
	code, err := f.submitConsent(ctx, finalURL, string(body), clientID, redirectURI, challenge, state, nonce)
	if err != nil {
		return nil, err
	}
	if code == "" {
		return nil, errors.New("consent response missing authorization code")
	}

	// 3) Exchange code → tokens
	token, err := f.exchangeCode(ctx, code, verifier, redirectURI, clientID)
	if err != nil {
		return nil, err
	}
	return f.requireGrokBuildReferrer(token)
}

func (f *ssoDeviceFlow) requireGrokBuildReferrer(token *TokenResponse) (*TokenResponse, error) {
	if token == nil || strings.TrimSpace(token.AccessToken) == "" {
		return nil, errors.New("empty token response")
	}
	if JWTClaimString(DecodeJWTClaims(token.AccessToken), "referrer") != SSOBuildReferrer {
		return nil, ErrSSOMissingReferrer
	}
	return token, nil
}

func (f *ssoDeviceFlow) submitConsent(
	ctx context.Context,
	consentURL string,
	consentHTML string,
	clientID, redirectURI, challenge, state, nonce string,
) (string, error) {
	// Path A: classic form POST to /oauth2/authorize (local sso2cpa path).
	form := url.Values{}
	form.Set("client_id", clientID)
	form.Set("redirect_uri", redirectURI)
	form.Set("scope", SSOBuildScope)
	form.Set("state", state)
	form.Set("code_challenge", challenge)
	form.Set("code_challenge_method", "S256")
	form.Set("nonce", nonce)
	form.Set("principal_type", "User")
	form.Set("principal_id", "")
	form.Set("referrer", SSOBuildReferrer)
	form.Set("plan", SSOBuildPlan)

	status, finalURL, body, err := f.doOnce(ctx, http.MethodPost, EffectiveAuthorizeURL(), form, map[string]string{
		"Content-Type": "application/x-www-form-urlencoded",
		"Origin":       "https://accounts.x.ai",
		"Referer":      consentURL,
		"Accept":       "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
	}, "")
	if err == nil {
		if code := extractOAuthCode(finalURL, string(body)); code != "" {
			return code, nil
		}
		_ = status
	}

	// Path B: Next.js Server Action on consent page (CPA upstream style).
	actionIDs := extractNextActionIDs(consentHTML)
	if len(actionIDs) == 0 {
		actionIDs = []string{
			"401b73e22a5e68737d0037e1aa449fef82cd1b35fb",
			"4005315a1d7e426de592990bb54bb37471f39dd6d2",
		}
	}
	payloadObj := []map[string]any{{
		"action":              "allow",
		"clientId":            clientID,
		"redirectUri":         redirectURI,
		"scope":               SSOBuildScope,
		"state":               state,
		"codeChallenge":       challenge,
		"codeChallengeMethod": "S256",
		"nonce":               nonce,
		"principalType":       "User",
		"principalId":         "",
		"referrer":            SSOBuildReferrer,
		"plan":                SSOBuildPlan,
	}}
	payloadBytes, _ := json.Marshal(payloadObj)
	var lastErr error
	for _, actionID := range actionIDs {
		if len(actionID) < 20 {
			continue
		}
		status, finalURL, body, err := f.doOnce(ctx, http.MethodPost, consentURL, nil, map[string]string{
			"Content-Type": "text/plain;charset=UTF-8",
			"Accept":       "text/x-component",
			"Origin":       "https://accounts.x.ai",
			"Referer":      consentURL,
			"Next-Action":  actionID,
		}, string(payloadBytes))
		if err != nil {
			lastErr = err
			continue
		}
		if code := extractOAuthCode(finalURL, string(body)); code != "" {
			return code, nil
		}
		lowBody := strings.ToLower(string(body))
		if status == http.StatusNotFound || strings.Contains(lowBody, "server action not found") {
			lastErr = fmt.Errorf("next-action %s invalid", shortID(actionID))
			continue
		}
		lastErr = fmt.Errorf("consent action %s status=%d no code", shortID(actionID), status)
	}
	if lastErr != nil {
		return "", fmt.Errorf("consent failed: %w", lastErr)
	}
	return "", errors.New("consent failed: no authorization code")
}

func (f *ssoDeviceFlow) exchangeCode(ctx context.Context, code, verifier, redirectURI, clientID string) (*TokenResponse, error) {
	tokenURL, err := ValidatedTokenURL()
	if err != nil {
		return nil, fmt.Errorf("invalid token url: %w", err)
	}
	form := url.Values{}
	form.Set("grant_type", "authorization_code")
	form.Set("code", code)
	form.Set("redirect_uri", redirectURI)
	form.Set("client_id", clientID)
	form.Set("code_verifier", verifier)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "*/*")
	req.Header.Set("User-Agent", ssoTokenUA)
	req.Header.Set("X-Grok-Client-Version", ssoGrokVersion)
	if cookie := f.cookieHeader(); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	resp, err := f.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, ssoMaxAuthBody+1))
	if err != nil {
		return nil, err
	}
	if len(data) > ssoMaxAuthBody {
		return nil, errors.New("xAI token response exceeds 2 MiB")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("token exchange failed: %w body=%s", SSOHTTPError{Status: resp.StatusCode}, truncateForErr(string(data), 200))
	}
	var payload struct {
		AccessToken      string `json:"access_token"`
		RefreshToken     string `json:"refresh_token"`
		IDToken          string `json:"id_token"`
		TokenType        string `json:"token_type"`
		ExpiresIn        int64  `json:"expires_in"`
		Scope            string `json:"scope"`
		Error            string `json:"error"`
		ErrorDescription string `json:"error_description"`
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, fmt.Errorf("parse token response: %w", err)
	}
	if payload.AccessToken == "" {
		return nil, fmt.Errorf("token response missing access_token: %s", firstNonEmpty(payload.ErrorDescription, payload.Error, string(data)))
	}
	if payload.ExpiresIn <= 0 {
		payload.ExpiresIn = int64(ssoDefaultTokenTTL.Seconds())
	}
	if payload.TokenType == "" {
		payload.TokenType = "Bearer"
	}
	return &TokenResponse{
		AccessToken:  payload.AccessToken,
		RefreshToken: payload.RefreshToken,
		IDToken:      payload.IDToken,
		TokenType:    payload.TokenType,
		ExpiresIn:    payload.ExpiresIn,
		Scope:        payload.Scope,
	}, nil
}

// do follows redirects within trusted x.ai hosts (manual cookie jar).
func (f *ssoDeviceFlow) do(ctx context.Context, method, endpoint string, form url.Values) (int, string, []byte, error) {
	if !safeXAIAuthURL(endpoint) {
		return 0, "", nil, errors.New("xAI OAuth URL is not trusted")
	}
	currentURL := endpoint
	currentMethod := method
	currentForm := form
	for redirects := 0; redirects <= 8; redirects++ {
		var body io.Reader
		if currentForm != nil {
			body = strings.NewReader(currentForm.Encode())
		}
		request, err := http.NewRequestWithContext(ctx, currentMethod, currentURL, body)
		if err != nil {
			return 0, currentURL, nil, err
		}
		request.Header.Set("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
		request.Header.Set("Accept-Language", "en-US,en;q=0.9")
		request.Header.Set("User-Agent", f.userAgent)
		if cookie := f.cookieHeader(); cookie != "" {
			request.Header.Set("Cookie", cookie)
		}
		if currentForm != nil {
			request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		}

		response, err := f.client.Do(request)
		if err != nil {
			return 0, currentURL, nil, err
		}
		f.captureCookies(response)
		data, readErr := io.ReadAll(io.LimitReader(response.Body, ssoMaxAuthBody+1))
		_ = response.Body.Close()
		if readErr != nil {
			return response.StatusCode, currentURL, nil, readErr
		}
		if len(data) > ssoMaxAuthBody {
			return response.StatusCode, currentURL, nil, errors.New("xAI OAuth response exceeds 2 MiB")
		}
		if response.StatusCode < 300 || response.StatusCode > 399 {
			return response.StatusCode, currentURL, data, nil
		}

		location := strings.TrimSpace(response.Header.Get("Location"))
		if location == "" {
			return response.StatusCode, currentURL, data, errors.New("xAI OAuth redirect missing Location")
		}
		base, _ := url.Parse(currentURL)
		next, err := url.Parse(location)
		if err != nil {
			return response.StatusCode, currentURL, data, err
		}
		resolved := base.ResolveReference(next).String()
		// Allow loopback callback (authorization code lands there).
		if isLoopbackCallback(resolved) {
			return response.StatusCode, resolved, data, nil
		}
		if !safeXAIAuthURL(resolved) {
			return response.StatusCode, resolved, data, errors.New("xAI OAuth redirected to untrusted host")
		}
		currentURL = resolved
		if response.StatusCode == http.StatusSeeOther || ((response.StatusCode == http.StatusMovedPermanently || response.StatusCode == http.StatusFound) && currentMethod != http.MethodGet && currentMethod != http.MethodHead) {
			currentMethod = http.MethodGet
			currentForm = nil
		}
	}
	return 0, currentURL, nil, errors.New("xAI OAuth redirected too many times")
}

// doOnce does a single request without auto-following redirects.
func (f *ssoDeviceFlow) doOnce(ctx context.Context, method, endpoint string, form url.Values, headers map[string]string, rawBody string) (int, string, []byte, error) {
	if !safeXAIAuthURL(endpoint) && !isLoopbackCallback(endpoint) {
		return 0, "", nil, errors.New("xAI OAuth URL is not trusted")
	}
	var bodyReader io.Reader
	if rawBody != "" {
		bodyReader = strings.NewReader(rawBody)
	} else if form != nil {
		bodyReader = strings.NewReader(form.Encode())
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint, bodyReader)
	if err != nil {
		return 0, endpoint, nil, err
	}
	request.Header.Set("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
	request.Header.Set("Accept-Language", "en-US,en;q=0.9")
	request.Header.Set("User-Agent", f.userAgent)
	if cookie := f.cookieHeader(); cookie != "" {
		request.Header.Set("Cookie", cookie)
	}
	if form != nil && rawBody == "" {
		request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	for k, v := range headers {
		request.Header.Set(k, v)
	}
	response, err := f.client.Do(request)
	if err != nil {
		return 0, endpoint, nil, err
	}
	f.captureCookies(response)
	data, readErr := io.ReadAll(io.LimitReader(response.Body, ssoMaxAuthBody+1))
	_ = response.Body.Close()
	if readErr != nil {
		return response.StatusCode, endpoint, nil, readErr
	}
	if len(data) > ssoMaxAuthBody {
		return response.StatusCode, endpoint, nil, errors.New("xAI OAuth response exceeds 2 MiB")
	}
	finalURL := endpoint
	if loc := strings.TrimSpace(response.Header.Get("Location")); loc != "" {
		base, _ := url.Parse(endpoint)
		next, err := url.Parse(loc)
		if err == nil && base != nil {
			finalURL = base.ResolveReference(next).String()
		} else if err == nil {
			finalURL = next.String()
		} else {
			finalURL = loc
		}
	}
	return response.StatusCode, finalURL, data, nil
}

func (f *ssoDeviceFlow) captureCookies(response *http.Response) {
	for _, cookie := range response.Cookies() {
		name := strings.TrimSpace(cookie.Name)
		value := strings.TrimSpace(cookie.Value)
		if name == "" || len(name) > 128 || len(value) > 16384 || strings.ContainsAny(name+value, "\r\n\x00") {
			continue
		}
		if cookie.MaxAge < 0 {
			delete(f.cookies, name)
			continue
		}
		f.cookies[name] = value
	}
}

func (f *ssoDeviceFlow) cookieHeader() string {
	keys := make([]string, 0, len(f.cookies))
	for key := range f.cookies {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, key+"="+f.cookies[key])
	}
	return strings.Join(parts, "; ")
}

func safeXAIAuthURL(raw string) bool {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.User != nil || parsed.Hostname() == "" {
		return false
	}
	if AllowUnsafeURLOverrides() {
		return parsed.Scheme != "" && parsed.Host != ""
	}
	if parsed.Scheme != "https" {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	return host == "x.ai" || strings.HasSuffix(host, ".x.ai")
}

func NormalizeSSOToken(value string) string {
	value = strings.TrimSpace(value)
	if strings.HasPrefix(strings.ToLower(value), "cookie:") {
		value = strings.TrimSpace(value[len("cookie:"):])
	}
	for _, part := range strings.Split(value, ";") {
		name, token, found := strings.Cut(strings.TrimSpace(part), "=")
		if !found {
			continue
		}
		switch strings.ToLower(strings.TrimSpace(name)) {
		case "sso", "sso-rw":
			return sanitizeSSOToken(token)
		}
	}
	if token, _, found := strings.Cut(value, ";"); found {
		value = strings.TrimSpace(token)
	}
	return sanitizeSSOToken(value)
}

func sanitizeSSOToken(value string) string {
	return strings.NewReplacer("\r", "", "\n", "", "\x00", "").Replace(strings.TrimSpace(value))
}

func DecodeJWTClaims(token string) map[string]any {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return nil
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil
	}
	var claims map[string]any
	if err := json.Unmarshal(payload, &claims); err != nil {
		return nil
	}
	return claims
}

func JWTClaimString(claims map[string]any, key string) string {
	if claims == nil {
		return ""
	}
	value, _ := claims[key].(string)
	return strings.TrimSpace(value)
}

func sleepContext(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func extractOAuthCode(finalURL, body string) string {
	if finalURL != "" {
		if u, err := url.Parse(finalURL); err == nil {
			if code := strings.TrimSpace(u.Query().Get("code")); code != "" {
				return code
			}
			if frag := u.Fragment; frag != "" {
				if vals, err := url.ParseQuery(frag); err == nil {
					if code := strings.TrimSpace(vals.Get("code")); code != "" {
						return code
					}
				}
			}
		}
	}
	// Next.js text/x-component / JSON lines
	for _, line := range strings.Split(body, "\n") {
		start := strings.Index(line, "{")
		if start < 0 {
			continue
		}
		var obj map[string]any
		if err := json.Unmarshal([]byte(line[start:]), &obj); err != nil {
			continue
		}
		if code, _ := obj["code"].(string); strings.TrimSpace(code) != "" {
			if success, ok := obj["success"].(bool); ok && !success {
				continue
			}
			return strings.TrimSpace(code)
		}
	}
	const needle = `"code":"`
	if i := strings.Index(body, needle); i >= 0 {
		rest := body[i+len(needle):]
		if j := strings.Index(rest, `"`); j > 0 {
			return rest[:j]
		}
	}
	return ""
}

func extractNextActionIDs(html string) []string {
	if html == "" {
		return nil
	}
	found := make([]string, 0, 8)
	seen := map[string]struct{}{}
	add := func(v string) {
		v = strings.ToLower(strings.TrimSpace(v))
		if len(v) < 40 {
			return
		}
		if _, ok := seen[v]; ok {
			return
		}
		seen[v] = struct{}{}
		found = append(found, v)
	}
	for _, part := range strings.Split(html, "$ACTION_ID_") {
		if part == html {
			continue
		}
		id := takeHexPrefix(part, 44)
		if len(id) >= 40 {
			add(id)
		}
	}
	markers := []string{`createServerReference("`, `createServerReference('`, `"actionId":"`, `"id":"`}
	for _, m := range markers {
		rest := html
		for {
			i := strings.Index(rest, m)
			if i < 0 {
				break
			}
			rest = rest[i+len(m):]
			id := takeHexPrefix(rest, 44)
			if len(id) >= 40 {
				add(id)
			}
		}
	}
	return found
}

func takeHexPrefix(s string, max int) string {
	id := ""
	for _, ch := range s {
		if (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') || (ch >= 'A' && ch <= 'F') {
			id += string(ch)
			if len(id) >= max {
				break
			}
			continue
		}
		break
	}
	return id
}

func isLoopbackCallback(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	return host == "127.0.0.1" || host == "localhost"
}

func truncateForErr(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func shortID(id string) string {
	if len(id) <= 12 {
		return id
	}
	return id[:12]
}
