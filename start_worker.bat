@echo off
setlocal
cd /d "%~dp0"
echo Start GrokPool headless worker
echo Keep Clash TUN / virtual NIC and Sub2API docker running.
echo.
call "%~dp0worker\start_worker.bat"
set ERR=%ERRORLEVEL%
echo.
echo exit=%ERR%
pause
exit /b %ERR%
