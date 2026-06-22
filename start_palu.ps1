<#
帕鲁后台启动脚本
用法：
  .\start_palu.ps1 start       # 后台启动（无窗口）
  .\start_palu.ps1 stop        # 停止帕鲁
  .\start_palu.ps1 restart     # 重启
  .\start_palu.ps1 status      # 查看运行状态
  .\start_palu.ps1 logs        # 查看日志
  .\start_palu.ps1 reload      # 触发 API 热重载

说明：
  - start 用 pythonw.exe 无窗口运行，关了终端也不停
  - 日志写到 palu_watcher.log
#>

param([string]$Action = "status")

# 脚本所在目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatcherLog = Join-Path $ScriptDir "palu_watcher.log"

# 【参数可调】帕鲁进程名（用于查找进程）
$ProcessName = "python"
$WatcherName = "palu_watcher"

function Get-PaluProcess {
    <# 查找帕鲁进程（兼容 PowerShell 5，用 WMI 查 CommandLine）#>
    $allProcs = Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue
    $paluProcs = $allProcs | Where-Object {
        $_.CommandLine -match "app.py" -or $_.CommandLine -match "palu_watcher"
    }
    return $paluProcs | ForEach-Object {
        [PSCustomObject]@{
            Id = $_.ProcessId
            CommandLine = $_.CommandLine
            Name = $_.Name
        }
    }
}

function Write-Time {
    <# 带时间戳输出 #>
    $time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$time] $args"
}

# ============================================================
# start — 后台启动（无窗口）
# ============================================================
if ($Action -eq "start") {
    $existing = Get-PaluProcess
    if ($existing) {
        Write-Time "帕鲁已在运行中 (PID: $($existing.Id -join ', '))"
        Write-Time "如需重启请用: .\start_palu.ps1 restart"
        exit 0
    }

    Write-Time "启动帕鲁热更新守护进程..."
    
    # 【逻辑说明】pythonw.exe 启动无窗口进程，关闭终端也不影响运行
    # 日志重定向到 palu_watcher.log
    $watcherPath = Join-Path $ScriptDir "palu_watcher.py"
    $logFile = $WatcherLog
    
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "pythonw.exe"
    $psi.Arguments = "`"$watcherPath`""
    $psi.WorkingDirectory = $ScriptDir
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true  # 不创建窗口

    $p = [System.Diagnostics.Process]::Start($psi)
    
    # 等 3 秒确认启动成功
    Start-Sleep -Seconds 3
    
    $running = Get-PaluProcess
    if ($running) {
        Write-Time "帕鲁已后台启动 ✅"
        Write-Time "   PID: $($running.Id -join ', ')"
        Write-Time "   日志: $WatcherLog"
        Write-Time "   状态: .\start_palu.ps1 status"
        Write-Time "   停止: .\start_palu.ps1 stop"
    } else {
        Write-Time "启动失败 ❌ 查看日志: $WatcherLog"
        Get-Content $WatcherLog -Tail 5
    }
}

# ============================================================
# stop — 停止帕鲁（包括守护进程）
# ============================================================
elseif ($Action -eq "stop") {
    Write-Time "停止帕鲁..."

    # 先试优雅退出（发 POST 到 /api/reload — 让 watcher 不重启）
    # 但更直接的方式是 kill 所有相关进程
    $processes = Get-PaluProcess
    
    if (-not $processes) {
        Write-Time "帕鲁未在运行"
        exit 0
    }

    foreach ($p in $processes) {
        Write-Time "停止进程 PID=$($p.Id)..."
        $proc = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
        if ($proc) {
            $proc.CloseMainWindow()  # 先温柔关闭
            Start-Sleep -Seconds 1
            if (-not $proc.HasExited) {
                Stop-Process -Id $p.Id -Force  # 强制结束
                Write-Time "强制结束 PID=$($p.Id)"
            }
        }
    }
    
    Start-Sleep -Seconds 2
    $left = Get-PaluProcess
    if (-not $left) {
        Write-Time "帕鲁已停止 ✅"
    } else {
        Write-Time "部分进程未能停止: $($left.Id -join ', ')"
    }
}

# ============================================================
# restart — 重启
# ============================================================
elseif ($Action -eq "restart") {
    & $MyInvocation.MyCommand.Path "stop"
    Start-Sleep -Seconds 2
    & $MyInvocation.MyCommand.Path "start"
}

# ============================================================
# reload — 通过 API 触发热重载
# ============================================================
elseif ($Action -eq "reload") {
    Write-Time "通过 API 触发热重载..."
    try {
        # 【参数可调】帕鲁地址和 API Key
        $paluUrl = "http://127.0.0.1:5000/api/reload"
        $apiKey = $env:PALU_API_KEY
        
        $headers = @{}
        if ($apiKey) {
            $headers["X-API-Key"] = $apiKey
        }
        
        $resp = Invoke-RestMethod -Uri $paluUrl -Method POST -Headers $headers -ContentType "application/json"
        Write-Time "帕鲁响应: $($resp.answer)"
        
        # 等几秒让重启完成
        Start-Sleep -Seconds 3
        
        # 检查是否恢复
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/status" -Method GET -TimeoutSec 5
            Write-Time "重载完成，帕鲁正常运行 ✅"
        } catch {
            Write-Time "帕鲁正在重启中..."
            # 再等一会
            Start-Sleep -Seconds 5
            try {
                $status = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/status" -Method GET -TimeoutSec 5
                Write-Time "重载完成，帕鲁恢复正常 ✅"
            } catch {
                Write-Time "重载后帕鲁未恢复 ❌ 请检查日志"
            }
        }
    } catch {
        Write-Time "热重载失败 ❌"
        Write-Time "  原因: $_"
        Write-Time "  可能帕鲁未运行？用 .\start_palu.ps1 start 启动"
    }
}

# ============================================================
# status — 查看运行状态
# ============================================================
elseif ($Action -eq "status") {
    $processes = Get-PaluProcess
    if (-not $processes) {
        Write-Host ""
        Write-Host "═══════════════════════════════"
        Write-Host "  帕鲁当前状态: ❌ 未运行"
        Write-Host "  启动: .\start_palu.ps1 start"
        Write-Host "═══════════════════════════════"
        Write-Host ""
    } else {
        Write-Host ""
        Write-Host "═══════════════════════════════"
        Write-Host "  帕鲁当前状态: ✅ 运行中"
        foreach ($p in $processes) {
            $cmd = ($p.CommandLine -replace ".+pythonw?\.exe\s*", "").Substring(0, [Math]::Min(60, ($p.CommandLine -replace ".+pythonw?\.exe\s*", "").Length))
            Write-Host "  PID: $($p.Id) | $cmd"
        }
        Write-Host "  日志: $WatcherLog"
        Write-Host "═══════════════════════════════"
        Write-Host ""
        
        # 尝试调 status API
        try {
            $status = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/status" -Method GET -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($status) {
                Write-Host "  API 状态:"
                Write-Host "    总请求: $($status.total_requests)"
                Write-Host "    缓存命中率: $($status.cache_hit_rate)"
                Write-Host "    LLM 调用: $($status.llm_calls)"
                Write-Host "    预估费用: ¥$($status.estimated_cost_yuan)"
            }
        } catch {
            Write-Host "  API 状态: 无法连接（可能启动中）"
        }
    }
}

# ============================================================
# logs — 查看日志
# ============================================================
elseif ($Action -eq "logs") {
    if (Test-Path $WatcherLog) {
        Get-Content $WatcherLog -Tail 30
    } else {
        Write-Host "日志文件不存在: $WatcherLog"
    }
}

# ============================================================
# 默认 — 显示帮助
# ============================================================
else {
    Write-Host ""
    Write-Host "帕鲁后台管理脚本"
    Write-Host "═══════════════════════════════"
    Write-Host "用法: .\start_palu.ps1 <命令>"
    Write-Host ""
    Write-Host "  start    后台启动（无窗口，关终端不影响）"
    Write-Host "  stop     停止帕鲁"
    Write-Host "  restart  重启"
    Write-Host "  reload   通过 API 热重载（不重启 watcher）"
    Write-Host "  status   查看运行状态"
    Write-Host "  logs     查看最近 30 行日志"
    Write-Host "═══════════════════════════════"
    Write-Host ""
}
