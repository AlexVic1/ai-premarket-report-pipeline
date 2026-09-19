"""
run_daily.py

The automated morning pipeline: refresh premarket data, run the two mechanical
scans (Stage 2 Rider, FinViz Sector Scan), run the Claude-only analyst + merge
pass for the main AI Premarket Report (claude_analyst.py, no Codex), and
combine everything into a single HTML page plus a PDF of it, saved locally under
reports/, then emails the report to Yoel (ykalifa@gmail.com, see
EMAIL_RECIPIENT) via deliver.py, and finally records how the run went in
logs/last_run_status.json and starts the PremarketNotifier task, which shows
a Windows toast in the logged-in user's own session (see notify.py, this
task's own background session can't show one).

Test switches: --force ignores the weekend gate, --no-email skips the email.

Weekdays only. Windows Task Scheduler fired this on weekends too (its trigger
isn't restricted to weekdays), so this script gates on the day of week itself
and exits immediately on Saturday/Sunday, regardless of what the scheduler is
configured to do.

Also waits out a dead internet connection instead of just failing. If there's
no connection at the scheduled time, it checks every 5 minutes and runs the
full pipeline (with fresh, live data at whatever time the connection actually
comes back) as soon as one shows up, up to a cutoff later the same day, past
which it gives up until tomorrow's run rather than firing into the evening.

Task Scheduler is set to run whether the user is logged on or not, so a
locked screen or no active session doesn't block this either, only the
machine being fully asleep or powered off does, since nothing can wake it
from here.

Everything this script and the scripts it calls print goes to
logs/run_daily_<date>.log as well as stdout. Task Scheduler never captures
stdout anywhere, so without this, an unattended run failing partway through
(the render step, the analyst pass, whatever) leaves no trace of why. Check
that log first when something looks wrong with an automated run.

The AI Premarket Report step is optional and self-skipping: if the Claude
Code CLI isn't installed or isn't logged in, claude_analyst.py prints a skip
message and exits cleanly, and this script just sends the two mechanical
reports instead. The two-brain Claude + Codex merge (prompt_codex.md, Codex
CLI) is NOT part of this, it's still a manual, on-demand workflow, a live
Codex pass needs its own CLI session and isn't something this script drives.

Every day's REPORT.md, STAGE2_RIDER_REPORT.md, FINVIZ_SECTOR_SCAN_REPORT.md,
and packet.json get copied into weekly_archive/<week>/<date>/ once that day's
reports are ready (see weekly_summary.py), so there's a local record to look
back on. On Fridays, after the normal daily save, this script also runs
weekly_summary.py against that week's archived reports and saves the result
as its own HTML page, same as the daily one.

Usage:
    python run_daily.py
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from render_report import output_prefix
from weekly_summary import archive_day, week_folder_name

# Windows' default console encoding (cp1252) can't represent a lot of
# Unicode, box-drawing characters in a tool's error output being a real
# example that has crashed this exact print() before. Reconfigure to UTF-8
# with a safe fallback so a weird character in some downstream error message
# never takes down the whole run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ET = ZoneInfo("America/New_York")
REPORT_MD_PATH = os.path.join(HERE, "REPORT.md")
LOG_DIR = os.path.join(HERE, "logs")
STATUS_PATH = os.path.join(LOG_DIR, "last_run_status.json")
NOTIFIER_TASK = "PremarketNotifier"
EMAIL_RECIPIENT = "ykalifa@gmail.com"
MIN_PDF_BYTES = 5000

CONNECTIVITY_CHECK_URL = "https://www.google.com"
CONNECTIVITY_RETRY_SECONDS = 300  # 5 minutes
CONNECTIVITY_CUTOFF_HOUR_ET = 20  # stop waiting at 8pm ET, try again tomorrow

MECHANICAL_REPORTS = [
    ("stage2_scan.py", "STAGE2_RIDER_REPORT.md"),
    ("finviz_sector_scan.py", "FINVIZ_SECTOR_SCAN_REPORT.md"),
]


def has_internet():
    try:
        requests.head(CONNECTIVITY_CHECK_URL, timeout=10)
        return True
    except Exception:
        return False


def wait_for_internet(log):
    if has_internet():
        return True

    log("No internet connection right now, will check every 5 minutes and run as soon as one's back")
    while True:
        now = datetime.now(ET)
        if now.hour >= CONNECTIVITY_CUTOFF_HOUR_ET:
            log(f"Still no internet as of {now.strftime('%H:%M')} ET, giving up for today, will try again tomorrow")
            return False
        time.sleep(CONNECTIVITY_RETRY_SECONDS)
        if has_internet():
            log(f"Internet connection is back as of {datetime.now(ET).strftime('%H:%M')} ET, continuing")
            return True


def make_logger(log_file):
    def log(msg=""):
        print(msg)
        log_file.write(str(msg) + "\n")
        log_file.flush()
    return log


def make_runner(log_file, log):
    # Force every child script to run in UTF-8 mode, matching the parent's
    # own reconfigure above, so a stray Unicode character in some tool's
    # output (Playwright's install-hint box, an em-dash in a headline,
    # whatever) can't crash a child's print() the way it crashed deliver.py
    # today. encoding/errors here control how THIS process decodes what the
    # child wrote, PYTHONUTF8/PYTHONIOENCODING control how the child itself
    # encodes it, both ends need to agree.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    def run(cmd, label):
        log(f"=== {label} ===")
        result = subprocess.run(
            cmd,
            cwd=HERE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
        )
        run.last_output = result.stdout or ""
        if result.stdout:
            log(result.stdout.rstrip("\n"))
        if result.stderr:
            log(result.stderr.rstrip("\n"))
        if result.returncode != 0:
            log(f"!!! {label} failed, exit code {result.returncode}")
            return False
        return True

    run.last_output = ""
    return run


def finish(log, status, title, message, open_path=None):
    """Record how the run went and ask the notifier task to announce it.

    The notification itself can't be shown from here (this task runs in a
    background session with no desktop), so this just writes the status file
    and starts PremarketNotifier, which runs in the logged-in user's own
    session. If nobody's logged in that start does nothing, and the notifier's
    logon trigger picks the same status file up at the next sign-in.
    """
    payload = {
        "status": status,
        "title": title,
        "message": message,
        "open_path": open_path,
        "created": datetime.now(ET).isoformat(timespec="seconds"),
        "acknowledged": False,
    }
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log(f"=== Status: {status}, {message} ===")

    result = subprocess.run(
        ["schtasks", "/run", "/tn", NOTIFIER_TASK],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode == 0:
        log("Notifier started")
    else:
        log(f"Notifier not started ({result.stderr.strip() or result.stdout.strip()}), it will show at next logon")


def main():
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    weekday = datetime.now(ET).weekday()  # Monday = 0 ... Sunday = 6
    force = "--force" in sys.argv        # test only: ignore the weekend gate
    send_email = "--no-email" not in sys.argv  # test only: skip the real email

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"run_daily_{date_str}.log")
    with open(log_path, "a", encoding="utf-8") as log_file:
        log = make_logger(log_file)
        run = make_runner(log_file, log)

        if weekday >= 5 and not force:
            log(f"=== {date_str} is a weekend, skipping, no report saved ===")
            return

        if not wait_for_internet(log):
            return

        # date may have rolled over while waiting for a connection
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        log(f"=== Daily pipeline starting for {date_str} ===")

        if not run([PY, "scan.py"], "scan.py"):
            log("scan.py failed, nothing downstream has fresh data, stopping here")
            finish(log, "failed", "Daily premarket report FAILED",
                   f"{date_str}: the data scan failed, no report was made. See logs/run_daily_{date_str}.log")
            sys.exit(1)

        ready_md_files = []
        problems = []

        before_mtime = os.path.getmtime(REPORT_MD_PATH) if os.path.exists(REPORT_MD_PATH) else None
        analyst_ok = run([PY, "claude_analyst.py"], "claude_analyst.py")
        after_mtime = os.path.getmtime(REPORT_MD_PATH) if os.path.exists(REPORT_MD_PATH) else None
        if analyst_ok and after_mtime is not None and after_mtime != before_mtime:
            ready_md_files.append("REPORT.md")
        else:
            log("AI Premarket Report not included this run (Claude Code CLI not logged in, or the pass failed)")
            problems.append("no AI section (Claude not logged in?)")

        for scan_script, md_file in MECHANICAL_REPORTS:
            if not run([PY, scan_script], scan_script):
                log(f"!!! {scan_script} failed, {md_file} won't be in today's report")
                problems.append(f"{md_file} missing")
                continue
            ready_md_files.append(md_file)

        if not ready_md_files:
            log("=== Daily pipeline done, nothing rendered, nothing saved ===")
            finish(log, "failed", "Daily premarket report FAILED",
                   f"{date_str}: no report could be built. See logs/run_daily_{date_str}.log")
            sys.exit(1)

        archive_files = [(os.path.join(HERE, "packet.json"), "packet.json")]
        archive_files += [(os.path.join(HERE, f), f) for f in ready_md_files]
        day_dir, archived = archive_day(date_str, archive_files)
        log(f"Archived to {day_dir}: {', '.join(archived)}")

        if not run([PY, "render_report.py"] + ready_md_files + [date_str], "render combined report"):
            log("=== Daily pipeline done, render failed, nothing saved ===")
            finish(log, "failed", "Daily premarket report FAILED",
                   f"{date_str}: the report could not be rendered. See logs/run_daily_{date_str}.log")
            sys.exit(1)

        if len(ready_md_files) == 1:
            html_file = os.path.join("reports", f"{output_prefix(ready_md_files[0])}_{date_str}.html")
        else:
            combo_slug = "-".join(output_prefix(p) for p in ready_md_files)
            html_file = os.path.join("reports", f"combined_{combo_slug}_{date_str}.html")

        log(f"=== Daily pipeline done for {date_str} ===")
        log(f"  reports rendered: {', '.join(ready_md_files)}")
        log(f"  HTML saved at {html_file}")

        pdf_file = os.path.splitext(html_file)[0] + ".pdf"
        pdf_abs = os.path.join(HERE, pdf_file)
        pdf_ok = run([PY, "html_to_pdf.py", html_file], f"PDF {html_file}")
        pdf_ok = pdf_ok and os.path.exists(pdf_abs) and os.path.getsize(pdf_abs) >= MIN_PDF_BYTES
        if pdf_ok:
            log(f"  PDF saved at {pdf_file}")
        else:
            log(f"  PDF failed, HTML is still saved at {html_file}")
            problems.append("PDF failed (HTML saved instead)")

        if send_email:
            email_ok = run([PY, "deliver.py", html_file, "--to", EMAIL_RECIPIENT], f"email to {EMAIL_RECIPIENT}")
            email_ok = email_ok and "email sent" in run.last_output
        else:
            log("  email skipped (--no-email)")
            email_ok = False
        if not email_ok:
            problems.append("email to Yoel failed")

        weekly_note = None
        if weekday == 4:
            log("=== Friday, running weekly summary ===")
            weekly_md = os.path.join(
                "weekly_archive", week_folder_name(datetime.now(ET)), "WEEKLY_SUMMARY.md"
            )
            weekly_before = os.path.getmtime(weekly_md) if os.path.exists(weekly_md) else None
            weekly_ok = run([PY, "weekly_summary.py"], "weekly_summary.py")
            weekly_after = os.path.getmtime(weekly_md) if os.path.exists(weekly_md) else None

            if weekly_ok and weekly_after is not None and weekly_after != weekly_before:
                if run([PY, "render_report.py", weekly_md, date_str], "render weekly summary"):
                    weekly_html = os.path.join("reports", f"weekly_summary_{date_str}.html")
                    log(f"  weekly summary HTML saved at {weekly_html}")
                    weekly_note = "weekly summary saved too"
                else:
                    log("  weekly summary render failed, not saved")
                    problems.append("weekly summary render failed")
            else:
                log("  weekly summary not generated (no archived reports this week, or CLI not logged in)")
                problems.append("weekly summary not generated")

        parts = ["PDF saved" if pdf_ok else "HTML saved", "emailed to Yoel" if email_ok else "email NOT sent"]
        if weekly_note:
            parts.append(weekly_note)
        message = f"{date_str}: " + ", ".join(parts)
        if problems:
            message += ". Issues: " + "; ".join(problems)
        open_path = pdf_abs if pdf_ok else os.path.join(HERE, html_file)
        finish(
            log,
            "partial" if problems else "ok",
            "Daily premarket report ready" + (" (with issues)" if problems else ""),
            message,
            open_path,
        )


if __name__ == "__main__":
    main()
