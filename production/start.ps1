# 用法：.\start.ps1；独立验收可加 -BackendPort 8001 -FrontendPort 5174 -NoBrowser。
param([int]$BackendPort = 8000, [int]$FrontendPort = 5173, [switch]$NoBrowser, [switch]$SkipGpuTunnel)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$logs = Join-Path $root 'runtime'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$py = Join-Path $backend '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { throw '缺少后端环境：请按 使用说明.md 安装 backend/.venv。' }
$vite = Join-Path $frontend 'node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $vite)) { throw '缺少前端依赖：请在 production/frontend 执行 npm install。' }
$node = (Get-Command node -ErrorAction Stop).Source

if (-not $SkipGpuTunnel) {
    $workerServiceConfig = Join-Path $logs 'worker-service\config.json'
    if (Test-Path -LiteralPath $workerServiceConfig -PathType Leaf) {
        try { & (Join-Path $root 'scripts\worker-service.ps1') -Action Start }
        catch { Write-Warning "云端 GPU Worker 检查失败：$($_.Exception.Message)；本机工作台将继续启动。" }
    }
    $tunnelConfig = Join-Path $logs 'tunnel\config.json'
    if (Test-Path -LiteralPath $tunnelConfig -PathType Leaf) {
        try { & (Join-Path $root 'scripts\tunnel.ps1') -Action Start -ConfigPath $tunnelConfig }
        catch { Write-Warning "GPU 隧道启动失败：$($_.Exception.Message)；本机工作台将继续启动。" }
    } else {
        Write-Warning 'GPU 隧道尚未首次配置：请运行 production/scripts/setup_tunnel.py；文案与已有成片功能仍可使用。'
    }
}

function Test-Endpoint([string]$Url, [switch]$Backend) {
    try {
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing
        if ($Backend) { return (($response.Content | ConvertFrom-Json).ok -eq $true) }
        return ($response.StatusCode -eq 200)
    } catch { return $false }
}
function Wait-Endpoint([string]$Url, [switch]$Backend) {
    $deadline = (Get-Date).AddSeconds(25)
    do {
        if (Test-Endpoint $Url -Backend:$Backend) { return }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    throw "启动未通过健康检查：$Url；请查看 production/runtime 下对应日志。"
}
$backendUrl = "http://127.0.0.1:$BackendPort"
$frontendUrl = "http://127.0.0.1:$FrontendPort"
if (Test-Endpoint "$backendUrl/api/v1/health" -Backend) {
    Write-Host "复用已运行的后端：$backendUrl（代码更新后需重启后端进程）"
} else {
    if (Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue) {
        throw "端口 $BackendPort 被其他服务占用，请更换 -BackendPort。"
    }
    $process = Start-Process -FilePath $py -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$BackendPort") -WorkingDirectory $backend -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "backend-$BackendPort.log") -RedirectStandardError (Join-Path $logs "backend-$BackendPort.err.log")
    $process.Id | Set-Content (Join-Path $logs "backend-$BackendPort.pid")
    Wait-Endpoint "$backendUrl/api/v1/health" -Backend
}
try {
    & (Join-Path $root 'scripts\local-storyboard-worker.ps1') -PythonPath $py -BackendPath $backend -RuntimePath $logs
} catch {
    Write-Warning "本机分镜拼接 Worker 启动失败：$($_.Exception.Message)；工作台将继续启动，拼接任务暂不可执行。"
}
try {
    & (Join-Path $root 'scripts\local-storyboard-worker.ps1') -PythonPath $py -BackendPath $backend -RuntimePath $logs -WorkerModule 'app.short_video.subtitle_jobs'
} catch {
    Write-Warning "本机字幕 Worker 启动失败：$($_.Exception.Message)；请检查 production/runtime/subtitle-worker.err.log。"
}
if (Test-Endpoint $frontendUrl) {
    Write-Host "复用已运行的前端：$frontendUrl"
} else {
    if (Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue) {
        throw "端口 $FrontendPort 被其他服务占用，请更换 -FrontendPort。"
    }
    $previousTarget = $env:AVATAR_STUDIO_API_TARGET
    try {
        $env:AVATAR_STUDIO_API_TARGET = $backendUrl
        $process = Start-Process -FilePath $node -ArgumentList @('node_modules/vite/bin/vite.js','--port',"$FrontendPort",'--host','127.0.0.1','--strictPort') -WorkingDirectory $frontend -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs "frontend-$FrontendPort.log") -RedirectStandardError (Join-Path $logs "frontend-$FrontendPort.err.log")
        $process.Id | Set-Content (Join-Path $logs "frontend-$FrontendPort.pid")
        Wait-Endpoint $frontendUrl
    } finally { $env:AVATAR_STUDIO_API_TARGET = $previousTarget }
}
Write-Host "工作台已就绪：$frontendUrl"
Write-Host '从顶部“新建作品”开始，按创作向导完成五步。GPU 连接和模型错误会在对应步骤提示。'
if (-not $NoBrowser) { Start-Process $frontendUrl }
