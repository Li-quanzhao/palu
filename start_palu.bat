@echo off
chcp 65001 >nul

if /i "%1"=="start" goto start
if /i "%1"=="stop" goto stop
if /i "%1"=="status" goto status
if /i "%1"=="reload" goto reload
goto help

:start
echo [%time:~0,8%] 启动帕鲁...
tasklist /fi "imagename eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
if not errorlevel 1 echo 帕鲁已在运行中 && goto :eof
start /b /min "" pythonw.exe "%~dp0palu_watcher.py"
timeout /t 3 /nobreak >nul
tasklist /fi "imagename eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
if errorlevel 1 (echo 启动失败) else (echo 帕鲁已后台启动成功)
goto :eof

:stop
echo [%time:~0,8%] 停止帕鲁...
for /f "skip=1 tokens=2 delims=," %%a in ('wmic process where "name='python.exe' or name='pythonw.exe'" get processid^,commandline /format:csv 2^>nul ^| findstr /i "app.py palu_watcher"') do taskkill /f /pid %%a >nul 2>&1
echo 帕鲁已停止
goto :eof

:status
echo ========================================
wmic process where "name='python.exe' or name='pythonw.exe'" get processid,commandline /format:csv 2>nul | findstr /i "app.py palu_watcher"
if errorlevel 1 echo   帕鲁未运行
echo ========================================
goto :eof

:reload
echo [%time:~0,8%] 热重载...
powershell -Command "try{ Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/reload' -Method POST -TimeoutSec 5 | ForEach-Object { Write-Host ('响应: '+$_.answer) } }catch{ Write-Host ('失败: '+$_.Exception.Message) }"
goto :eof

:help
echo 用法: start_palu.bat start^|stop^|status^|reload
