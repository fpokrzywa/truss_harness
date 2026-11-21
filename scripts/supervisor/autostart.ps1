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

# The cluster may need many minutes of crash recovery after a hard stop; start waits it out.
& $py scripts/dev_db.py start 2>&1 | ForEach-Object { Write-Log "$_" }
& $py scripts/dev_db.py status 2>&1 | ForEach-Object { Write-Log "$_" }

if (Test-Running 'supervise\.py') {
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
