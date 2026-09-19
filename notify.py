"""
notify.py

Shows a Windows toast notification for the latest daily run, read from
logs/last_run_status.json (written by run_daily.py). Clicking the toast
opens the report.

This has to run in your own logged-in desktop session to be able to show
anything, the main PremarketDailyReports task runs "whether user is logged on
or not", a background session with no desktop. So it's run by a second,
separate scheduled task (PremarketNotifier, "only when user is logged on"):
run_daily.py starts it when a run finishes, and it also fires at every logon,
so a run that finished while nobody was logged in gets announced the next time
you sign in instead of being silently missed.

Each status is shown once, then marked acknowledged in the file.

Usage:
    python notify.py            show the pending notification, if any
    python notify.py --test     show a test toast without touching the status file
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "logs", "last_run_status.json")

# Windows PowerShell's own AppUserModelID, an always registered id that lets a
# toast be shown without registering an app of our own.
APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

CREATE_NO_WINDOW = 0x08000000


def xml_escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def show_toast(title, message, open_path=None):
    launch = ""
    if open_path and os.path.exists(open_path):
        launch = f' activationType="protocol" launch="{xml_escape(Path(open_path).resolve().as_uri())}"'

    toast_xml = (
        f"<toast{launch}><visual><binding template=\"ToastGeneric\">"
        f"<text>{xml_escape(title)}</text><text>{xml_escape(message)}</text>"
        f"</binding></visual><audio src=\"ms-winsoundevent:Notification.Default\"/></toast>"
    )

    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml('{toast_xml.replace(chr(39), chr(39) * 2)}')\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}').Show($toast)\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        print(f"toast failed: {result.stderr.strip()[:500]}")
        return False
    return True


def main():
    if "--test" in sys.argv:
        ok = show_toast("Premarket report", "Test notification, if you can see this it works.")
        sys.exit(0 if ok else 1)

    if not os.path.exists(STATUS_PATH):
        print("no status file, nothing to announce")
        return

    with open(STATUS_PATH, "r", encoding="utf-8") as f:
        status = json.load(f)

    if status.get("acknowledged"):
        print("latest run already announced")
        return

    if show_toast(status.get("title", "Premarket report"), status.get("message", ""), status.get("open_path")):
        status["acknowledged"] = True
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
        print("notification shown")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
