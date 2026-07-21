@echo off
title ComfyUI Idle VRAM Watchdog
cd /d "%~dp0"
echo ComfyUI idle VRAM watchdog running...  (close this window to stop)
python comfy_idle_unload.py
pause
