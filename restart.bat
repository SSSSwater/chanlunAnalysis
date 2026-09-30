@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Restart the local frontend, backend, and the named Cloudflare Tunnel.
rem Steps: stop everything first, then start everything again.

set "ROOT=%~dp0"

rem The persistent backend is launched by an elevated scheduled task. A
rem non-elevated restart cannot replace that process and would leave old code
rem listening on port 5000.
fltmc >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Restart must be run from an elevated Administrator terminal.
  exit /b 1
)

if not exist "%ROOT%stop.bat" (
  echo [ERROR] Missing stop script: %ROOT%stop.bat
  exit /b 1
)
if not exist "%ROOT%start.bat" (
  echo [ERROR] Missing start script: %ROOT%start.bat
  exit /b 1
)

echo ============================================
echo Stopping services...
echo ============================================
call "%ROOT%stop.bat"
if errorlevel 1 (
  echo [ERROR] Stop phase failed; aborting restart to avoid a half-stopped state.
  exit /b 1
)

echo Waiting for ports to be released...
powershell -NoProfile -Command "Start-Sleep -Seconds 2"

echo.
echo ============================================
echo Starting services...
echo ============================================
call "%ROOT%start.bat"
if errorlevel 1 (
  echo [ERROR] Start phase failed.
  exit /b 1
)

echo.
echo Restart completed.
exit /b 0
