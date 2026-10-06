# 管理当前用户的 GPU 隧道；首次密钥配置由 setup_tunnel.py 完成。
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Start', 'Status', 'Stop')][string]$Action = 'Status',
    [string]$ConfigPath = (Join-Path $PSScriptRoot '..\runtime\tunnel\config.json'),
    [ValidateRange(0, 60)][int]$WaitSeconds = 15
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$productionRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$managerPath = Join-Path $PSScriptRoot 'managed_tunnel.py'
$pythonPath = Join-Path $productionRoot 'backend\.venv\Scripts\python.exe'
$backgroundPython = Join-Path $productionRoot 'backend\.venv\Scripts\pythonw.exe'
$taskName = 'AvatarStudio-GpuTunnel'

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw '尚未配置 GPU 隧道：请先运行 production/scripts/setup_tunnel.py 完成首次安装。'
}
if (-not (Test-Path -LiteralPath $managerPath -PathType Leaf)) { throw "缺少隧道守护程序：$managerPath" }
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw '缺少 backend/.venv Python 环境，请先安装项目依赖。' }
if (-not (Test-Path -LiteralPath $backgroundPython -PathType Leaf)) { $backgroundPython = $pythonPath }
if ($managerPath.Contains('"') -or $ConfigPath.Contains('"')) { throw '隧道文件路径不能包含双引号。' }
$managerArguments = '"{0}" --config "{1}"' -f $managerPath, $ConfigPath

function Invoke-TunnelManager([string]$Mode) {
    $outputLines = @(& $pythonPath $managerPath --config $ConfigPath $Mode 2>&1)
    $commandExit = $LASTEXITCODE
    $raw = ($outputLines | ForEach-Object { $_.ToString() }) -join "`n"
    try { $payload = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "隧道管理器未返回有效 JSON（退出码 $commandExit），请检查配置与 runtime/tunnel 日志。" }
    if ($null -eq $payload) { throw '隧道管理器返回空状态，请检查 runtime/tunnel 日志。' }
    return [pscustomobject]@{ Data = $payload; ExitCode = $commandExit }
}

function Get-TunnelTask {
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { return $null }
    return Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}

function Assert-TunnelTask($Task) {
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1 -or $actions[0].Execute -ne $backgroundPython -or $actions[0].Arguments -ne $managerArguments) {
        throw "任务 $taskName 指向其他程序或配置；请检查后再操作，未修改该任务。"
    }
}

function Start-TunnelSupervisor {
    $task = $null
    try { $task = Get-TunnelTask }
    catch { Write-Warning '无法访问 Windows 任务计划；改用当前登录会话后台启动，未确认登录自启。' }
    if ($null -ne $task) {
        Assert-TunnelTask $task
        if ($task.State -eq 'Disabled') { Enable-ScheduledTask -TaskName $taskName | Out-Null }
        if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName $taskName }
    } else {
        Start-Process -FilePath $backgroundPython -ArgumentList $managerArguments -WorkingDirectory $productionRoot -WindowStyle Hidden | Out-Null
        Write-Warning '隧道已请求在后台运行；尚未安装登录自启任务，请执行 tunnel.ps1 -Action Install。'
    }
    # 守护程序的文件锁负责同时启动时的单实例互斥。
    if ($WaitSeconds -eq 0) { return }
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $lastHealth = $null
    do {
        $lastHealth = Invoke-TunnelManager '--check'
        if ($lastHealth.ExitCode -eq 0 -and $lastHealth.Data.state -eq 'ready') {
            Write-Host 'GPU Worker 已连接：四阶段健康检查通过。'
            return
        }
        if ((Get-Date) -ge $deadline) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    $state = if ($null -ne $lastHealth) { $lastHealth.Data.state } else { 'unknown' }
    Write-Warning "GPU Worker 尚未就绪（$state）。请用 tunnel.ps1 -Action Status 查看原因；网络断线由守护程序重连，Worker 未运行时仍需恢复远端服务。"
}

switch ($Action) {
    'Install' {
        $registered = $false
        try {
            if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) { throw '当前环境没有 ScheduledTasks 模块。' }
            $existingTask = Get-TunnelTask
            if ($null -ne $existingTask) { Assert-TunnelTask $existingTask }
            $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $taskAction = New-ScheduledTaskAction -Execute $backgroundPython -Argument $managerArguments -WorkingDirectory $productionRoot
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
            $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartInterval ([TimeSpan]::FromMinutes(1)) -RestartCount 3 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
            Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description 'Avatar Studio GPU SSH tunnel supervisor' -Force | Out-Null
            $registered = $true
        } catch {
            Write-Warning "登录自启任务安装失败：$($_.Exception.Message)"
        }
        if ($registered) { Write-Host "已安装当前用户登录自启任务：$taskName" }
        Start-TunnelSupervisor
    }
    'Start' { Start-TunnelSupervisor }
    'Status' {
        $supervisor = Invoke-TunnelManager '--status'
        $health = Invoke-TunnelManager '--check'
        [ordered]@{ supervisor = $supervisor.Data; health = $health.Data } | ConvertTo-Json -Depth 12
        exit $health.ExitCode
    }
    'Stop' {
        $task = Get-TunnelTask
        if ($null -ne $task) {
            Assert-TunnelTask $task
            Disable-ScheduledTask -TaskName $taskName | Out-Null
        }
        $stop = Invoke-TunnelManager '--stop'
        $stop.Data | ConvertTo-Json -Depth 12
        $deadline = (Get-Date).AddSeconds([Math]::Max(1, $WaitSeconds))
        do {
            $status = Invoke-TunnelManager '--status'
            if ($status.Data.state -eq 'stopped') {
                Write-Host '隧道守护程序已停止；再次 Start 会恢复登录自启任务。'
                return
            }
            Start-Sleep -Milliseconds 300
        } while ((Get-Date) -lt $deadline)
        Write-Warning '已发送停止请求，守护程序尚未确认退出；请稍后查看 Status。未强制终止其他 SSH 进程。'
    }
}
