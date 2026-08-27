"""
run_daily.py

The automated morning pipeline: refresh premarket data, run the two mechanical
scans (Stage 2 Rider, FinViz Sector Scan), run the Claude-only analyst + merge
pass for the main AI Premarket Report (claude_analyst.py, no Codex), and
combine everything into a single HTML page saved locally under reports/.
Nothing gets emailed and no PDF gets generated, that's a deliberate choice,
deliver.py and html_to_pdf.py still exist and work standalone for anyone who
wants either back.

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
        if result.stdout:
            log(result.stdout.rstrip("\n"))
        if result.stderr:
            log(result.stderr.rstrip("\n"))
        if result.returncode != 0:
            log(f"!!! {label} failed, exit code {result.returncode}")
            return False
        return True
    return run


def main():
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    weekday = datetime.now(ET).weekday()  # Monday = 0 ... Sunday = 6

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"run_daily_{date_str}.log")
    with open(log_path, "a", encoding="utf-8") as log_file:
        log = make_logger(log_file)
        run = make_runner(log_file, log)

        if weekday >= 5:
            log(f"=== {date_str} is a weekend, skipping, no report saved ===")
            return

        if not wait_for_internet(log):
            return

        # date may have rolled over while waiting for a connection
        date_str = datetime.now(ET).strftime("%Y-%m-%d")
        log(f"=== Daily pipeline starting for {date_str} ===")

        if not run([PY, "scan.py"], "scan.py"):
            log("scan.py failed, nothing downstream has fresh data, stopping here")
            sys.exit(1)

        ready_md_files = []

        before_mtime = os.path.getmtime(REPORT_MD_PATH) if os.path.exists(REPORT_MD_PATH) else None
        analyst_ok = run([PY, "claude_analyst.py"], "claude_analyst.py")
        after_mtime = os.path.getmtime(REPORT_MD_PATH) if os.path.exists(REPORT_MD_PATH) else None
        if analyst_ok and after_mtime is not None and after_mtime != before_mtime:
            ready_md_files.append("REPORT.md")
        else:
            log("AI Premarket Report not included this run (Claude Code CLI not logged in, or the pass failed)")

        for scan_script, md_file in MECHANICAL_REPORTS:
            if not run([PY, scan_script], scan_script):
                log(f"!!! {scan_script} failed, {md_file} won't be in today's report")
                continue
            ready_md_files.append(md_file)

        if not ready_md_files:
            log("=== Daily pipeline done, nothing rendered, nothing saved ===")
            sys.exit(1)

        archive_files = [(os.path.join(HERE, "packet.json"), "packet.json")]
        archive_files += [(os.path.join(HERE, f), f) for f in ready_md_files]
        day_dir, archived = archive_day(date_str, archive_files)
        log(f"Archived to {day_dir}: {', '.join(archived)}")

        if not run([PY, "render_report.py"] + ready_md_files + [date_str], "render combined report"):
            log("=== Daily pipeline done, render failed, nothing saved ===")
            sys.exit(1)

        if len(ready_md_files) == 1:
            html_file = os.path.join("reports", f"{output_prefix(ready_md_files[0])}_{date_str}.html")
        else:
            combo_slug = "-".join(output_prefix(p) for p in ready_md_files)
            html_file = os.path.join("reports", f"combined_{combo_slug}_{date_str}.html")

        log(f"=== Daily pipeline done for {date_str} ===")
        log(f"  reports rendered: {', '.join(ready_md_files)}")
        log(f"  HTML saved at {html_file}")

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
                else:
                    log("  weekly summary render failed, not saved")
            else:
                log("  weekly summary not generated (no archived reports this week, or CLI not logged in)")


if __name__ == "__main__":
    main()
