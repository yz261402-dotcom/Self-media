# 首次安装保留交互式 SSH；日常启动只调用受限的 ensure 控制命令。
[CmdletBinding()]
param([ValidateSet('Install', 'Start', 'Status')][string]$Action = 'Status')
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$productionRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $productionRoot 'backend\.venv\Scripts\python.exe'
$managerPath = Join-Path $PSScriptRoot 'manage_worker.py'

function Assert-WorkerManagerFiles {
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw '缺少 backend/.venv Python 环境，请先安装项目依赖。' }
    if (-not (Test-Path -LiteralPath $managerPath -PathType Leaf)) { throw '缺少 production/scripts/manage_worker.py。' }
}

switch ($Action) {
    'Install' {
        Assert-WorkerManagerFiles
        # 不捕获或重定向交互输入，密码仍由本机 OpenSSH 读取。
        & $pythonPath $managerPath install
        exit $LASTEXITCODE
    }
    'Status' {
        Assert-WorkerManagerFiles
        & $pythonPath $managerPath status
        exit $LASTEXITCODE
    }
    'Start' {
        try {
            Assert-WorkerManagerFiles
            $lines = @(& $pythonPath $managerPath ensure 2>&1)
            $commandExit = $LASTEXITCODE
            $raw = ($lines | ForEach-Object { $_.ToString() }) -join "`n"
            try { $result = $raw | ConvertFrom-Json -ErrorAction Stop }
            catch { throw '管理器未返回有效状态 JSON，请执行 worker-service.ps1 -Action Status 检查。' }
            if ($commandExit -eq 0 -and $result.state -eq 'ready') {
                Write-Host '云端 GPU Worker 已就绪。'
            } else {
                $state = if ($result.state) { $result.state } else { 'unknown' }
                Write-Warning "云端 GPU Worker 尚未就绪（$state）：$($result.detail)；本机工作台将继续启动。可用 worker-service.ps1 -Action Status 查看状态。"
            }
        } catch {
            Write-Warning "云端 GPU Worker 检查失败：$($_.Exception.Message)；本机工作台将继续启动。"
        }
    }
}
