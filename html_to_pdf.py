"""
html_to_pdf.py

Converts a rendered HTML report into a PDF using a real Chromium based browser
via Playwright, headless. This is a real browser doing the rendering, the
same engine behind Chrome, so the PDF comes out pixel-for-pixel the same as
the HTML page, CSS custom properties and all, not an approximation from a
lightweight PDF library that would drop the styling.

Tries the system's actual installed Edge or Chrome first (via Playwright's
"channel" option), falling back to Playwright's own separately downloaded
Chromium only if neither is present, see the comment above LAUNCH_STRATEGIES
for why.

Usage:
    python html_to_pdf.py reports/premarket_<date>.html [output.pdf]

If no output path is given, writes next to the HTML file with a .pdf extension.
"""

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Must be set before playwright is imported, it reads this at import time to
# find its installed browsers. Windows Task Scheduler's "run whether user is
# logged on or not" session doesn't always resolve %LOCALAPPDATA% the same
# way an interactive session does, even though it's still running as the same
# user, so the default lookup can miss browsers that are genuinely installed.
# Pointing at the real path explicitly sidesteps that instead of guessing why.
# USERPROFILE tends to stay correctly populated in more execution contexts
# than LOCALAPPDATA does, use it as the base rather than trusting LOCALAPPDATA
# directly.
_user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
_default_browsers_path = os.path.join(_user_profile, "AppData", "Local", "ms-playwright")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _default_browsers_path)

from playwright.sync_api import sync_playwright


# Every automated run this week hit the identical failure: Playwright's own
# bundled Chromium (downloaded into ms-playwright/ by `playwright install`)
# reports "Executable doesn't exist" specifically under Task Scheduler's
# background session, even though the exact same file is genuinely on disk
# and launches fine when run interactively. A same-process retry didn't help,
# it failed the same way every time, so this isn't a transient timing issue,
# something about that non-interactive launch consistently can't use it.
# The likely cause is security software treating a background launch of an
# unrecognized, separately-downloaded browser binary with more suspicion than
# a browser that's already installed, signed, and in everyday use on the
# machine. So try real installed browsers first via Playwright's "channel"
# option (no separate download, launches the actual system Edge/Chrome), and
# only fall back to Playwright's own bundled Chromium if neither is present.
LAUNCH_STRATEGIES = [
    {"channel": "msedge"},
    {"channel": "chrome"},
    {},
]


def html_to_pdf(html_path, pdf_path=None):
    html_path = os.path.abspath(html_path)
    if pdf_path is None:
        pdf_path = os.path.splitext(html_path)[0] + ".pdf"

    url = Path(html_path).resolve().as_uri()

    last_error = None
    for launch_kwargs in LAUNCH_STRATEGIES:
        label = launch_kwargs.get("channel", "Playwright's bundled Chromium")
        try:
            with sync_playwright() as p:
                # --no-sandbox and friends matter here specifically because
                # this also runs unattended under Task Scheduler, a
                # restricted, non-interactive session where Chromium's
                # normal sandboxing can fail to initialize. Harmless when
                # run interactively too.
                browser = p.chromium.launch(
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                    **launch_kwargs,
                )
                page = browser.new_page()
                page.goto(url)
                page.pdf(
                    path=pdf_path,
                    format="A4",
                    print_background=True,
                    margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
                )
                browser.close()
            return pdf_path
        except Exception as e:
            last_error = e
            print(f"PDF via {label} failed ({e}), trying next option...")

    raise last_error


def main():
    if len(sys.argv) < 2:
        print("Usage: python html_to_pdf.py report.html [output.pdf]")
        sys.exit(1)

    html_path = sys.argv[1]
    if not os.path.exists(html_path):
        print(f"File not found: {html_path}")
        sys.exit(1)

    pdf_path = sys.argv[2] if len(sys.argv) >= 3 else None
    out_path = html_to_pdf(html_path, pdf_path)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
