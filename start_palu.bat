@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "ACTION=%1"
if "%ACTION%"=="" set "ACTION=help"

set "DIR=%~dp0"
set "LOG=%DIR%palu_watcher.log"

if /i "%ACTION%"=="start" (
    echo [%time:~0,8%] 启动帕鲁热更新守护进程...
    tasklist /fi "imagename eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
    if not errorlevel 1 (
        echo [%time:~0,8%] 帕鲁已在运行中
        exit /b 0
    )
    start /b /min pythonw.exe "%DIR%palu_watcher.py"
    timeout /t 3 /nobreak >nul
    tasklist /fi "imagename eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
    if errorlevel 1 (
        echo [%time:~0,8%] 启动失败，检查日志: %LOG%
    ) else (
        echo [%time:~0,8%] 帕鲁已后台启动成功
        echo [%time:~0,8%] 查看状态: start_palu.bat status
    )
    goto :eof
)

if /i "%ACTION%"=="stop" (
    echo [%time:~0,8%] 停止帕鲁...
    for /f "skip=1 tokens=2 delims=," %%a in ('wmic process where "name='python.exe' or name='pythonw.exe'" get processid,commandline /format:csv 2^>nul ^| findstr /i "app.py palu_watcher"') do (
        taskkill /f /pid %%a >nul 2>&1
        echo [%time:~0,8%] 已停止 PID=%%a
    )
    echo [%time:~0,8%] 帕鲁已停止
    goto :eof
)

if /i "%ACTION%"=="status" (
    echo ========================================
    set "FOUND=0"
    for /f "skip=1 tokens=2,* delims=," %%a in ('wmic process where "name='python.exe' or name='pythonw.exe'" get processid,commandline /format:csv 2^>nul ^| findstr /i "app.py palu_watcher"') do (
        echo   PID=%%a ^| %%b
        set "FOUND=1"
    )
    if "!FOUND!"=="0" (
        echo   帕鲁状态: 未运行
        echo   启动: start_palu.bat start
    )
    echo ========================================
    goto :eof
)

if /i "%ACTION%"=="reload" (
    echo [%time:~0,8%] 触发热重载...
    powershell -Command "try{ $r=Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/reload' -Method POST -TimeoutSec 5; Write-Host ('响应: '+$r.answer) }catch{ Write-Host ('失败: '+$_.Exception.Message) }"
    goto :eof
)

:help
echo.
echo 帕鲁管理脚本
echo ========================
echo   start    后台启动
echo   stop     停止
echo   status   查看状态
echo   reload   热重载
echo ========================
echo.
