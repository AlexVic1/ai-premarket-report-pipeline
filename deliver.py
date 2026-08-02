"""
deliver.py

Emails a rendered HTML premarket report via Resend.

Usage:
    python deliver.py reports/premarket_<date>.html

Reads RESEND_API_KEY, EMAIL_TO, and optional EMAIL_FROM from a local .env file
using a tiny built in KEY=VALUE parser, no extra dependency. Real environment
variables always win over whatever is in .env.

If RESEND_API_KEY or EMAIL_TO aren't set anywhere, the script prints a clear
skip message and exits cleanly, it never crashes just because email isn't
configured yet.
"""

import os
import re
import sys
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "AI Premarket Analyst <onboarding@resend.dev>"


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


def main():
    if len(sys.argv) < 2:
        print("Usage: python deliver.py reports/premarket_<date>.html")
        sys.exit(1)

    html_path = sys.argv[1]
    if not os.path.exists(html_path):
        print(f"File not found: {html_path}")
        sys.exit(1)

    dotenv_values = parse_env_file(ENV_PATH)
    resend_api_key = get_setting("RESEND_API_KEY", dotenv_values)
    email_to = get_setting("EMAIL_TO", dotenv_values)
    email_from = get_setting("EMAIL_FROM", dotenv_values, default=DEFAULT_FROM)

    if not resend_api_key or not email_to:
        print("email skipped, set RESEND_API_KEY + EMAIL_TO")
        sys.exit(0)

    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    date_str = guess_date(html_path)
    title = guess_title(html_content)
    subject = f"{title} - {date_str}"

    payload = {
        "from": email_from,
        "to": [email_to],
        "subject": subject,
        "html": html_content,
    }

    try:
        resp = requests.post(
            RESEND_URL,
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except Exception as e:
        print(f"email failed, could not reach Resend: {e}")
        sys.exit(1)

    if resp.status_code >= 400:
        print(f"email failed, Resend returned {resp.status_code}: {resp.text}")
        sys.exit(1)

    print(f"email sent to {email_to}, subject: {subject}")


if __name__ == "__main__":
    main()
