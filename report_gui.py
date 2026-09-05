"""
report_gui.py

A manual, on-demand control panel for the premarket report pipeline, sitting
alongside run_daily.py's automated Task Scheduler run rather than replacing
it. Two buttons, generate today's daily report or the current week's
summary, each saved locally as HTML + PDF under reports/, plus a checkbox
per known recipient and a Send button to email whichever one you want.

Doesn't reimplement any pipeline logic, it calls the same scripts run_daily.py
already calls (scan.py, claude_analyst.py, stage2_scan.py,
finviz_sector_scan.py, render_report.py, html_to_pdf.py, weekly_summary.py,
deliver.py) as subprocesses, the same way run_daily.py does. Unlike
run_daily.py, it does not gate on weekday or wait for internet, this is for
running by hand whenever you want, not for the unattended schedule.

Usage:
    python report_gui.py
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from zoneinfo import ZoneInfo

from render_report import output_prefix
from weekly_summary import archive_day, week_folder_name

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ET = ZoneInfo("America/New_York")
REPORT_MD_PATH = os.path.join(HERE, "REPORT.md")

MECHANICAL_REPORTS = [
    ("stage2_scan.py", "STAGE2_RIDER_REPORT.md"),
    ("finviz_sector_scan.py", "FINVIZ_SECTOR_SCAN_REPORT.md"),
]

EMAIL_OPTIONS = ["afire12@gmail.com", "ykalifa@gmail.com"]

CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


class ReportGUI:
    def __init__(self, root):
        self.root = root
        root.title("Premarket Report Control Panel")
        root.geometry("760x520")

        self.busy = False
        self.last_daily_html = None
        self.last_weekly_html = None
        self.log_queue = queue.Queue()
        self.all_buttons = []

        self._build_ui()
        self.root.after(100, self._poll_log_queue)

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        daily_frame = self._build_section(
            top, "Daily Report", self.start_daily, "daily"
        )
        daily_frame.pack(fill=tk.X, pady=(0, 8))

        weekly_frame = self._build_section(
            top, "Weekly Summary", self.start_weekly, "weekly"
        )
        weekly_frame.pack(fill=tk.X)

        log_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(log_frame, text="Progress").pack(anchor=tk.W)

        text_container = ttk.Frame(log_frame)
        text_container.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(text_container, height=16, wrap=tk.WORD, state=tk.DISABLED)
        scrollbar = ttk.Scrollbar(text_container, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_section(self, parent, title, generate_command, key):
        frame = ttk.LabelFrame(parent, text=title, padding=10)

        generate_btn = ttk.Button(frame, text=f"Generate {title}", command=generate_command)
        generate_btn.grid(row=0, column=0, rowspan=2, padx=(0, 16), sticky="ns")
        self.all_buttons.append(generate_btn)

        email_vars = {}
        for i, addr in enumerate(EMAIL_OPTIONS):
            var = tk.BooleanVar(value=False)
            chk = ttk.Checkbutton(frame, text=addr, variable=var)
            chk.grid(row=0, column=1 + i, sticky="w", padx=(0, 12))
            email_vars[addr] = var

        send_btn = ttk.Button(
            frame,
            text="Send Email",
            command=lambda: self.send_email(key),
            state=tk.DISABLED,
        )
        send_btn.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.all_buttons.append(send_btn)

        status_label = ttk.Label(frame, text="Not generated yet this session", foreground="#666")
        status_label.grid(row=1, column=2, columnspan=len(EMAIL_OPTIONS), sticky="w", padx=(12, 0), pady=(6, 0))

        setattr(self, f"{key}_email_vars", email_vars)
        setattr(self, f"{key}_send_btn", send_btn)
        setattr(self, f"{key}_status_label", status_label)

        return frame

    # ---- logging (worker threads push here, UI thread drains it) ----

    def log(self, msg):
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        drained = False
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.configure(state=tk.DISABLED)
            drained = True
        if drained:
            self.log_text.see(tk.END)
        self.root.after(100, self._poll_log_queue)

    # ---- running child scripts ----

    def run_cmd(self, cmd, label):
        self.log(f"=== {label} ===")
        result = subprocess.run(
            cmd,
            cwd=HERE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=CHILD_ENV,
        )
        if result.stdout:
            self.log(result.stdout.rstrip("\n"))
        if result.stderr:
            self.log(result.stderr.rstrip("\n"))
        if result.returncode != 0:
            self.log(f"!!! {label} failed, exit code {result.returncode}")
            return False
        return True

    def set_busy(self, busy):
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in self.all_buttons:
            btn.config(state=state)
        # Send buttons only re-enable if there's actually something to send
        if not busy:
            self.daily_send_btn.config(state=tk.NORMAL if self.last_daily_html else tk.DISABLED)
            self.weekly_send_btn.config(state=tk.NORMAL if self.last_weekly_html else tk.DISABLED)

    # ---- daily report ----

    def start_daily(self):
        if self.busy:
            return
        self.set_busy(True)
        threading.Thread(target=self._daily_worker, daemon=True).start()

    def _daily_worker(self):
        try:
            date_str = datetime.now(ET).strftime("%Y-%m-%d")
            self.log(f"=== Daily report starting for {date_str} ===")

            if not self.run_cmd([PY, "scan.py"], "scan.py"):
                self.log("scan.py failed, stopping")
                return

            ready = []
            before = os.path.getmtime(REPORT_MD_PATH) if os.path.exists(REPORT_MD_PATH) else None
            analyst_ok = self.run_cmd([PY, "claude_analyst.py"], "claude_analyst.py")
            after = os.path.getmtime(REPORT_MD_PATH) if os.path.exists(REPORT_MD_PATH) else None
            if analyst_ok and after is not None and after != before:
                ready.append("REPORT.md")
            else:
                self.log("AI Premarket Report not included (Claude CLI not logged in, or the pass failed)")

            for script, md_file in MECHANICAL_REPORTS:
                if self.run_cmd([PY, script], script):
                    ready.append(md_file)
                else:
                    self.log(f"!!! {script} failed, {md_file} won't be included")

            if not ready:
                self.log("Nothing rendered, stopping")
                return

            archive_files = [(os.path.join(HERE, "packet.json"), "packet.json")]
            archive_files += [(os.path.join(HERE, f), f) for f in ready]
            day_dir, archived = archive_day(date_str, archive_files)
            self.log(f"Archived to {day_dir}: {', '.join(archived)}")

            if not self.run_cmd([PY, "render_report.py"] + ready + [date_str], "render combined report"):
                self.log("Render failed, stopping")
                return

            if len(ready) == 1:
                html_file = os.path.join("reports", f"{output_prefix(ready[0])}_{date_str}.html")
            else:
                slug = "-".join(output_prefix(p) for p in ready)
                html_file = os.path.join("reports", f"combined_{slug}_{date_str}.html")

            if not self.run_cmd([PY, "html_to_pdf.py", html_file], f"PDF {html_file}"):
                self.log("PDF generation failed, HTML is still saved")

            self.last_daily_html = html_file
            self.log(f"=== Daily report done: {html_file} ===")
            self.root.after(0, lambda: self.daily_status_label.config(text=f"Ready: {html_file}"))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    # ---- weekly summary ----

    def start_weekly(self):
        if self.busy:
            return
        self.set_busy(True)
        threading.Thread(target=self._weekly_worker, daemon=True).start()

    def _weekly_worker(self):
        try:
            now = datetime.now(ET)
            date_str = now.strftime("%Y-%m-%d")
            weekly_md = os.path.join("weekly_archive", week_folder_name(now), "WEEKLY_SUMMARY.md")

            before = os.path.getmtime(weekly_md) if os.path.exists(weekly_md) else None
            if not self.run_cmd([PY, "weekly_summary.py"], "weekly_summary.py"):
                self.log("weekly_summary.py failed, stopping")
                return
            after = os.path.getmtime(weekly_md) if os.path.exists(weekly_md) else None

            if not (os.path.exists(weekly_md) and after != before):
                self.log("No weekly summary written (no archived daily reports this week yet, or Claude CLI not logged in)")
                return

            if not self.run_cmd([PY, "render_report.py", weekly_md, date_str], "render weekly summary"):
                self.log("Render failed, stopping")
                return

            weekly_html = os.path.join("reports", f"weekly_summary_{date_str}.html")
            if not self.run_cmd([PY, "html_to_pdf.py", weekly_html], f"PDF {weekly_html}"):
                self.log("PDF generation failed, HTML is still saved")

            self.last_weekly_html = weekly_html
            self.log(f"=== Weekly summary done: {weekly_html} ===")
            self.root.after(0, lambda: self.weekly_status_label.config(text=f"Ready: {weekly_html}"))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    # ---- email ----

    def send_email(self, key):
        if self.busy:
            return
        html_file = self.last_daily_html if key == "daily" else self.last_weekly_html
        if not html_file:
            return
        email_vars = self.daily_email_vars if key == "daily" else self.weekly_email_vars
        selected = [addr for addr, var in email_vars.items() if var.get()]
        if not selected:
            self.log("No recipient checked, nothing to send")
            return

        self.set_busy(True)
        threading.Thread(target=self._send_worker, args=(html_file, selected), daemon=True).start()

    def _send_worker(self, html_file, selected):
        try:
            self.run_cmd(
                [PY, "deliver.py", html_file, "--to", ",".join(selected)],
                f"email {html_file} to {', '.join(selected)}",
            )
        finally:
            self.root.after(0, lambda: self.set_busy(False))


def main():
    root = tk.Tk()
    ReportGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
