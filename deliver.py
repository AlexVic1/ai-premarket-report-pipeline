"""
deliver.py

Emails a rendered HTML premarket report via Resend.

Usage:
    python deliver.py reports/premarket_<date>.html

The HTML is sent inline as the email body (Resend/Gmail render it directly in
the message, no attachment involved). To send multiple reports in one email,
combine them into a single HTML page first with render_report.py (pass it
several markdown files at once), then deliver that one combined file, don't
try to attach several separate HTML files here, mail clients tend to show
those as raw source instead of a rendered page.

Also generates a PDF of the same page (via html_to_pdf.py, a real headless
Chromium render, so it looks exactly like the HTML) and attaches it to the
same email. If PDF generation fails for any reason, the email still goes out
with just the HTML body, a missing PDF isn't worth blocking delivery over.

Reads RESEND_API_KEY, EMAIL_TO, and optional EMAIL_FROM from a local .env
file using a tiny built in KEY=VALUE parser, no extra dependency. Real
environment variables always win over whatever is in .env.

Supports more than one Resend account, each with its own key and recipient
list, numbered _2, _3, and so on:

    RESEND_API_KEY=re_...      EMAIL_TO=you@example.com
    RESEND_API_KEY_2=re_...    EMAIL_TO_2=friend@example.com

This exists because Resend's sandbox mode (the free default, no verified
domain) only allows an API key to send to the email address that owns that
Resend account. A single key can't send to two different people's inboxes
until a domain is verified, so the workaround is one Resend account per
recipient, each with its own free key. Once you verify a domain, a single
account's EMAIL_TO can hold as many comma separated addresses as you want,
and the extra numbered accounts become unnecessary. The same report is sent
once per configured account.

If no account is configured at all, the script prints a clear skip message
and exits cleanly, it never crashes just because email isn't set up yet.
"""

import base64
import os
import re
import sys
from datetime import datetime

import requests

from html_to_pdf import html_to_pdf

# Windows' default console encoding (cp1252) can't represent everything a
# downstream error message might contain (Playwright's install-hint box uses
# Unicode box-drawing characters, for example), and that crashed this exact
# script's own graceful PDF-failure fallback before, taking the whole email
# down with it. Reconfigure to UTF-8 with a safe fallback so that can't
# happen again, whether this runs standalone or via run_daily.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "AI Premarket Analyst <onboarding@resend.dev>"
MAX_ACCOUNTS = 5


def parse_env_file(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def get_setting(name, dotenv_values, default=None):
    env_value = os.environ.get(name)
    if env_value:
        return env_value
    file_value = dotenv_values.get(name)
    if file_value:
        return file_value
    return default


def gather_accounts(dotenv_values):
    accounts = []
    suffixes = [""] + [f"_{i}" for i in range(2, MAX_ACCOUNTS + 1)]
    for suf in suffixes:
        api_key = get_setting(f"RESEND_API_KEY{suf}", dotenv_values)
        to_raw = get_setting(f"EMAIL_TO{suf}", dotenv_values)
        if not api_key or not to_raw:
            continue
        to_list = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
        if not to_list:
            continue
        from_addr = get_setting(f"EMAIL_FROM{suf}", dotenv_values, default=DEFAULT_FROM)
        accounts.append({"api_key": api_key, "to": to_list, "from": from_addr})
    return accounts


def guess_date(html_path):
    match = re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(html_path))
    if match:
        return match.group(0)
    return datetime.now().strftime("%Y-%m-%d")


def guess_title(html_content, fallback="AI Premarket Report"):
    match = re.search(r"<title>(.*?)</title>", html_content, re.IGNORECASE | re.DOTALL)
    if not match:
        return fallback
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or fallback


def build_pdf_attachment(html_path):
    try:
        pdf_path = html_to_pdf(html_path)
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        print(f"  PDF attached: {pdf_path}")
        return [
            {
                "filename": os.path.basename(pdf_path),
                "content": base64.b64encode(pdf_bytes).decode("ascii"),
                "content_type": "application/pdf",
            }
        ]
    except Exception as e:
        print(f"  PDF generation failed, sending HTML only: {e}")
        return None


def send_via_account(account, subject, html_content, attachments):
    payload = {
        "from": account["from"],
        "to": account["to"],
        "subject": subject,
        "html": html_content,
    }
    if attachments:
        payload["attachments"] = attachments

    try:
        resp = requests.post(
            RESEND_URL,
            headers={
                "Authorization": f"Bearer {account['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except Exception as e:
        print(f"email failed, could not reach Resend for {', '.join(account['to'])}: {e}")
        return False

    if resp.status_code >= 400:
        print(f"email failed for {', '.join(account['to'])}, Resend returned {resp.status_code}: {resp.text}")
        return False

    print(f"email sent to {', '.join(account['to'])}, subject: {subject}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python deliver.py reports/premarket_<date>.html")
        sys.exit(1)

    html_path = sys.argv[1]
    if not os.path.exists(html_path):
        print(f"File not found: {html_path}")
        sys.exit(1)

    dotenv_values = parse_env_file(ENV_PATH)
    accounts = gather_accounts(dotenv_values)

    if not accounts:
        print("email skipped, set RESEND_API_KEY + EMAIL_TO")
        sys.exit(0)

    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    date_str = guess_date(html_path)
    title = guess_title(html_content)
    subject = f"{title} - {date_str}"

    attachments = build_pdf_attachment(html_path)

    results = [send_via_account(account, subject, html_content, attachments) for account in accounts]

    if not any(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
