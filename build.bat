@echo off
cd /d %~dp0
echo Building Album.exe ...
pip install pyinstaller -q
pyinstaller --onefile --name Album --collect-all pillow_heif --hidden-import pillow_heif album.py
echo.
echo Done: dist\Album.exe
pause
