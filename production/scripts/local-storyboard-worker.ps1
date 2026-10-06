param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$BackendPath,
    [Parameter(Mandatory = $true)][string]$RuntimePath,
    [ValidateSet('app.short_video.worker', 'app.short_video.subtitle_jobs')][string]$WorkerModule = 'app.short_video.worker'
)
$ErrorActionPreference = 'Stop'
$workerName = if ($WorkerModule -eq 'app.short_video.subtitle_jobs') { 'subtitle' } else { 'storyboard' }
$workerModulePattern = [regex]::Escape($WorkerModule)

function Test-StoryboardWorker($ProcessInfo) {
    if ($null -eq $ProcessInfo) { return $false }
    $command = [string]$ProcessInfo.CommandLine
    # The venv python.exe on Windows may be a launcher: its child has a
    # different ExecutablePath, while CommandLine still starts with the
    # requested venv path. Accept both representations.
    $pathMatches = [string]::Equals([string]$ProcessInfo.ExecutablePath, $PythonPath,
        [System.StringComparison]::OrdinalIgnoreCase) -or
        $command.StartsWith('"' + $PythonPath + '"', [System.StringComparison]::OrdinalIgnoreCase)
    if (-not $pathMatches) { return $false }
    return ($command -match ('(?<!\S)-m\s+' + $workerModulePattern + '(?=\s|$)') -and
        $command -match '(?<!\S)--loop(?=\s|$)')
}

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "Python not found: $PythonPath" }
New-Item -ItemType Directory -Path $RuntimePath -Force | Out-Null
$pidPath = Join-Path $RuntimePath "$workerName-worker.pid"
$bytes = [System.Text.Encoding]::UTF8.GetBytes($BackendPath.ToLowerInvariant() + ':' + $WorkerModule)
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $key = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').Substring(0, 16) }
finally { $sha.Dispose() }
$mutex = [System.Threading.Mutex]::new($false, "Local\WanStoryboardWorker_$key")
$locked = $false
try {
    try { $locked = $mutex.WaitOne(10000) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Timed out waiting for local storyboard Worker startup lock.' }

    $previousId = 0
    if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
        $raw = (Get-Content -LiteralPath $pidPath -Raw).Trim()
        if ([int]::TryParse($raw, [ref]$previousId) -and $previousId -gt 0) {
            $previous = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $previousId" -OperationTimeoutSec 5
            if (Test-StoryboardWorker $previous) {
                Write-Host "Reusing local storyboard Worker PID $previousId"
                return
            }
        }
    }

    $candidates = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -OperationTimeoutSec 5)
    foreach ($candidate in $candidates) {
        if (Test-StoryboardWorker $candidate) {
            [int]$candidate.ProcessId | Set-Content -LiteralPath $pidPath -Encoding ascii
            Write-Host "Reusing local storyboard Worker PID $($candidate.ProcessId)"
            return
        }
    }

    $worker = Start-Process -FilePath $PythonPath -ArgumentList @('-m', $WorkerModule, '--loop') `
        -WorkingDirectory $BackendPath -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $RuntimePath "$workerName-worker.log") `
        -RedirectStandardError (Join-Path $RuntimePath "$workerName-worker.err.log")
    # Python on Windows can expose the venv launcher and its child process a little
    # after Start-Process returns. Give the process table a bounded window to settle;
    # a single 300ms probe falsely reported a live Worker as exited.
    $started = $null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $started = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $($worker.Id)" -OperationTimeoutSec 5
        if (-not (Test-StoryboardWorker $started)) {
            $started = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -OperationTimeoutSec 5 |
                Where-Object { Test-StoryboardWorker $_ }) | Select-Object -First 1
        }
        if (Test-StoryboardWorker $started) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-StoryboardWorker $started)) {
        throw 'Local storyboard Worker exited or its command line could not be verified; see storyboard-worker.err.log.'
    }
    [int]$worker.Id | Set-Content -LiteralPath $pidPath -Encoding ascii
    Write-Host "Started local storyboard Worker PID $($worker.Id)"
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
