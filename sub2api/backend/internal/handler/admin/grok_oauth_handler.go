package admin

import (
	"context"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/handler/dto"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/pkg/response"
	"github.com/Wei-Shaw/sub2api/internal/pkg/xai"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

const grokSSOImportConcurrency = 3

type GrokOAuthHandler struct {
	grokOAuthService *service.GrokOAuthService
	adminService     service.AdminService
	quotaService     *service.GrokQuotaService
	importProber     grokImportProber
	reconciler       service.GrokOAuthReconciler
}

func NewGrokOAuthHandler(
	grokOAuthService *service.GrokOAuthService,
	adminService service.AdminService,
	quotaService *service.GrokQuotaService,
	reconciler service.GrokOAuthReconciler,
) *GrokOAuthHandler {
	return &GrokOAuthHandler{
		grokOAuthService: grokOAuthService,
		adminService:     adminService,
		quotaService:     quotaService,
		importProber:     quotaService,
		reconciler:       reconciler,
	}
}

type GrokGenerateAuthURLRequest struct {
	ProxyID     *int64 `json:"proxy_id"`
	RedirectURI string `json:"redirect_uri"`
}

func (h *GrokOAuthHandler) GenerateAuthURL(c *gin.Context) {
	var req GrokGenerateAuthURLRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		req = GrokGenerateAuthURLRequest{}
	}
	result, err := h.grokOAuthService.GenerateAuthURL(c.Request.Context(), req.ProxyID, req.RedirectURI)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}

type GrokExchangeCodeRequest struct {
	SessionID   string `json:"session_id" binding:"required"`
	Code        string `json:"code" binding:"required"`
	State       string `json:"state"`
	RedirectURI string `json:"redirect_uri"`
	ProxyID     *int64 `json:"proxy_id"`
}

func (h *GrokOAuthHandler) ExchangeCode(c *gin.Context) {
	var req GrokExchangeCodeRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	tokenInfo, err := h.grokOAuthService.ExchangeCode(c.Request.Context(), &service.GrokExchangeCodeInput{
		SessionID:   req.SessionID,
		Code:        req.Code,
		State:       req.State,
		RedirectURI: req.RedirectURI,
		ProxyID:     req.ProxyID,
	})
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, tokenInfo)
}

type GrokRefreshTokenRequest struct {
	RefreshToken string `json:"refresh_token"`
	RT           string `json:"rt"`
	ClientID     string `json:"client_id"`
	ProxyID      *int64 `json:"proxy_id"`
}

func (h *GrokOAuthHandler) RefreshToken(c *gin.Context) {
	var req GrokRefreshTokenRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	refreshToken := strings.TrimSpace(req.RefreshToken)
	if refreshToken == "" {
		refreshToken = strings.TrimSpace(req.RT)
	}
	if refreshToken == "" {
		response.BadRequest(c, "refresh_token is required")
		return
	}

	var proxyURL string
	if req.ProxyID != nil {
		proxy, err := h.adminService.GetProxy(c.Request.Context(), *req.ProxyID)
		if err == nil && proxy != nil {
			proxyURL = proxy.URL()
		}
	}
	tokenInfo, err := h.grokOAuthService.RefreshToken(c.Request.Context(), refreshToken, proxyURL, req.ClientID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, tokenInfo)
}

func (h *GrokOAuthHandler) RefreshAccountToken(c *gin.Context) {
	accountID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		response.BadRequest(c, "Invalid account ID")
		return
	}
	account, err := h.adminService.GetAccount(c.Request.Context(), accountID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	if account.Platform != service.PlatformGrok {
		response.BadRequest(c, "Account platform does not match Grok OAuth endpoint")
		return
	}
	if !account.IsOAuth() {
		response.BadRequest(c, "Cannot refresh non-OAuth account credentials")
		return
	}
	tokenInfo, err := h.grokOAuthService.RefreshAccountToken(c.Request.Context(), account)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	newCredentials := h.grokOAuthService.BuildAccountCredentials(tokenInfo)
	newCredentials = service.MergeCredentials(account.Credentials, newCredentials)
	if baseURL := strings.TrimSpace(account.GetCredential("base_url")); baseURL != "" {
		newCredentials["base_url"] = baseURL
	}
	updatedAccount, err := h.adminService.UpdateAccount(c.Request.Context(), accountID, &service.UpdateAccountInput{
		Credentials: newCredentials,
	})
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, dto.AccountFromService(updatedAccount))
}

type GrokOAuthReconcileRequest struct {
	DryRun               *bool `json:"dry_run"`
	Apply                bool  `json:"apply"`
	AfterID              int64 `json:"after_id"`
	Limit                int   `json:"limit"`
	RefreshWindowSeconds int64 `json:"refresh_window_seconds"`
}

func (h *GrokOAuthHandler) ReconcileOAuthAccounts(c *gin.Context) {
	var req GrokOAuthReconcileRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request")
		return
	}
	dryRun := true
	if req.DryRun != nil {
		dryRun = *req.DryRun
	}
	if req.Apply == dryRun {
		response.ErrorFrom(c, service.ErrGrokOAuthReconcileMode)
		return
	}
	if req.RefreshWindowSeconds < 0 || req.RefreshWindowSeconds > int64((24*time.Hour)/time.Second) {
		response.ErrorFrom(c, service.ErrGrokOAuthReconcileWindow)
		return
	}
	if h.reconciler == nil {
		response.InternalError(c, "Grok OAuth reconciliation service is unavailable")
		return
	}
	result, err := h.reconciler.ReconcileGrokOAuth(c.Request.Context(), service.GrokOAuthReconcileInput{
		DryRun:        dryRun,
		Apply:         req.Apply,
		AfterID:       req.AfterID,
		Limit:         req.Limit,
		RefreshWindow: time.Duration(req.RefreshWindowSeconds) * time.Second,
	})
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}

func (h *GrokOAuthHandler) CreateAccountFromOAuth(c *gin.Context) {
	var req struct {
		SessionID   string  `json:"session_id" binding:"required"`
		Code        string  `json:"code" binding:"required"`
		State       string  `json:"state"`
		RedirectURI string  `json:"redirect_uri"`
		ProxyID     *int64  `json:"proxy_id"`
		Name        string  `json:"name"`
		Concurrency int     `json:"concurrency"`
		Priority    int     `json:"priority"`
		GroupIDs    []int64 `json:"group_ids"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	tokenInfo, err := h.grokOAuthService.ExchangeCode(c.Request.Context(), &service.GrokExchangeCodeInput{
		SessionID:   req.SessionID,
		Code:        req.Code,
		State:       req.State,
		RedirectURI: req.RedirectURI,
		ProxyID:     req.ProxyID,
	})
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	credentials := h.grokOAuthService.BuildAccountCredentials(tokenInfo)

	name := strings.TrimSpace(req.Name)
	if name == "" && tokenInfo.Email != "" {
		name = tokenInfo.Email
	}
	if name == "" {
		name = "Grok OAuth Account"
	}

	account, err := h.adminService.CreateAccount(c.Request.Context(), &service.CreateAccountInput{
		Name:        name,
		Platform:    service.PlatformGrok,
		Type:        service.AccountTypeOAuth,
		Credentials: credentials,
		ProxyID:     req.ProxyID,
		Concurrency: req.Concurrency,
		Priority:    req.Priority,
		GroupIDs:    req.GroupIDs,
	})
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	h.scheduleGrokImportProbe(account)
	response.Success(c, dto.AccountFromService(account))
}

type GrokSSOToOAuthRequest struct {
	SSOTokens          []string       `json:"sso_tokens"`
	SSOToken           string         `json:"sso_token"`
	Name               string         `json:"name"`
	Notes              *string        `json:"notes"`
	ProxyID            *int64         `json:"proxy_id"`
	GroupIDs           []int64        `json:"group_ids"`
	Credentials        map[string]any `json:"credentials"`
	Extra              map[string]any `json:"extra"`
	Concurrency        int            `json:"concurrency"`
	LoadFactor         *int           `json:"load_factor"`
	Priority           int            `json:"priority"`
	RateMultiplier     *float64       `json:"rate_multiplier"`
	ExpiresAt          *int64         `json:"expires_at"`
	AutoPauseOnExpired *bool          `json:"auto_pause_on_expired"`
}

type GrokSSOToOAuthItemResult struct {
	Index   int          `json:"index"`
	Name    string       `json:"name,omitempty"`
	Email   string       `json:"email,omitempty"`
	Account *dto.Account `json:"account,omitempty"`
	Error   string       `json:"error,omitempty"`
}

type GrokSSOToOAuthResponse struct {
	Created []GrokSSOToOAuthItemResult `json:"created"`
	Failed  []GrokSSOToOAuthItemResult `json:"failed"`
}

type grokSSOImportJob struct {
	index int
	token string
}

type grokSSOImportWorkerResult struct {
	created bool
	item    GrokSSOToOAuthItemResult
}

func (h *GrokOAuthHandler) CreateAccountsFromSSO(c *gin.Context) {
	var req GrokSSOToOAuthRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	tokens := normalizeSSOImportTokens(req.SSOTokens, req.SSOToken)
	if len(tokens) == 0 {
		response.BadRequest(c, "sso_tokens is required")
		return
	}

	ctx := c.Request.Context()
	workerCount := grokSSOImportConcurrency
	if len(tokens) < workerCount {
		workerCount = len(tokens)
	}
	jobs := make(chan grokSSOImportJob)
	items := make([]grokSSOImportWorkerResult, len(tokens))
	var wg sync.WaitGroup
	for i := 0; i < workerCount; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for job := range jobs {
				items[job.index] = h.safeCreateAccountFromSSOToken(ctx, req, job.token, job.index+1, len(tokens))
			}
		}()
	}
	for i, token := range tokens {
		jobs <- grokSSOImportJob{index: i, token: token}
	}
	close(jobs)
	wg.Wait()

	result := GrokSSOToOAuthResponse{
		Created: make([]GrokSSOToOAuthItemResult, 0, len(tokens)),
		Failed:  make([]GrokSSOToOAuthItemResult, 0),
	}
	for _, item := range items {
		if item.created {
			result.Created = append(result.Created, item.item)
		} else {
			result.Failed = append(result.Failed, item.item)
		}
	}
	response.Success(c, result)
}

func (h *GrokOAuthHandler) safeCreateAccountFromSSOToken(ctx context.Context, req GrokSSOToOAuthRequest, token string, index, total int) (result grokSSOImportWorkerResult) {
	defer func() {
		if recovered := recover(); recovered != nil {
			slog.Error("grok_sso_import_worker_panic", "index", index, "recover", recovered)
			result = grokSSOImportWorkerResult{
				item: GrokSSOToOAuthItemResult{
					Index: index,
					Error: fmt.Sprintf("internal worker panic: %v", recovered),
				},
			}
		}
	}()
	return h.createAccountFromSSOToken(ctx, req, token, index, total)
}

func (h *GrokOAuthHandler) createAccountFromSSOToken(ctx context.Context, req GrokSSOToOAuthRequest, token string, index, total int) grokSSOImportWorkerResult {
	tokenInfo, err := h.grokOAuthService.ConvertFromSSO(ctx, token, req.ProxyID)
	if err != nil {
		return grokSSOImportWorkerResult{item: GrokSSOToOAuthItemResult{Index: index, Error: grokSSOImportErrorMessage(err)}}
	}

	credentials := grokSSOImportCredentials(h.grokOAuthService.BuildAccountCredentials(tokenInfo), req.Credentials)
	name := grokSSOImportAccountName(req.Name, tokenInfo, index, total)
	expiresAt, autoPauseOnExpired := grokSSOImportExpiry(req.ExpiresAt, req.AutoPauseOnExpired, tokenInfo)
	account, err := h.adminService.CreateAccount(ctx, &service.CreateAccountInput{
		Name:               name,
		Notes:              req.Notes,
		Platform:           service.PlatformGrok,
		Type:               service.AccountTypeOAuth,
		Credentials:        credentials,
		Extra:              cloneGrokSSOMap(req.Extra),
		ProxyID:            req.ProxyID,
		Concurrency:        req.Concurrency,
		LoadFactor:         req.LoadFactor,
		Priority:           req.Priority,
		RateMultiplier:     req.RateMultiplier,
		GroupIDs:           append([]int64(nil), req.GroupIDs...),
		ExpiresAt:          expiresAt,
		AutoPauseOnExpired: autoPauseOnExpired,
	})
	if err != nil {
		return grokSSOImportWorkerResult{item: GrokSSOToOAuthItemResult{Index: index, Name: name, Email: tokenInfo.Email, Error: grokSSOImportErrorMessage(err)}}
	}
	h.scheduleGrokImportProbe(account)
	return grokSSOImportWorkerResult{
		created: true,
		item: GrokSSOToOAuthItemResult{
			Index:   index,
			Name:    name,
			Email:   tokenInfo.Email,
			Account: dto.AccountFromService(account),
		},
	}
}

// grokSSOImportCredentials 合并 SSO 兑换出的凭据与导入请求携带的运营侧配置。
// token 字段以 BuildAccountCredentials 为准（请求不可覆盖）；但 base_url 是运营侧
// 配置且 Build 恒写官方地址，会吞掉导入时指定的自定义转发地址——与
// RefreshAccountToken 的保留逻辑对齐，请求显式提供时以请求为准。
func grokSSOImportCredentials(built map[string]any, reqCredentials map[string]any) map[string]any {
	credentials := service.MergeCredentials(cloneGrokSSOMap(reqCredentials), built)
	if reqBaseURL, ok := reqCredentials["base_url"].(string); ok && strings.TrimSpace(reqBaseURL) != "" {
		credentials["base_url"] = strings.TrimSpace(reqBaseURL)
	}
	return credentials
}

func grokSSOImportExpiry(requestExpiresAt *int64, requestAutoPause *bool, tokenInfo *service.GrokTokenInfo) (*int64, *bool) {
	if tokenInfo == nil || strings.TrimSpace(tokenInfo.RefreshToken) != "" || tokenInfo.ExpiresAt <= 0 {
		return requestExpiresAt, requestAutoPause
	}

	expiresAt := tokenInfo.ExpiresAt
	if requestExpiresAt != nil && *requestExpiresAt > 0 && *requestExpiresAt < expiresAt {
		expiresAt = *requestExpiresAt
	}
	autoPause := true
	return &expiresAt, &autoPause
}

func cloneGrokSSOMap(source map[string]any) map[string]any {
	if source == nil {
		return nil
	}
	clone := make(map[string]any, len(source))
	for key, value := range source {
		clone[key] = cloneGrokSSOValue(value)
	}
	return clone
}

func cloneGrokSSOValue(value any) any {
	switch v := value.(type) {
	case map[string]any:
		return cloneGrokSSOMap(v)
	case []any:
		clone := make([]any, len(v))
		for i, item := range v {
			clone[i] = cloneGrokSSOValue(item)
		}
		return clone
	default:
		return value
	}
}

func normalizeSSOImportTokens(tokens []string, single string) []string {
	items := make([]string, 0, len(tokens)+1)
	if strings.TrimSpace(single) != "" {
		items = append(items, single)
	}
	items = append(items, tokens...)
	seen := make(map[string]struct{}, len(items))
	result := make([]string, 0, len(items))
	for _, item := range items {
		parts := strings.Split(strings.NewReplacer(",", "\n", "\r", "\n").Replace(item), "\n")
		for _, token := range parts {
			if token = xai.NormalizeSSOToken(token); token == "" {
				continue
			}
			if _, ok := seen[token]; ok {
				continue
			}
			seen[token] = struct{}{}
			result = append(result, token)
		}
	}
	return result
}

func grokSSOImportAccountName(base string, tokenInfo *service.GrokTokenInfo, index, total int) string {
	base = strings.TrimSpace(base)
	if base == "" && tokenInfo != nil {
		base = strings.TrimSpace(tokenInfo.Email)
	}
	if base == "" {
		base = "Grok OAuth Account"
	}
	if total > 1 {
		return base + " #" + strconv.Itoa(index)
	}
	return base
}

func grokSSOImportErrorMessage(err error) string {
	status := infraerrors.FromError(err)
	if status == nil {
		return ""
	}
	if status.Reason != "" {
		return status.Reason + ": " + status.Message
	}
	return status.Message
}

func (h *GrokOAuthHandler) QueryQuota(c *gin.Context) {
	accountID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		response.BadRequest(c, "Invalid account ID")
		return
	}
	if h.quotaService == nil {
		response.BadRequest(c, "grok quota service is not enabled")
		return
	}
	result, err := h.quotaService.QueryQuota(c.Request.Context(), accountID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}

type grokProbeDeadRequest struct {
	AccountIDs []int64 `json:"account_ids"`
	// DeleteDead when true deletes accounts classified as permanently dead.
	DeleteDead bool `json:"delete_dead"`
}

type grokProbeDeadItem struct {
	AccountID            int64  `json:"account_id"`
	Name                 string `json:"name,omitempty"`
	Email                string `json:"email,omitempty"`
	Alive                bool   `json:"alive"`
	Dead                 bool   `json:"dead"`
	Unknown              bool   `json:"unknown"`
	Reason               string `json:"reason,omitempty"`
	StatusCode           int    `json:"status_code,omitempty"`
	Source               string `json:"source,omitempty"`
	HeadersObserved      bool   `json:"headers_observed"`
	ProbeError           string `json:"probe_error,omitempty"`
	SchedulableDisabled  bool   `json:"schedulable_disabled,omitempty"`
	Deleted              bool   `json:"deleted,omitempty"`
	DeleteError          string `json:"delete_error,omitempty"`
	Error                string `json:"error,omitempty"`
}

type grokProbeDeadResponse struct {
	Total     int                 `json:"total"`
	Alive     int                 `json:"alive"`
	Dead      int                 `json:"dead"`
	Unknown   int                 `json:"unknown"`
	Deleted   int                 `json:"deleted"`
	Items     []grokProbeDeadItem `json:"items"`
}

// ProbeDeadAccounts bulk-probes selected Grok OAuth accounts and optionally deletes
// those classified as permanently dead (revoked/entitlement/401), not flaky billing 403.
func (h *GrokOAuthHandler) ProbeDeadAccounts(c *gin.Context) {
	if h.quotaService == nil {
		response.BadRequest(c, "grok quota service is not enabled")
		return
	}
	var req grokProbeDeadRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		response.BadRequest(c, "Invalid request: "+err.Error())
		return
	}
	if len(req.AccountIDs) == 0 {
		response.BadRequest(c, "account_ids is required")
		return
	}
	// Soft safety cap only — large enough for full-pool admin bulk ops.
	const grokProbeDeadMaxBatch = 5000
	if len(req.AccountIDs) > grokProbeDeadMaxBatch {
		response.BadRequest(c, fmt.Sprintf("account_ids max is %d per request", grokProbeDeadMaxBatch))
		return
	}

	// de-dupe while preserving order
	seen := make(map[int64]struct{}, len(req.AccountIDs))
	ids := make([]int64, 0, len(req.AccountIDs))
	for _, id := range req.AccountIDs {
		if id <= 0 {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		ids = append(ids, id)
	}
	if len(ids) == 0 {
		response.BadRequest(c, "no valid account_ids")
		return
	}

	type jobResult struct {
		item grokProbeDeadItem
	}
	results := make([]jobResult, len(ids))
	var wg sync.WaitGroup
	sem := make(chan struct{}, 3)
	for i, id := range ids {
		wg.Add(1)
		go func(idx int, accountID int64) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			item := grokProbeDeadItem{AccountID: accountID}
			acc, err := h.adminService.GetAccount(c.Request.Context(), accountID)
			if err != nil {
				item.Error = err.Error()
				item.Unknown = true
				item.Reason = "account_not_found"
				results[idx] = jobResult{item: item}
				return
			}
			item.Name = acc.Name
			if acc.Credentials != nil {
				if email, ok := acc.Credentials["email"].(string); ok {
					item.Email = email
				}
			}
			if acc.Platform != service.PlatformGrok || acc.Type != service.AccountTypeOAuth {
				item.Error = "not_grok_oauth"
				item.Unknown = true
				item.Reason = "skipped_non_grok_oauth"
				results[idx] = jobResult{item: item}
				return
			}

			live := h.quotaService.ProbeAccountLiveness(c.Request.Context(), accountID)
			item.Alive = live.Alive
			item.Dead = live.Dead
			item.Unknown = live.Unknown
			item.Reason = live.Reason
			item.StatusCode = live.StatusCode
			item.Source = live.Source
			item.HeadersObserved = live.HeadersObserved
			item.ProbeError = live.ProbeError

			notesText := ""
				if acc.Notes != nil {
					notesText = strings.TrimSpace(*acc.Notes)
				}
				prevMarkedDead := strings.HasPrefix(strings.TrimSpace(acc.ErrorMessage), "DEAD:") ||
					strings.HasPrefix(notesText, "DEAD:")

			switch {
			case live.Alive:
				// Clear stale DEAD marks from earlier false positives.
				if prevMarkedDead || !acc.Schedulable || acc.Status != service.StatusActive {
					if _, err := h.adminService.ClearAccountError(c.Request.Context(), accountID); err != nil {
						slog.Warn("grok_probe_alive_clear_error_failed", "account_id", accountID, "error", err)
					}
					if _, err := h.adminService.SetAccountSchedulable(c.Request.Context(), accountID, true); err != nil {
						slog.Warn("grok_probe_alive_reschedule_failed", "account_id", accountID, "error", err)
					}
					if strings.HasPrefix(notesText, "DEAD:") {
						empty := ""
						if _, err := h.adminService.UpdateAccount(c.Request.Context(), accountID, &service.UpdateAccountInput{Notes: &empty}); err != nil {
							slog.Warn("grok_probe_alive_clear_notes_failed", "account_id", accountID, "error", err)
						}
					}
				}
				item.Reason = firstNonEmpty(item.Reason, "alive")
			case live.Dead:
				// Permanently disable the account — 402/403/401 won't self-recover.
				msg := "DEAD: " + live.Reason
				if live.ProbeError != "" {
					msg = msg + " | " + live.ProbeError
				}
				if live.StatusCode > 0 {
					msg = fmt.Sprintf("%s | status=%d", msg, live.StatusCode)
				}
				if acc.Status != service.StatusActive {
					if _, err := h.adminService.ClearAccountError(c.Request.Context(), accountID); err != nil {
						slog.Warn("grok_probe_dead_restore_active_failed", "account_id", accountID, "error", err)
					}
				}
				disabled := service.StatusDisabled
				if _, err := h.adminService.UpdateAccount(c.Request.Context(), accountID, &service.UpdateAccountInput{
					Status: disabled,
				}); err != nil {
					slog.Warn("grok_probe_dead_disable_status_failed", "account_id", accountID, "error", err)
				}
				if _, err := h.adminService.SetAccountSchedulable(c.Request.Context(), accountID, false); err != nil {
					slog.Warn("grok_probe_dead_unschedulable_failed", "account_id", accountID, "error", err)
				} else {
					item.SchedulableDisabled = true
				}
				notes := msg
				if len(notes) > 500 {
					notes = notes[:500]
				}
				if _, err := h.adminService.UpdateAccount(c.Request.Context(), accountID, &service.UpdateAccountInput{
					Notes: &notes,
				}); err != nil {
					slog.Warn("grok_probe_dead_notes_failed", "account_id", accountID, "error", err)
				}
			default:
				// unknown: keep previous DEAD mark only if still present; do not invent new DEAD.
				if prevMarkedDead {
					item.Dead = true
					item.Unknown = false
					item.Alive = false
					if item.Reason == "" || item.Reason == "unprobed" || item.Reason == "inconclusive" {
						item.Reason = "previously_marked_dead"
					}
				}
			}

			// Delete when classified dead now, or still carrying a DEAD mark and not proven alive.
			shouldDelete := req.DeleteDead && (item.Dead || (prevMarkedDead && !live.Alive))
			if shouldDelete {
				if delErr := h.adminService.DeleteAccount(c.Request.Context(), accountID); delErr != nil {
					item.DeleteError = delErr.Error()
				} else {
					item.Deleted = true
					item.Dead = true
					item.Unknown = false
					item.Alive = false
				}
			}
			results[idx] = jobResult{item: item}
		}(i, id)
	}
	wg.Wait()

	resp := grokProbeDeadResponse{Items: make([]grokProbeDeadItem, 0, len(results))}
	for _, r := range results {
		item := r.item
		resp.Total++
		switch {
		case item.Deleted:
			resp.Deleted++
			resp.Dead++
		case item.Dead:
			resp.Dead++
		case item.Alive:
			resp.Alive++
		default:
			resp.Unknown++
		}
		resp.Items = append(resp.Items, item)
	}
	response.Success(c, resp)
}

func (h *GrokOAuthHandler) ResetQuota(c *gin.Context) {
	accountID, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		response.BadRequest(c, "Invalid account ID")
		return
	}
	if h.quotaService == nil {
		response.BadRequest(c, "grok quota service is not enabled")
		return
	}
	result, err := h.quotaService.ResetQuota(c.Request.Context(), accountID)
	if err != nil {
		response.ErrorFrom(c, err)
		return
	}
	response.Success(c, result)
}

func (h *GrokOAuthHandler) RuntimeSanity(c *gin.Context) {
	response.Success(c, xai.RuntimeSanity())
}
