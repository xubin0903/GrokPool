#!/usr/bin/env python3
"""Live-probe Grok-new accounts; disable 402/dead and move to Grok-402 group."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:18080"
ENV_PATH = Path(r"d:\Projects\GrokPool\deploy\.env")
TOKEN_PATH = Path(r"d:\Projects\GrokPool\.tmp_admin_token.txt")
IDS_PATH = Path(r"d:\Projects\GrokPool\.tmp_grok_new_ids.txt")
OUT_PATH = Path(r"d:\Projects\GrokPool\.tmp_grok_new_probe_result.json")
GROUP_402 = 10
BATCH = 40
TIMEOUT = 300

_TOKEN = ""


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def login() -> str:
    env = load_env()
    email = env.get("ADMIN_EMAIL", "")
    password = env.get("ADMIN_PASSWORD", "")
    status, payload = api(
        "POST",
        "/api/v1/auth/login",
        {"email": email, "password": password},
        timeout=30,
        auth=False,
    )
    data = unwrap(payload)
    token = ""
    if isinstance(data, dict):
        token = str(data.get("access_token") or "")
    if not token:
        raise RuntimeError(f"login failed status={status} payload={payload}")
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def api(method: str, path: str, body=None, timeout: int = TIMEOUT, auth: bool = True):
    global _TOKEN
    data = None
    headers = {"Accept": "application/json"}
    if auth:
        if not _TOKEN:
            _TOKEN = TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else ""
        headers["Authorization"] = f"Bearer {_TOKEN}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw}
        # Auto re-login once on session fingerprint mismatch.
        if auth and e.code == 401 and isinstance(payload, dict) and payload.get("code") == "SESSION_BINDING_MISMATCH":
            _TOKEN = login()
            return api(method, path, body=body, timeout=timeout, auth=True)
        return e.code, payload


def unwrap(payload):
    if isinstance(payload, dict) and "data" in payload and payload.get("code", 0) == 0:
        return payload["data"]
    return payload


def main() -> int:
    global _TOKEN
    _TOKEN = login()
    ids = [int(x) for x in IDS_PATH.read_text(encoding="utf-8").splitlines() if x.strip().isdigit()]
    print(f"total_ids={len(ids)} group_402={GROUP_402} batch={BATCH}", flush=True)

    summary = {
        "total": len(ids),
        "alive": 0,
        "dead": 0,
        "unknown": 0,
        "moved": 0,
        "move_errors": 0,
        "probe_errors": 0,
        "dead_items": [],
        "alive_items": [],
        "unknown_items": [],
        "errors": [],
    }

    for i in range(0, len(ids), BATCH):
        chunk = ids[i : i + BATCH]
        print(f"\n== probe batch {i // BATCH + 1}/{(len(ids) + BATCH - 1) // BATCH} size={len(chunk)}", flush=True)
        status, payload = api(
            "POST",
            "/api/v1/admin/grok/accounts/probe-dead",
            {"account_ids": chunk, "delete_dead": False},
            timeout=TIMEOUT,
        )
        data = unwrap(payload)
        if status != 200 or not isinstance(data, dict) or "items" not in data:
            msg = f"probe_failed status={status} payload={json.dumps(payload, ensure_ascii=False)[:500]}"
            print(msg, flush=True)
            summary["probe_errors"] += 1
            summary["errors"].append(msg)
            continue

        items = data.get("items") or []
        print(
            f"  batch_result total={data.get('total')} alive={data.get('alive')} dead={data.get('dead')} unknown={data.get('unknown')}",
            flush=True,
        )
        for item in items:
            aid = item.get("account_id")
            name = item.get("name") or item.get("email") or ""
            reason = item.get("reason") or ""
            sc = item.get("status_code") or 0
            row = {
                "account_id": aid,
                "name": name,
                "alive": bool(item.get("alive")),
                "dead": bool(item.get("dead")),
                "unknown": bool(item.get("unknown")),
                "reason": reason,
                "status_code": sc,
                "probe_error": item.get("probe_error") or "",
                "error": item.get("error") or "",
            }
            if item.get("dead"):
                summary["dead"] += 1
                summary["dead_items"].append(row)
                # Move out of Grok-new into Grok-402 quarantine only.
                st, up = api(
                    "PUT",
                    f"/api/v1/admin/accounts/{aid}",
                    {
                        "group_ids": [GROUP_402],
                        "status": "disabled",
                        "notes": f"DEAD 402 quarantine: {reason} status={sc}",
                    },
                    timeout=60,
                )
                # Also force unschedulable (probe-dead already does this, belt-and-suspenders)
                st2, _ = api(
                    "POST",
                    f"/api/v1/admin/accounts/{aid}/schedulable",
                    {"schedulable": False},
                    timeout=30,
                )
                ok = st == 200
                if ok:
                    summary["moved"] += 1
                    print(f"  DEAD moved id={aid} name={name} reason={reason} sc={sc}", flush=True)
                else:
                    summary["move_errors"] += 1
                    err = f"move_failed id={aid} status={st} body={json.dumps(up, ensure_ascii=False)[:300]}"
                    summary["errors"].append(err)
                    print(f"  {err}", flush=True)
                if st2 != 200:
                    print(f"  unsched_warn id={aid} status={st2}", flush=True)
            elif item.get("alive"):
                summary["alive"] += 1
                summary["alive_items"].append(row)
            else:
                summary["unknown"] += 1
                summary["unknown_items"].append(row)
                print(
                    f"  UNKNOWN id={aid} name={name} reason={reason} sc={sc} err={row['probe_error'] or row['error']}",
                    flush=True,
                )

        # small pause between batches to avoid hammering upstream
        time.sleep(1)

    OUT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n==== SUMMARY ====", flush=True)
    print(
        json.dumps(
            {
                "total": summary["total"],
                "alive": summary["alive"],
                "dead": summary["dead"],
                "unknown": summary["unknown"],
                "moved": summary["moved"],
                "move_errors": summary["move_errors"],
                "probe_errors": summary["probe_errors"],
                "out": str(OUT_PATH),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
