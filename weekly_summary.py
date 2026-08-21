"""
weekly_summary.py

Synthesizes one week's worth of archived daily AI Premarket Reports into a single
weekly recap, via the same Claude Code CLI headless call claude_analyst.py uses
(Pro subscription login, no API key). Reads the current week's folder under
weekly_archive/, not packet.json, it's summarizing the daily reports themselves,
not re-analyzing raw data.

Meant to run at the end of the trading week (see run_daily.py, which calls this on
Fridays after that day's own report has been archived). Safe to run by hand any time
though, it just picks up whatever daily reports have been archived for the current
week so far.

If the Claude CLI isn't installed or isn't logged in, or if no daily reports have
been archived for this week yet, prints a clear skip message and exits cleanly.

Writes WEEKLY_SUMMARY.md inside that week's folder, e.g.
weekly_archive/10.8-14.8/WEEKLY_SUMMARY.md
"""

import os
import shutil
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from claude_analyst import ask_claude, logged_in

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(HERE, "weekly_archive")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def week_folder_name(dt):
    """Monday-Friday of dt's week, formatted like '10.8-14.8' (day.month, no zero padding)."""
    monday = dt - timedelta(days=dt.weekday())
    friday = monday + timedelta(days=4)
    return f"{monday.day}.{monday.month}-{friday.day}.{friday.month}"


def week_folder_path(dt):
    return os.path.join(ARCHIVE_DIR, week_folder_name(dt))


def archive_day(date_str, files_to_copy):
    """Copy today's finished reports into this week's archive folder.

    files_to_copy is a list of (source_path, archive_filename) pairs. Missing
    sources are skipped rather than failing the whole archive step, a report
    that didn't run today (e.g. the AI pass skipped) just isn't archived today.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ET)
    day_dir = os.path.join(week_folder_path(dt), date_str)
    os.makedirs(day_dir, exist_ok=True)

    copied = []
    for src, name in files_to_copy:
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(day_dir, name))
            copied.append(name)
    return day_dir, copied


def collect_week_reports(dt):
    """Every archived REPORT.md for dt's week so far, oldest date first."""
    week_dir = week_folder_path(dt)
    if not os.path.isdir(week_dir):
        return []

    found = []
    for entry in sorted(os.listdir(week_dir)):
        day_dir = os.path.join(week_dir, entry)
        report_path = os.path.join(day_dir, "REPORT.md")
        if os.path.isdir(day_dir) and os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                found.append((entry, f.read()))
    return found


def main():
    cli = shutil.which("claude")
    if not cli:
        print("Weekly summary skipped, claude CLI not installed")
        sys.exit(0)

    if not logged_in(cli):
        print("Weekly summary skipped, run 'claude auth login' first")
        sys.exit(0)

    now = datetime.now(ET)
    week_reports = collect_week_reports(now)
    if not week_reports:
        print("Weekly summary skipped, no archived daily reports found for this week yet")
        sys.exit(0)

    print(f"=== Weekly summary, {len(week_reports)} daily report(s) found ===")
    for date_str, _ in week_reports:
        print(f"  including {date_str}")

    prompt_weekly = ""
    with open(os.path.join(HERE, "prompt_weekly.md"), "r", encoding="utf-8") as f:
        prompt_weekly = f.read()

    date_line = now.strftime("%A, %B %d, %Y") + " · " + now.strftime("%H:%M") + " ET"
    inputs = "\n\n".join(
        f"=== INPUT: {date_str} REPORT.md ===\n{content}" for date_str, content in week_reports
    )
    prompt_input = (
        f"{prompt_weekly}\n\n"
        f"This summary is being generated on {date_line}. It covers {len(week_reports)} "
        f"trading day(s): {', '.join(d for d, _ in week_reports)}.\n\n"
        f"{inputs}"
    )

    try:
        summary = ask_claude(cli, prompt_input, "Weekly summary pass")
    except Exception as e:
        print(f"{e}")
        sys.exit(1)

    out_path = os.path.join(week_folder_path(now), "WEEKLY_SUMMARY.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(summary)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
