#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone CPA → Sub2API pusher (does not touch register/panel processes).

Watches data/cpa/xai-*.json, imports each once into Sub2, binds target group.
Reads credentials from register-win/config.json (or env). Safe to run while
panel is registering — only reads CPA files and talks to Sub2API.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from sso2cpa_core import build_sub2_payload, cpa_to_sub2_account  # noqa: E402

CPA_DIR = Path(os.environ.get("CPA_DIR", str(ROOT / "data" / "cpa"))).resolve()
STATE_PATH = Path(os.environ.get("SUB2_PUSH_STATE", str(ROOT / "data" / "sub2_push_state.json")))
CONFIG_PATH = ROOT / "config.json"
GROUP_CFG = ROOT / "data" / "sub2_group.json"
POLL_SEC = float(os.environ.get("SUB2_PUSH_POLL", "4") or "4")
ONCE = os.environ.get("SUB2_PUSH_ONCE", "").strip() in ("1", "true", "yes")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"read {path.name} fail: {e}")
    return default if default is not None else {}


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cfg_get() -> dict:
    c = load_json(CONFIG_PATH, {})
    if not isinstance(c, dict):
        c = {}
    g = load_json(GROUP_CFG, {})
    return c, g if isinstance(g, dict) else {}


def settings():
    c, g = cfg_get()
    base = (
        os.environ.get("SUB2API_BASE_URL")
        or c.get("sub2api_base_url")
        or "http://127.0.0.1:18080"
    ).rstrip("/")
    email = (os.environ.get("SUB2API_ADMIN_EMAIL") or c.get("sub2api_admin_email") or "").strip()
    password = (
        os.environ.get("SUB2API_ADMIN_PASSWORD") or c.get("sub2api_admin_password") or ""
    ).strip()
    api_key = (
        os.environ.get("SUB2API_ADMIN_API_KEY") or c.get("sub2api_admin_api_key") or ""
    ).strip()
    try:
        gid = int(os.environ.get("SUB2_TARGET_GROUP_ID") or g.get("group_id") or c.get("sub2_target_group_id") or 0)
    except Exception:
        gid = 0
    return {
        "base": base,
        "email": email,
        "password": password,
        "api_key": api_key,
        "group_id": gid,
    }


_jwt = {"token": "", "at": 0.0}


def auth_headers(st: dict) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if st["api_key"]:
        h["x-api-key"] = st["api_key"]
        return h
    if not (st["email"] and st["password"]):
        raise RuntimeError("missing sub2api_admin_email/password or api_key in config.json")
    now = time.time()
    if _jwt["token"] and now - _jwt["at"] < 6 * 3600:
        h["Authorization"] = f"Bearer {_jwt['token']}"
        return h
    body = json.dumps({"email": st["email"], "password": st["password"]}).encode("utf-8")
    req = urllib.request.Request(
        f"{st['base']}/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        obj = json.loads(resp.read().decode("utf-8", errors="replace"))
    data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else obj
    token = ""
    if isinstance(data, dict):
        token = str(data.get("access_token") or data.get("token") or "").strip()
    if not token and isinstance(obj, dict):
        token = str(obj.get("access_token") or obj.get("token") or "").strip()
    if not token:
        raise RuntimeError(f"login no token: {list(obj) if isinstance(obj, dict) else type(obj)}")
    _jwt["token"] = token
    _jwt["at"] = now
    h["Authorization"] = f"Bearer {token}"
    return h


def http_json(method: str, url: str, headers: dict, payload=None, timeout=60):
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(f"HTTP {e.code}: {body[:400]}") from e
    if not raw:
        return {"_http": code}
    return json.loads(raw)


def find_account_id(st: dict, headers: dict, email: str):
    email = (email or "").strip().lower()
    if not email:
        return 0
    q = urllib.request.quote(email)
    urls = [
        f"{st['base']}/api/v1/admin/accounts?page=1&page_size=50&search={q}",
        f"{st['base']}/api/v1/admin/accounts?page=1&page_size=50&platform=grok&search={q}",
        f"{st['base']}/api/v1/admin/accounts?page=1&page_size=100&platform=grok",
    ]
    for url in urls:
        try:
            obj = http_json("GET", url, headers, timeout=30)
        except Exception as e:
            log(f"list accounts fail: {e}")
            continue
        data = obj.get("data") if isinstance(obj, dict) else obj
        items = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("accounts") or data.get("list") or []
        elif isinstance(data, list):
            items = data
        for it in items or []:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip().lower()
            creds = it.get("credentials") if isinstance(it.get("credentials"), dict) else {}
            em = str(creds.get("email") or it.get("email") or "").strip().lower()
            if em == email or name == email:
                try:
                    return int(it.get("id") or 0)
                except Exception:
                    return 0
    return 0


def bind_group(st: dict, headers: dict, account_id: int, group_id: int) -> str:
    if not account_id or not group_id:
        return ""
    # Sub2 bulk-update: POST + account_ids + group_ids pointer
    try:
        http_json(
            "POST",
            f"{st['base']}/api/v1/admin/accounts/bulk-update",
            headers,
            {
                "account_ids": [int(account_id)],
                "group_ids": [int(group_id)],
                "confirm_mixed_channel_risk": True,
            },
            timeout=30,
        )
        return f"bound_group={group_id} account_id={account_id}"
    except Exception as e1:
        try:
            http_json(
                "PUT",
                f"{st['base']}/api/v1/admin/accounts/{int(account_id)}",
                headers,
                {"group_ids": [int(group_id)]},
                timeout=30,
            )
            return f"bound_group={group_id} account_id={account_id}"
        except Exception as e2:
            return f"bind_fail={e1} / {e2}"


def push_one(path: Path, st: dict) -> tuple[bool, str]:
    try:
        cpa = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        return False, f"read fail: {e}"
    if not isinstance(cpa, dict):
        return False, "not a dict"
    email = str(cpa.get("email") or "").strip()
    payload = build_sub2_payload([cpa], name_hints=[email or path.stem])
    if not payload.get("accounts"):
        acc = cpa_to_sub2_account(cpa, name_hint=email or path.stem)
        if not acc:
            return False, "empty mapper"
        payload = {
            "type": "sub2api-data",
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "proxies": [],
            "accounts": [acc],
        }
    for acc in payload.get("accounts") or []:
        if not isinstance(acc, dict):
            continue
        acc["type"] = "oauth"
        acc["platform"] = "grok"
        creds = acc.get("credentials") if isinstance(acc.get("credentials"), dict) else {}
        creds["base_url"] = str(creds.get("base_url") or "https://cli-chat-proxy.grok.com/v1").strip()
        if email and not creds.get("email"):
            creds["email"] = email
        for junk in ("sso", "sso_token", "cookie", "cf_clearance"):
            creds.pop(junk, None)
        acc["credentials"] = creds

    headers = auth_headers(st)
    body = {
        "data": payload,
        "skip_default_group_bind": bool(st["group_id"] > 0),
    }
    obj = http_json(
        "POST",
        f"{st['base']}/api/v1/admin/accounts/data",
        headers,
        body,
        timeout=60,
    )
    data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else obj
    created = failed = 0
    if isinstance(data, dict):
        created = int(data.get("account_created") or data.get("created") or 0)
        failed = int(data.get("account_failed") or data.get("failed") or 0)
    msg = f"created={created} failed={failed}"
    # already exists still counts as success if no hard fail
    time.sleep(1.0)
    aid = find_account_id(st, headers, email)
    bind_msg = ""
    if aid and st["group_id"] > 0:
        bind_msg = " " + bind_group(st, headers, aid, st["group_id"])
    elif aid:
        bind_msg = f" account_id={aid}"
    if failed and not created and not aid:
        return False, msg + bind_msg + " " + str(obj)[:200]
    return True, msg + bind_msg


def load_state() -> dict:
    st = load_json(STATE_PATH, {"done": {}, "ok": 0, "fail": 0})
    if not isinstance(st, dict):
        st = {"done": {}, "ok": 0, "fail": 0}
    if not isinstance(st.get("done"), dict):
        st["done"] = {}
    return st


def main() -> int:
    log(f"CPA dir: {CPA_DIR}")
    st_cfg = settings()
    log(
        f"Sub2 {st_cfg['base']} group=#{st_cfg['group_id']} "
        f"creds={'api_key' if st_cfg['api_key'] else ('password' if st_cfg['email'] else 'MISSING')}"
    )
    if not (st_cfg["api_key"] or (st_cfg["email"] and st_cfg["password"])):
        log("FATAL: fill sub2api_admin_email/password in config.json")
        return 2
    # probe login
    try:
        auth_headers(st_cfg)
        log("login OK")
    except Exception as e:
        log(f"FATAL login: {e}")
        return 2

    state = load_state()
    log(f"already done: {len(state['done'])}")

    def scan_once() -> int:
        nonlocal state
        st_cfg = settings()
        n_new = 0
        files = sorted(CPA_DIR.glob("xai-*.json"), key=lambda p: p.stat().st_mtime)
        for path in files:
            key = path.name
            try:
                mtime = path.stat().st_mtime
            except Exception:
                continue
            prev = state["done"].get(key)
            if prev and float(prev.get("mtime") or 0) >= mtime and prev.get("ok"):
                continue
            n_new += 1
            log(f"push {key} ...")
            try:
                ok, msg = push_one(path, st_cfg)
            except Exception as e:
                ok, msg = False, str(e)
            if ok:
                state["ok"] = int(state.get("ok") or 0) + 1
                state["done"][key] = {
                    "ok": True,
                    "mtime": mtime,
                    "msg": msg,
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                log(f"  OK {msg}")
            else:
                state["fail"] = int(state.get("fail") or 0) + 1
                state["done"][key] = {
                    "ok": False,
                    "mtime": mtime,
                    "msg": msg,
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                log(f"  FAIL {msg}")
            save_json(STATE_PATH, state)
        return n_new

    if ONCE:
        n = scan_once()
        log(f"once done new={n} ok={state.get('ok')} fail={state.get('fail')}")
        return 0

    log(f"watching every {POLL_SEC}s (Ctrl+C to stop)")
    while True:
        try:
            scan_once()
        except Exception as e:
            log(f"scan error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
