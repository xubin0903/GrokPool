# GrokPool patches on upstream Sub2API

All patches live under `sub2api/` and are baked into image `grokpool-sub2api:local`.

## 1. Free-tier Grok scheduling

Files:
- `backend/internal/service/openai_gateway_scheduling.go`
- `backend/internal/service/openai_account_scheduler.go`
- `backend/internal/service/openai_gateway_grok.go`
- `backend/internal/service/openai_gateway_grok_cache.go`
- `backend/internal/service/grok_token_provider.go`

Intent:
- Prefer free accounts with more remaining tokens (~1M windows)
- Soft fit / headroom scoring for Grok OAuth
- Shorter free-account 429 cooldowns
- Avoid queuing when free slots still exist (TopK overflow / free-slot scan / sticky escape)

Details: `docs/SCHEDULER.md`

## 2. Dead-account probe

Files:
- `backend/internal/service/grok_quota_service.go` — `ClassifyGrokAccountLiveness`
- `backend/internal/handler/admin/grok_oauth_handler.go` — `ProbeDeadAccounts`
- `backend/internal/handler/admin/grok_import_probe.go` — import-time hybrid probe
- `backend/internal/server/routes/admin.go` — `POST /admin/grok/accounts/probe-dead`
- Frontend bulk actions + i18n (`probeDead`)

Rules:
- **Dead** = real upstream chat ban / permanent credential death (e.g. permission-denied on chat)
- Billing-only 403 is **not** auto-dead
- Do not `SetAccountError` for dead marks in a way that breaks OAuth refresh; use schedulable=false + notes

## 3. OAuth SSO → Build token (critical)

Files:
- `backend/internal/pkg/xai/sso_device.go` — Authorization Code + PKCE
- `backend/internal/pkg/xai/oauth.go` — `referrer=grok-build` (not `sub2api`)
- `backend/internal/service/grok_oauth_service.go` — credentials `base_url=cli-chat-proxy.grok.com/v1`

Hard requirements:
1. SSO cookie is only an entry ticket
2. Final credential is OAuth access/refresh
3. JWT must contain `referrer=grok-build`
4. `base_url` must be `https://cli-chat-proxy.grok.com/v1`
5. Device-flow tokens without the claim are rejected

## 4. Admin UX

- Groups route allowed in simple mode
- Accounts bulk bar: probe dead / delete dead
- Quick nav links as needed

## Rebuild

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-sub2api.ps1
powershell -ExecutionPolicy Bypass -File scripts\check-parity.ps1
```

Stock image `weishaw/sub2api:latest` does **not** include these patches.
