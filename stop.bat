@echo off
setlocal EnableExtensions

rem Stop the local frontend, backend, and the named Cloudflare Tunnel only.

set "ROOT=%~dp0"
set "FRONTEND=%ROOT%frontend"
set "STOP_FAILED="

call :stop_service 5000 BACKEND
if errorlevel 1 set "STOP_FAILED=1"
call :stop_service 5173 FRONTEND
if errorlevel 1 set "STOP_FAILED=1"

echo Stopping Cloudflare Tunnel...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'SilentlyContinue'; $service = Get-Service -Name 'CloudflaredJiaren' -ErrorAction SilentlyContinue; if ($service -and $service.Status -eq 'Running') { Stop-Service -Name 'CloudflaredJiaren' -Force -ErrorAction SilentlyContinue }; $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'cloudflared.exe' -and $_.CommandLine -match 'jiaren\.yml' }); foreach ($process in $processes) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }; if ($processes.Count -or ($service -and $service.Status -eq 'Running')) { Write-Host 'Cloudflare Tunnel stopped.' } else { Write-Host 'Cloudflare Tunnel is not running.' }"

if defined STOP_FAILED (
  echo [ERROR] One or more local services were not stopped.
  exit /b 1
)
echo Frontend, backend, and Cloudflare Tunnel have stopped.
exit /b 0

:stop_service
set "TARGET_PORT=%~1"
set "TARGET_KIND=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$port = [int]$env:TARGET_PORT; $kind = $env:TARGET_KIND; $root = $env:ROOT.TrimEnd('\'); $entry = Join-Path $env:FRONTEND 'serve-dist.mjs'; $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue); $targets = [System.Collections.Generic.HashSet[int]]::new(); $failed = $false; foreach ($listener in $listeners) { $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $listener.OwningProcess) -ErrorAction SilentlyContinue; $command = [string]$process.CommandLine; if ($kind -eq 'BACKEND') { $match = $process -and $process.Name -ieq 'python.exe' -and $command -match 'run_prod\.py' -and $command -like ('*' + $root + '*') } else { $match = $process -and $process.Name -ieq 'node.exe' -and (($command -like ('*' + $entry + '*')) -or $command -match 'serve-dist\.mjs') }; if (-not $match) { if ($kind -eq 'BACKEND' -and $process -and $process.Name -ieq 'python.exe') { Write-Host ('[WARN] PID ' + $listener.OwningProcess + ' on port ' + $port + ' may be the elevated ChanlunBackend scheduled task. Run restart.bat from an elevated terminal.') } else { Write-Host ('[WARN] Refusing to stop PID ' + $listener.OwningProcess + ' on port ' + $port) }; $failed = $true; continue }; if ([int]$listener.OwningProcess -gt 0) { [void]$targets.Add([int]$listener.OwningProcess) } }; if ($kind -eq 'BACKEND') { $matches = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -ieq 'python.exe' -and ([string]$_.CommandLine) -match 'run_prod\.py' -and ([string]$_.CommandLine) -like ('*' + $root + '*') }); $shellMatches = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -ieq 'powershell.exe' -and ([string]$_.CommandLine) -match 'run_prod\.py' -and ([string]$_.CommandLine) -like ('*' + $root + '*') }) } else { $matches = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -ieq 'node.exe' -and (([string]$_.CommandLine) -like ('*' + $entry + '*') -or ([string]$_.CommandLine) -match 'serve-dist\.mjs') -and ([string]$_.CommandLine) -like ('*' + $root + '*') }); $shellMatches = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -ieq 'powershell.exe' -and ([string]$_.CommandLine) -match 'serve-dist\.mjs' -and ([string]$_.CommandLine) -like ('*' + $root + '*') }) }; foreach ($process in @($matches) + @($shellMatches)) { if ($process -and [int]$process.ProcessId -gt 0) { [void]$targets.Add([int]$process.ProcessId) } }; foreach ($processId in @($targets)) { try { Stop-Process -Id $processId -Force -ErrorAction Stop } catch { Write-Host ('[WARN] Could not stop PID ' + $processId); $failed = $true } }; if ($targets.Count -gt 0) { Write-Host ('Stopped ' + $kind + ' process group (' + (($targets | Sort-Object) -join ', ') + ')') }; Start-Sleep -Milliseconds 500; if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { Write-Host ('[WARN] Port ' + $port + ' is still listening'); $failed = $true }; if ($failed) { exit 1 }; exit 0"
exit /b %ERRORLEVEL%
