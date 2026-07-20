# Why Authorization Code + `referrer=grok-build`

This is the difference between a working GrokPool free OAuth account and a dead-looking import.

## Rules

1. **SSO is not a Sub2API credential.**  
   Sub2API Grok OAuth accounts need `access_token` / `refresh_token`.

2. **Token must carry JWT claim `referrer=grok-build`.**  
   Without it, `cli-chat-proxy.grok.com` rejects chat with:
   `permission-denied` / `Access to the chat endpoint is denied`.

3. **Device flow cannot inject that claim reliably.**  
   Early device-code converters produced tokens that all failed on chat.

4. **Solution: Authorization Code + PKCE**  
   Inject `referrer=grok-build` (and `plan=generic`) on:
   - `GET /oauth2/authorize?...`
   - consent submit  
   Then exchange `authorization_code` for tokens and **verify the claim**.

5. **`base_url` must be**  
   `https://cli-chat-proxy.grok.com/v1`  
   Empty/wrong base falls back toward `api.x.ai/v1` (billing path) and also fails free chat.

## Where GrokPool implements this

| Layer | Path |
|-------|------|
| Register panel convert | `register-win/lib/sso2cpa_core.py` |
| Sub2 server convert | `sub2api/backend/internal/pkg/xai/sso_device.go` |
| Sub2 credential default base | `sub2api/backend/internal/service/grok_oauth_service.go` |
| Panel push | `register-win/panel/app.py` (`SUB2_IMPORT_MODE=cpa-data` default) |

## Import modes (panel)

- **`cpa-data` (default)** — panel already ran auth-code convert; pushes `type=oauth` package to `/admin/accounts/data`
- **`sso-to-oauth`** — Sub2 runs `POST /admin/grok/sso-to-oauth` (also auth-code after GrokPool patch)

Both must end as OAuth + cli-chat-proxy + grok-build claim.
