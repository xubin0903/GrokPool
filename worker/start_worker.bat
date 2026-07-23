@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."
title GrokPool Worker

REM Prefer ASCII junction path (Windows cmd + Chinese paths are fragile)
set "REG_DIR=%CD%\register-win"
if not exist "%REG_DIR%\panel\app.py" (
  echo [ERROR] register panel not found: %REG_DIR%
  echo Create junction, e.g.:
  echo   mklink /J "%CD%\register-win" "D:\path\to\grok-register-win"
  pause
  exit /b 1
)

REM ---- defaults (override by setting env before running) ----
if not defined PANEL_HOST set PANEL_HOST=127.0.0.1
if not defined PANEL_PORT set PANEL_PORT=9000
if not defined PANEL_AUTH set PANEL_AUTH=0
if not defined GROK_BROWSER_ENGINE set GROK_BROWSER_ENGINE=camoufox
if not defined GROK_PROXY set GROK_PROXY=http://127.0.0.1:7895
if not defined AUTO_CPA set AUTO_CPA=1
if not defined CPA_DELAY set CPA_DELAY=1.0
if not defined AUTO_SUB2_PUSH set AUTO_SUB2_PUSH=1
if not defined SUB2_IMPORT_MODE set SUB2_IMPORT_MODE=cpa-data
if not defined SUB2API_BASE_URL set SUB2API_BASE_URL=http://127.0.0.1:18080
if not defined SUB2API_ADMIN_EMAIL set SUB2API_ADMIN_EMAIL=admin@sub2api.local
if not defined SUB2_SKIP_DEFAULT_GROUP_BIND set SUB2_SKIP_DEFAULT_GROUP_BIND=0
if not defined SUB2_PUSH_CONCURRENCY set SUB2_PUSH_CONCURRENCY=1
if not defined SUB2_PUSH_PRIORITY set SUB2_PUSH_PRIORITY=50
set GROKPOOL_NO_OPEN_BROWSER=1

if not defined SUB2API_ADMIN_PASSWORD (
  if exist "%CD%\deploy\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /c:"ADMIN_PASSWORD=" "%CD%\deploy\.env"`) do set "SUB2API_ADMIN_PASSWORD=%%B"
  )
)
if not defined SUB2API_ADMIN_PASSWORD (
  echo [ERROR] SUB2API_ADMIN_PASSWORD not set.
  echo Set env SUB2API_ADMIN_PASSWORD, or put ADMIN_PASSWORD=... in deploy\.env
  pause
  exit /b 1
)

echo ============================================================
echo GrokPool Worker
echo Register : %REG_DIR%
echo Panel    : http://%PANEL_HOST%:%PANEL_PORT%
echo Engine   : %GROK_BROWSER_ENGINE%
echo Proxy    : %GROK_PROXY%
echo Sub2API  : %SUB2API_BASE_URL%
echo Import   : %SUB2_IMPORT_MODE%  (cpa-data = local auth-code OAuth package)
echo ============================================================
echo Keep Clash/Mihomo ON (prefer TUN / virtual NIC).
echo Keep Sub2API docker ON.
echo Do not close this window while registering.
echo.

cd /d "%REG_DIR%"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] venv missing. Run once: start.bat inside register-win
  pause
  exit /b 1
)

echo [..] sync config browser_engine/proxy
".venv\Scripts\python.exe" -c "import json,os; p='config.json'; c=json.load(open(p,encoding='utf-8-sig')) if os.path.exists(p) else {}; c['browser_engine']=os.environ.get('GROK_BROWSER_ENGINE','camoufox'); c['proxy']=os.environ.get('GROK_PROXY','http://127.0.0.1:7895'); c['allow_proxy_fallback']=True; open(p,'w',encoding='utf-8').write(json.dumps(c,ensure_ascii=False,indent=2)+chr(10)); print('config ok', c.get('browser_engine'), c.get('proxy'))"
if errorlevel 1 (
  echo [ERROR] config update failed
  pause
  exit /b 1
)

echo [..] start panel worker
echo Open: http://%PANEL_HOST%:%PANEL_PORT%
echo Set email + count + optional Sub2 group, then Start.
echo Expect logs: [CPA] OK ... then [SUB2] PUSH OK mode=cpa-data ...
echo.

set "PYTHONPATH=%REG_DIR%\lib;%REG_DIR%"
".venv\Scripts\python.exe" -c "from panel.app import app, HOST, PORT, start_cpa_worker, log_line, AUTO_SUB2_PUSH, SUB2API_BASE_URL, SUB2_IMPORT_MODE; start_cpa_worker(); log_line('[GrokPool] AUTO_SUB2_PUSH=%%s mode=%%s -> %%s' %% (AUTO_SUB2_PUSH, SUB2_IMPORT_MODE, SUB2API_BASE_URL)); app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)"

echo.
echo Worker exited. code=%ERRORLEVEL%
pause
exit /b %ERRORLEVEL%
