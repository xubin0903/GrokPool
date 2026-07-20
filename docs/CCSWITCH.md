# CC Switch / Claude Code → GrokPool (OpenAI format)

GrokPool exposes an **OpenAI-compatible** gateway through Sub2API.  
Configure CC Switch / Claude Code / OpenAI SDKs against that shape — **not** raw xAI web SSO.

## 1. Create a Sub2API API token

1. Open admin: `http://127.0.0.1:18080`
2. Create / open a **user** (or use admin if your build allows API keys there)
3. Create an **API Key / Token** bound to a group that contains your Grok OAuth accounts
4. Copy the token (shown once)

## 2. Ensure accounts are OAuth Grok in that group

Account row should look like:
- Platform: `grok`
- Type: `oauth`
- Credentials base_url: `https://cli-chat-proxy.grok.com/v1`
- Not raw SSO cookie type

## 3. CC Switch profile (OpenAI)

Use **OpenAI** provider / protocol:

| Field | Value |
|------|--------|
| Provider / API type | **OpenAI** (Chat Completions / OpenAI compatible) |
| API Base URL | `http://127.0.0.1:18080/v1` |
| API Key | Sub2API token from step 1 |
| Model | A model your group exposes, e.g. `grok-4` / `grok-3` / whatever appears in Sub2 model list |
| Path | usually default `/chat/completions` (do not point at Anthropic `/v1/messages` unless you intentionally use that adapter) |

Example JSON-ish config many tools accept:

```json
{
  "api_type": "openai",
  "base_url": "http://127.0.0.1:18080/v1",
  "api_key": "sk-your-sub2api-token",
  "model": "grok-4"
}
```

cURL smoke test:

```bash
curl http://127.0.0.1:18080/v1/chat/completions ^
  -H "Authorization: Bearer sk-your-sub2api-token" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"grok-4\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

## 4. Claude Code notes

- Prefer OpenAI-compatible custom endpoint settings if your CC Switch build supports it.
- `base_url` must include `/v1` (or the tool will append paths incorrectly).
- Do **not** paste xAI SSO cookie into Claude Code.
- If you get 401: bad/missing Sub2 token.
- If you get 403/permission-denied from upstream: account is not Build OAuth (`referrer!=grok-build`) or is dead — probe dead accounts in admin.

## 5. Remote access

If CC Switch runs on another machine:
- Either reverse-proxy Sub2 with HTTPS + auth
- Or SSH tunnel: `ssh -L 18080:127.0.0.1:18080 user@host`
- Do not expose admin port naked to the internet without hardening
