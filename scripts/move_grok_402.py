#!/usr/bin/env python3
"""Move already-probed dead Grok accounts into Grok-402 quarantine group."""

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
RESULT_PATH = Path(r"d:\Projects\GrokPool\.tmp_grok_new_probe_result.json")
GROUP_402 = 10

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


def unwrap(payload):
    if isinstance(payload, dict) and "data" in payload and payload.get("code", 0) == 0:
        return payload["data"]
    return payload


def api(method: str, path: str, body=None, timeout: int = 60, auth: bool = True):
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
        if auth and e.code == 401 and isinstance(payload, dict) and payload.get("code") == "SESSION_BINDING_MISMATCH":
            _TOKEN = login()
            return api(method, path, body=body, timeout=timeout, auth=True)
        return e.code, payload


def login() -> str:
    env = load_env()
    status, payload = api(
        "POST",
        "/api/v1/auth/login",
        {"email": env.get("ADMIN_EMAIL", ""), "password": env.get("ADMIN_PASSWORD", "")},
        timeout=30,
        auth=False,
    )
    data = unwrap(payload)
    token = str((data or {}).get("access_token") or "") if isinstance(data, dict) else ""
    if not token:
        raise RuntimeError(f"login failed status={status} payload={payload}")
    TOKEN_PATH.write_text(token, encoding="utf-8")
    return token


def main() -> int:
    global _TOKEN
    _TOKEN = login()
    result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    dead = result.get("dead_items") or []
    print(f"dead_to_move={len(dead)} group={GROUP_402}", flush=True)

    moved = 0
    errors = 0
    for i, item in enumerate(dead, 1):
        aid = item["account_id"]
        name = item.get("name") or ""
        reason = item.get("reason") or ""
        sc = item.get("status_code") or 0
        notes = f"DEAD 402 quarantine: {reason} status={sc}"
        # HTTP API only accepts active|inactive|error (not disabled).
        # probe-dead already set DB status=disabled + unschedulable=false.
        # Here we only rebind groups + force inactive/unschedulable via allowed API values.
        st, up = api(
            "PUT",
            f"/api/v1/admin/accounts/{aid}",
            {
                "group_ids": [GROUP_402],
                "status": "error",
                "notes": notes[:500],
            },
        )
        st2, _ = api(
            "POST",
            f"/api/v1/admin/accounts/{aid}/schedulable",
            {"schedulable": False},
        )
        if st == 200:
            moved += 1
            if i % 20 == 0 or i == len(dead):
                print(f"  progress {i}/{len(dead)} moved={moved} errors={errors}", flush=True)
        else:
            errors += 1
            print(f"  FAIL id={aid} name={name} status={st} body={json.dumps(up, ensure_ascii=False)[:240]}", flush=True)
        if st2 != 200:
            print(f"  unsched_warn id={aid} status={st2}", flush=True)
        if i % 50 == 0:
            time.sleep(0.2)

    print(json.dumps({"moved": moved, "errors": errors, "total": len(dead)}, ensure_ascii=False), flush=True)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
