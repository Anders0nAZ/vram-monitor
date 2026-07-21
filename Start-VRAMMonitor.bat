@echo off
title VRAM Monitor
cd /d "%~dp0"

rem --- open the dashboard in a tight Chrome app window, pinned always-on-top ---
start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0Open-Dashboard.ps1"

rem --- start a server only if one isn't already running (e.g. hidden autostart) ---
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 11435 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo Server already running - opened dashboard window.
  timeout /t 3 >nul
) else (
  echo Starting VRAM Monitor server...  (close this window to stop, or use the Quit button)
  python vram_monitor.py
  pause
)
