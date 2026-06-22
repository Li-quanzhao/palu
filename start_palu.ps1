<#
帕鲁后台管理脚本（兼容 PowerShell 5）
用法：
  .\start_palu.ps1 start      后台启动（无窗口）
  .\start_palu.ps1 stop       停止
  .\start_palu.ps1 status     查看状态
  .\start_palu.ps1 reload     触发热重载
#>

param([string]$Action = "status")

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatcherLog = Join-Path $ScriptDir "palu_watcher.log"

function Write-Time($msg) {
    Write-Host ("[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg)
}

function Get-PaluProcess {
    $rs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    $out = @()
    foreach ($p in $rs) {
        if ($p.CommandLine -match "app.py|palu_watcher") {
            $out += [PSCustomObject]@{Id=$p.ProcessId; Cmd=$p.CommandLine}
        }
    }
    return $out
}

# ==================== start ====================
if ($Action -eq "start") {
    $procs = Get-PaluProcess
    if ($procs) {
        Write-Time "帕鲁已在运行中"
        exit 0
    }
    Write-Time "启动帕鲁热更新守护进程..."
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "pythonw.exe"
    $psi.Arguments = "`"" + (Join-Path $ScriptDir "palu_watcher.py") + "`""
    $psi.WorkingDirectory = $ScriptDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    [void][System.Diagnostics.Process]::Start($psi)
    Start-Sleep -Seconds 3
    $procs = Get-PaluProcess
    if ($procs) {
        Write-Time "帕鲁已后台启动成功"
        Write-Time "查看状态: .\start_palu.ps1 status"
        Write-Time "查看日志: $WatcherLog"
    } else {
        Write-Time "启动失败，查看日志: $WatcherLog"
    }
}

# ==================== stop ====================
elseif ($Action -eq "stop") {
    $procs = Get-PaluProcess
    if (-not $procs) {
        Write-Time "帕鲁未在运行"
        exit 0
    }
    foreach ($p in $procs) {
        Write-Time ("停止进程 PID=" + $p.Id)
        $proc = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
        if ($proc) {
            $proc.CloseMainWindow()
            Start-Sleep -Seconds 1
            if (-not $proc.HasExited) {
                Stop-Process -Id $p.Id -Force
            }
        }
    }
    Write-Time "帕鲁已停止"
}

# ==================== reload ====================
elseif ($Action -eq "reload") {
    Write-Time "通过 API 触发热重载..."
    try {
        $headers = @{}
        $key = [Environment]::GetEnvironmentVariable("PALU_API_KEY")
        if ($key) { $headers["X-API-Key"] = $key }
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/reload" -Method POST -Headers $headers -ContentType "application/json" -TimeoutSec 5
        Write-Time ("帕鲁响应: " + $resp.answer)
        Start-Sleep -Seconds 4
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/status" -Method GET -TimeoutSec 5
            Write-Time "热重载成功，帕鲁正常运行"
        } catch {
            Start-Sleep -Seconds 5
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/status" -Method GET -TimeoutSec 5
            Write-Time "热重载成功，帕鲁恢复正常"
        }
    } catch {
        Write-Time ("热重载失败: " + $_.Exception.Message)
    }
}

# ==================== status ====================
elseif ($Action -eq "status") {
    $procs = Get-PaluProcess
    $line = "=" * 50
    Write-Host ""
    Write-Host $line
    if (-not $procs) {
        Write-Host "  帕鲁状态: 未运行"
        Write-Host "  启动: .\start_palu.ps1 start"
    } else {
        Write-Host "  帕鲁状态: 运行中"
        foreach ($p in $procs) {
            $name = "python"
            if ($p.Cmd -match "palu_watcher") { $name = "watcher" }
            elseif ($p.Cmd -match "app.py") { $name = "app" }
            Write-Host ("    [" + $name + "] PID=" + $p.Id)
        }
        Write-Host "  日志: $WatcherLog"
        Write-Host $line
        try {
            $s = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/status" -Method GET -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($s) {
                Write-Host ("  总请求: " + $s.total_requests)
                Write-Host ("  缓存命中率: " + $s.cache_hit_rate)
                Write-Host ("  LLM调用: " + $s.llm_calls)
                Write-Host ("  预估费用: ¥" + $s.estimated_cost_yuan)
            }
        } catch {
            Write-Host "  API: 无法连接"
        }
    }
    Write-Host $line
    Write-Host ""
}

# ==================== 其他 ====================
else {
    Write-Host ""
    Write-Host "帕鲁后台管理脚本"
    Write-Host ("=" * 30)
    Write-Host "  start    后台启动"
    Write-Host "  stop     停止"
    Write-Host "  status   查看状态"
    Write-Host "  reload   热重载"
    Write-Host ("=" * 30)
    Write-Host ""
}
