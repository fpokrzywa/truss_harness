# Starts the unattended build after a logon or reboot: the local cluster, then
# one supervisor and one watchdog. Registered as the "SETH build autostart"
# scheduled task (at logon). Does nothing for a part that is already running.
param([int]$Goal = 2, [int]$WatchMinutes = 50, [switch]$Detached)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$py = 'C:\Python313\python.exe'
$logs = Join-Path $repo '.devdata\supervisor\logs'
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs 'autostart.log'
function Write-Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -Append -Encoding utf8 $log }

# The task's own console can be closed at logon (it can show as a terminal window even with
# -WindowStyle Hidden); that ends this script with 0xC000013A before anything starts
# (2026-11-22 06:28). Relaunch under a headless console, which has no window, and return.
if (-not $Detached) {
    Start-Process -FilePath (Join-Path $env:WINDIR 'System32\conhost.exe') -WindowStyle Hidden -ArgumentList @(
        '--headless', 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`"", '-Goal', "$Goal", '-WatchMinutes', "$WatchMinutes", '-Detached')
    Write-Log 'autostart: relaunched headless'
    exit 0
}

function Test-Running($pattern) {
    [bool](Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $pattern })
}

Set-Location $repo
Write-Log "autostart: begin (goal $Goal)"

# Native stderr must not end the script: under 'Stop', Windows PowerShell 5.1 turns each
# stderr line piped through 2>&1 into a terminating error (2026-11-21: the task exited 1
# before logging why or starting anything).
function Invoke-DevDb($verb) {
    $ErrorActionPreference = 'Continue'
    & $py scripts/dev_db.py $verb 2>&1 | ForEach-Object { Write-Log "$_" }
    return $LASTEXITCODE
}

# The cluster may need many minutes of crash recovery after a hard stop; start waits it out.
$clusterUp = $false
foreach ($try in 1..3) {
    $code = Invoke-DevDb 'start'
    if ($code -eq 0) { $clusterUp = $true; break }
    Write-Log "cluster start failed (exit $code, try $try of 3)"
    Start-Sleep -Seconds 30
}
Invoke-DevDb 'status' | Out-Null

if (-not $clusterUp) {
    # No lead without a database. The watchdog below still starts and alerts that no lead runs.
    Write-Log 'cluster not running; not starting the supervisor'
} elseif (Test-Running 'supervise\.py') {
    Write-Log 'supervisor already running; not starting another'
} else {
    Start-Process -FilePath $py -ArgumentList '-I', 'scripts/supervisor/supervise.py', "$Goal" `
        -WorkingDirectory $repo -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'autostart-supervisor.out') `
        -RedirectStandardError (Join-Path $logs 'autostart-supervisor.err')
    Write-Log 'supervisor started'
}

if (Test-Running 'watchdog\.py') {
    Write-Log 'watchdog already running; not starting another'
} else {
    Start-Process -FilePath $py -ArgumentList '-I', 'scripts/supervisor/watchdog.py', '--minutes', "$WatchMinutes" `
        -WorkingDirectory $repo -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'autostart-watchdog.out') `
        -RedirectStandardError (Join-Path $logs 'autostart-watchdog.err')
    Write-Log 'watchdog started'
}

# The factory dashboard (http://127.0.0.1:8000/dashboard.html), localhost only. HTTP/1.1 keeps
# the browser's polling on a few kept-alive connections; HTTP/1.0 (the default) closed one per
# file fetch, and the TIME_WAIT sockets exhausted the ephemeral ports under test load (S-034).
if (Test-Running 'http\.server 8000') {
    Write-Log 'dashboard already running; not starting another'
} else {
    Start-Process -FilePath $py -ArgumentList '-I', '-m', 'http.server', '8000', '--bind', '127.0.0.1', '--protocol', 'HTTP/1.1' `
        -WorkingDirectory (Join-Path $repo 'factory') -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'dashboard.out') `
        -RedirectStandardError (Join-Path $logs 'dashboard.err')
    Write-Log 'dashboard started'
}
Write-Log 'autostart: done'
