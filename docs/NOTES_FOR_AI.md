# Notes for AI agents / contributors (GrokPool)

## Product
GrokPool = bulk free Grok OAuth registration + Sub2API multi-account reverse proxy, tuned for many ~1M-token free accounts.

## Claude Code / CC Switch
- Use **OpenAI-compatible** client settings. See `docs/CCSWITCH.md`.
- `base_url`: `http://127.0.0.1:18080/v1`
- Auth: Sub2API API token (Bearer), never raw xAI SSO cookies.
- Model: whatever the bound group exposes.

## OAuth (non-negotiable)
See `docs/OAUTH.md`.
- Authorization Code + PKCE
- JWT `referrer=grok-build`
- `base_url=https://cli-chat-proxy.grok.com/v1`
- Device-flow tokens are invalid for free chat

## Free account economics
- Free windows ≈ 1M tokens; many accounts.
- Scheduler patches: remaining-aware scoring, soft fit, free-slot overflow. See `docs/SCHEDULER.md`.

## Mail
- Gmail `+` / dots / googlemail collapse to one xAI identity — useless for bulk.
- Prefer self-host CF Worker mail / DuckMail key / real distinct mailboxes.
- Public temp domains are often abused and blocked.

## Import path
Register success → panel CPA worker (auth-code) → `AUTO_SUB2_PUSH` → Sub2 `type=oauth` account.
Fallback manual: panel Download Sub2 ZIP → Admin import `all.json`.

## Dead accounts
Admin Accounts → select → **探测死号**.
Dead = upstream permanent chat ban / credential death, not flaky billing 403.

## Docker parity
Running image must be `grokpool-sub2api:local` built from this tree:
```powershell
scripts\build-sub2api.ps1
scripts\check-parity.ps1
```
Stock `weishaw/sub2api:latest` lacks GrokPool patches.

## Secrets — never commit
- `deploy/.env`
- register `config.json`, `token.json`, `mail_credentials.txt`, `data/cpa/*`, `accounts_*.txt`
- OAuth access/refresh tokens, admin passwords, JWT secrets

## Proxy on Windows
Local Sub2 usage: enable Clash/Mihomo **TUN / virtual NIC / enhanced mode** so Docker + host Python share the same egress. System-proxy-only often leaves Docker on dirty ISP IP.
