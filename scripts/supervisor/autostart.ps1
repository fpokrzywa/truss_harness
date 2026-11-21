# Starts the unattended build after a logon or reboot: the local cluster, then
# one supervisor and one watchdog. Registered as the "SETH build autostart"
# scheduled task (at logon). Does nothing for a part that is already running.
param([int]$Goal = 2, [int]$WatchMinutes = 30)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$py = 'C:\Python313\python.exe'
$logs = Join-Path $repo '.devdata\supervisor\logs'
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs 'autostart.log'
function Write-Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -Append -Encoding utf8 $log }

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
Write-Log 'autostart: done'
