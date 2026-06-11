@echo off
setlocal

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "FRONTEND=%ROOT%frontend"
set "PYTHON=%ROOT%venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo [ERROR] Missing virtual environment: %ROOT%venv
  echo Please create it and install backend requirements first.
  pause
  exit /b 1
)

if not exist "%FRONTEND%\node_modules" (
  echo [ERROR] Missing frontend dependencies: %FRONTEND%\node_modules
  echo Please run npm install in the frontend directory first.
  pause
  exit /b 1
)

echo Starting Chanlun Analysis...
echo Backend:  http://127.0.0.1:5000
echo Frontend: http://127.0.0.1:5173

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  start "Chanlun Backend" powershell -NoExit -ExecutionPolicy Bypass -Command "Set-Location -LiteralPath '%BACKEND%'; & '%PYTHON%' app.py"
) else (
  echo Backend is already running on port 5000.
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  start "Chanlun Frontend" powershell -NoExit -ExecutionPolicy Bypass -Command "Set-Location -LiteralPath '%FRONTEND%'; npm run dev"
) else (
  echo Frontend is already running on port 5173.
)

timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5173"

endlocal
