#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grok Register 账号面板 + 启动注册（代理/节点由本机 Clash 管理）"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

# Ensure stdout/stderr use UTF-8 on Windows (default is GBK/CP936)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from typing import Deque, Dict, List, Optional, Set, Tuple

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)

# Project root = parent of panel/ (Windows / portable layout)
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("GROK_REGISTER_DIR", str(_DEFAULT_ROOT))).resolve()
# 面板默认不设登录密码（本机 127.0.0.1）。若需开启：PANEL_AUTH=1 且 PANEL_PASSWORD=xxx
PANEL_AUTH = os.environ.get("PANEL_AUTH", "0").strip() not in ("0", "false", "False", "no", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "admin")
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "9000"))
SECRET = os.environ.get("PANEL_SECRET", "grok-register-panel-local-secret")
CLASH_API = os.environ.get("CLASH_API", "http://127.0.0.1:9090").rstrip("/")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "")
# Prefer project venv; Windows uses Scripts\python.exe
_VENV_WIN = BASE_DIR / ".venv" / "Scripts" / "python.exe"
_VENV_UNIX = BASE_DIR / ".venv" / "bin" / "python"
_DEFAULT_PY = (
    str(_VENV_WIN)
    if _VENV_WIN.exists()
    else (str(_VENV_UNIX) if _VENV_UNIX.exists() else sys.executable)
)
VENV_PYTHON = os.environ.get("GROK_PYTHON", _DEFAULT_PY)
MAIN_SCRIPT = BASE_DIR / "grok_register_ttk.py"
CONFIG_PATH = BASE_DIR / "config.json"
PROXY_URL = os.environ.get("GROK_PROXY", "http://127.0.0.1:7890")
LOG_DIR = Path(os.environ.get("PANEL_LOG_DIR", str(BASE_DIR / "data" / "logs"))).resolve()
LOG_DIR.mkdir(parents=True, exist_ok=True)

# SSO → real CPA (CLIProxyAPI OAuth JSON)
CPA_DIR = Path(os.environ.get("CPA_DIR", str(BASE_DIR / "data" / "cpa"))).resolve()
CPA_DIR.mkdir(parents=True, exist_ok=True)
CPA_INDEX_PATH = CPA_DIR / "index.json"
CPA_FAILED_PATH = CPA_DIR / "failed.jsonl"
SSO2CPA_PATH = Path(
    os.environ.get("SSO2CPA_PATH", str(BASE_DIR / "lib"))
).resolve()
AUTO_CPA = os.environ.get("AUTO_CPA", "1").strip() not in ("0", "false", "False", "no")
CPA_DELAY = float(os.environ.get("CPA_DELAY", "1.0"))
# GrokPool: after CPA convert, push OAuth account into local/remote Sub2API.
# Default ON — free web SSO path dies fast; OAuth/cli-chat-proxy is the durable route.
# Creds: env first, then config.json (hot-read each push so launcher/config fixes work without full re-code).
AUTO_SUB2_PUSH = os.environ.get("AUTO_SUB2_PUSH", "1").strip() not in ("0", "false", "False", "no")
# Import mode:
#   sso-to-oauth  → Sub2 official POST /admin/grok/sso-to-oauth (server-side ConvertFromSSO)
#   cpa-data      → local SSO→OAuth then POST /admin/accounts/data (type=oauth package)
# Default cpa-data: panel already did Authorization Code + PKCE with
# referrer=grok-build via sso2cpa_core. Push ready OAuth package so Sub2 does
# NOT re-run its (historically broken) device-flow converter.
# Set SUB2_IMPORT_MODE=sso-to-oauth only after Sub2 is rebuilt with auth-code ConvertSSOToBuild.
SUB2_IMPORT_MODE = (
    os.environ.get("SUB2_IMPORT_MODE", "cpa-data").strip().lower() or "cpa-data"
)
SUB2API_BASE_URL = os.environ.get("SUB2API_BASE_URL", "http://127.0.0.1:18080").rstrip("/")
SUB2API_ADMIN_EMAIL = os.environ.get("SUB2API_ADMIN_EMAIL", "").strip()
SUB2API_ADMIN_PASSWORD = os.environ.get("SUB2API_ADMIN_PASSWORD", "").strip()
SUB2API_ADMIN_API_KEY = os.environ.get("SUB2API_ADMIN_API_KEY", "").strip()
SUB2API_JWT = os.environ.get("SUB2API_JWT", "").strip()
SUB2_SKIP_DEFAULT_GROUP_BIND = os.environ.get("SUB2_SKIP_DEFAULT_GROUP_BIND", "0").strip() not in (
    "0",
    "false",
    "False",
    "no",
)
SUB2_PUSH_CONCURRENCY = int(os.environ.get("SUB2_PUSH_CONCURRENCY", "1") or "1")
SUB2_PUSH_PRIORITY = int(os.environ.get("SUB2_PUSH_PRIORITY", "50") or "50")
SUB2_GROUP_CFG_PATH = Path(os.environ.get("SUB2_GROUP_CFG", str(BASE_DIR / "data" / "sub2_group.json")))


def _load_config_json() -> dict:
    try:
        if CONFIG_PATH.exists():
            obj = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {}


def _sub2_cfg_str(cfg: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = (cfg or {}).get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default


def refresh_sub2_settings_from_config(force: bool = False) -> dict:
    """Merge config.json Sub2 settings into module globals (env still wins if already set).

    Called before each push/status so filling config.json fixes a live panel without
    needing env vars baked at process start. Does not override non-empty env values.
    """
    global AUTO_SUB2_PUSH, SUB2_IMPORT_MODE, SUB2API_BASE_URL
    global SUB2API_ADMIN_EMAIL, SUB2API_ADMIN_PASSWORD, SUB2API_ADMIN_API_KEY, SUB2API_JWT
    global SUB2_PUSH_CONCURRENCY, SUB2_PUSH_PRIORITY
    cfg = _load_config_json()

    def env_or_cfg(env_key: str, *cfg_keys: str, default: str = "") -> str:
        ev = str(os.environ.get(env_key) or "").strip()
        if ev:
            return ev
        return _sub2_cfg_str(cfg, *cfg_keys, default=default)

    # auto push: env AUTO_SUB2_PUSH wins; else config auto_sub2_push; else keep current
    if "AUTO_SUB2_PUSH" in os.environ:
        AUTO_SUB2_PUSH = os.environ.get("AUTO_SUB2_PUSH", "1").strip() not in (
            "0",
            "false",
            "False",
            "no",
        )
    elif "auto_sub2_push" in cfg:
        v = cfg.get("auto_sub2_push")
        if isinstance(v, str):
            AUTO_SUB2_PUSH = v.strip().lower() not in ("0", "false", "no", "off")
        else:
            AUTO_SUB2_PUSH = bool(v)

    SUB2_IMPORT_MODE = (
        env_or_cfg("SUB2_IMPORT_MODE", "sub2_import_mode", "SUB2_IMPORT_MODE", default="cpa-data")
        .strip()
        .lower()
        or "cpa-data"
    )
    SUB2API_BASE_URL = env_or_cfg(
        "SUB2API_BASE_URL", "sub2api_base_url", "SUB2API_BASE_URL", default="http://127.0.0.1:18080"
    ).rstrip("/")
    SUB2API_ADMIN_EMAIL = env_or_cfg(
        "SUB2API_ADMIN_EMAIL", "sub2api_admin_email", "SUB2API_ADMIN_EMAIL"
    )
    SUB2API_ADMIN_PASSWORD = env_or_cfg(
        "SUB2API_ADMIN_PASSWORD", "sub2api_admin_password", "SUB2API_ADMIN_PASSWORD"
    )
    SUB2API_ADMIN_API_KEY = env_or_cfg(
        "SUB2API_ADMIN_API_KEY", "sub2api_admin_api_key", "SUB2API_ADMIN_API_KEY"
    )
    SUB2API_JWT = env_or_cfg("SUB2API_JWT", "sub2api_jwt", "SUB2API_JWT")

    # optional group from config if state empty
    try:
        gid_cfg = int(cfg.get("sub2_target_group_id") or 0)
    except Exception:
        gid_cfg = 0
    if gid_cfg > 0 and get_target_group_id() <= 0:
        set_target_group(
            gid_cfg,
            _sub2_cfg_str(cfg, "sub2_target_group_name"),
            _sub2_cfg_str(cfg, "sub2_target_group_platform", default="grok"),
        )

    return {
        "enabled": AUTO_SUB2_PUSH,
        "base_url": SUB2API_BASE_URL,
        "import_mode": SUB2_IMPORT_MODE,
        "has_api_key": bool(SUB2API_ADMIN_API_KEY),
        "has_password": bool(SUB2API_ADMIN_EMAIL and SUB2API_ADMIN_PASSWORD),
        "target_group_id": get_target_group_id(),
    }
# Hard wall-clock per register round (one account). Stuck process is killed, next round starts.
DEFAULT_ROUND_TIMEOUT_SEC = 300
# Optional: talk to local Clash Meta external-controller for node list.
# Default: external Clash managed by user; node UI is best-effort.
ENABLE_CLASH_UI = os.environ.get("ENABLE_CLASH_UI", "1").strip() not in (
    "0",
    "false",
    "False",
    "no",
)

# import shared convert core
for _p in (str(SSO2CPA_PATH), str(BASE_DIR / "lib"), str(Path(__file__).resolve().parent)):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from sso2cpa_core import (  # type: ignore
        build_sub2_payload,
        cpa_to_sub2_account,
        convert_one,
        normalize_sso,
        safe_filename as cpa_safe_filename,
        sso_fingerprint,
    )

    _CPA_CORE_OK = True
    _CPA_CORE_ERR = ""
except Exception as _e:  # pragma: no cover
    convert_one = None  # type: ignore
    build_sub2_payload = None  # type: ignore
    cpa_to_sub2_account = None  # type: ignore
    normalize_sso = lambda t: (t or "").strip()  # type: ignore
    cpa_safe_filename = lambda s: re.sub(r"[^\w.@+-]+", "_", s or "unknown")[:100]  # type: ignore
    sso_fingerprint = lambda s: hashlib.sha256((s or "").encode()).hexdigest()  # type: ignore
    _CPA_CORE_OK = False
    _CPA_CORE_ERR = str(_e)

HK_RE = re.compile(r"(香港|Hong\s*Kong|\bHK\b|🇭🇰)", re.I)

app = Flask(__name__)
app.secret_key = SECRET

# --------------- job state ---------------
_job_lock = threading.Lock()
_job: Dict = {
    "running": False,
    "stop": False,
    "pid": None,
    "started_at": None,
    "finished_at": None,
    "count": 0,
    "success": 0,
    "fail": 0,
    "current_round": 0,
    "current_node": "",
    "node_mode": "fixed",  # fixed | rotate_on_fail | rotate_each
    "node_list": [],
    "node_index": 0,
    "log_path": "",
    "last_error": "",
    "status": "idle",
}
_logs: Deque[str] = deque(maxlen=2000)
_proc: Optional[subprocess.Popen] = None

# --------------- CPA auto-convert queue ---------------
_cpa_lock = threading.Lock()
_cpa_q: "queue.Queue[Optional[dict]]" = queue.Queue()
_cpa_state: Dict = {
    "enabled": AUTO_CPA,
    "core_ok": _CPA_CORE_OK,
    "core_error": _CPA_CORE_ERR,
    "pending": 0,
    "ok": 0,
    "fail": 0,
    "running": False,
    "last_error": "",
    "last_ok_email": "",
}
_cpa_done: Set[str] = set()  # sso fingerprints already converted
_cpa_inflight: Set[str] = set()


def log_line(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _logs.append(line)
    path = _job.get("log_path")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# 日志过滤：只保留关键信息，屏蔽第三方库噪音
# 注意：不要把业务日志里的 Camoufox/Playwright 字样当噪音误杀
_LOG_NOISE_PATTERNS = re.compile(
    r"(?i)"
    r"(<html|<!doctype|<div|<script|<svg|<path\b)"          # HTML 片段
    r"|(?:^|\s)(?:playwright|drissionpage|selenium|urllib3)[\s:.]"  # 库调试，不含业务 Camoufox
    r"|(connection\.(reusable|pool)|starting new (http|https))"  # urllib3 连接日志
    r"|(\bDEBUG\b|\bTRACE\b)"                                # 调试级别
    r"|(node:|child_process|events\.js|node_modules)"        # Node.js 内部
    r"|(pip\s|Downloading\s|Installing collected)"           # pip 安装
)
_LOG_KEY_PREFIXES = ("[*]", "[+]", "[-]", "[!]", "[Debug]", "[i]", "[OK]", "[ERR]")
_LOG_KEY_KEYWORDS = (
    "注册成功", "注册失败", "任务结束", "任务异常", "浏览器已启动", "开始注册",
    "验证码", "邮箱", "NSFW", "CPA", "SSO", "OAuth", "账号", "停止", "清理",
    "成功账号", "当前统计", "保存", "失败", "成功", "启动", "结束",
    "浏览器", "Camoufox", "Chromium", "硬超时", "下载", "就绪",
)
# 噪音行模式（即使是 [*] 前缀也过滤）：Cloudflare 轮询、GC 回收、网络模式重复
_LOG_NOISE_LINES = re.compile(
    r"(?i)"
    r"(等待\s*Cloudflare\s*人机验证)"           # Cloudflare 轮询刷屏
    r"|(Cloudflare\s*token\s*为空.*继续检测)"    # Cloudflare token 空轮询
    r"|(Python\s*GC\s*已回收)"                  # GC 回收细节
    r"|(浏览器网络模式)"                        # 每轮重复的网络模式
    r"|(浏览器已启动)(?!.*\b第\b)"              # 第 N 轮以外的「浏览器已启动」重复
    r"|(邮箱源\s*\w+\s*创建成功)"               # 与「已创建邮箱」重复
    r"|(已创建邮箱.*源=)"                       # 与「已创建 tempmailer 邮箱」重复
    r"|(资料已填:)"                             # 与「已填写注册资料并提交」重复
    r"|(Turnstile\s*二次复用完成)"              # 调试细节
    r"|(提交前仍卡住.*复用\s*Turnstile)"        # 调试细节
)


def _strip_inner_timestamp(line: str) -> str:
    """去掉子进程日志自带的时间戳，避免与 panel 的 log_line 时间戳重复。
    子进程原始行形如 "[02:30:39] [*] CLI 已加载配置" → 去掉前导时间戳 → "[*] CLI 已加载配置"
    这样 log_line 再加时间戳就只有一层 "[02:30:39] [*] CLI 已加载配置"。
    """
    # 标准形式：[HH:MM:SS] 后跟内容
    m = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s+(.*)$", line)
    if m:
        return m.group(1)
    # 带 > 前缀形式：> [HH:MM:SS] [*] xxx
    m = re.match(r"^>\s*\[\d{2}:\d{2}:\d{2}\]\s+(.*)$", line)
    if m:
        return m.group(1)
    return line


def _truncate_line(line: str, max_len: int = 200) -> str:
    """超长行截断，保留前部关键信息。"""
    if len(line) <= max_len:
        return line
    return line[:max_len] + " …"


def _is_key_log(line: str) -> bool:
    """判断一行日志是否为关键信息，应保留显示。"""
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    # 超长单行通常是 URL 或 HTML 片段
    if len(stripped) > 400:
        return False
    # 即使带 [*] 前缀的噪音行也过滤（Cloudflare 轮询、GC、网络模式重复）
    if _LOG_NOISE_LINES.search(stripped):
        return False
    # 业务前缀优先保留（避免 “Camoufox/Playwright” 字样被整行误杀）
    for prefix in _LOG_KEY_PREFIXES:
        if prefix in stripped:
            return True
    # 噪音模式（无业务前缀时）
    if _LOG_NOISE_PATTERNS.search(stripped):
        return False
    # 关键业务关键词
    for kw in _LOG_KEY_KEYWORDS:
        if kw in stripped:
            return True
    # panel 自己写的 [!] 前缀日志（已带时间戳）
    if stripped.startswith("[") and "]" in stripped[:9]:
        rest = stripped[stripped.find("]") + 1 :].strip()
        if rest.startswith("[!]") or rest.startswith("[*]") or rest.startswith("[+]"):
            return True
    # 默认过滤（非关键噪音）
    return False


def require_login():
    """默认关闭鉴权；仅当 PANEL_AUTH=1 时校验 session。"""
    if not PANEL_AUTH:
        return None
    if session.get("ok"):
        return None
    # API requests get JSON 401
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return redirect(url_for("login", next=request.path))


def list_account_files() -> List[Path]:
    return sorted(
        BASE_DIR.glob("accounts_*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def read_account_lines(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def collect_all_accounts() -> List[Tuple[str, str]]:
    items = []
    for f in list_account_files():
        for line in read_account_lines(f):
            items.append((f.name, line))
    return items


def parse_line(line: str):
    parts = line.split("----")
    if len(parts) >= 3:
        return {
            "email": parts[0],
            "password": parts[1],
            "sso": "----".join(parts[2:]),
            "raw": line,
        }
    return {"email": line, "password": "", "sso": "", "raw": line}


def _b64url_json(segment: str):
    import base64

    try:
        pad = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + pad)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}


def decode_sso_meta(sso: str) -> dict:
    """Best-effort parse web SSO JWT payload (not xAI OAuth)."""
    if not sso or sso.count(".") < 2:
        return {}
    return _b64url_json(sso.split(".")[1])


def unique_accounts() -> List[dict]:
    seen = set()
    out = []
    for source, line in collect_all_accounts():
        if line in seen:
            continue
        seen.add(line)
        info = parse_line(line)
        info["source"] = source
        meta = decode_sso_meta(info.get("sso") or "")
        info["session_id"] = meta.get("session_id") or meta.get("sid") or ""
        out.append(info)
    return out


def active_account_emails() -> Set[str]:
    """Emails still present in remaining accounts_*.txt (panel account list)."""
    emails: Set[str] = set()
    for acc in unique_accounts():
        email = str(acc.get("email") or "").strip().lower()
        if email:
            emails.add(email)
    return emails


def _cpa_entry_email(obj: Optional[dict], path: Optional[Path] = None) -> str:
    """Best-effort email for a CPA json object / filename."""
    if isinstance(obj, dict):
        email = str(obj.get("email") or "").strip().lower()
        if email:
            return email
        # Some older dumps only keep email under nested keys.
        for key in ("preferred_username", "user", "account"):
            email = str(obj.get(key) or "").strip().lower()
            if email and "@" in email:
                return email
    if path is not None:
        stem = path.stem
        hint = stem[4:] if stem.lower().startswith("xai-") else stem
        hint = str(hint or "").strip().lower()
        # Strip optional -fingerprint suffix: email-abcdef12
        if "@" in hint:
            # If suffix looks like short hex after last '-', drop it when base still has @
            parts = hint.rsplit("-", 1)
            if len(parts) == 2 and len(parts[1]) in (6, 8, 10, 12) and all(
                c in "0123456789abcdef" for c in parts[1]
            ):
                if "@" in parts[0]:
                    return parts[0]
            return hint
    return ""


def list_active_cpa_files() -> List[Path]:
    """CPA files whose email still exists in remaining accounts_*.txt.

    TXT export already only reads remaining account files. Sub2/CPA downloads
    previously dumped the whole data/cpa tree (every historical conversion),
    which is wrong after the user deletes account batches in the panel.
    """
    active = active_account_emails()
    if not active:
        return []
    out: List[Path] = []
    for path in list_cpa_files():
        obj = None
        try:
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                obj = parsed
        except Exception:
            obj = None
        email = _cpa_entry_email(obj, path)
        if email and email in active:
            out.append(path)
    return out


def prune_orphan_cpa_files() -> Dict[str, int]:
    """Delete CPA json files for emails no longer in any accounts_*.txt."""
    active = active_account_emails()
    removed = 0
    kept = 0
    errors = 0
    for path in list_cpa_files():
        obj = None
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                obj = parsed
        except Exception:
            obj = None
        email = _cpa_entry_email(obj, path)
        # Keep unreadable/unknown files rather than nuking them blindly.
        if not email:
            kept += 1
            continue
        if email in active:
            kept += 1
            continue
        try:
            path.unlink()
            removed += 1
            log_line(f"[*] 已清理孤儿 CPA: {path.name} ({email})")
        except Exception as e:
            errors += 1
            log_line(f"[!] 清理 CPA 失败 {path.name}: {e}")
    return {"removed": removed, "kept": kept, "errors": errors, "active_emails": len(active)}


def safe_filename_part(s: str) -> str:
    s = re.sub(r"[^\w.@+-]+", "_", s or "unknown")
    return s[:80] or "unknown"


def account_line_set() -> Set[str]:
    return {line for _, line in collect_all_accounts()}


def load_cpa_index() -> None:
    """Load converted SSO fingerprints + counts from disk."""
    global _cpa_done
    done: Set[str] = set()
    ok_count = 0
    if CPA_INDEX_PATH.exists():
        try:
            data = json.loads(CPA_INDEX_PATH.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, dict):
                for fp, meta in items.items():
                    done.add(fp)
                    if isinstance(meta, dict) and meta.get("file"):
                        ok_count += 1
            elif isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("fp"):
                        done.add(it["fp"])
                        ok_count += 1
        except Exception:
            pass
    # also scan existing json files
    for p in CPA_DIR.glob("xai-*.json"):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            sso = normalize_sso(obj.get("sso") or "")
            if sso:
                done.add(sso_fingerprint(sso))
                ok_count = max(ok_count, 1)
        except Exception:
            continue
    with _cpa_lock:
        _cpa_done = done
        if ok_count and not _cpa_state.get("ok"):
            _cpa_state["ok"] = len(done)


def save_cpa_index_item(fp: str, meta: dict) -> None:
    items: Dict[str, dict] = {}
    if CPA_INDEX_PATH.exists():
        try:
            data = json.loads(CPA_INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), dict):
                items = data["items"]
        except Exception:
            items = {}
    items[fp] = meta
    CPA_INDEX_PATH.write_text(
        json.dumps(
            {"updated_at": datetime.now().isoformat(timespec="seconds"), "items": items},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def list_cpa_files() -> List[Path]:
    return sorted(CPA_DIR.glob("xai-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def cpa_stats() -> dict:
    with _cpa_lock:
        st = dict(_cpa_state)
        done_n = len(_cpa_done)
    all_files = list_cpa_files()
    active_files = list_active_cpa_files()
    # UI / download scope: only CPA still covered by remaining accounts_*.txt
    st["files"] = len(active_files)
    st["files_active"] = len(active_files)
    st["files_all"] = len(all_files)
    st["done"] = done_n
    st["dir"] = str(CPA_DIR)
    return st


def enqueue_cpa_convert(
    email: str,
    sso: str,
    password: str = "",
    source: str = "",
    force: bool = False,
) -> Tuple[bool, str]:
    """Queue one SSO for real OAuth CPA conversion. Returns (queued, reason)."""
    if not AUTO_CPA and not force:
        return False, "auto_cpa disabled"
    if not _CPA_CORE_OK or convert_one is None:
        return False, f"sso2cpa core unavailable: {_CPA_CORE_ERR}"
    sso = normalize_sso(sso)
    if not sso:
        return False, "empty sso"
    fp = sso_fingerprint(sso)
    with _cpa_lock:
        if not force and (fp in _cpa_done or fp in _cpa_inflight):
            return False, "already converted or queued"
        _cpa_inflight.add(fp)
        _cpa_state["pending"] = int(_cpa_state.get("pending") or 0) + 1
    _cpa_q.put(
        {
            "email": email or "",
            "sso": sso,
            "password": password or "",
            "source": source or "",
            "fp": fp,
            "force": force,
        }
    )
    return True, "queued"


def enqueue_new_accounts(before: Set[str]) -> int:
    """Diff account lines after a round and queue new ones."""
    after = account_line_set()
    new_lines = after - before
    n = 0
    for line in new_lines:
        info = parse_line(line)
        ok, _ = enqueue_cpa_convert(
            email=info.get("email") or "",
            sso=info.get("sso") or "",
            password=info.get("password") or "",
            source="register",
        )
        if ok:
            n += 1
    return n


def enqueue_missing_accounts(limit: int = 500) -> int:
    """Queue accounts that have SSO but no CPA file yet."""
    n = 0
    for acc in unique_accounts():
        if n >= limit:
            break
        ok, _ = enqueue_cpa_convert(
            email=acc.get("email") or "",
            sso=acc.get("sso") or "",
            password=acc.get("password") or "",
            source=acc.get("source") or "",
        )
        if ok:
            n += 1
    return n


def _cpa_worker_loop():
    log_line(
        f"[CPA] worker start · core={'ok' if _CPA_CORE_OK else 'FAIL'} · auto={AUTO_CPA} · dir={CPA_DIR}"
    )
    if not _CPA_CORE_OK:
        log_line(f"[CPA] core import error: {_CPA_CORE_ERR}")
    while True:
        item = _cpa_q.get()
        if item is None:
            break
        email = item.get("email") or ""
        sso = item.get("sso") or ""
        fp = item.get("fp") or sso_fingerprint(sso)
        with _cpa_lock:
            _cpa_state["running"] = True
            _cpa_state["pending"] = max(0, int(_cpa_state.get("pending") or 0) - 1)
        try:
            if convert_one is None:
                raise RuntimeError(f"core missing: {_CPA_CORE_ERR}")
            entry = convert_one(sso, email=email, proxy=PROXY_URL)
            # keep password if known (not required by CPA, useful for bookkeeping)
            if item.get("password") and not entry.get("password"):
                entry["password"] = item["password"]
            entry["_source"] = "grok-register-auto-cpa"
            entry["_source_file"] = item.get("source") or ""
            email_out = entry.get("email") or email or "unknown"
            fname = f"xai-{cpa_safe_filename(email_out)}.json"
            path = CPA_DIR / fname
            if path.exists():
                try:
                    old = json.loads(path.read_text(encoding="utf-8"))
                    old_fp = sso_fingerprint(normalize_sso(old.get("sso") or ""))
                except Exception:
                    old_fp = ""
                if old_fp and old_fp != fp:
                    fname = f"xai-{cpa_safe_filename(email_out)}-{fp[:8]}.json"
                    path = CPA_DIR / fname
            path.write_text(
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            save_cpa_index_item(
                fp,
                {
                    "email": email_out,
                    "file": fname,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "auth_kind": entry.get("auth_kind"),
                },
            )
            with _cpa_lock:
                _cpa_done.add(fp)
                _cpa_inflight.discard(fp)
                _cpa_state["ok"] = int(_cpa_state.get("ok") or 0) + 1
                _cpa_state["last_ok_email"] = email_out
                _cpa_state["last_error"] = ""
            log_line(f"[CPA] OK {email_out} -> {fname}")
            if AUTO_SUB2_PUSH:
                ok_push, push_msg = push_cpa_to_sub2api(entry, email_hint=email_out)
                if ok_push:
                    log_line(f"[SUB2] PUSH OK {email_out} · {push_msg}")
                else:
                    log_line(f"[SUB2] PUSH FAIL {email_out}: {push_msg}")
        except Exception as e:
            err = str(e)
            with _cpa_lock:
                _cpa_inflight.discard(fp)
                _cpa_state["fail"] = int(_cpa_state.get("fail") or 0) + 1
                _cpa_state["last_error"] = err
            try:
                with open(CPA_FAILED_PATH, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "at": datetime.now().isoformat(timespec="seconds"),
                                "email": email,
                                "fp": fp,
                                "error": err,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except Exception:
                pass
            log_line(f"[CPA] FAIL {email or fp[:12]}: {err}")
        finally:
            with _cpa_lock:
                _cpa_state["running"] = not _cpa_q.empty()
            if CPA_DELAY > 0:
                time.sleep(CPA_DELAY)
            _cpa_q.task_done()


def start_cpa_worker() -> None:
    load_cpa_index()
    th = threading.Thread(target=_cpa_worker_loop, name="cpa-worker", daemon=True)
    th.start()


# --------------- Sub2API auto push (GrokPool) ---------------
_sub2_lock = threading.Lock()
_sub2_state: Dict = {
    "ok": 0,
    "fail": 0,
    "last_ok_email": "",
    "last_error": "",
    "last_result": "",
    "target_group_id": 0,
    "target_group_name": "",
    "target_group_platform": "",
}
_sub2_jwt_cache = {"token": SUB2API_JWT, "at": 0.0}


def _load_sub2_group_cfg() -> dict:
    try:
        if SUB2_GROUP_CFG_PATH.exists():
            obj = json.loads(SUB2_GROUP_CFG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {}


def _save_sub2_group_cfg(cfg: dict) -> None:
    try:
        SUB2_GROUP_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUB2_GROUP_CFG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception as e:
        log_line(f"[SUB2] save group cfg fail: {e}")


def _init_sub2_group_state() -> None:
    cfg = _load_sub2_group_cfg()
    with _sub2_lock:
        _sub2_state["target_group_id"] = int(cfg.get("group_id") or 0)
        _sub2_state["target_group_name"] = str(cfg.get("group_name") or "")
        _sub2_state["target_group_platform"] = str(cfg.get("group_platform") or "")


def get_target_group_id() -> int:
    with _sub2_lock:
        return int(_sub2_state.get("target_group_id") or 0)


def set_target_group(group_id: int, group_name: str = "", group_platform: str = "") -> None:
    with _sub2_lock:
        _sub2_state["target_group_id"] = int(group_id or 0)
        _sub2_state["target_group_name"] = group_name or ""
        _sub2_state["target_group_platform"] = group_platform or ""
        cfg = {
            "group_id": int(group_id or 0),
            "group_name": group_name or "",
            "group_platform": group_platform or "",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    _save_sub2_group_cfg(cfg)


def sub2_status() -> dict:
    try:
        refresh_sub2_settings_from_config()
    except Exception:
        pass
    with _sub2_lock:
        return {
            "enabled": AUTO_SUB2_PUSH,
            "base_url": SUB2API_BASE_URL,
            "import_mode": SUB2_IMPORT_MODE,
            "ok": int(_sub2_state.get("ok") or 0),
            "fail": int(_sub2_state.get("fail") or 0),
            "last_ok_email": _sub2_state.get("last_ok_email") or "",
            "last_error": _sub2_state.get("last_error") or "",
            "last_result": _sub2_state.get("last_result") or "",
            "has_api_key": bool(SUB2API_ADMIN_API_KEY),
            "has_password": bool(SUB2API_ADMIN_EMAIL and SUB2API_ADMIN_PASSWORD),
            "target_group_id": int(_sub2_state.get("target_group_id") or 0),
            "target_group_name": _sub2_state.get("target_group_name") or "",
            "target_group_platform": _sub2_state.get("target_group_platform") or "",
        }


def _sub2_login_jwt() -> str:
    """Login admin and cache JWT briefly."""
    if SUB2API_JWT:
        return SUB2API_JWT
    if not (SUB2API_ADMIN_EMAIL and SUB2API_ADMIN_PASSWORD):
        return ""
    now = time.time()
    with _sub2_lock:
        tok = str(_sub2_jwt_cache.get("token") or "")
        at = float(_sub2_jwt_cache.get("at") or 0)
        if tok and now - at < 6 * 3600:
            return tok
    payload = json.dumps(
        {"email": SUB2API_ADMIN_EMAIL, "password": SUB2API_ADMIN_PASSWORD},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{SUB2API_BASE_URL}/api/v1/auth/login",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        raise RuntimeError(f"sub2api login failed: {e}") from e
    # tolerate {data:{access_token}} or {access_token}
    token = ""
    if isinstance(body, dict):
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        token = (
            (data or {}).get("access_token")
            or (data or {}).get("token")
            or body.get("access_token")
            or body.get("token")
            or ""
        )
    token = str(token or "").strip()
    if not token:
        raise RuntimeError(f"sub2api login: no access_token in response keys={list(body) if isinstance(body, dict) else type(body)}")
    with _sub2_lock:
        _sub2_jwt_cache["token"] = token
        _sub2_jwt_cache["at"] = time.time()
    return token


def _sub2_auth_headers() -> dict:
    try:
        refresh_sub2_settings_from_config()
    except Exception:
        pass
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if SUB2API_ADMIN_API_KEY:
        headers["x-api-key"] = SUB2API_ADMIN_API_KEY
        return headers
    jwt = _sub2_login_jwt()
    if not jwt:
        raise RuntimeError("missing SUB2API_ADMIN_API_KEY or admin email/password")
    headers["Authorization"] = f"Bearer {jwt}"
    return headers


def _sub2_request(method: str, path: str, payload: Optional[dict] = None, timeout: int = 30) -> dict:
    url = f"{SUB2API_BASE_URL}{path}"
    headers = _sub2_auth_headers()
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(f"HTTP {e.code} {path}: {body[:300]}") from e
    except Exception as e:
        raise RuntimeError(f"{method} {path} failed: {e}") from e
    if not raw:
        return {"_http": code}
    try:
        obj = json.loads(raw)
    except Exception:
        return {"_http": code, "_raw": raw}
    if isinstance(obj, dict):
        obj["_http"] = code
    return obj


def _sub2_unwrap(obj: dict):
    if not isinstance(obj, dict):
        return obj
    if "data" in obj:
        return obj.get("data")
    return obj


def sub2_list_groups() -> List[dict]:
    """List groups from Sub2API admin API."""
    # prefer /all then fallback list
    try:
        obj = _sub2_request("GET", "/api/v1/admin/groups/all")
        data = _sub2_unwrap(obj)
        if isinstance(data, list):
            return [g for g in data if isinstance(g, dict)]
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [g for g in data["items"] if isinstance(g, dict)]
        if isinstance(data, dict) and isinstance(data.get("groups"), list):
            return [g for g in data["groups"] if isinstance(g, dict)]
    except Exception:
        pass
    obj = _sub2_request("GET", "/api/v1/admin/groups?page_size=200")
    data = _sub2_unwrap(obj)
    if isinstance(data, list):
        return [g for g in data if isinstance(g, dict)]
    if isinstance(data, dict):
        for k in ("items", "list", "groups", "data"):
            if isinstance(data.get(k), list):
                return [g for g in data[k] if isinstance(g, dict)]
    return []


def sub2_create_group(name: str, platform: str = "grok", description: str = "") -> dict:
    name = (name or "").strip()
    platform = (platform or "grok").strip().lower() or "grok"
    if not name:
        raise RuntimeError("group name required")
    if platform not in ("anthropic", "openai", "gemini", "antigravity", "grok"):
        raise RuntimeError(f"invalid platform: {platform}")
    payload = {
        "name": name,
        "description": description or f"GrokPool auto group ({platform})",
        "platform": platform,
        "rate_multiplier": 1.0,
        "is_exclusive": False,
        "subscription_type": "standard",
    }
    obj = _sub2_request("POST", "/api/v1/admin/groups", payload)
    data = _sub2_unwrap(obj)
    if not isinstance(data, dict):
        raise RuntimeError(f"create group unexpected response: {obj}")
    return data


def sub2_find_account_id_by_email(email: str) -> Optional[int]:
    email = (email or "").strip().lower()
    if not email:
        return None
    # search endpoint
    q = urllib.parse.quote(email)
    try:
        obj = _sub2_request("GET", f"/api/v1/admin/accounts?search={q}&page_size=50")
        data = _sub2_unwrap(obj)
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("items", "list", "accounts", "data"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").lower()
            em = ""
            cred = it.get("credentials") if isinstance(it.get("credentials"), dict) else {}
            extra = it.get("extra") if isinstance(it.get("extra"), dict) else {}
            em = str(cred.get("email") or extra.get("email") or "").lower()
            if email == name or email == em or email in name:
                aid = it.get("id")
                if aid is not None:
                    return int(aid)
    except Exception as e:
        log_line(f"[SUB2] find account by email fail: {e}")
    return None


def sub2_bind_account_groups(account_id: int, group_ids: List[int]) -> dict:
    payload = {
        "account_ids": [int(account_id)],
        "group_ids": [int(g) for g in group_ids],
        "confirm_mixed_channel_risk": True,
    }
    # bulk-update supports group_ids pointer
    return _sub2_request("POST", "/api/v1/admin/accounts/bulk-update", payload)


def sub2_probe_grok_quota(account_id: int) -> dict:
    """Trigger the same Grok quota probe as the admin UI '探测' button.

    Fresh imports often show usage=forbidden until billing+active probe runs.
    Call this after import so the account list is usable without manual clicks.
    """
    return _sub2_request(
        "GET",
        f"/api/v1/admin/grok/accounts/{int(account_id)}/quota",
        timeout=60,
    )


def sub2_probe_dead_accounts(account_ids: List[int], delete_dead: bool = False) -> dict:
    """Bulk liveness probe; optionally delete permanently dead Grok accounts."""
    ids = [int(x) for x in account_ids if int(x) > 0]
    if not ids:
        return {"total": 0, "alive": 0, "dead": 0, "unknown": 0, "deleted": 0, "items": []}
    return _sub2_request(
        "POST",
        "/api/v1/admin/grok/accounts/probe-dead",
        {"account_ids": ids, "delete_dead": bool(delete_dead)},
        timeout=max(60, 15 * len(ids)),
    )


def sub2_delete_account(account_id: int) -> dict:
    return _sub2_request("DELETE", f"/api/v1/admin/accounts/{int(account_id)}", timeout=30)


def _sub2_mark_push(ok: bool, email: str, msg: str) -> None:
    with _sub2_lock:
        if ok:
            _sub2_state["ok"] = int(_sub2_state.get("ok") or 0) + 1
            _sub2_state["last_ok_email"] = email or ""
            _sub2_state["last_result"] = msg
            _sub2_state["last_error"] = ""
        else:
            _sub2_state["fail"] = int(_sub2_state.get("fail") or 0) + 1
            _sub2_state["last_error"] = msg


def _sub2_post_import_bind_and_probe(email: str, account_id: Optional[int] = None) -> str:
    """Bind selected group + soft quota probe. Never auto-delete fresh accounts."""
    bind_msg = ""
    probe_msg = ""
    gid = get_target_group_id()
    aid = int(account_id) if account_id else 0
    email = (email or "").strip()
    if not aid and email:
        time.sleep(1.5)
        found = sub2_find_account_id_by_email(email)
        aid = int(found) if found else 0
    if not aid:
        return f" bind_skip=account_not_found email={email}"

    if gid > 0:
        try:
            sub2_bind_account_groups(aid, [gid])
            bind_msg = f" bound_group={gid} account_id={aid}"
        except Exception as be:
            bind_msg = f" bind_fail={be} account_id={aid}"
    else:
        bind_msg = f" account_id={aid}"

    last_probe_err = ""
    for attempt in range(1, 4):
        try:
            pr = sub2_probe_grok_quota(aid)
            pdata = _sub2_unwrap(pr) if isinstance(pr, dict) else pr
            st = 0
            src = ""
            headers_obs = False
            probe_err = ""
            if isinstance(pdata, dict):
                st = int(pdata.get("status_code") or 0)
                src = str(pdata.get("source") or "")
                headers_obs = bool(pdata.get("headers_observed"))
                probe_err = str(pdata.get("probe_error") or "")
                snap = pdata.get("snapshot") if isinstance(pdata.get("snapshot"), dict) else {}
                if not st and isinstance(snap, dict):
                    st = int(snap.get("status_code") or 0)
                if not headers_obs and isinstance(snap, dict):
                    headers_obs = bool(snap.get("headers_observed"))
            if headers_obs and 200 <= st < 300 and not probe_err:
                probe_msg = f" probe_ok attempt={attempt} status={st} source={src}"
                last_probe_err = ""
                break
            low = (probe_err or "").lower()
            if st in (0, 403, 429, 503) or "permission-denied" in low or "chat endpoint" in low or "cloudflare" in low:
                last_probe_err = f"soft_fail status={st} source={src} err={(probe_err or '')[:120]}"
            else:
                probe_msg = f" probe_done attempt={attempt} status={st} source={src}"
                last_probe_err = ""
                break
        except Exception as pe:
            last_probe_err = str(pe)
        time.sleep(2.0 * attempt)
    if last_probe_err:
        probe_msg = f" probe_soft_fail={last_probe_err} kept=1"
    return bind_msg + probe_msg


def push_sso_to_sub2api_oauth(
    sso: str,
    email_hint: str = "",
    name_hint: str = "",
) -> Tuple[bool, str]:
    """Preferred path: Sub2 official SSO→OAuth import.

    POST /api/v1/admin/grok/sso-to-oauth
    Server runs ConvertFromSSO (referrer=grok-build) and stores type=oauth
    with base_url=cli-chat-proxy.grok.com.
    """
    if not AUTO_SUB2_PUSH:
        return False, "auto_sub2_push disabled"
    sso_norm = normalize_sso(sso)
    if not sso_norm:
        return False, "empty sso"
    email = (email_hint or "").strip()
    name = (name_hint or email or "grok-oauth").strip()
    gid = get_target_group_id()
    body: Dict = {
        "sso_token": sso_norm,
        "name": name,
        "concurrency": int(SUB2_PUSH_CONCURRENCY) if SUB2_PUSH_CONCURRENCY else 1,
        "priority": int(SUB2_PUSH_PRIORITY) if SUB2_PUSH_PRIORITY else 50,
        "credentials": {
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        },
        "extra": {
            "import_source": "grok-register-panel",
            "import_mode": "sso-to-oauth",
            "email_hint": email,
        },
    }
    if email:
        body["credentials"]["email"] = email
        body["notes"] = email
    if gid > 0:
        body["group_ids"] = [int(gid)]

    try:
        obj = _sub2_request("POST", "/api/v1/admin/grok/sso-to-oauth", body, timeout=90)
        data = _sub2_unwrap(obj) if isinstance(obj, dict) else obj
        created = []
        failed = []
        if isinstance(data, dict):
            created = data.get("created") or []
            failed = data.get("failed") or []
            if not isinstance(created, list):
                created = []
            if not isinstance(failed, list):
                failed = []

        if failed and not created:
            err0 = ""
            try:
                err0 = str((failed[0] or {}).get("error") or "")
            except Exception:
                err0 = str(failed[:1])
            msg = f"mode=sso-to-oauth created=0 failed={len(failed)} err={err0[:240]}"
            _sub2_mark_push(False, email, msg)
            return False, msg

        aid = None
        out_email = email
        if created:
            item0 = created[0] if isinstance(created[0], dict) else {}
            out_email = str(item0.get("email") or email or "").strip()
            acc = item0.get("account") if isinstance(item0.get("account"), dict) else {}
            try:
                aid = int(acc.get("id") or 0) or None
            except Exception:
                aid = None
            if not out_email and isinstance(acc, dict):
                out_email = str(acc.get("email") or acc.get("name") or email or "").strip()

        extra = _sub2_post_import_bind_and_probe(out_email or email, account_id=aid)
        msg = f"mode=sso-to-oauth created={len(created)} failed={len(failed)}{extra}"
        _sub2_mark_push(True, out_email or email, msg)
        return True, msg
    except Exception as e:
        err = f"mode=sso-to-oauth err={e}"
        _sub2_mark_push(False, email, err)
        return False, err


def push_cpa_to_sub2api(cpa_entry: dict, email_hint: str = "") -> Tuple[bool, str]:
    """Push one account into Sub2 as OAuth.

    Default: Sub2 official sso-to-oauth (server-side ConvertFromSSO).
    Fallback: local CPA OAuth package via /admin/accounts/data.
    """
    try:
        refresh_sub2_settings_from_config()
    except Exception as e:
        log_line(f"[SUB2] refresh settings: {e}")
    if not AUTO_SUB2_PUSH:
        return False, "auto_sub2_push disabled"

    email = (email_hint or (cpa_entry or {}).get("email") or "").strip()
    sso = normalize_sso((cpa_entry or {}).get("sso") or "")
    mode = (SUB2_IMPORT_MODE or "cpa-data").lower()

    if mode in ("sso-to-oauth", "sso", "oauth", "official") and sso:
        ok, msg = push_sso_to_sub2api_oauth(
            sso,
            email_hint=email,
            name_hint=email or (cpa_entry or {}).get("sub") or "grok-oauth",
        )
        if ok:
            return ok, msg
        log_line(f"[SUB2] sso-to-oauth failed, fallback cpa-data: {msg}")

    if cpa_to_sub2_account is None and build_sub2_payload is None:
        if sso:
            return push_sso_to_sub2api_oauth(sso, email_hint=email, name_hint=email or "grok-oauth")
        return False, "sub2 mapper unavailable and no sso for official import"

    try:
        if build_sub2_payload is not None:
            payload = build_sub2_payload(
                [cpa_entry],
                name_hints=[email or ""],
                concurrency=SUB2_PUSH_CONCURRENCY,
                priority=SUB2_PUSH_PRIORITY,
            )
        else:
            acc = cpa_to_sub2_account(
                cpa_entry,
                name_hint=email or "",
                concurrency=SUB2_PUSH_CONCURRENCY,
                priority=SUB2_PUSH_PRIORITY,
            )
            if not acc:
                return False, "cpa_to_sub2_account returned empty"
            payload = {
                "type": "sub2api-data",
                "version": 1,
                "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "proxies": [],
                "accounts": [acc],
            }
        if not payload.get("accounts"):
            return False, "no accounts in sub2 payload"

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

        gid = get_target_group_id()
        skip_default = True if gid > 0 else bool(SUB2_SKIP_DEFAULT_GROUP_BIND)
        body = {
            "data": payload,
            "skip_default_group_bind": skip_default,
        }
        headers = _sub2_auth_headers()
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{SUB2API_BASE_URL}/api/v1/admin/accounts/data",
            data=raw,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            code = resp.status
        created = failed = 0
        try:
            obj = json.loads(resp_body)
            data = obj.get("data") if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else obj
            if isinstance(data, dict):
                created = int(data.get("account_created") or data.get("created") or 0)
                failed = int(data.get("account_failed") or data.get("failed") or 0)
        except Exception:
            pass
        msg = f"mode=cpa-data http={code} created={created} failed={failed}"
        extra = _sub2_post_import_bind_and_probe(email)
        msg = msg + extra
        if failed and not created:
            _sub2_mark_push(False, email, msg + " " + resp_body[:200])
            return False, msg
        _sub2_mark_push(True, email, msg)
        return True, msg
    except Exception as e:
        err = f"mode=cpa-data err={e}"
        _sub2_mark_push(False, email, err)
        return False, err


def to_grok2api_pool(accounts: List[dict]) -> dict:
    """grok2api-style local token pool using web SSO tokens."""
    tokens = []
    for acc in accounts:
        sso = (acc.get("sso") or "").strip()
        if not sso:
            continue
        tokens.append(
            {
                "token": sso,
                "email": acc.get("email") or "",
                "status": "active",
            }
        )
    return {
        "ssoBasic": tokens,
        "ssoSuper": [],
    }


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def email_config_public(cfg: Optional[dict] = None) -> dict:
    """Email settings for panel UI (multi-provider dropdown)."""
    c = cfg if isinstance(cfg, dict) else load_config()
    provider = str(c.get("email_provider") or "cfworker").strip().lower()
    if provider in ("tempmailer", "inboxkitten", "inbox_kitten", "custom"):
        provider = "cfworker" if provider != "custom" else "cfworker"
    if provider == "custom":
        provider = "cfworker"
    # alias yyds -> maliapi for UI
    if provider == "yyds":
        provider = "maliapi"

    choices = [
        {"id": "cfworker", "label": "CF Worker / 自建域名"},
        {"id": "cloudflare", "label": "自定义 cloudflare_temp_email"},
        {"id": "moemail", "label": "MoeMail (sall.cc)"},
        {"id": "tempmail_lol", "label": "TempMail.lol（自动生成）"},
        {"id": "duckmail", "label": "DuckMail"},
        {"id": "gptmail", "label": "GPTMail"},
        {"id": "maliapi", "label": "YYDS / MaliAPI"},
        {"id": "luckmail", "label": "LuckMail（接码/买邮）"},
        {"id": "mailnest", "label": "MailNest（mailnest.top）"},
        {"id": "gmail_forward", "label": "域名转发→Gmail（无限别名）"},
        {"id": "skymail", "label": "SkyMail"},
        {"id": "cloudmail", "label": "CloudMail"},
        {"id": "freemail", "label": "Freemail 自建"},
        {"id": "opentrashmail", "label": "OpenTrashMail"},
        {"id": "laoudo", "label": "Laoudo 固定邮箱"},
    ]
    valid = {x["id"] for x in choices}
    if provider not in valid:
        provider = "cfworker"

    hint = (
        "公共 Tempmailer 已移除（滥用后拒收 xAI 验证码）。"
        "请从下拉框选择邮箱源；自建/CF Worker 通常更稳，公共源可能仍被 xAI 拒绝。"
    )
    return {
        "provider": provider,
        "choices": choices,
        "email_failover": bool(c.get("email_failover", True)),
        # generic / cfworker / cloudflare
        "cfworker_api_url": str(c.get("cfworker_api_url") or c.get("cloudflare_api_base") or "").strip(),
        "cfworker_admin_token": str(c.get("cfworker_admin_token") or c.get("cloudflare_api_key") or "").strip(),
        "cfworker_domain": str(c.get("cfworker_domain") or c.get("defaultDomains") or "").strip(),
        "cfworker_custom_auth": str(c.get("cfworker_custom_auth") or "").strip(),
        "cfworker_subdomain": str(c.get("cfworker_subdomain") or "").strip(),
        "custom_api_base": str(c.get("cloudflare_api_base") or c.get("cfworker_api_url") or "").strip(),
        "custom_api_key": str(c.get("cloudflare_api_key") or c.get("cfworker_admin_token") or "").strip(),
        "custom_auth_mode": (
            "bearer"
            if str(c.get("cloudflare_auth_mode") or "").strip().lower()
            in ("auth", "bearer", "authorization")
            else str(c.get("cloudflare_auth_mode") or "x-admin-auth").strip()
        ),
        "custom_domain": str(c.get("defaultDomains") or c.get("cfworker_domain") or "").strip(),
        "custom_path_accounts": str(c.get("cloudflare_path_accounts") or "/admin/new_address").strip(),
        "custom_path_messages": str(c.get("cloudflare_path_messages") or "/api/mails").strip(),
        "custom_path_token": str(c.get("cloudflare_path_token") or "/api/token").strip(),
        # providers
        "moemail_api_url": str(c.get("moemail_api_url") or "https://sall.cc").strip(),
        "moemail_api_key": str(c.get("moemail_api_key") or "").strip(),
        "gptmail_base_url": str(c.get("gptmail_base_url") or "https://mail.chatgpt.org.uk").strip(),
        "gptmail_api_key": str(c.get("gptmail_api_key") or "").strip(),
        "gptmail_domain": str(c.get("gptmail_domain") or "").strip(),
        "duckmail_api_url": str(c.get("duckmail_api_url") or "https://www.duckmail.sbs").strip(),
        "duckmail_provider_url": str(c.get("duckmail_provider_url") or "https://api.duckmail.sbs").strip(),
        "duckmail_bearer": str(c.get("duckmail_bearer") or "").strip(),
        "duckmail_domain": str(c.get("duckmail_domain") or "").strip(),
        "duckmail_api_key": str(c.get("duckmail_api_key") or "").strip(),
        "maliapi_base_url": str(c.get("maliapi_base_url") or "https://maliapi.215.im/v1").strip(),
        "maliapi_api_key": str(c.get("maliapi_api_key") or c.get("yyds_api_key") or "").strip(),
        "maliapi_domain": str(c.get("maliapi_domain") or "").strip(),
        "luckmail_base_url": str(c.get("luckmail_base_url") or "https://mails.luckyous.com/").strip(),
        "luckmail_api_key": str(c.get("luckmail_api_key") or "").strip(),
        "luckmail_project_code": str(c.get("luckmail_project_code") or "grok").strip(),
        "luckmail_domain": str(c.get("luckmail_domain") or "").strip(),
        "mailnest_base_url": str(c.get("mailnest_base_url") or "https://mailnest.top").strip(),
        "mailnest_api_key": str(c.get("mailnest_api_key") or "").strip(),
        "mailnest_project_code": str(c.get("mailnest_project_code") or "x-ai001").strip(),
        "mailnest_sale_mode": str(c.get("mailnest_sale_mode") or "temporary").strip(),
        "gmail_forward_domain": str(c.get("gmail_forward_domain") or "").strip(),
        "gmail_imap_user": str(c.get("gmail_imap_user") or "").strip(),
        "gmail_imap_password": str(c.get("gmail_imap_password") or "").strip(),
        "gmail_imap_host": str(c.get("gmail_imap_host") or "imap.gmail.com").strip(),
        "gmail_imap_port": str(c.get("gmail_imap_port") or "993").strip(),
        "gmail_imap_folders": str(
            c.get("gmail_imap_folders") or "INBOX,Spam,[Gmail]/Spam"
        ).strip(),
        "gmail_forward_local_len": str(c.get("gmail_forward_local_len") or "10").strip(),
        "skymail_api_base": str(c.get("skymail_api_base") or "https://api.skymail.ink").strip(),
        "skymail_token": str(c.get("skymail_token") or "").strip(),
        "skymail_domain": str(c.get("skymail_domain") or "").strip(),
        "cloudmail_api_base": str(c.get("cloudmail_api_base") or "").strip(),
        "cloudmail_admin_email": str(c.get("cloudmail_admin_email") or "").strip(),
        "cloudmail_admin_password": str(c.get("cloudmail_admin_password") or "").strip(),
        "cloudmail_domain": str(c.get("cloudmail_domain") or "").strip(),
        "freemail_api_url": str(c.get("freemail_api_url") or "").strip(),
        "freemail_admin_token": str(c.get("freemail_admin_token") or "").strip(),
        "freemail_domain": str(c.get("freemail_domain") or "").strip(),
        "opentrashmail_api_url": str(c.get("opentrashmail_api_url") or "").strip(),
        "opentrashmail_domain": str(c.get("opentrashmail_domain") or "").strip(),
        "opentrashmail_password": str(c.get("opentrashmail_password") or "").strip(),
        "laoudo_auth": str(c.get("laoudo_auth") or "").strip(),
        "laoudo_email": str(c.get("laoudo_email") or "").strip(),
        "laoudo_account_id": str(c.get("laoudo_account_id") or "").strip(),
        "hint": hint,
    }


def apply_email_config_from_ui(data: dict) -> dict:
    """Merge panel email form into config.json and return public view."""
    cfg = load_config()
    provider = str(data.get("provider") or "cfworker").strip().lower()
    if provider in ("tempmailer", "inboxkitten", "inbox_kitten"):
        raise ValueError("内置公共 Tempmailer 已移除，请选择其它邮箱源")
    if provider == "custom":
        provider = "cfworker"
    if provider == "yyds":
        provider = "maliapi"

    if provider in ("domain_forward", "spaceship_forward", "gmail_catchall", "catchall"):
        provider = "gmail_forward"

    valid = {
        "cfworker", "cloudflare", "moemail", "tempmail_lol", "duckmail", "gptmail",
        "maliapi", "luckmail", "mailnest", "gmail_forward", "skymail", "cloudmail",
        "freemail", "opentrashmail", "laoudo",
    }
    if provider not in valid:
        raise ValueError(f"不支持的邮箱源: {provider}")

    cfg["email_failover"] = bool(data.get("email_failover", True))
    cfg["email_provider"] = provider
    cfg["email_providers"] = [provider]

    def g(key, default=""):
        return str(data.get(key, cfg.get(key, default)) or default).strip()

    # always store fields (so switching providers keeps values)
    cfg["cfworker_api_url"] = g("cfworker_api_url") or g("custom_api_base")
    cfg["cfworker_admin_token"] = g("cfworker_admin_token") or g("custom_api_key")
    cfg["cfworker_domain"] = g("cfworker_domain") or g("custom_domain")
    cfg["cfworker_custom_auth"] = g("cfworker_custom_auth")
    cfg["cfworker_subdomain"] = g("cfworker_subdomain")

    # cloudflare_temp_email legacy keys
    cfg["cloudflare_api_base"] = g("custom_api_base") or g("cfworker_api_url")
    cfg["cloudflare_api_key"] = g("custom_api_key") or g("cfworker_admin_token")
    mode = g("custom_auth_mode", "x-admin-auth").lower() or "x-admin-auth"
    if mode not in ("none", "bearer", "x-api-key", "x-admin-auth", "query-key"):
        mode = "x-admin-auth"
    cfg["cloudflare_auth_mode"] = "auth" if mode == "bearer" else mode
    cfg["defaultDomains"] = g("custom_domain") or g("cfworker_domain")
    cfg["cloudflare_path_accounts"] = g("custom_path_accounts", "/admin/new_address") or "/admin/new_address"
    cfg["cloudflare_path_messages"] = g("custom_path_messages", "/api/mails") or "/api/mails"
    cfg["cloudflare_path_token"] = g("custom_path_token", "/api/token") or "/api/token"

    for key in (
        "moemail_api_url", "moemail_api_key",
        "gptmail_base_url", "gptmail_api_key", "gptmail_domain",
        "duckmail_api_url", "duckmail_provider_url", "duckmail_bearer", "duckmail_domain", "duckmail_api_key",
        "maliapi_base_url", "maliapi_api_key", "maliapi_domain",
        "luckmail_base_url", "luckmail_api_key", "luckmail_project_code", "luckmail_domain",
        "mailnest_base_url", "mailnest_api_key", "mailnest_project_code", "mailnest_sale_mode",
        "gmail_forward_domain", "gmail_imap_user", "gmail_imap_password",
        "gmail_imap_host", "gmail_imap_port", "gmail_imap_folders", "gmail_forward_local_len",
        "skymail_api_base", "skymail_token", "skymail_domain",
        "cloudmail_api_base", "cloudmail_admin_email", "cloudmail_admin_password", "cloudmail_domain",
        "freemail_api_url", "freemail_admin_token", "freemail_domain",
        "opentrashmail_api_url", "opentrashmail_domain", "opentrashmail_password",
        "laoudo_auth", "laoudo_email", "laoudo_account_id",
    ):
        if key in data or key in cfg:
            cfg[key] = g(key, cfg.get(key, ""))

    # sync yyds keys for legacy
    if cfg.get("maliapi_api_key") and not cfg.get("yyds_api_key"):
        cfg["yyds_api_key"] = cfg["maliapi_api_key"]

    # required fields soft-check for selected provider
    need = {
        "cfworker": ["cfworker_api_url"],
        "cloudflare": ["cloudflare_api_base"],
        "luckmail": ["luckmail_api_key"],
        "mailnest": ["mailnest_api_key"],
        "gmail_forward": ["gmail_forward_domain", "gmail_imap_user", "gmail_imap_password"],
        "skymail": ["skymail_token"],
        "cloudmail": ["cloudmail_api_base"],
        "freemail": ["freemail_api_url"],
        "opentrashmail": ["opentrashmail_api_url"],
        "laoudo": ["laoudo_email"],
        "maliapi": ["maliapi_api_key"],
    }
    for field in need.get(provider, []):
        if not str(cfg.get(field) or "").strip():
            # allow cloudflare/cfworker alias
            if provider == "cfworker" and cfg.get("cloudflare_api_base"):
                continue
            if provider == "cloudflare" and cfg.get("cfworker_api_url"):
                continue
            raise ValueError(f"邮箱源 {provider} 需要配置: {field}")

    cfg.pop("tempmailer_api_base", None)
    cfg.pop("tempmailer_domain", None)
    cfg.pop("tempmailer_domains", None)
    save_config(cfg)
    return email_config_public(cfg)


def resolve_proxy_url() -> str:
    """Prefer config.json proxy; auto-probe common Clash ports if dead."""
    import socket
    from urllib.parse import urlparse

    def open_port(host: str, port: int, timeout: float = 0.35) -> bool:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    preferred = ""
    try:
        cfg = load_config()
        preferred = str(cfg.get("proxy") or "").strip()
    except Exception:
        preferred = ""
    preferred = preferred or os.environ.get("GROK_PROXY", "").strip() or PROXY_URL

    def ok(url: str) -> bool:
        u = urlparse(url if "://" in url else "http://" + url)
        return open_port(u.hostname or "127.0.0.1", u.port or 7890)

    if preferred and ok(preferred):
        return preferred
    for port in (7897, 7890, 7891, 7892, 10809, 20171, 1080, 2080, 8888):
        url = f"http://127.0.0.1:{port}"
        if ok(url):
            return url
    return preferred or "http://127.0.0.1:7890"


def save_config(cfg: dict):
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --------------- Clash helpers (optional external controller) ---------------
def clash_request(method: str, path: str, data=None, timeout=15):
    if not ENABLE_CLASH_UI:
        raise RuntimeError("clash ui disabled")
    url = CLASH_API + path
    body = None if data is None else json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if CLASH_SECRET:
        headers["Authorization"] = f"Bearer {CLASH_SECRET}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode())


def clash_list_nodes() -> dict:
    """Return usable non-HK leaf nodes + selectors + current."""
    try:
        prox = clash_request("GET", "/proxies")["proxies"]
        cfg = clash_request("GET", "/configs") or {}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "nodes": [],
            "selectors": {},
            "hint": "未检测到本机 Clash API。请在自己的 Clash 里选节点；本工具默认走 http://127.0.0.1:7890",
        }

    leaves = []
    for name, v in prox.items():
        t = v.get("type") or ""
        if t in (
            "Selector",
            "URLTest",
            "Fallback",
            "LoadBalance",
            "Relay",
            "Direct",
            "Reject",
            "Compatible",
            "Pass",
            "Dns",
        ):
            continue
        if name in ("PASS-RULE", "REJECT-DROP"):
            continue
        if HK_RE.search(name):
            continue
        leaves.append({"name": name, "type": t})

    # sort by region preference
    pref = ["US", "JP", "SG", "TW", "MY", "TH", "UK"]

    def key(n):
        name = n["name"].upper()
        for i, p in enumerate(pref):
            if name.startswith(p):
                return (i, name)
        return (99, name)

    leaves.sort(key=key)

    selectors = {}
    for name, v in prox.items():
        if v.get("type") == "Selector":
            selectors[name] = {"now": v.get("now"), "all": v.get("all") or []}

    return {
        "ok": True,
        "mode": cfg.get("mode"),
        "nodes": leaves,
        "selectors": selectors,
        "global_now": (selectors.get("GLOBAL") or {}).get("now"),
        "main_now": (selectors.get("🚀 使用节点") or {}).get("now"),
    }


def clash_set_node(node: str) -> Tuple[bool, str]:
    if not node:
        return True, "未指定节点（使用外部 Clash 当前节点）"
    if not ENABLE_CLASH_UI:
        return True, "Clash UI 关闭：请在本机 Clash 客户端切换节点"
    try:
        # ensure global mode so browser always uses proxy
        try:
            clash_request("PATCH", "/configs", {"mode": "global"})
        except Exception:
            pass
        prox = clash_request("GET", "/proxies")["proxies"]
        set_count = 0
        for name, v in prox.items():
            if v.get("type") != "Selector":
                continue
            alln = v.get("all") or []
            if node not in alln:
                continue
            try:
                clash_request(
                    "PUT",
                    "/proxies/" + urllib.parse.quote(name, safe=""),
                    {"name": node},
                )
                set_count += 1
            except Exception as e:
                log_line(f"[Clash] set {name} fail: {e}")
        if set_count == 0:
            return False, f"节点 {node} 不在任何选择器中（也可直接在 Clash 客户端切换）"
        return True, f"已切换到 {node}（{set_count} 个选择器）"
    except Exception as e:
        # soft-fail: external Clash without API is OK
        return True, f"Clash API 不可用，跳过切换（{e}）；请在客户端自选节点"


def clash_exit_ip() -> str:
    try:
        proxy_handler = urllib.request.ProxyHandler(
            {"http": PROXY_URL, "https": PROXY_URL}
        )
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open(
            "http://ip-api.com/json/?fields=country,city,query,isp", timeout=12
        ) as resp:
            d = json.loads(resp.read().decode())
            return f"{d.get('query')} {d.get('country')}/{d.get('city')} ({d.get('isp')})"
    except Exception as e:
        return f"unknown ({e})"


# --------------- job runner ---------------
def _update_stats_from_log(line: str):
    if "注册成功" in line or "[+] 注册成功" in line:
        with _job_lock:
            _job["success"] = int(_job.get("success") or 0) + 1
    if "注册失败" in line or "[-] 注册失败" in line:
        with _job_lock:
            _job["fail"] = int(_job.get("fail") or 0) + 1


def resolve_round_timeout_sec(cfg: Optional[dict] = None) -> int:
    """Per-account wall-clock timeout (seconds). Default 300; clamp 60..3600."""
    for key in ("ROUND_TIMEOUT_SEC", "ROUND_TIMEOUT"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            return max(60, min(int(float(raw)), 3600))
        except Exception:
            pass
    try:
        c = cfg if isinstance(cfg, dict) else load_config()
        raw_cfg = c.get("round_timeout_sec", DEFAULT_ROUND_TIMEOUT_SEC)
        return max(60, min(int(float(raw_cfg)), 3600))
    except Exception:
        return DEFAULT_ROUND_TIMEOUT_SEC


def _terminate_register_proc(proc: Optional[subprocess.Popen]) -> None:
    """Kill register CLI and its browser children (Windows process tree)."""
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    try:
        if os.name == "nt" and pid:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        else:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
                return
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _cleanup_browser_leftovers() -> None:
    """Best-effort cleanup of temp browser profiles (Windows/Linux)."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/FI", "WINDOWTITLE eq *autoPortData*", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "chromium.*autoPortData"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
    except Exception:
        pass


def _run_one_round(round_no: int, total: int) -> bool:
    """Run register_count=1 once. Return True if success detected.

    Enforces round_timeout_sec (default 300s): if the CLI hangs (Turnstile /
    proxy / browser dead), kill the process tree and let job_worker start the
    next account instead of blocking forever.
    """
    global _proc
    cfg = load_config()
    # 面板每轮强制 register_count=1（job_worker 自己控轮数），但不要永久改坏用户配置
    cfg_run = dict(cfg)
    cfg_run["register_count"] = 1
    cfg_run["proxy"] = resolve_proxy_url()
    global PROXY_URL
    PROXY_URL = cfg_run["proxy"]
    os.environ["GROK_PROXY"] = PROXY_URL
    cfg_run.setdefault("email_provider", "cfworker")
    engine = str(cfg_run.get("browser_engine") or "chromium").strip().lower()
    if engine in ("camoufox", "firefox", "headless", "cfox"):
        engine = "camoufox"
    else:
        engine = "chromium"
    cfg_run["browser_engine"] = engine
    # 只把代理/引擎写回；register_count 保持用户原值
    try:
        cfg_save = load_config()
        cfg_save["proxy"] = cfg_run["proxy"]
        cfg_save["browser_engine"] = engine
        if "round_timeout_sec" not in cfg_save:
            cfg_save["round_timeout_sec"] = DEFAULT_ROUND_TIMEOUT_SEC
        save_config(cfg_save)
        cfg = cfg_save
    except Exception:
        cfg = cfg_run

    round_timeout = resolve_round_timeout_sec(cfg)
    # 子进程强制单账号；用环境变量覆盖，避免改坏 config.json 里的 register_count
    os.environ["GROK_REGISTER_COUNT"] = "1"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["GROK_BROWSER_ENGINE"] = engine
    env["ROUND_TIMEOUT_SEC"] = str(round_timeout)
    env["GROK_REGISTER_COUNT"] = "1"
    # Windows / local: use system Chrome/Edge; allow override (chromium engine only)
    if engine == "chromium":
        if os.name == "nt":
            if not env.get("BROWSER_PATH"):
                for cand in (
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                ):
                    if Path(cand).exists():
                        env["BROWSER_PATH"] = cand
                        break
        else:
            env["DISPLAY"] = env.get("DISPLAY") or ":0"
            env.setdefault("BROWSER_PATH", env.get("BROWSER_PATH") or "")

    log_line(f"=== 第 {round_no}/{total} 轮开始 · 节点 {_job.get('current_node') or '外部Clash'} ===")
    engine_label = "Camoufox 无头" if engine == "camoufox" else "Chromium 有头"
    log_line(
        f"[*] proxy={PROXY_URL} engine={engine_label} python={VENV_PYTHON} "
        f"round_timeout={round_timeout}s"
    )

    # 注册前检查邮箱源是否可用（公共 Tempmailer 已移除）
    try:
        mail_cfg = load_config()
        mail_prov = str(mail_cfg.get("email_provider") or "cfworker").strip().lower()
        if mail_prov in ("tempmailer", "inboxkitten", "inbox_kitten"):
            log_line("[!] 内置公共临时邮已移除，请在面板下拉选择其它邮箱源")
            return False
        # no-key providers
        free_ok = mail_prov in ("tempmail_lol", "moemail", "gptmail", "duckmail")
        has_cf = bool(str(mail_cfg.get("cfworker_api_url") or mail_cfg.get("cloudflare_api_base") or "").strip())
        has_luck = bool(str(mail_cfg.get("luckmail_api_key") or "").strip())
        has_mailnest = bool(str(mail_cfg.get("mailnest_api_key") or "").strip())
        has_gmail_fwd = bool(
            str(mail_cfg.get("gmail_forward_domain") or "").strip()
            and str(mail_cfg.get("gmail_imap_user") or "").strip()
            and str(mail_cfg.get("gmail_imap_password") or "").strip()
        )
        has_mali = bool(str(mail_cfg.get("maliapi_api_key") or mail_cfg.get("yyds_api_key") or "").strip())
        has_sky = bool(str(mail_cfg.get("skymail_token") or "").strip())
        has_cloud = bool(str(mail_cfg.get("cloudmail_api_base") or "").strip())
        has_free = bool(str(mail_cfg.get("freemail_api_url") or "").strip())
        has_otm = bool(str(mail_cfg.get("opentrashmail_api_url") or "").strip())
        has_lao = bool(str(mail_cfg.get("laoudo_email") or "").strip())
        ok = free_ok
        if mail_prov in ("cfworker", "cloudflare", "custom"):
            ok = has_cf
        elif mail_prov == "luckmail":
            ok = has_luck
        elif mail_prov == "mailnest":
            ok = has_mailnest
        elif mail_prov in ("gmail_forward", "domain_forward", "spaceship_forward", "gmail_catchall"):
            ok = has_gmail_fwd
        elif mail_prov in ("maliapi", "yyds"):
            ok = has_mali
        elif mail_prov == "skymail":
            ok = has_sky
        elif mail_prov == "cloudmail":
            ok = has_cloud
        elif mail_prov == "freemail":
            ok = has_free
        elif mail_prov == "opentrashmail":
            ok = has_otm
        elif mail_prov == "laoudo":
            ok = has_lao
        if not ok:
            log_line(f"[!] 邮箱源 {mail_prov} 尚未配置完整，请到面板「邮箱服务」填写后保存")
            return False
        log_line(f"[*] 邮箱源: {mail_prov}")
    except Exception as e:
        log_line(f"[!] 检查邮箱配置失败: {e}")
        return False

    # Camoufox 首次要下载浏览器二进制，不计入 5 分钟注册超时
    if engine == "camoufox":
        try:
            lib_dir = str(BASE_DIR / "lib")
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)
            from camoufox_backend import ensure_camoufox_ready  # type: ignore

            log_line("[*] 检查 Camoufox 浏览器（首次会下载，可能几分钟）...")
            exe = ensure_camoufox_ready(log_callback=log_line)
            log_line(f"[*] Camoufox 就绪: {exe}")
        except Exception as e:
            log_line(f"[!] Camoufox 准备失败: {e}")
            log_line("[!] 可改用 Chromium 有头引擎，或手动执行: .venv\\Scripts\\python.exe -m camoufox fetch")
            return False

    cmd = [
        VENV_PYTHON,
        "-u",
        str(MAIN_SCRIPT),
        "cli",
    ]
    try:
        _proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as e:
        log_line(f"[!] 启动失败: {e}")
        return False

    with _job_lock:
        _job["pid"] = _proc.pid
        _job["round_timeout_sec"] = round_timeout
        _job["round_deadline"] = time.time() + round_timeout

    # send start
    try:
        assert _proc.stdin is not None
        _proc.stdin.write("start\n")
        _proc.stdin.flush()
    except Exception as e:
        log_line(f"[!] 写入 start 失败: {e}")

    success = False
    failed = False
    timed_out = False
    stopped = False
    line_q: "queue.Queue[Optional[str]]" = queue.Queue()

    def _stdout_reader() -> None:
        try:
            assert _proc is not None and _proc.stdout is not None
            for raw in _proc.stdout:
                line_q.put(raw)
        except Exception:
            pass
        finally:
            line_q.put(None)

    reader = threading.Thread(target=_stdout_reader, name=f"round-{round_no}-stdout", daemon=True)
    reader.start()

    deadline = time.time() + round_timeout
    while True:
        if _job.get("stop"):
            stopped = True
            log_line("[!] 收到停止指令，终止当前轮")
            _terminate_register_proc(_proc)
            break

        remaining = deadline - time.time()
        if remaining <= 0:
            timed_out = True
            log_line(
                f"[!] 第 {round_no} 轮超时（{round_timeout}s），终止进程并进入下一轮"
            )
            with _job_lock:
                _job["last_error"] = f"round {round_no} timeout after {round_timeout}s"
            _terminate_register_proc(_proc)
            break

        try:
            raw = line_q.get(timeout=min(1.0, max(0.05, remaining)))
        except queue.Empty:
            if _proc.poll() is not None:
                # process exited; drain residual lines briefly
                drain_deadline = time.time() + 1.0
                while time.time() < drain_deadline:
                    try:
                        raw = line_q.get(timeout=0.1)
                    except queue.Empty:
                        break
                    if raw is None:
                        break
                    line = raw.rstrip("\n")
                    if not line:
                        continue
                    if _is_key_log(line):
                        log_line(_truncate_line(_strip_inner_timestamp(line)))
                    if "注册成功" in line or "[+] 注册成功" in line:
                        success = True
                    if "注册失败" in line or "[-] 注册失败" in line:
                        failed = True
                break
            continue

        if raw is None:
            break

        line = raw.rstrip("\n")
        if not line:
            continue
        # 只有关键日志才写入面板显示，但状态检测仍基于原始内容
        if _is_key_log(line):
            log_line(_truncate_line(_strip_inner_timestamp(line)))
        if "注册成功" in line or "[+] 注册成功" in line:
            success = True
        if "注册失败" in line or "[-] 注册失败" in line:
            failed = True
        if "任务结束" in line and ("成功" in line or "失败" in line):
            # final summary line often has both
            pass

    if _proc is not None and _proc.poll() is None:
        _terminate_register_proc(_proc)
    try:
        if _proc is not None:
            _proc.wait(timeout=15)
    except Exception:
        _terminate_register_proc(_proc)

    with _job_lock:
        _job["pid"] = None
        _job.pop("round_deadline", None)
    _proc = None
    _cleanup_browser_leftovers()

    if stopped:
        return False
    # 已打出「注册成功」后若在 NSFW/关浏览器阶段卡住被硬超时杀掉，账号其实已可用
    if success:
        if timed_out:
            log_line(
                f"[!] 第 {round_no} 轮在成功后超时被终止，仍记为成功（账号文件可能已写入）"
            )
        return True
    if timed_out:
        log_line(f"[-] 第 {round_no} 轮因超时记为失败")
        return False
    return False


def _next_node(nodes: List[str], index: int) -> Tuple[str, int]:
    if not nodes:
        return "", 0
    index = (index + 1) % len(nodes)
    return nodes[index], index


def job_worker(count: int, node: str = "", node_mode: str = "fixed", node_list: Optional[List[str]] = None):
    """Run register rounds. Node switching is intentionally not managed here —
    user selects nodes in their own Clash client."""
    global _job
    try:
        with _job_lock:
            _job["running"] = True
            _job["stop"] = False
            _job["status"] = "running"
            _job["count"] = count
            _job["success"] = 0
            _job["fail"] = 0
            _job["current_round"] = 0
            _job["node_mode"] = "external"
            _job["node_list"] = []
            _job["current_node"] = "external-clash"
            _job["started_at"] = datetime.now().isoformat(timespec="seconds")
            _job["finished_at"] = None
            _job["last_error"] = ""

        proxy_now = resolve_proxy_url()
        global PROXY_URL
        PROXY_URL = proxy_now
        os.environ["GROK_PROXY"] = proxy_now
        try:
            cfg0 = load_config(); cfg0["proxy"] = proxy_now; save_config(cfg0)
        except Exception:
            pass
        log_line(f"[*] 使用外部 Clash 代理: {proxy_now}（节点请在 Clash 客户端选择）")
        log_line(f"[*] 出口探测: {clash_exit_ip()}")

        for i in range(1, count + 1):
            if _job.get("stop"):
                log_line("[!] 用户停止，结束任务")
                break

            with _job_lock:
                _job["current_round"] = i

            before_lines = account_line_set()
            ok = _run_one_round(i, count)

            # 无论本轮判定成功/失败，都扫一遍新账号文件：
            # 避免「已写入 accounts 但日志未刷出/成功后硬超时」漏掉 CPA 转换
            queued = 0
            if AUTO_CPA:
                time.sleep(0.8)
                queued = enqueue_new_accounts(before_lines)
                if queued:
                    log_line(f"[CPA] 本轮新账号入队转换: {queued}")
                elif ok:
                    queued2 = enqueue_missing_accounts(limit=3)
                    if queued2:
                        log_line(f"[CPA] 未匹配到新行，补队最近未转换: {queued2}")
                        queued = queued2
                    else:
                        log_line("[CPA] 本轮未发现可转换的新 SSO（可能文件未写出）")

            # 日志没成功但文件里多了账号 → 也算成功
            if not ok and queued > 0:
                ok = True
                log_line(f"[+] 第 {i} 轮日志未显示成功，但检测到 {queued} 个新账号，记为成功")

            if ok:
                with _job_lock:
                    _job["success"] = int(_job.get("success") or 0) + 1
                log_line(f"[+] 第 {i} 轮成功（累计成功 {_job['success']}）")
            else:
                with _job_lock:
                    _job["fail"] = int(_job.get("fail") or 0) + 1
                log_line(f"[-] 第 {i} 轮失败（累计失败 {_job['fail']}），继续下一轮")

            time.sleep(1)

        log_line(
            f"[*] 全部结束：成功 {_job.get('success')} | 失败 {_job.get('fail')} / 目标 {count}"
        )
    except Exception as e:
        log_line(f"[!] 任务异常: {e}")
        log_line(traceback.format_exc())
        with _job_lock:
            _job["last_error"] = str(e)
    finally:
        with _job_lock:
            _job["running"] = False
            _job["status"] = "idle"
            _job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _job["pid"] = None


def start_job(count: int, node: str = "", node_mode: str = "fixed") -> Tuple[bool, str]:
    with _job_lock:
        if _job.get("running"):
            return False, "已有任务在运行"
    if count < 1 or count > 500:
        return False, "轮数范围 1-500"

    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    with _job_lock:
        _job["log_path"] = str(log_path)
    _logs.clear()
    log_line(f"任务创建：轮数={count} proxy={PROXY_URL}（节点由本机 Clash 管理）")

    th = threading.Thread(
        target=job_worker,
        args=(count,),
        daemon=True,
    )
    th.start()
    return True, "已启动"


def stop_job() -> Tuple[bool, str]:
    global _proc
    with _job_lock:
        if not _job.get("running"):
            return False, "当前没有运行中的任务"
        _job["stop"] = True
    log_line("[!] 正在停止…")
    p = _proc
    _terminate_register_proc(p)
    _cleanup_browser_leftovers()
    return True, "已发送停止"


# --------------- HTML ---------------
LOGIN_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>登录 · Grok Register</title>
  <style>
    :root{--bg:#0b0e14;--card:#141a26;--fg:#eef2fb;--muted:#8b97b0;--line:#222b3d;--accent:#6ea8fe;--accent2:#4f8cff}
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
      background:radial-gradient(1200px 600px at 20% -10%,#1a2540 0%,transparent 55%),radial-gradient(900px 500px at 80% 100%,#1a1f3a 0%,transparent 50%),var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
    .card{width:min(420px,92vw);background:var(--card);border:1px solid var(--line);border-radius:18px;padding:32px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    .brand{display:flex;align-items:center;gap:12px;margin-bottom:6px}
    .logo{width:40px;height:40px;border-radius:11px;background:#000;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:900;color:#fff;flex-shrink:0;box-shadow:0 6px 18px rgba(0,0,0,.35);letter-spacing:-1px}
    h1{margin:0;font-size:22px;font-weight:700} p{margin:6px 0 22px;color:var(--muted);font-size:13.5px}
    input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#0f131c;color:var(--fg);font-size:14px;font-family:inherit;transition:border-color .15s}
    input:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.15)}
    button{margin-top:16px;width:100%;padding:12px;border:0;border-radius:10px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;font-weight:600;font-size:14px;cursor:pointer;transition:box-shadow .15s}
    button:hover{box-shadow:0 6px 18px rgba(79,140,255,.45)}
    .err{color:#ff8f8f;margin-top:10px;font-size:13px}
  </style>
</head>
<body>
<form class="card" method="post">
  <div class="brand"><div class="logo">G</div><h1>Grok Register</h1></div>
  <p>账号面板 · 启动注册 · 外置 Clash 代理</p>
  <input type="password" name="password" placeholder="面板密码" autofocus required/>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <button type="submit">进入</button>
</form>
</body></html>
"""

INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Grok Register 面板</title>
  <style>
    :root{
      --bg:#0b0e14;--bg2:#0f131c;--card:#141a26;--card2:#1a2130;--fg:#eef2fb;--muted:#8b97b0;--muted2:#6b7793;
      --accent:#6ea8fe;--accent2:#4f8cff;--ok:#3dd68c;--bad:#ff7b7b;--warn:#ffb454;
      --line:#222b3d;--line2:#2c3650;--chip:#1c2434;--chip2:#222c40;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:
      radial-gradient(1200px 600px at 12% -18%,#1a2540 0%,transparent 55%),
      radial-gradient(900px 500px at 92% 8%,#1a1f3a 0%,transparent 50%),
      var(--bg);color:var(--fg);min-height:100vh;-webkit-font-smoothing:antialiased}
    .wrap{max-width:1200px;margin:0 auto;padding:24px 16px 56px}
    header{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:18px;border-bottom:1px solid var(--line)}
    .brand{display:flex;align-items:center;gap:14px}
    .logo{width:42px;height:42px;border-radius:12px;background:#000;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:900;color:#fff;flex-shrink:0;box-shadow:0 6px 18px rgba(0,0,0,.35);letter-spacing:-1px}
    h1{margin:0;font-size:22px;font-weight:700;letter-spacing:.3px} .sub{color:var(--muted);font-size:12.5px;margin-top:3px}
    .actions{display:flex;flex-wrap:wrap;gap:10px}
    a.btn,button.btn{border:1px solid var(--line2);background:var(--chip);color:var(--fg);padding:10px 14px;border-radius:10px;text-decoration:none;font-size:13px;cursor:pointer;transition:all .15s ease;display:inline-flex;align-items:center;gap:6px}
    a.btn:hover,button.btn:hover{background:var(--chip2);border-color:var(--accent);transform:translateY(-1px)}
    a.btn:active,button.btn:active{transform:translateY(0)}
    a.btn.primary,button.btn.primary{background:linear-gradient(135deg,var(--accent2),var(--accent));border-color:transparent;color:#fff;font-weight:600;box-shadow:0 4px 12px rgba(79,140,255,.3)}
    a.btn.primary:hover,button.btn.primary:hover{box-shadow:0 6px 18px rgba(79,140,255,.45)}
    a.btn.ok,button.btn.ok{background:linear-gradient(135deg,#1f9d63,#3dd68c);border:0;color:#042;font-weight:600;box-shadow:0 4px 12px rgba(61,214,140,.25)}
    a.btn.ok:hover,button.btn.ok:hover{box-shadow:0 6px 18px rgba(61,214,140,.4)}
    a.btn.sub2,button.btn.sub2{background:linear-gradient(135deg,#6d28d9,#a78bfa);border:0;color:#fff;font-weight:600;box-shadow:0 4px 12px rgba(167,139,250,.28)}
    a.btn.sub2:hover,button.btn.sub2:hover{box-shadow:0 6px 18px rgba(167,139,250,.45)}
    a.btn.danger,button.btn.danger{background:#2a1717;border-color:#5a2b2b;color:#ffb4b4}
    a.btn.danger:hover,button.btn.danger:hover{background:#381c1c}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0 20px}
    .stat{background:linear-gradient(180deg,var(--card) 0%,var(--card2) 100%);border:1px solid var(--line);border-radius:14px;padding:14px 16px;position:relative;overflow:hidden;transition:border-color .15s}
    .stat:hover{border-color:var(--accent)}
    .stat::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),transparent);opacity:.7}
    .stat .k{color:var(--muted2);font-size:11.5px;text-transform:uppercase;letter-spacing:.5px}
    .stat .v{font-size:22px;font-weight:700;margin-top:6px;color:var(--fg)}
    .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:16px;box-shadow:0 4px 16px rgba(0,0,0,.15)}
    .card h2{margin:0 0 14px;font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
    .card h2::before{content:"";width:3px;height:14px;background:linear-gradient(180deg,var(--accent),var(--accent2));border-radius:2px}
    .row{display:flex;flex-wrap:wrap;gap:12px;align-items:end}
    label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted)}
    input,select{background:var(--bg2);border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:10px 12px;min-width:150px;font-size:13px;transition:border-color .15s;font-family:inherit}
    input:focus,select:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.15)}
    table{width:100%;border-collapse:collapse}
    th,td{padding:11px 14px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}
    th{color:var(--muted);background:var(--bg2);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
    tbody tr{transition:background .12s}
    tbody tr:hover{background:rgba(110,168,254,.04)}
    .mono{font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;word-break:break-all;font-size:12.5px}
    .muted{color:var(--muted)} .tag{display:inline-block;padding:3px 10px;border-radius:999px;background:var(--chip);color:var(--accent);font-size:12px;font-weight:500}
    #logbox{height:340px;overflow:auto;background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:14px;font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;font-size:12.5px;line-height:1.5;white-space:pre-wrap;color:var(--muted)}
    #logbox::-webkit-scrollbar{width:8px}
    #logbox::-webkit-scrollbar-thumb{background:var(--line2);border-radius:4px}
    .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;background:#555;vertical-align:middle}
    .dot.run{background:var(--ok);box-shadow:0 0 10px var(--ok);animation:pulse 1.5s ease-in-out infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
    .toast{position:fixed;right:20px;bottom:20px;background:var(--card2);border:1px solid var(--line2);padding:12px 16px;border-radius:10px;display:none;z-index:9;box-shadow:0 8px 24px rgba(0,0,0,.4);font-size:13px}
    code{background:var(--chip);padding:2px 6px;border-radius:4px;font-size:12px;color:var(--accent)}
    @media(max-width:800px){ th:nth-child(3),td:nth-child(3){display:none} .row{flex-direction:column;align-items:stretch} input,select{min-width:0} }
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">G</div>
      <div>
        <h1>Grok Register</h1>
        <div class="sub">{{ base_dir }} · 代理走本机 Clash（Clash Verge 默认 7897）</div>
      </div>
    </div>
    <div class="actions">
      <a class="btn primary" href="/download/sso.txt" title="email----password----sso">⬇ 下载 SSO (TXT)</a>
      <a class="btn ok" href="/download/cpa.zip" title="CPA OAuth JSON（CLIProxyAPI 可用）">⬇ 下载 CPA (JSON)</a>
      <a class="btn sub2" href="/download/sub2.zip" title="Sub2API 官方导入包 type=sub2api-data：单账号 JSON + all 合集">⬇ 下载 Sub2 (JSON)</a>
    </div>
  </header>

  <div class="grid">
    <div class="stat"><div class="k">文件数</div><div class="v" id="st_files">{{ file_count }}</div></div>
    <div class="stat"><div class="k">SSO 账号</div><div class="v" id="st_accounts">{{ account_count }}</div></div>
    <div class="stat"><div class="k">CPA 已转换</div><div class="v" id="st_cpa_ok">{{ cpa_files }}</div></div>
    <div class="stat"><div class="k">CPA 队列</div><div class="v" style="font-size:16px" id="st_cpa_q">0 / 0 / 0</div></div>
    <div class="stat"><div class="k">任务状态</div><div class="v" style="font-size:16px"><span class="dot" id="st_dot"></span><span id="st_status">idle</span></div></div>
    <div class="stat"><div class="k">注册 成功/失败</div><div class="v" style="font-size:16px"><span id="st_sf">0 / 0</span></div></div>
  </div>

  <div class="card">
    <h2>启动注册</h2>
    <div class="row">
      <label>轮数
        <input type="number" id="count" min="1" max="500" value="1"/>
      </label>
      <label>浏览器引擎
        <select id="browser_engine" onchange="saveBrowserEngine()">
          <option value="chromium">Chromium 有头（默认）</option>
          <option value="camoufox">Camoufox 无头（反检测 Firefox）</option>
        </select>
      </label>
      <button class="btn ok" id="btn_start" onclick="startJob()">▶ 开始注册</button>
      <button class="btn danger" id="btn_stop" onclick="stopJob()">■ 停止</button>
      <button class="btn" onclick="backfillCpa()" title="把尚未转成 CPA 的历史 SSO 入队">补转未转换 CPA</button>
    </div>
    <div class="muted" style="margin-top:10px;font-size:12px" id="cpa_hint">
      代理走本机 Clash（config.json 的 proxy，常见 7897）。节点在 Clash 里选。注册成功后自动转 CPA。
      Camoufox 首次使用会自动下载浏览器二进制。
    </div>
    <div class="muted" style="margin-top:8px;font-size:12px;line-height:1.55">
      提示：绝大多数注册失败来自网络环境，而非脚本本身。实测机场节点里<strong style="color:var(--ok);font-weight:600">日本</strong>更稳；
      新加坡 / 美国 / 德国成功率偏低。失败时请先在 Clash 换日本节点再试。
    </div>
  </div>

  <div class="card">
    <h2>Sub2API 分组</h2>
    <div class="muted" style="font-size:12px;margin:0 0 12px;line-height:1.55" id="sub2_status_line">
      加载 Sub2 状态中…
    </div>
    <div class="row">
      <label style="flex:2">现有分组
        <select id="sub2_group_select">
          <option value="0">（未选择 / 不强制绑定）</option>
        </select>
      </label>
      <button class="btn" onclick="loadSub2Groups()">刷新分组</button>
      <button class="btn primary" onclick="selectSub2Group()">设为导入目标</button>
      <button class="btn danger" onclick="clearSub2Group()">清除目标</button>
    </div>
    <div class="row" style="margin-top:12px">
      <label style="flex:2">新建分组名
        <input type="text" id="sub2_new_name" placeholder="例如 grok-batch-0719"/>
      </label>
      <label>平台
        <select id="sub2_new_platform">
          <option value="grok" selected>grok</option>
          <option value="openai">openai</option>
          <option value="anthropic">anthropic</option>
          <option value="gemini">gemini</option>
          <option value="antigravity">antigravity</option>
        </select>
      </label>
      <button class="btn sub2" onclick="createSub2Group()">创建并选用</button>
    </div>
    <div class="muted" style="margin-top:10px;font-size:12px;line-height:1.55">
      自动推送开启时：CPA 转换成功 → 导入 Sub2API → 绑定上方所选分组。
      导入接口本身不带 <code>group_ids</code>，所以是 import 后再 bulk-update 绑定。
      目标分组会持久化到 <code>data/sub2_group.json</code>。
    </div>
  </div>

  <div class="card">
    <h2>邮箱服务</h2>
    <div class="muted" style="font-size:12px;margin:0 0 10px;line-height:1.55;padding:10px 12px;border:1px solid #5b3b14;background:rgba(180,100,20,.12);border-radius:10px;color:#f0c674">
      公共 Tempmailer 已移除（滥用后拒收 xAI 验证码）。请用下拉框选择邮箱源；自建/CF Worker 通常更稳，公共源可能仍被拒。
    </div>
    <div class="row">
      <label>邮箱源
        <select id="email_provider" onchange="onEmailProviderChange()"></select>
      </label>
      <label style="min-width:auto;flex-direction:row;align-items:center;gap:8px;padding-bottom:10px">
        <input type="checkbox" id="email_failover" style="width:auto;min-width:0"/> 失败时自动换源
      </label>
      <button class="btn primary" onclick="saveEmailConfig()">保存邮箱设置</button>
    </div>

    <div id="box_cfworker" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="cfworker_api_url" placeholder="https://apimail.example.com"/>
        </label>
        <label>Admin Token
          <input type="password" id="cfworker_admin_token" placeholder="管理员密钥"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>域名
          <input type="text" id="cfworker_domain" placeholder="mail.example.com"/>
        </label>
        <label>站点密码
          <input type="password" id="cfworker_custom_auth" placeholder="可选"/>
        </label>
        <label>子域名
          <input type="text" id="cfworker_subdomain" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_cloudflare" class="mail-box" style="display:none;margin-top:10px">
      <div class="muted" style="font-size:12px;margin-bottom:8px">兼容 cloudflare_temp_email：创建地址 + 收信。</div>
      <div class="row">
        <label style="flex:2">API 根地址
          <input type="text" id="custom_api_base" placeholder="https://mail.example.com"/>
        </label>
        <label>API Key
          <input type="password" id="custom_api_key" placeholder="x-admin-auth"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>鉴权方式
          <select id="custom_auth_mode">
            <option value="x-admin-auth">x-admin-auth</option>
            <option value="bearer">Bearer</option>
            <option value="x-api-key">X-API-Key</option>
            <option value="query-key">?key=</option>
            <option value="none">无</option>
          </select>
        </label>
        <label>域名
          <input type="text" id="custom_domain" placeholder="mail.example.com"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>创建路径
          <input type="text" id="custom_path_accounts" placeholder="/admin/new_address"/>
        </label>
        <label>收信路径
          <input type="text" id="custom_path_messages" placeholder="/api/mails"/>
        </label>
        <label>Token 路径
          <input type="text" id="custom_path_token" placeholder="/api/token"/>
        </label>
      </div>
    </div>

    <div id="box_moemail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="moemail_api_url" placeholder="https://sall.cc"/>
        </label>
        <label>API Key
          <input type="password" id="moemail_api_key" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_tempmail_lol" class="mail-box" style="display:none;margin-top:10px">
      <div class="muted" style="font-size:12px">TempMail.lol：无需 Key，自动生成邮箱后轮询收信（可能被 xAI 拒绝）。</div>
    </div>

    <div id="box_duckmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label>Web URL
          <input type="text" id="duckmail_api_url" placeholder="https://www.duckmail.sbs"/>
        </label>
        <label>Provider URL
          <input type="text" id="duckmail_provider_url" placeholder="https://api.duckmail.sbs"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>Bearer
          <input type="password" id="duckmail_bearer" placeholder="可选"/>
        </label>
        <label>API Key
          <input type="password" id="duckmail_api_key" placeholder="可选"/>
        </label>
        <label>域名
          <input type="text" id="duckmail_domain" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_gptmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="gptmail_base_url" placeholder="https://mail.chatgpt.org.uk"/>
        </label>
        <label>API Key
          <input type="password" id="gptmail_api_key" placeholder="可选"/>
        </label>
        <label>域名
          <input type="text" id="gptmail_domain" placeholder="可选，填了则本地拼地址"/>
        </label>
      </div>
    </div>

    <div id="box_maliapi" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="maliapi_base_url" placeholder="https://maliapi.215.im/v1"/>
        </label>
        <label>API Key
          <input type="password" id="maliapi_api_key"/>
        </label>
        <label>域名
          <input type="text" id="maliapi_domain" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_luckmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">平台地址
          <input type="text" id="luckmail_base_url" placeholder="https://mails.luckyous.com"/>
        </label>
        <label>API Key
          <input type="password" id="luckmail_api_key"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>项目代码
          <input type="text" id="luckmail_project_code" placeholder="grok"/>
        </label>
        <label>域名
          <input type="text" id="luckmail_domain" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_mailnest" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">平台地址
          <input type="text" id="mailnest_base_url" placeholder="https://mailnest.top"/>
        </label>
        <label>API Key
          <input type="password" id="mailnest_api_key" placeholder="sk_..."/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>项目代号
          <input type="text" id="mailnest_project_code" placeholder="x-ai001"/>
        </label>
        <label>购买模式
          <select id="mailnest_sale_mode">
            <option value="temporary">temporary 临时邮箱</option>
            <option value="exclusive">exclusive 专属邮箱</option>
          </select>
        </label>
      </div>
      <div style="margin-top:8px;opacity:.75;font-size:12px">
        Grok/xAI 用 project_code=<code>x-ai001</code>。库存为 0 时购买会失败，面板保存不扣费。
      </div>
    </div>

    <div id="box_gmail_forward" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:1.2">你的域名
          <input type="text" id="gmail_forward_domain" placeholder="your-domain.com"/>
        </label>
        <label style="flex:1.5">Gmail 地址
          <input type="text" id="gmail_imap_user" placeholder="you@gmail.com"/>
        </label>
        <label style="flex:1.5">应用专用密码
          <input type="password" id="gmail_imap_password" placeholder="xxxx xxxx xxxx xxxx"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>IMAP 主机
          <input type="text" id="gmail_imap_host" placeholder="imap.gmail.com"/>
        </label>
        <label style="max-width:100px">端口
          <input type="text" id="gmail_imap_port" placeholder="993"/>
        </label>
        <label style="flex:2">扫描文件夹
          <input type="text" id="gmail_imap_folders" placeholder="INBOX,Spam,[Gmail]/Spam"/>
        </label>
        <label style="max-width:110px">别名长度
          <input type="text" id="gmail_forward_local_len" placeholder="10"/>
        </label>
      </div>
      <div style="margin-top:8px;opacity:.75;font-size:12px">
        原理：每次随机 <code>xxx@你的域名</code> 去注册 Grok；Spaceship 免费邮箱转发到 Gmail；脚本用 IMAP 读 Gmail 提验证码。
        必须开启<strong>通配/catch-all 转发</strong>（任意前缀都能进 Gmail），Gmail 开 IMAP +
        <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">应用专用密码</a>（不是登录密码）。
      </div>
    </div>

    <div id="box_skymail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API Base
          <input type="text" id="skymail_api_base" placeholder="https://api.skymail.ink"/>
        </label>
        <label>Token
          <input type="password" id="skymail_token"/>
        </label>
        <label>域名
          <input type="text" id="skymail_domain"/>
        </label>
      </div>
    </div>

    <div id="box_cloudmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API Base
          <input type="text" id="cloudmail_api_base"/>
        </label>
        <label>管理员邮箱
          <input type="text" id="cloudmail_admin_email"/>
        </label>
        <label>管理员密码
          <input type="password" id="cloudmail_admin_password"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>域名
          <input type="text" id="cloudmail_domain"/>
        </label>
      </div>
    </div>

    <div id="box_freemail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="freemail_api_url"/>
        </label>
        <label>Admin Token
          <input type="password" id="freemail_admin_token"/>
        </label>
        <label>域名
          <input type="text" id="freemail_domain"/>
        </label>
      </div>
    </div>

    <div id="box_opentrashmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="opentrashmail_api_url"/>
        </label>
        <label>域名
          <input type="text" id="opentrashmail_domain"/>
        </label>
        <label>密码
          <input type="password" id="opentrashmail_password"/>
        </label>
      </div>
    </div>

    <div id="box_laoudo" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label>Auth
          <input type="password" id="laoudo_auth"/>
        </label>
        <label>邮箱
          <input type="text" id="laoudo_email"/>
        </label>
        <label>Account ID
          <input type="text" id="laoudo_account_id"/>
        </label>
      </div>
    </div>

    <div class="muted" style="margin-top:10px;font-size:12px;display:none" id="email_hint"></div>
  </div>

  <div class="card">
    <h2>运行日志</h2>
    <div id="logbox">等待任务…</div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:14px 14px 0;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between">
      <h2 style="margin:0">账号文件</h2>
      <div class="actions" style="margin:0">
        <button class="btn" type="button" onclick="toggleSelectAllFiles(true)">全选</button>
        <button class="btn" type="button" onclick="toggleSelectAllFiles(false)">取消全选</button>
        <button class="btn danger" type="button" onclick="deleteSelectedFiles()">删除选中</button>
      </div>
    </div>
    <div class="muted" style="padding:8px 14px 0;font-size:12px">勾选已下载/不需要的 accounts_*.txt，删除后不会再出现在「下载 SSO / CPA / Sub2」导出结果里（历史 CPA 也会按邮箱过滤/清理）。</div>
    {% if files %}
    <table>
      <thead>
        <tr>
          <th style="width:44px"><input type="checkbox" id="chk_all_files" onclick="toggleSelectAllFiles(this.checked)" title="全选"/></th>
          <th>文件</th><th>数量</th><th>时间</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
      {% for f in files %}
        <tr>
          <td><input type="checkbox" class="chk-file" value="{{ f.name }}"/></td>
          <td class="mono">{{ f.name }}</td>
          <td><span class="tag">{{ f.count }}</span></td>
          <td class="muted">{{ f.mtime }}</td>
          <td>
            <a class="btn" href="/preview/{{ f.name }}">预览</a>
            <a class="btn primary" href="/download/{{ f.name }}">下载</a>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div style="padding:24px;color:var(--muted);text-align:center">暂无 accounts_*.txt</div>
    {% endif %}
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2200)}
async function api(url, opt){
  const r = await fetch(url, Object.assign({credentials:'same-origin'}, opt||{}));
  const j = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||r.statusText||'request failed');
  return j;
}
function onEmailProviderChange(){
  const p=document.getElementById('email_provider').value||'cfworker';
  document.querySelectorAll('.mail-box').forEach(el=>{ el.style.display='none'; });
  const box=document.getElementById('box_'+p);
  if(box) box.style.display='block';
  // cloudflare alias box
  if(p==='cloudflare'){
    const b=document.getElementById('box_cloudflare');
    if(b) b.style.display='block';
  }
  if(p==='cfworker'){
    const b=document.getElementById('box_cfworker');
    if(b) b.style.display='block';
  }
}
function _val(id){const el=document.getElementById(id); return el?el.value:'';}
function _set(id,v){const el=document.getElementById(id); if(el) el.value=v||'';}
function _check(id,v){const el=document.getElementById(id); if(el) el.checked=!!v;}
async function loadEmailConfig(){
  try{
    const j=await api('/api/config/email');
    const e=j.email||{};
    const sel=document.getElementById('email_provider');
    sel.innerHTML='';
    (e.choices||[]).forEach(c=>{
      const o=document.createElement('option');
      o.value=c.id; o.textContent=c.label;
      sel.appendChild(o);
    });
    let prov=e.provider||'cfworker';
    if(![...sel.options].some(o=>o.value===prov)) prov='cfworker';
    sel.value=prov;
    _check('email_failover', e.email_failover);
    _set('cfworker_api_url', e.cfworker_api_url);
    _set('cfworker_admin_token', e.cfworker_admin_token);
    _set('cfworker_domain', e.cfworker_domain);
    _set('cfworker_custom_auth', e.cfworker_custom_auth);
    _set('cfworker_subdomain', e.cfworker_subdomain);
    _set('custom_api_base', e.custom_api_base);
    _set('custom_api_key', e.custom_api_key);
    _set('custom_auth_mode', e.custom_auth_mode||'x-admin-auth');
    _set('custom_domain', e.custom_domain);
    _set('custom_path_accounts', e.custom_path_accounts||'/admin/new_address');
    _set('custom_path_messages', e.custom_path_messages||'/api/mails');
    _set('custom_path_token', e.custom_path_token||'/api/token');
    _set('moemail_api_url', e.moemail_api_url||'https://sall.cc');
    _set('moemail_api_key', e.moemail_api_key);
    _set('gptmail_base_url', e.gptmail_base_url||'https://mail.chatgpt.org.uk');
    _set('gptmail_api_key', e.gptmail_api_key);
    _set('gptmail_domain', e.gptmail_domain);
    _set('duckmail_api_url', e.duckmail_api_url||'https://www.duckmail.sbs');
    _set('duckmail_provider_url', e.duckmail_provider_url||'https://api.duckmail.sbs');
    _set('duckmail_bearer', e.duckmail_bearer);
    _set('duckmail_api_key', e.duckmail_api_key);
    _set('duckmail_domain', e.duckmail_domain);
    _set('maliapi_base_url', e.maliapi_base_url||'https://maliapi.215.im/v1');
    _set('maliapi_api_key', e.maliapi_api_key);
    _set('maliapi_domain', e.maliapi_domain);
    _set('luckmail_base_url', e.luckmail_base_url||'https://mails.luckyous.com');
    _set('luckmail_api_key', e.luckmail_api_key);
    _set('luckmail_project_code', e.luckmail_project_code||'grok');
    _set('luckmail_domain', e.luckmail_domain);
    _set('mailnest_base_url', e.mailnest_base_url||'https://mailnest.top');
    _set('mailnest_api_key', e.mailnest_api_key);
    _set('mailnest_project_code', e.mailnest_project_code||'x-ai001');
    _set('mailnest_sale_mode', e.mailnest_sale_mode||'temporary');
    _set('gmail_forward_domain', e.gmail_forward_domain||'');
    _set('gmail_imap_user', e.gmail_imap_user||'');
    _set('gmail_imap_password', e.gmail_imap_password||'');
    _set('gmail_imap_host', e.gmail_imap_host||'imap.gmail.com');
    _set('gmail_imap_port', e.gmail_imap_port||'993');
    _set('gmail_imap_folders', e.gmail_imap_folders||'INBOX,Spam,[Gmail]/Spam');
    _set('gmail_forward_local_len', e.gmail_forward_local_len||'10');
    _set('skymail_api_base', e.skymail_api_base||'https://api.skymail.ink');
    _set('skymail_token', e.skymail_token);
    _set('skymail_domain', e.skymail_domain);
    _set('cloudmail_api_base', e.cloudmail_api_base);
    _set('cloudmail_admin_email', e.cloudmail_admin_email);
    _set('cloudmail_admin_password', e.cloudmail_admin_password);
    _set('cloudmail_domain', e.cloudmail_domain);
    _set('freemail_api_url', e.freemail_api_url);
    _set('freemail_admin_token', e.freemail_admin_token);
    _set('freemail_domain', e.freemail_domain);
    _set('opentrashmail_api_url', e.opentrashmail_api_url);
    _set('opentrashmail_domain', e.opentrashmail_domain);
    _set('opentrashmail_password', e.opentrashmail_password);
    _set('laoudo_auth', e.laoudo_auth);
    _set('laoudo_email', e.laoudo_email);
    _set('laoudo_account_id', e.laoudo_account_id);
    setEmailHint(e.hint||'');
    onEmailProviderChange();
  }catch(err){
    setEmailHint('加载邮箱配置失败: '+err.message);
  }
}
async function saveEmailConfig(){
  const body={
    provider: (document.getElementById('email_provider').value||'cfworker'),
    email_failover: document.getElementById('email_failover').checked,
    cfworker_api_url: _val('cfworker_api_url'),
    cfworker_admin_token: _val('cfworker_admin_token'),
    cfworker_domain: _val('cfworker_domain'),
    cfworker_custom_auth: _val('cfworker_custom_auth'),
    cfworker_subdomain: _val('cfworker_subdomain'),
    custom_api_base: _val('custom_api_base'),
    custom_api_key: _val('custom_api_key'),
    custom_auth_mode: _val('custom_auth_mode')||'x-admin-auth',
    custom_domain: _val('custom_domain'),
    custom_path_accounts: _val('custom_path_accounts'),
    custom_path_messages: _val('custom_path_messages'),
    custom_path_token: _val('custom_path_token'),
    moemail_api_url: _val('moemail_api_url'),
    moemail_api_key: _val('moemail_api_key'),
    gptmail_base_url: _val('gptmail_base_url'),
    gptmail_api_key: _val('gptmail_api_key'),
    gptmail_domain: _val('gptmail_domain'),
    duckmail_api_url: _val('duckmail_api_url'),
    duckmail_provider_url: _val('duckmail_provider_url'),
    duckmail_bearer: _val('duckmail_bearer'),
    duckmail_api_key: _val('duckmail_api_key'),
    duckmail_domain: _val('duckmail_domain'),
    maliapi_base_url: _val('maliapi_base_url'),
    maliapi_api_key: _val('maliapi_api_key'),
    maliapi_domain: _val('maliapi_domain'),
    luckmail_base_url: _val('luckmail_base_url'),
    luckmail_api_key: _val('luckmail_api_key'),
    luckmail_project_code: _val('luckmail_project_code'),
    luckmail_domain: _val('luckmail_domain'),
    mailnest_base_url: _val('mailnest_base_url'),
    mailnest_api_key: _val('mailnest_api_key'),
    mailnest_project_code: _val('mailnest_project_code'),
    mailnest_sale_mode: _val('mailnest_sale_mode')||'temporary',
    gmail_forward_domain: _val('gmail_forward_domain'),
    gmail_imap_user: _val('gmail_imap_user'),
    gmail_imap_password: _val('gmail_imap_password'),
    gmail_imap_host: _val('gmail_imap_host')||'imap.gmail.com',
    gmail_imap_port: _val('gmail_imap_port')||'993',
    gmail_imap_folders: _val('gmail_imap_folders')||'INBOX,Spam,[Gmail]/Spam',
    gmail_forward_local_len: _val('gmail_forward_local_len')||'10',
    skymail_api_base: _val('skymail_api_base'),
    skymail_token: _val('skymail_token'),
    skymail_domain: _val('skymail_domain'),
    cloudmail_api_base: _val('cloudmail_api_base'),
    cloudmail_admin_email: _val('cloudmail_admin_email'),
    cloudmail_admin_password: _val('cloudmail_admin_password'),
    cloudmail_domain: _val('cloudmail_domain'),
    freemail_api_url: _val('freemail_api_url'),
    freemail_admin_token: _val('freemail_admin_token'),
    freemail_domain: _val('freemail_domain'),
    opentrashmail_api_url: _val('opentrashmail_api_url'),
    opentrashmail_domain: _val('opentrashmail_domain'),
    opentrashmail_password: _val('opentrashmail_password'),
    laoudo_auth: _val('laoudo_auth'),
    laoudo_email: _val('laoudo_email'),
    laoudo_account_id: _val('laoudo_account_id'),
  };
  try{
    const j=await api('/api/config/email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(j.message||'邮箱设置已保存');
    if(j.email){
      setEmailHint('已保存 · 当前: '+(j.email.provider||''));
    }
  }catch(e){toast('保存失败: '+e.message)}
}
function setEmailHint(text){
  const el=document.getElementById('email_hint');
  if(!el) return;
  const t=String(text||'').trim();
  el.textContent=t;
  el.style.display=t ? '' : 'none';
}
function toggleSelectAllFiles(on){
  const boxes=document.querySelectorAll('.chk-file');
  boxes.forEach(b=>{ b.checked=!!on; });
  const all=document.getElementById('chk_all_files');
  if(all) all.checked=!!on;
}
async function deleteSelectedFiles(){
  const files=[...document.querySelectorAll('.chk-file:checked')].map(b=>b.value);
  if(!files.length){
    toast('请先勾选要删除的账号文件');
    return;
  }
  if(!confirm('确认删除选中的 '+files.length+' 个账号文件？\n删除后无法恢复，下载 SSO 时也不会再包含它们。')){
    return;
  }
  try{
    const j=await api('/api/accounts/delete',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({files})
    });
    toast(j.message||('已删除 '+((j.deleted||[]).length)+' 个文件'));
    setTimeout(()=>location.reload(), 500);
  }catch(e){toast('删除失败: '+e.message)}
}
async function loadBrowserEngine(){
  try{
    const j=await api('/api/config/browser');
    const eng=(j.browser_engine||'chromium').toLowerCase();
    document.getElementById('browser_engine').value=(eng==='camoufox'?'camoufox':'chromium');
  }catch(e){}
}
async function saveBrowserEngine(){
  const browser_engine=document.getElementById('browser_engine').value;
  try{
    const j=await api('/api/config/browser',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({browser_engine})});
    toast(j.message||('浏览器引擎: '+(j.browser_engine||browser_engine)));
  }catch(e){toast('保存浏览器引擎失败: '+e.message)}
}
async function startJob(){
  const count=parseInt(document.getElementById('count').value||'1',10);
  try{
    // auto-save email settings before start
    try{ await saveEmailConfig(); }catch(e){}
    try{ await saveBrowserEngine(); }catch(e){}
    const j=await api('/api/job/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({count, browser_engine: document.getElementById('browser_engine').value})});
    toast(j.message||'已启动');
    poll();
  }catch(e){toast('启动失败: '+e.message)}
}
async function stopJob(){
  try{
    const j=await api('/api/job/stop',{method:'POST'});
    toast(j.message||'已停止');
  }catch(e){toast('停止失败: '+e.message)}
}
async function backfillCpa(){
  try{
    const j=await api('/api/cpa/backfill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit:200})});
    toast(j.message||('已入队 '+j.queued));
    poll();
  }catch(e){toast('补转失败: '+e.message)}
}
function renderSub2Status(sub2){
  const el=document.getElementById('sub2_status_line');
  if(!el) return;
  const s=sub2||{};
  const en=s.enabled?'开':'关';
  const tgt=s.target_group_id
    ? (`目标: ${s.target_group_name||s.target_group_id} (#${s.target_group_id}${s.target_group_platform?(' / '+s.target_group_platform):''})`)
    : '目标: 未选择';
  const push=`推送 ${s.ok||0}成/${s.fail||0}败`;
  const last=s.last_ok_email?(' · 最近OK: '+s.last_ok_email):'';
  const err=s.last_error?(' · 错: '+s.last_error):'';
  const base=s.base_url?(' · '+s.base_url):'';
  el.textContent=`自动推送:${en} · ${tgt} · ${push}${last}${err}${base}`;
}
async function loadSub2Groups(){
  const sel=document.getElementById('sub2_group_select');
  if(!sel) return;
  try{
    const j=await api('/api/sub2/groups');
    const groups=j.groups||[];
    const cur=Number(j.target_group_id||0);
    const prev=sel.value;
    sel.innerHTML='';
    const opt0=document.createElement('option');
    opt0.value='0';
    opt0.textContent='（未选择 / 不强制绑定）';
    sel.appendChild(opt0);
    for(const g of groups){
      const o=document.createElement('option');
      o.value=String(g.id);
      const plat=g.platform?(' ['+g.platform+']'):'';
      o.textContent=`#${g.id} ${g.name||''}${plat}`;
      o.dataset.name=g.name||'';
      o.dataset.platform=g.platform||'';
      sel.appendChild(o);
    }
    const want=cur>0?String(cur):(prev||'0');
    if([...sel.options].some(o=>o.value===want)) sel.value=want;
    else sel.value='0';
    renderSub2Status(j.sub2||{});
  }catch(e){
    renderSub2Status({});
    const el=document.getElementById('sub2_status_line');
    if(el) el.textContent='加载分组失败: '+e.message+'（检查 AUTO_SUB2_PUSH / SUB2API_* 凭据）';
  }
}
async function selectSub2Group(){
  const sel=document.getElementById('sub2_group_select');
  if(!sel) return;
  const opt=sel.options[sel.selectedIndex];
  const group_id=parseInt(sel.value||'0',10)||0;
  const body={
    group_id,
    group_name:(opt && opt.dataset && opt.dataset.name)||'',
    group_platform:(opt && opt.dataset && opt.dataset.platform)||''
  };
  try{
    const j=await api('/api/sub2/group/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(j.message||'已设置目标分组');
    renderSub2Status(j.sub2||{});
    await loadSub2Groups();
  }catch(e){toast('设置失败: '+e.message)}
}
async function clearSub2Group(){
  try{
    const j=await api('/api/sub2/group/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:0})});
    toast(j.message||'已清除目标分组');
    renderSub2Status(j.sub2||{});
    const sel=document.getElementById('sub2_group_select');
    if(sel) sel.value='0';
  }catch(e){toast('清除失败: '+e.message)}
}
async function createSub2Group(){
  const name=(document.getElementById('sub2_new_name').value||'').trim();
  const platform=document.getElementById('sub2_new_platform').value||'grok';
  if(!name){toast('先填分组名');return;}
  try{
    const j=await api('/api/sub2/groups',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name, platform, select:true})});
    toast(j.message||'已创建');
    document.getElementById('sub2_new_name').value='';
    renderSub2Status(j.sub2||{});
    await loadSub2Groups();
  }catch(e){toast('创建失败: '+e.message)}
}
let lastLogLen=0;
async function poll(){
  try{
    const j=await api('/api/job/status');
    const st=j.job||{};
    const cpa=j.cpa||{};
    document.getElementById('st_status').textContent=st.status||'idle';
    document.getElementById('st_dot').className='dot'+(st.running?' run':'');
    document.getElementById('st_sf').textContent=`${st.success||0} / ${st.fail||0}`;
    document.getElementById('btn_start').disabled=!!st.running;
    if(document.getElementById('st_cpa_ok')){
      // Prefer active-export count; fall back to files.
      document.getElementById('st_cpa_ok').textContent=String(
        (cpa.files_active!=null?cpa.files_active:cpa.files)||0
      );
    }
    if(document.getElementById('st_cpa_q')){
      document.getElementById('st_cpa_q').textContent=
        `${cpa.pending||0}待 / ${cpa.ok||0}成 / ${cpa.fail||0}败`;
    }
    if(document.getElementById('cpa_hint')){
      const core = cpa.core_ok ? 'core就绪' : ('core失败: '+(cpa.core_error||''));
      const last = cpa.last_ok_email ? (' · 最近OK: '+cpa.last_ok_email) : '';
      const err = cpa.last_error ? (' · 最近错: '+cpa.last_error) : '';
      const sub2 = j.sub2 || {};
      const tgt = sub2.target_group_id ? (`组#${sub2.target_group_id}`) : '未选组';
      const sub2txt = sub2.enabled
        ? (` · Sub2推送:开 ${sub2.ok||0}成/${sub2.fail||0}败 · ${tgt}` + (sub2.last_error?(' 错:'+sub2.last_error):''))
        : ' · Sub2推送:关';
      const activeN = (cpa.files_active!=null?cpa.files_active:cpa.files)||0;
      const allN = (cpa.files_all!=null?cpa.files_all:cpa.files)||0;
      const fileTxt = (allN && allN!==activeN)
        ? (`当前可导出 ${activeN} / 历史 ${allN}`)
        : (`当前可导出 ${activeN}`);
      document.getElementById('cpa_hint').textContent =
        `代理走本机 Clash · 自动CPA: ${cpa.enabled?'开':'关'} · ${core} · ${fileTxt}${last}${err}${sub2txt}`;
      renderSub2Status(sub2);
    }
    const box=document.getElementById('logbox');
    const logs=j.logs||[];
    if(logs.length!==lastLogLen){
      box.textContent=logs.join('\n');
      box.scrollTop=box.scrollHeight;
      lastLogLen=logs.length;
    }
  }catch(e){}
}
loadEmailConfig();
loadBrowserEngine();
loadSub2Groups();
poll();
setInterval(poll, 2000);
</script>
</body></html>
"""

PREVIEW_HTML = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>预览 {{ name }}</title>
<style>
:root{--bg:#0b0e14;--card:#141a26;--fg:#eef2fb;--muted:#8b97b0;--line:#222b3d;--accent:#6ea8fe;--accent2:#4f8cff;--bg2:#0f131c}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  background:radial-gradient(1000px 500px at 12% -18%,#1a2540 0%,transparent 55%),var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:24px 16px 56px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;padding-bottom:16px;border-bottom:1px solid var(--line);flex-wrap:wrap;gap:10px}
.top a{color:var(--accent);text-decoration:none;font-size:13.5px;padding:8px 14px;border:1px solid var(--line);border-radius:8px;transition:all .15s}
.top a:hover{border-color:var(--accent);background:rgba(110,168,254,.06)}
.brand{display:flex;align-items:center;gap:10px}
.logo{width:32px;height:32px;border-radius:9px;background:#000;display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:900;color:#fff;letter-spacing:-1px}
h1{margin:0;font-size:18px;font-weight:700}
pre{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:18px;overflow:auto;white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;font-size:13px;line-height:1.5;color:var(--muted)}
pre::-webkit-scrollbar{width:8px}
pre::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
</style>
</head><body><div class="wrap">
<div class="top">
  <div class="brand"><div class="logo">G</div><h1>{{ name }}</h1></div>
  <div><a href="/">← 返回</a> · <a href="/download/{{ name }}">下载</a></div>
</div>
<pre>{{ content }}</pre>
</div></body></html>
"""


# --------------- routes ---------------
@app.get("/login")
def login():
    # 默认无密码：直接进面板
    if not PANEL_AUTH:
        return redirect(url_for("index"))
    if session.get("ok"):
        return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML, error=None)


@app.post("/login")
def login_post():
    if not PANEL_AUTH:
        return redirect(url_for("index"))
    if request.form.get("password") == PANEL_PASSWORD:
        session["ok"] = True
        return redirect(request.args.get("next") or url_for("index"))
    return render_template_string(LOGIN_HTML, error="密码错误"), 401


@app.get("/logout")
def logout():
    session.clear()
    if not PANEL_AUTH:
        return redirect(url_for("index"))
    return redirect(url_for("login"))


@app.get("/")
def index():
    need = require_login()
    if need:
        return need
    files_meta = []
    total = 0
    for p in list_account_files():
        lines = read_account_lines(p)
        total += len(lines)
        files_meta.append(
            {
                "name": p.name,
                "count": len(lines),
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    return render_template_string(
        INDEX_HTML,
        base_dir=str(BASE_DIR),
        files=files_meta,
        file_count=len(files_meta),
        account_count=total,
        cpa_files=len(list_active_cpa_files()),
    )


def safe_name(name: str) -> Optional[Path]:
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not re.fullmatch(r"accounts_[\w.-]+\.txt", name):
        return None
    path = (BASE_DIR / name).resolve()
    if path.parent != BASE_DIR or not path.exists():
        return None
    return path


@app.get("/preview/<name>")
def preview_file(name: str):
    need = require_login()
    if need:
        return need
    path = safe_name(name)
    if not path:
        return "文件不存在", 404
    return render_template_string(
        PREVIEW_HTML,
        name=path.name,
        content=path.read_text(encoding="utf-8", errors="replace"),
    )


@app.get("/download/<name>")
def download_file(name: str):
    need = require_login()
    if need:
        return need
    path = safe_name(name)
    if not path:
        return "文件不存在", 404
    return send_file(
        path,
        as_attachment=True,
        download_name=path.name,
        mimetype="text/plain; charset=utf-8",
    )


def _merged_sso_txt() -> str:
    seen = set()
    lines = []
    for _, line in collect_all_accounts():
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


@app.get("/download/sso.txt")
def download_sso_txt():
    """主接口 1：全部 SSO，格式 email----password----sso"""
    need = require_login()
    if need:
        return need
    body = _merged_sso_txt()
    fname = f"sso_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/merged.txt")
def download_merged():
    """兼容旧链接 → 同 SSO txt"""
    return download_sso_txt()


@app.get("/download/all.zip")
def download_zip():
    need = require_login()
    if need:
        return need
    buf = io.BytesIO()
    files = list_account_files()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if not files:
            zf.writestr("README.txt", "暂无 accounts_*.txt\n")
        for p in files:
            zf.write(p, arcname=p.name)
        seen = set()
        merged = []
        for _, line in collect_all_accounts():
            if line not in seen:
                seen.add(line)
                merged.append(line)
        zf.writestr(
            "accounts_merged_all.txt",
            "\n".join(merged) + ("\n" if merged else ""),
        )
    buf.seek(0)
    fname = f"accounts_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


@app.get("/download/accounts.json")
def download_accounts_json():
    """All accounts as one JSON array (email/password/sso)."""
    need = require_login()
    if need:
        return need
    accounts = unique_accounts()
    body = json.dumps(accounts, ensure_ascii=False, indent=2) + "\n"
    fname = f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/cpa.zip")
def download_cpa_zip():
    """主接口 2：已自动 OAuth 转换的真 CPA JSON（auth_kind=oauth）。

    只导出「当前账号文件里还在」的邮箱对应 CPA，和下载 SSO TXT 口径一致。
    """
    need = require_login()
    if need:
        return need
    files = list_active_cpa_files()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            "Grok Register → 真 CPA (CLIProxyAPI) JSON\n"
            "====================================\n\n"
            "1) 每个 xai-*.json 是 OAuth 凭证（access_token + refresh_token）。\n"
            "2) auth_kind=oauth，可直接放进 CLIProxyAPI auth-dir。\n"
            "3) 由注册成功后的 web SSO 自动换票生成。\n"
            "4) all.json 为当前账号列表内邮箱的合集（与面板 accounts_*.txt 对齐）。\n"
            "5) 面板删除账号文件后，对应历史 CPA 不会再被导出。\n"
            "6) 若 zip 为空：先注册，或点「补转未转换 CPA」。\n"
        )
        zf.writestr("README.txt", readme)
        all_entries = []
        for i, p in enumerate(files, 1):
            try:
                raw = p.read_text(encoding="utf-8")
                obj = json.loads(raw)
                all_entries.append(obj)
                # keep original filename
                zf.writestr(p.name, raw if raw.endswith("\n") else raw + "\n")
            except Exception as e:
                zf.writestr(f"BAD-{p.name}.txt", str(e))
        zf.writestr(
            "all.json",
            json.dumps(all_entries, ensure_ascii=False, indent=2) + "\n",
        )
        if CPA_FAILED_PATH.exists():
            try:
                zf.write(CPA_FAILED_PATH, arcname="failed.jsonl")
            except Exception:
                pass
        if not files:
            zf.writestr(
                "EMPTY.txt",
                "当前账号列表没有可导出的 CPA。\n"
                "若你刚删了 accounts_*.txt，历史 CPA 已按邮箱过滤掉。\n"
                "需要导出：保留对应账号文件，或重新注册 / 补转 CPA。\n",
            )
    buf.seek(0)
    fname = f"cpa_oauth_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


def _load_cpa_entries_for_sub2() -> Tuple[List[dict], List[str]]:
    """Read CPA JSON for Sub2 export, filtered to remaining account emails.

    No re-OAuth. Aligns with SSO TXT export scope (remaining accounts_*.txt).
    """
    entries: List[dict] = []
    name_hints: List[str] = []
    for p in list_active_cpa_files():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                continue
            entries.append(obj)
            # Prefer real email field; fall back to filename stem.
            email = _cpa_entry_email(obj, p)
            if email:
                name_hints.append(email)
            else:
                stem = p.stem
                hint = stem[4:] if stem.lower().startswith("xai-") else stem
                name_hints.append(hint or "")
        except Exception:
            continue
    return entries, name_hints


def _fallback_sub2_payload(cpa_entries: List[dict], name_hints: List[str]) -> dict:
    """If sso2cpa_core import failed, still build a minimal sub2api-data package."""
    accounts: List[dict] = []
    for i, cpa in enumerate(cpa_entries):
        if not isinstance(cpa, dict):
            continue
        access = str(cpa.get("access_token") or "").strip()
        refresh = str(cpa.get("refresh_token") or "").strip()
        if not access and not refresh:
            continue
        email = str(cpa.get("email") or "").strip()
        sub = str(cpa.get("sub") or "").strip()
        hint = name_hints[i] if i < len(name_hints) else ""
        name = hint or email or sub or "grok-oauth"
        expires_at = str(cpa.get("expires_at") or cpa.get("expired") or "").strip()
        if not expires_at:
            expires_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        creds = {
            "access_token": access,
            "expires_at": expires_at,
            "base_url": str(cpa.get("base_url") or "https://cli-chat-proxy.grok.com/v1").strip(),
        }
        if refresh:
            creds["refresh_token"] = refresh
        token_type = str(cpa.get("token_type") or "Bearer").strip()
        if token_type:
            creds["token_type"] = token_type
        for k in ("id_token", "email", "sub", "client_id", "scope"):
            v = str(cpa.get(k) or "").strip()
            if v:
                creds[k] = v
        accounts.append(
            {
                "name": name,
                "platform": "grok",
                "type": "oauth",
                "credentials": creds,
                "concurrency": 1,
                "priority": 50,
            }
        )
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": accounts,
    }


def _build_sub2_accounts(
    cpa_entries: List[dict], name_hints: List[str]
) -> List[dict]:
    """Map CPA entries → Sub2 DataAccount list (no re-OAuth)."""
    if build_sub2_payload is not None:
        payload = build_sub2_payload(cpa_entries, name_hints=name_hints)
        return list(payload.get("accounts") or [])
    payload = _fallback_sub2_payload(cpa_entries, name_hints)
    return list(payload.get("accounts") or [])


def _sub2_package(accounts: List[dict]) -> dict:
    """Official Sub2API import wrapper around account list."""
    if build_sub2_payload is not None:
        # reuse core helper for type/version/exported_at; pass empty CPA list
        # then inject accounts (avoids re-mapping)
        base = build_sub2_payload([])
        base["accounts"] = accounts
        return base
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": accounts,
    }


def _sub2_safe_arcname(name: str, used: Set[str]) -> str:
    """Unique zip member name: grok-{name}.json"""
    base = cpa_safe_filename(name or "grok-oauth")
    fname = f"grok-{base}.json"
    if fname not in used:
        used.add(fname)
        return fname
    i = 2
    while True:
        alt = f"grok-{base}-{i}.json"
        if alt not in used:
            used.add(alt)
            return alt
        i += 1


@app.get("/download/sub2.zip")
def download_sub2_zip():
    """主接口 3：Sub2API 官方导入包 ZIP（对齐 CPA zip 结构）。

    从已转换的 CPA JSON 现场映射，不重新注册/换票。
    仅包含当前 accounts_*.txt 里还在的邮箱（与下载 SSO 一致）。

    zip 内容：
      README.txt
      grok-*.json     — 每个账号一份完整 sub2api-data（可单独导入）
      all.json        — 当前账号合集（推荐一键导入）
      EMPTY.txt       — 无账号时的说明
    """
    need = require_login()
    if need:
        return need
    cpa_entries, name_hints = _load_cpa_entries_for_sub2()
    accounts = _build_sub2_accounts(cpa_entries, name_hints)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            "Grok Register → Sub2API 官方导入包 (sub2api-data)\n"
            "================================================\n\n"
            "1) all.json：当前账号合集，推荐直接导入 Sub2API。\n"
            "   管理后台 → 账号 → 导入数据 → 上传 all.json\n"
            "2) grok-*.json：每个账号一份完整 sub2api-data（也可单独导入）。\n"
            "3) type=sub2api-data / version=1 / platform=grok / type=oauth\n"
            "4) 由已转换的 CPA OAuth 凭证现场映射，不重新注册/换票。\n"
            "5) 导出范围与面板账号文件一致：删除 accounts_*.txt 后不会再导出历史号。\n"
            "6) proxies 为空；导入后请在 Sub2API 里绑定分组/代理。\n"
            "7) 若 zip 为空：先注册，或点面板「补转未转换 CPA」。\n"
        )
        zf.writestr("README.txt", readme)

        used_names: Set[str] = set()
        for acc in accounts:
            try:
                single = _sub2_package([acc])
                raw = json.dumps(single, ensure_ascii=False, indent=2) + "\n"
                arc = _sub2_safe_arcname(str(acc.get("name") or ""), used_names)
                zf.writestr(arc, raw)
            except Exception as e:
                bad = _sub2_safe_arcname(
                    f"BAD-{acc.get('name') or 'unknown'}", used_names
                )
                zf.writestr(bad.replace(".json", ".txt"), str(e))

        all_pkg = _sub2_package(accounts)
        zf.writestr(
            "all.json",
            json.dumps(all_pkg, ensure_ascii=False, indent=2) + "\n",
        )

        if not accounts:
            zf.writestr(
                "EMPTY.txt",
                "当前账号列表没有可导出的 Sub2 账号。\n"
                "面板删除 accounts_*.txt 后，对应历史 CPA/Sub2 不会再被导出。\n"
                "需要导出：保留账号文件，或重新注册 / 补转 CPA。\n",
            )

    buf.seek(0)
    fname = f"sub2api_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


@app.get("/download/sub2.json")
def download_sub2_json():
    """兼容旧链接：返回 all 合集 JSON（等同 zip 内 all.json）。"""
    need = require_login()
    if need:
        return need
    cpa_entries, name_hints = _load_cpa_entries_for_sub2()
    accounts = _build_sub2_accounts(cpa_entries, name_hints)
    payload = _sub2_package(accounts)
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fname = f"sub2api_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/grok2api.json")
def download_grok2api_json():
    need = require_login()
    if need:
        return need
    body = (
        json.dumps(to_grok2api_pool(unique_accounts()), ensure_ascii=False, indent=2)
        + "\n"
    )
    fname = f"grok2api_pool_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/accounts")
def api_accounts():
    need = require_login()
    if need:
        return need
    data = []
    for source, line in collect_all_accounts():
        info = parse_line(line)
        info["source"] = source
        data.append(info)
    return jsonify(
        {
            "count": len(data),
            "files": [p.name for p in list_account_files()],
            "accounts": data,
        }
    )


@app.get("/api/nodes")
def api_nodes():
    need = require_login()
    if need:
        return need
    return jsonify(clash_list_nodes())


@app.post("/api/nodes/select")
def api_nodes_select():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    node = str(data.get("node") or "").strip()
    if not node:
        return jsonify({"ok": False, "error": "node required"}), 400
    ok, msg = clash_set_node(node)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg, "exit": clash_exit_ip()})


@app.post("/api/accounts/delete")
def api_accounts_delete():
    """Delete selected accounts_*.txt files (after user downloaded them).

    Also prune orphan CPA json for emails that no longer appear in any remaining
    account file, so Sub2/CPA exports stay aligned with the panel list.
    """
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    names = data.get("files") or data.get("names") or []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not names:
        return jsonify({"ok": False, "error": "files required"}), 400

    deleted = []
    missing = []
    errors = []
    for name in names:
        name = str(name or "").strip()
        path = safe_name(name)
        if not path:
            missing.append(name)
            continue
        try:
            path.unlink()
            deleted.append(path.name)
            log_line(f"[*] 已删除账号文件: {path.name}")
        except Exception as e:
            errors.append(f"{name}: {e}")

    pruned = {"removed": 0, "kept": 0, "errors": 0, "active_emails": 0}
    if deleted:
        try:
            pruned = prune_orphan_cpa_files()
        except Exception as e:
            log_line(f"[!] 清理孤儿 CPA 失败: {e}")
            pruned = {"removed": 0, "kept": 0, "errors": 1, "active_emails": 0, "error": str(e)}

    if not deleted and errors:
        return jsonify({"ok": False, "error": "; ".join(errors)}), 400
    msg = f"已删除 {len(deleted)} 个文件"
    if missing:
        msg += f"，跳过 {len(missing)}"
    removed_cpa = int(pruned.get("removed") or 0)
    if removed_cpa:
        msg += f"，并清理 {removed_cpa} 个历史 CPA"
    return jsonify(
        {
            "ok": True,
            "deleted": deleted,
            "missing": missing,
            "errors": errors,
            "pruned_cpa": pruned,
            "message": msg,
        }
    )


@app.get("/api/config/email")
def api_get_email_config():
    need = require_login()
    if need:
        return need
    return jsonify({"ok": True, "email": email_config_public()})


@app.post("/api/config/email")
def api_set_email_config():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        email = apply_email_config_from_ui(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "message": "邮箱设置已保存", "email": email})


@app.get("/api/job/status")
def api_job_status():
    need = require_login()
    if need:
        return need
    with _job_lock:
        job = dict(_job)
    return jsonify({"ok": True, "job": job, "logs": list(_logs), "cpa": cpa_stats(), "sub2": sub2_status()})


@app.get("/api/cpa/status")
def api_cpa_status():
    need = require_login()
    if need:
        return need
    return jsonify({"ok": True, "cpa": cpa_stats(), "sub2": sub2_status()})


@app.post("/api/cpa/backfill")
def api_cpa_backfill():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        limit = int(data.get("limit") or 200)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 1000))
    if not _CPA_CORE_OK:
        return jsonify({"ok": False, "error": f"core unavailable: {_CPA_CORE_ERR}"}), 500
    n = enqueue_missing_accounts(limit=limit)
    log_line(f"[CPA] 手动补转入队: {n}")
    return jsonify({"ok": True, "queued": n, "message": f"已入队 {n} 个待转换 SSO"})


@app.get("/api/sub2/status")
def api_sub2_status():
    need = require_login()
    if need:
        return need
    return jsonify({"ok": True, "sub2": sub2_status()})


@app.get("/api/sub2/groups")
def api_sub2_groups():
    need = require_login()
    if need:
        return need
    try:
        groups = sub2_list_groups()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "sub2": sub2_status()}), 502
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        try:
            gid = int(g.get("id") or 0)
        except Exception:
            gid = 0
        if gid <= 0:
            continue
        out.append(
            {
                "id": gid,
                "name": str(g.get("name") or f"group-{gid}"),
                "platform": str(g.get("platform") or ""),
                "description": str(g.get("description") or ""),
                "status": str(g.get("status") or ""),
                "is_exclusive": bool(g.get("is_exclusive")),
            }
        )
    out.sort(key=lambda x: (x.get("platform") or "", x.get("name") or "", x.get("id") or 0))
    st = sub2_status()
    return jsonify(
        {
            "ok": True,
            "groups": out,
            "target_group_id": st.get("target_group_id") or 0,
            "target_group_name": st.get("target_group_name") or "",
            "sub2": st,
        }
    )


@app.post("/api/sub2/groups")
def api_sub2_create_group():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name") or "").strip()
    platform = str(data.get("platform") or "grok").strip().lower() or "grok"
    description = str(data.get("description") or "").strip()
    select_after = bool(data.get("select", True))
    if not name:
        return jsonify({"ok": False, "error": "分组名不能为空"}), 400
    try:
        created = sub2_create_group(name=name, platform=platform, description=description)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    try:
        gid = int(created.get("id") or 0)
    except Exception:
        gid = 0
    gname = str(created.get("name") or name)
    gplat = str(created.get("platform") or platform)
    if select_after and gid > 0:
        set_target_group(gid, gname, gplat)
        log_line(f"[SUB2] 创建并选中分组 id={gid} name={gname} platform={gplat}")
    else:
        log_line(f"[SUB2] 创建分组 id={gid} name={gname} platform={gplat}")
    return jsonify(
        {
            "ok": True,
            "group": {
                "id": gid,
                "name": gname,
                "platform": gplat,
                "description": str(created.get("description") or description),
            },
            "sub2": sub2_status(),
            "message": f"已创建分组 {gname}" + (" 并设为导入目标" if select_after and gid > 0 else ""),
        }
    )


@app.post("/api/sub2/group/select")
def api_sub2_select_group():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        gid = int(data.get("group_id") or data.get("id") or 0)
    except Exception:
        gid = 0
    gname = str(data.get("group_name") or data.get("name") or "").strip()
    gplat = str(data.get("group_platform") or data.get("platform") or "").strip()
    if gid < 0:
        return jsonify({"ok": False, "error": "invalid group_id"}), 400
    if gid > 0 and not gname:
        try:
            for g in sub2_list_groups():
                if int(g.get("id") or 0) == gid:
                    gname = str(g.get("name") or gname)
                    gplat = str(g.get("platform") or gplat)
                    break
        except Exception:
            pass
    set_target_group(gid, gname, gplat)
    if gid > 0:
        log_line(f"[SUB2] 目标分组 -> id={gid} name={gname or '?'} platform={gplat or '?'}")
        msg = f"已选择分组: {gname or gid}"
    else:
        log_line("[SUB2] 已清除目标分组（导入后不强制绑定）")
        msg = "已清除目标分组"
    return jsonify({"ok": True, "message": msg, "sub2": sub2_status()})


def _normalize_browser_engine(value: str) -> str:
    eng = str(value or "").strip().lower()
    if eng in ("camoufox", "firefox", "headless", "cfox"):
        return "camoufox"
    return "chromium"


@app.get("/api/config/browser")
def api_get_browser_config():
    need = require_login()
    if need:
        return need
    cfg = load_config()
    return jsonify(
        {
            "ok": True,
            "browser_engine": _normalize_browser_engine(cfg.get("browser_engine") or "chromium"),
        }
    )


@app.post("/api/config/browser")
def api_set_browser_config():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    eng = _normalize_browser_engine(data.get("browser_engine") or "chromium")
    cfg = load_config()
    cfg["browser_engine"] = eng
    save_config(cfg)
    label = "Camoufox 无头" if eng == "camoufox" else "Chromium 有头"
    return jsonify(
        {
            "ok": True,
            "browser_engine": eng,
            "message": f"浏览器引擎已保存: {label}",
        }
    )


@app.post("/api/job/start")
def api_job_start():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        count = int(data.get("count") or 1)
    except Exception:
        count = 1
    if "browser_engine" in data:
        eng = _normalize_browser_engine(data.get("browser_engine"))
        cfg = load_config()
        cfg["browser_engine"] = eng
        save_config(cfg)
    ok, msg = start_job(count)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg})


@app.post("/api/job/stop")
def api_job_stop():
    need = require_login()
    if need:
        return need
    ok, msg = stop_job()
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg})


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "base_dir": str(BASE_DIR),
            "files": len(list_account_files()),
            "running": bool(_job.get("running")),
            "cpa": cpa_stats(),
        }
    )


# start background CPA worker when module loads (systemd imports/runs this file)
start_cpa_worker()
_init_sub2_group_state()
try:
    refresh_sub2_settings_from_config(force=True)
except Exception as _e:
    print(f"[SUB2] initial settings load: {_e}")


if __name__ == "__main__":
    print(f"Grok Register Panel -> http://0.0.0.0:{PORT}")
    print(f"CPA auto-convert dir -> {CPA_DIR} core={_CPA_CORE_OK}")
    print(
        f"Sub2 push={AUTO_SUB2_PUSH} mode={SUB2_IMPORT_MODE} "
        f"base={SUB2API_BASE_URL} group_id={get_target_group_id()} "
        f"creds={'api_key' if SUB2API_ADMIN_API_KEY else ('password' if SUB2API_ADMIN_EMAIL else 'MISSING')}"
    )
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
