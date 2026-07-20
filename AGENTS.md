# GrokPool — Agent / Contributor Brief

## What this is
Bulk-register free Grok (xAI) **OAuth** accounts → import into a patched **Sub2API** multi-account reverse proxy → schedule many ~1M-token free accounts.

Product name: **GrokPool**

## Critical rules
1. Clients (CC Switch / Claude Code) use **OpenAI-compatible** API: `http://HOST:18080/v1` + Sub2 API token. See `docs/CCSWITCH.md`.
2. Credentials must be OAuth with JWT `referrer=grok-build` and `base_url=https://cli-chat-proxy.grok.com/v1`. See `docs/OAUTH.md`.
3. Running Docker image must be `grokpool-sub2api:local` built from `sub2api/`. Stock upstream image lacks patches.
4. Never commit `.env`, SSO/OAuth tokens, admin passwords, `config.json`, `token.json`.
5. Windows local use: Clash/Mihomo **TUN / virtual NIC** so Docker egress is proxied.
6. Gmail aliases do not mint unique xAI accounts.

## Layout
- `sub2api/` — patched Sub2API
- `register-win/` — register panel (host)
- `deploy/` — compose stack
- `scripts/build-sub2api.ps1` / `check-parity.ps1`
- `docs/*` — OAuth, scheduler, CC Switch, patches

## After code changes
```powershell
scripts\build-sub2api.ps1
scripts\check-parity.ps1
```
