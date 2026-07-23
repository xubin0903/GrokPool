# GrokPool Worker

Headless register + auto OAuth push into local Sub2API.

## Start

From repo root:

```bat
start_worker.bat
```

Or:

```bat
worker\start_worker.bat
```

Panel: http://127.0.0.1:9000

## Prerequisites

1. Sub2API healthy on http://127.0.0.1:18080 (`grokpool-sub2api:local`)
2. Clash/Mihomo with **TUN / virtual NIC**
3. `register-win` present (junction or copy) with `.venv` installed (`start.bat` once)
4. `deploy\.env` contains `ADMIN_PASSWORD=...` (worker reads it if env not set)

## What it sets

| Env | Default |
|-----|---------|
| `AUTO_CPA` | 1 |
| `AUTO_SUB2_PUSH` | 1 |
| `SUB2_IMPORT_MODE` | cpa-data |
| `SUB2API_BASE_URL` | http://127.0.0.1:18080 |
| `GROK_BROWSER_ENGINE` | camoufox |
| `GROK_PROXY` | http://127.0.0.1:7895 |
| `PANEL_PORT` | 9000 |

## Success logs

```text
[CPA] OK user@example.com -> xai-user_example_com.json
[SUB2] PUSH OK user@example.com · mode=cpa-data http=200 created=1 ...
```

## Security

Do not hardcode admin passwords in bat files. Use `deploy/.env` or set `SUB2API_ADMIN_PASSWORD` in the environment.
