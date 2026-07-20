//go:build unit

package xai

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

type ssoAuthCodeFakeClient struct {
	t             *testing.T
	tokenCalls    int
	cookieHeaders []string
}

func (c *ssoAuthCodeFakeClient) Do(req *http.Request) (*http.Response, error) {
	c.cookieHeaders = append(c.cookieHeaders, req.Header.Get("Cookie"))
	rawURL := req.URL.String()
	switch {
	case rawURL == SSOAccountsURL:
		require.Equal(c.t, http.MethodGet, req.Method)
		return ssoDeviceResponse(http.StatusOK, http.Header{"Set-Cookie": {"session=web-session; Path=/"}}, `{}`), nil
	case strings.HasPrefix(rawURL, DefaultAuthorizeURL):
		if req.Method == http.MethodGet {
			// authorize → consent
			q := req.URL.Query()
			require.Equal(c.t, SSOBuildReferrer, q.Get("referrer"))
			require.Equal(c.t, SSOBuildPlan, q.Get("plan"))
			require.Equal(c.t, "S256", q.Get("code_challenge_method"))
			require.NotEmpty(c.t, q.Get("code_challenge"))
			return ssoDeviceResponse(http.StatusFound, http.Header{"Location": {"https://accounts.x.ai/oauth2/consent?x=1"}}, ``), nil
		}
		// consent form POST → callback with code
		require.Equal(c.t, http.MethodPost, req.Method)
		values := readSSODeviceForm(c.t, req)
		require.Equal(c.t, SSOBuildReferrer, values.Get("referrer"))
		require.Equal(c.t, SSOBuildPlan, values.Get("plan"))
		require.Equal(c.t, DefaultClientID, values.Get("client_id"))
		return ssoDeviceResponse(http.StatusFound, http.Header{"Location": {"http://127.0.0.1:56121/callback?code=auth-code-1&state=s"}}, ``), nil
	case rawURL == "https://accounts.x.ai/oauth2/consent?x=1":
		require.Equal(c.t, http.MethodGet, req.Method)
		return ssoDeviceResponse(http.StatusOK, http.Header{"Set-Cookie": {"csrf=csrf-token; Path=/"}}, `<html>consent $ACTION_ID_401b73e22a5e68737d0037e1aa449fef82cd1b35fb</html>`), nil
	case rawURL == SSOTokenURL || rawURL == DefaultTokenURL:
		require.Equal(c.t, http.MethodPost, req.Method)
		c.tokenCalls++
		values := readSSODeviceForm(c.t, req)
		require.Equal(c.t, "authorization_code", values.Get("grant_type"))
		require.Equal(c.t, "auth-code-1", values.Get("code"))
		require.NotEmpty(c.t, values.Get("code_verifier"))
		// Build a minimal JWT with referrer=grok-build
		access := fakeJWTWithReferrer(SSOBuildReferrer)
		return ssoDeviceResponse(http.StatusOK, nil, `{"access_token":"`+access+`","refresh_token":"refresh-token","id_token":"id-token","token_type":"Bearer","expires_in":3600,"scope":"`+SSOBuildScope+`"}`), nil
	default:
		c.t.Fatalf("unexpected request: %s %s", req.Method, rawURL)
		return nil, nil
	}
}

func TestConvertSSOToBuildCompletesAuthCodeFlow(t *testing.T) {
	t.Setenv(EnvClientID, "")
	t.Setenv(EnvAuthorizeURL, "")
	t.Setenv(EnvTokenURL, "")
	t.Setenv(EnvRedirectURI, "")
	client := &ssoAuthCodeFakeClient{t: t}
	token, err := ConvertSSOToBuild(context.Background(), "sso=sso-token; ignored=1", &SSODeviceOptions{
		HTTPClient: client,
	})

	require.NoError(t, err)
	require.NotEmpty(t, token.AccessToken)
	require.Equal(t, "refresh-token", token.RefreshToken)
	require.Equal(t, "id-token", token.IDToken)
	require.Equal(t, SSOBuildScope, token.Scope)
	require.Equal(t, 1, client.tokenCalls)
	require.Equal(t, SSOBuildReferrer, JWTClaimString(DecodeJWTClaims(token.AccessToken), "referrer"))
	require.Contains(t, client.cookieHeaders[0], "sso=sso-token")
	require.Contains(t, client.cookieHeaders[0], "sso-rw=sso-token")
}

func TestConvertSSOToBuildRejectsMissingReferrer(t *testing.T) {
	t.Setenv(EnvClientID, "")
	t.Setenv(EnvAuthorizeURL, "")
	t.Setenv(EnvTokenURL, "")
	t.Setenv(EnvRedirectURI, "")
	client := &ssoMissingReferrerClient{t: t}
	_, err := ConvertSSOToBuild(context.Background(), "sso-token", &SSODeviceOptions{HTTPClient: client})
	require.ErrorIs(t, err, ErrSSOMissingReferrer)
}

func TestNormalizeSSOTokenAcceptsCookieHeader(t *testing.T) {
	require.Equal(t, "token-1", NormalizeSSOToken("Cookie: foo=bar; sso=token-1; sso-rw=token-2"))
	require.Equal(t, "token-2", NormalizeSSOToken("sso-rw=token-2; foo=bar"))
	require.Equal(t, "raw-token", NormalizeSSOToken(" raw-token ; ignored=1"))
}

type ssoMissingReferrerClient struct {
	t *testing.T
}

func (c *ssoMissingReferrerClient) Do(req *http.Request) (*http.Response, error) {
	rawURL := req.URL.String()
	switch {
	case rawURL == SSOAccountsURL:
		return ssoDeviceResponse(http.StatusOK, nil, `{}`), nil
	case strings.HasPrefix(rawURL, DefaultAuthorizeURL) && req.Method == http.MethodGet:
		return ssoDeviceResponse(http.StatusFound, http.Header{"Location": {"https://accounts.x.ai/oauth2/consent"}}, ``), nil
	case rawURL == "https://accounts.x.ai/oauth2/consent":
		return ssoDeviceResponse(http.StatusOK, nil, `<html>consent</html>`), nil
	case strings.HasPrefix(rawURL, DefaultAuthorizeURL) && req.Method == http.MethodPost:
		return ssoDeviceResponse(http.StatusFound, http.Header{"Location": {"http://127.0.0.1:56121/callback?code=c1"}}, ``), nil
	case rawURL == SSOTokenURL || rawURL == DefaultTokenURL:
		access := fakeJWTWithReferrer("sub2api")
		return ssoDeviceResponse(http.StatusOK, nil, `{"access_token":"`+access+`","refresh_token":"r","token_type":"Bearer","expires_in":3600}`), nil
	default:
		c.t.Fatalf("unexpected request: %s %s", req.Method, rawURL)
		return nil, nil
	}
}

func fakeJWTWithReferrer(referrer string) string {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`))
	payloadMap := map[string]any{"referrer": referrer, "sub": "u1"}
	raw, _ := json.Marshal(payloadMap)
	payload := base64.RawURLEncoding.EncodeToString(raw)
	return header + "." + payload + ".sig"
}

func ssoDeviceResponse(status int, header http.Header, body string) *http.Response {
	if header == nil {
		header = http.Header{}
	}
	return &http.Response{
		StatusCode: status,
		Header:     header,
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func readSSODeviceForm(t *testing.T, req *http.Request) url.Values {
	t.Helper()
	raw, err := io.ReadAll(req.Body)
	require.NoError(t, err)
	values, err := url.ParseQuery(string(raw))
	require.NoError(t, err)
	return values
}
