"""
run_daily.py

The automated morning pipeline: refresh premarket data, run the two mechanical
scans (Stage 2 Rider, FinViz Sector Scan), render each to HTML, and email both.

This deliberately does NOT run the two-brain Claude + Codex merge (prompt_claude.md,
prompt_codex.md, prompt_merge.md, REPORT.md). That stays a manual, on-demand
workflow, it needs an actual LLM reasoning pass, not something a scheduled script
can do on its own. Stage 2 Rider and FinViz Sector Scan are both pure data and
math, no AI judgment involved, so they're the ones safe to run unattended.

Usage:
    python run_daily.py
"""

import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ET = ZoneInfo("America/New_York")

REPORTS = [
    ("stage2_scan.py", "STAGE2_RIDER_REPORT.md", "stage2_rider_report"),
    ("finviz_sector_scan.py", "FINVIZ_SECTOR_SCAN_REPORT.md", "finviz_sector_scan_report"),
]


def run(cmd, label):
    print(f"=== {label} ===")
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print(f"!!! {label} failed, exit code {result.returncode}")
        return False
    return True


def main():
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    print(f"=== Daily pipeline starting for {date_str} ===")

    if not run([PY, "scan.py"], "scan.py"):
        print("scan.py failed, nothing downstream has fresh data, stopping here")
        sys.exit(1)

    results = {}
    for scan_script, md_file, html_prefix in REPORTS:
        html_file = os.path.join("reports", f"{html_prefix}_{date_str}.html")

        if not run([PY, scan_script], scan_script):
            results[md_file] = "scan failed"
            continue
        if not run([PY, "render_report.py", md_file, date_str], f"render {md_file}"):
            results[md_file] = "render failed"
            continue
        if not run([PY, "deliver.py", html_file], f"deliver {html_file}"):
            results[md_file] = "deliver failed"
            continue
        results[md_file] = "sent"

    print(f"=== Daily pipeline done for {date_str} ===")
    for label, status in results.items():
        print(f"  {label}: {status}")

    if any(status != "sent" for status in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
