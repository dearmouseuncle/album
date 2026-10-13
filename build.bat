@echo off
cd /d %~dp0
echo Building ÀĞÏóÏà²á.exe ...
python -m PyInstaller --onefile --name ÀĞÏóÏà²á --icon elephant.ico --collect-all pillow_heif --hidden-import pillow_heif album.py
echo.
echo Done: dist\xc0ĞÏóÏà²á.exe
pause
