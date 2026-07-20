package admin

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

const (
	grokImportProbeConcurrency = 3
	// Billing + active model probe can exceed 25s on cold Free accounts.
	grokImportProbeTimeout = 45 * time.Second
	// Fresh imports often hit a flaky billing 403; retry a few times so the
	// account list does not stick on "forbidden" until a manual probe.
	grokImportProbeMaxAttempts = 3
	grokImportProbeRetryDelay  = 3 * time.Second
)

type grokImportProber interface {
	QueryQuota(ctx context.Context, accountID int64) (*service.GrokQuotaProbeResult, error)
}

type grokImportProbeTask struct {
	prober    grokImportProber
	accountID int64
}

type grokImportProbeScheduler struct {
	mu          sync.Mutex
	queue       []grokImportProbeTask
	concurrency int
	workers     int
	maxWorkers  int
	timeout     time.Duration
}

var defaultGrokImportProbeScheduler = newGrokImportProbeScheduler(
	grokImportProbeConcurrency,
	grokImportProbeTimeout,
)

func newGrokImportProbeScheduler(concurrency int, timeout time.Duration) *grokImportProbeScheduler {
	if concurrency <= 0 {
		concurrency = 1
	}
	if timeout <= 0 {
		timeout = grokImportProbeTimeout
	}
	return &grokImportProbeScheduler{
		concurrency: concurrency,
		timeout:     timeout,
	}
}

func (s *grokImportProbeScheduler) schedule(prober grokImportProber, account *service.Account) {
	if s == nil || prober == nil || account == nil || account.ID <= 0 {
		return
	}
	if account.Platform != service.PlatformGrok || account.Type != service.AccountTypeOAuth {
		return
	}

	s.mu.Lock()
	s.queue = append(s.queue, grokImportProbeTask{prober: prober, accountID: account.ID})
	if s.workers < s.concurrency {
		s.workers++
		if s.workers > s.maxWorkers {
			s.maxWorkers = s.workers
		}
		go s.worker()
	}
	s.mu.Unlock()
}

func (s *grokImportProbeScheduler) worker() {
	for {
		task, ok := s.nextTask()
		if !ok {
			return
		}
		s.run(task.prober, task.accountID)
	}
}

func (s *grokImportProbeScheduler) nextTask() (grokImportProbeTask, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.queue) == 0 {
		s.workers--
		return grokImportProbeTask{}, false
	}
	task := s.queue[0]
	s.queue[0] = grokImportProbeTask{}
	s.queue = s.queue[1:]
	if len(s.queue) == 0 {
		s.queue = nil
	}
	return task, true
}

func (s *grokImportProbeScheduler) run(prober grokImportProber, accountID int64) {
	defer func() {
		if recovered := recover(); recovered != nil {
			slog.Error(
				"grok_import_active_probe_panic",
				"account_id", accountID,
				"recovery_type", panicType(recovered),
			)
		}
	}()

	// Queue time is intentionally excluded: every imported account is probed,
	// while this timeout only bounds the actual upstream probe execution.
	var lastErr error
	for attempt := 1; attempt <= grokImportProbeMaxAttempts; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), s.timeout)
		result, err := prober.QueryQuota(ctx, accountID)
		cancel()
		if err != nil {
			// Hard probe failures (auth/network/timeout) are not retried here —
			// only the flaky billing-403-without-active-headers case is.
			slog.Warn(
				"grok_import_active_probe_failed",
				"account_id", accountID,
				"attempt", attempt,
				"status", infraerrors.Code(err),
				"reason", infraerrors.Reason(err),
			)
			return
		}
		if result == nil {
			slog.Warn(
				"grok_import_active_probe_failed",
				"account_id", accountID,
				"attempt", attempt,
				"reason", "empty_result",
			)
			return
		}
		if grokImportProbeStillForbidden(result) {
			lastErr = errGrokImportProbeStillForbidden
			slog.Warn(
				"grok_import_active_probe_still_forbidden",
				"account_id", accountID,
				"attempt", attempt,
				"status", result.StatusCode,
				"source", result.Source,
				"headers_observed", result.HeadersObserved,
			)
			if attempt < grokImportProbeMaxAttempts {
				time.Sleep(grokImportProbeRetryDelay)
				continue
			}
			break
		}
		slog.Info(
			"grok_import_active_probe_completed",
			"account_id", accountID,
			"attempt", attempt,
			"model", result.Model,
			"status", result.StatusCode,
			"source", result.Source,
			"headers_observed", result.HeadersObserved,
		)
		return
	}
	if lastErr != nil {
		slog.Warn(
			"grok_import_active_probe_gave_up",
			"account_id", accountID,
			"attempts", grokImportProbeMaxAttempts,
			"error", lastErr.Error(),
		)
	}
}

var (
	errGrokImportProbeStillForbidden = errors.New("still_forbidden")
)

func grokImportProbeStillForbidden(result *service.GrokQuotaProbeResult) bool {
	if result == nil {
		return false
	}
	// Successful active/hybrid probe with observed headers means the account is
	// usable even if billing earlier returned a flaky 403.
	if result.HeadersObserved && result.StatusCode >= 200 && result.StatusCode < 300 {
		return false
	}
	if result.Snapshot != nil && result.Snapshot.StatusCode >= 200 && result.Snapshot.StatusCode < 300 &&
		result.Snapshot.HeadersObserved {
		return false
	}
	if result.StatusCode == 403 {
		return true
	}
	if result.Billing != nil && (result.Billing.StatusCode == 403 ||
		result.Billing.WeeklyStatusCode == 403 ||
		result.Billing.MonthlyStatusCode == 403) {
		// Billing-only 403 without a successful active probe stays forbidden.
		return true
	}
	return false
}

func panicType(value any) string {
	switch value.(type) {
	case string:
		return "string"
	case error:
		return "error"
	default:
		return "unknown"
	}
}

func (h *AccountHandler) scheduleGrokImportProbe(account *service.Account) {
	if h == nil {
		return
	}
	defaultGrokImportProbeScheduler.schedule(h.grokImportProber, account)
}

func (h *GrokOAuthHandler) scheduleGrokImportProbe(account *service.Account) {
	if h == nil {
		return
	}
	defaultGrokImportProbeScheduler.schedule(h.importProber, account)
}

// ProvideAccountHandler injects the Grok active prober for production while
// keeping NewAccountHandler convenient for focused unit tests.
func ProvideAccountHandler(
	adminService service.AdminService,
	oauthService *service.OAuthService,
	openaiOAuthService *service.OpenAIOAuthService,
	geminiOAuthService *service.GeminiOAuthService,
	antigravityOAuthService *service.AntigravityOAuthService,
	grokOAuthService service.GrokOAuthTokenService,
	rateLimitService *service.RateLimitService,
	accountUsageService *service.AccountUsageService,
	accountTestService *service.AccountTestService,
	concurrencyService *service.ConcurrencyService,
	crsSyncService *service.CRSSyncService,
	sessionLimitCache service.SessionLimitCache,
	rpmCache service.RPMCache,
	tokenCacheInvalidator service.TokenCacheInvalidator,
	grokQuotaService *service.GrokQuotaService,
) *AccountHandler {
	handler := NewAccountHandler(
		adminService,
		oauthService,
		openaiOAuthService,
		geminiOAuthService,
		antigravityOAuthService,
		grokOAuthService,
		rateLimitService,
		accountUsageService,
		accountTestService,
		concurrencyService,
		crsSyncService,
		sessionLimitCache,
		rpmCache,
		tokenCacheInvalidator,
	)
	handler.grokImportProber = grokQuotaService
	return handler
}
