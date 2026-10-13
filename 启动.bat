@echo off
cd /d %~dp0
title Album
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :18899 ^| findstr LISTENING') do taskkill /f /pid %%a >/dev/null 2>&1
start "" http://127.0.0.1:18899/
python -B album.py
pause
