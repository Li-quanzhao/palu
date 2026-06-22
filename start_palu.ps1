<#
帕鲁后台管理脚本
用法: .\start_palu.ps1 start|stop|status|reload
#>
param([string]$Action = 'status')
$d = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $d 'palu_watcher.log'
function T($m) { Write-Host ('[{0:HH:mm:ss}] {1}' -f (Get-Date),$m) }
function P {
    $r = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    $o = @(); foreach($p in $r){ if($p.CommandLine -match 'app.py|palu_watcher'){ $o+=[PSCustomObject]@{Id=$p.ProcessId;C=$p.CommandLine} } }
    $o
}
if($Action -eq 'start'){
    $p = P
    if($p){ T '帕鲁已在运行中'; exit 0 }
    T '启动帕鲁热更新守护进程...'
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'pythonw.exe'
    $psi.Arguments = '"' + (Join-Path $d 'palu_watcher.py') + '"'
    $psi.WorkingDirectory = $d; $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    [void][System.Diagnostics.Process]::Start($psi)
    Start-Sleep -Seconds 3
    if(P){ T '帕鲁已后台启动成功'; T ('日志: '+$log) }else{ T ('启动失败, 检查日志: '+$log) }
}
elseif($Action -eq 'stop'){
    $p = P
    if(-not $p){ T '帕鲁未在运行'; exit 0 }
    foreach($x in $p){
        T ('停止进程 PID='+$x.Id)
        $g = Get-Process -Id $x.Id -ErrorAction SilentlyContinue
        if($g){ $g.CloseMainWindow(); Start-Sleep 1; if(-not $g.HasExited){ Stop-Process -Id $x.Id -Force } }
    }
    T '帕鲁已停止'
}
elseif($Action -eq 'reload'){
    T '触发热重载...'
    try{
        $h = @{}; $k = [Environment]::GetEnvironmentVariable('PALU_API_KEY')
        if($k){ $h['X-API-Key'] = $k }
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/reload' -Method POST -Headers $h -ContentType 'application/json' -TimeoutSec 5
        T ('响应: '+$r.answer)
        Start-Sleep 4
        try{ $s = Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/status' -Method GET -TimeoutSec 5; T '热重载成功' }
        catch{ Start-Sleep 5; $s = Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/status' -Method GET -TimeoutSec 5; T '热重载成功' }
    }catch{ T ('失败: '+$_.Exception.Message) }
}
elseif($Action -eq 'status'){
    $p = P; $bar = ('='*40)
    Write-Host ''; Write-Host $bar
    if(-not $p){ Write-Host '  帕鲁状态: 未运行'; Write-Host '  启动: .\start_palu.ps1 start' }
    else{
        Write-Host '  帕鲁状态: 运行中'
        foreach($x in $p){
            $n = 'python'; if($x.C -match 'palu_watcher'){ $n='watcher' } elseif($x.C -match 'app.py'){ $n='app' }
            Write-Host ('    ['+$n+'] PID='+$x.Id)
        }
        Write-Host ('  日志: '+$log); Write-Host $bar
        try{
            $s = Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/status' -Method GET -TimeoutSec 3 -ErrorAction SilentlyContinue
            if($s){ Write-Host ('  总请求: '+$s.total_requests); Write-Host ('  缓存命中率: '+$s.cache_hit_rate); Write-Host ('  LLM调用: '+$s.llm_calls); Write-Host ('  预估费用: '+$s.estimated_cost_yuan) }
        }catch{ Write-Host '  API: 无法连接' }
    }
    Write-Host $bar; Write-Host ''
}
else{
    Write-Host ''; Write-Host '帕鲁管理脚本'; Write-Host ('='*20)
    Write-Host '  start    后台启动'; Write-Host '  stop     停止'
    Write-Host '  status   查看状态'; Write-Host '  reload   热重载'
    Write-Host ('='*20); Write-Host ''
}
