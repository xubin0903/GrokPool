# Free-tier Grok pool scheduling (GrokPool)

## Background

Free Grok OAuth accounts expose roughly **1,000,000 token** windows via rate-limit headers:

```text
x-ratelimit-limit-tokens
x-ratelimit-remaining-tokens
x-ratelimit-reset-tokens
```

Stock Sub2API issues for this pool:
1. Quota headroom weight often disabled for Grok
2. Near-empty accounts could still win TopK
3. Exhausted free accounts could crowd the candidate set
4. Over-aggressive 429 cooldowns left free capacity idle while requests queued

## GrokPool patches

Files under `sub2api/backend/internal/service/`:
- `openai_gateway_scheduling.go`
- `openai_account_scheduler.go`
- `openai_gateway_grok.go`
- `openai_gateway_grok_cache.go`
- `grok_token_provider.go`

Behaviors:
- Remaining-token headroom scoring for Grok
- Soft fit / free-slot overflow when free accounts still exist
- Sticky escape when sticky account is a bad fit
- Shorter free-account 429 cooldown caps
- Dead accounts marked non-schedulable without breaking OAuth refresh path

## Operator tips

- Keep per-account concurrency low (often 1) for free OAuth
- Bind accounts to a dedicated Grok group
- Probe dead accounts periodically
- Rebuild Docker after changing these files: `scripts/build-sub2api.ps1`

## Validation

1. Import many free OAuth accounts
2. Confirm remaining tokens appear after traffic/probes
3. Small chats should spread across higher-remaining accounts
4. Remaining=0 accounts stop being selected
5. Queue should not stick when free accounts still have capacity
