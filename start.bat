@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Start the local frontend, backend, and the named Cloudflare Tunnel.
rem Public URL: https://jiaren.aqhxx.top

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "FRONTEND=%ROOT%frontend"
set "PYTHON=%ROOT%venv\Scripts\python.exe"
rem Keep the long-lived API process on the project's stable environment.
rem CUDA/model-training runtimes are launched separately when needed; they are
rem not used by the production API because native extensions have crashed it.
set "PUBLIC_URL=https://jiaren.aqhxx.top"
set "CLOUDFLARED_CONFIG=%USERPROFILE%\.cloudflared\jiaren.yml"
set "CLOUDFLARED=%USERPROFILE%\.cloudflared\cloudflared.exe"
set "TUNNEL_OUT=%ROOT%cloudflared-jiaren.out.log"
set "TUNNEL_ERR=%ROOT%cloudflared-jiaren.err.log"

if not exist "%PYTHON%" (
  echo [ERROR] Missing Python virtual environment: %PYTHON%
  exit /b 1
)
where node.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Node.js is not available on PATH.
  exit /b 1
)
if not exist "%FRONTEND%\node_modules" (
  echo [ERROR] Missing frontend dependencies: %FRONTEND%\node_modules
  exit /b 1
)
if not exist "%CLOUDFLARED_CONFIG%" (
  echo [ERROR] Missing Cloudflare Tunnel config: %CLOUDFLARED_CONFIG%
  exit /b 1
)
if not exist "%CLOUDFLARED%" (
  set "CLOUDFLARED="
  for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$p = Get-Command cloudflared.exe -ErrorAction SilentlyContinue; if ($p) { $p.Source }"`) do if not defined CLOUDFLARED set "CLOUDFLARED=%%I"
)
if not defined CLOUDFLARED (
  echo [ERROR] cloudflared.exe was not found. Install it or add it to PATH.
  exit /b 1
)

echo Building frontend...
pushd "%FRONTEND%"
call npm run build
set "BUILD_EXIT=!ERRORLEVEL!"
popd
if not "!BUILD_EXIT!"=="0" (
  echo [ERROR] Frontend build failed.
  exit /b !BUILD_EXIT!
)

call :ensure_backend
if errorlevel 1 exit /b 1
call :ensure_frontend
if errorlevel 1 exit /b 1
call :ensure_tunnel
if errorlevel 1 exit /b 1

echo.
echo Public site: %PUBLIC_URL%
echo Backend:     http://127.0.0.1:5000
echo Frontend:    http://127.0.0.1:5173
start "" "%PUBLIC_URL%"
exit /b 0

:ensure_backend
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo Backend is already running on port 5000.
) else (
  echo Starting backend on port 5000...
  start "Chanlun Backend" powershell -NoProfile -ExecutionPolicy Bypass -NoExit -Command "Set-Location -LiteralPath '%BACKEND%'; & '%PYTHON%' 'run_prod.py'"
  call :wait_for_port 5000
  if errorlevel 1 (
    echo [ERROR] Backend did not start on port 5000.
    exit /b 1
  )
)
call :wait_for_endpoint http://127.0.0.1:5000/api/health
if errorlevel 1 (
  echo [ERROR] Backend health check failed.
  exit /b 1
)
call :verify_backend_copy_trading
if errorlevel 1 exit /b 1
exit /b 0

:ensure_frontend
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo Frontend is already running on port 5173.
) else (
  echo Starting frontend on port 5173...
  start "Chanlun Frontend" powershell -NoProfile -ExecutionPolicy Bypass -NoExit -Command "Set-Location -LiteralPath '%FRONTEND%'; & 'node' 'serve-dist.mjs'"
  call :wait_for_port 5173
  if errorlevel 1 (
    echo [ERROR] Frontend did not start on port 5173.
    exit /b 1
  )
)
call :wait_for_endpoint http://127.0.0.1:5173/health
if errorlevel 1 (
  echo [ERROR] Frontend health check failed.
  exit /b 1
)
exit /b 0

:ensure_tunnel
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = Get-Service -Name 'CloudflaredJiaren' -ErrorAction SilentlyContinue; if ($s -and $s.Status -eq 'Running') { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo Cloudflare Tunnel service is running.
  exit /b 0
)
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'cloudflared.exe' -and $_.CommandLine -match 'jiaren\.yml' }; if ($p) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo Cloudflare Tunnel process is already running.
  exit /b 0
)
echo Starting Cloudflare Tunnel...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$out = '%TUNNEL_OUT%'; $err = '%TUNNEL_ERR%'; Start-Process -FilePath '%CLOUDFLARED%' -ArgumentList @('tunnel', '--no-autoupdate', '--protocol', 'quic', '--edge-ip-version', '4', '--config', '%CLOUDFLARED_CONFIG%', 'run', 'jiaren') -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden"
if errorlevel 1 (
  echo [ERROR] Could not start cloudflared. See %TUNNEL_ERR%
  exit /b 1
)
call :wait_for_tunnel
if errorlevel 1 (
  echo [ERROR] Cloudflare Tunnel did not remain running. See %TUNNEL_ERR%
  exit /b 1
)
exit /b 0

:wait_for_port
set "WAIT_PORT=%~1"
for /l %%N in (1,1,30) do (
  powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %WAIT_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
  if not errorlevel 1 exit /b 0
  powershell -NoProfile -Command "Start-Sleep -Seconds 1"
)
exit /b 1

:wait_for_endpoint
set "WAIT_URL=%~1"
for /l %%N in (1,1,20) do (
  powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '%WAIT_URL%' -UseBasicParsing -TimeoutSec 2; if ([int]$r.StatusCode -ge 200 -and [int]$r.StatusCode -lt 500) { exit 0 } } catch {}; exit 1" >nul 2>&1
  if not errorlevel 1 exit /b 0
  powershell -NoProfile -Command "Start-Sleep -Milliseconds 500"
)
exit /b 1

:verify_backend_copy_trading
rem A listening port and /api/health alone cannot prove the current backend
rem code is running. Refuse to reuse an older process without Copy Trading.
powershell -NoProfile -Command "try { $snapshot = Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/binance/snapshot?market=FUTURES' -TimeoutSec 8; if ($snapshot.PSObject.Properties.Match('copyTrading').Count -eq 0) { Write-Host '[ERROR] The backend on port 5000 is an older process without Copy Trading. Run restart.bat from an elevated terminal to replace it.'; exit 1 }; exit 0 } catch { Write-Host ('[ERROR] Backend capability check failed: ' + $_.Exception.Message); exit 1 }"
exit /b %ERRORLEVEL%

:wait_for_tunnel
for /l %%N in (1,1,20) do (
  powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'cloudflared.exe' -and $_.CommandLine -match 'jiaren\.yml' }; if ($p) { exit 0 } else { exit 1 }" >nul 2>&1
  if not errorlevel 1 exit /b 0
  powershell -NoProfile -Command "Start-Sleep -Milliseconds 500"
)
exit /b 1
