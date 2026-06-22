@echo off

if /i "%1"=="start" goto start
if /i "%1"=="stop" goto stop
if /i "%1"=="status" goto status
if /i "%1"=="reload" goto reload

echo 用法: start_palu.bat start^|stop^|status^|reload
goto :eof

:start
tasklist /fi "imagename eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
if not errorlevel 1 echo 帕鲁已在运行 && goto :eof
start /b /min "" pythonw.exe palu_watcher.py
echo 帕鲁已后台启动
goto :eof

:stop
echo 停止帕鲁...
taskkill /f /im pythonw.exe >nul 2>&1
taskkill /f /im python.exe >nul 2>&1
echo 帕鲁已停止
goto :eof

:status
wmic process where "name='pythonw.exe' or name='python.exe'" get processid,commandline /format:csv 2>nul | findstr /i "palu_watcher app.py"
if errorlevel 1 echo 帕鲁未运行
goto :eof

:reload
powershell -Command "try{write-host (Invoke-RestMethod -Uri http://127.0.0.1:5000/api/reload -Method POST -TimeoutSec 5).answer}catch{write-host 失败: $($_.Exception.Message)}"
