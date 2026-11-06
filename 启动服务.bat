@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%PORT%"=="" set PORT=8090
echo 启动中，端口 %PORT% …
python keepalive.py
pause
