"""Tell the owner something needs attention: a Windows toast, a line in ALERTS.log, and stdout.

stdout reaches the supervising Claude session when the supervisor or watchdog runs as its
background task; the toast reaches the owner at the desk; ALERTS.log keeps the history.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:ALERT_TITLE)) | Out-Null
$x.Item(1).AppendChild($t.CreateTextNode($env:ALERT_BODY)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(log_dir: str, title: str, body: str, toast: bool = True) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "ALERTS.log"), "a", encoding="utf-8") as f:
        f.write(f"{stamp} {title}\n{body}\n\n")
    print(f"ALERT {stamp} {title} :: {body}", flush=True)
    if toast and sys.platform == "win32" and not os.environ.get("SUPERVISOR_NO_TOAST"):
        env = dict(os.environ, ALERT_TITLE=title[:120], ALERT_BODY=body[:600])
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST],
                       env=env, capture_output=True, timeout=60)
