"""
render_report.py

Renders a Markdown premarket report into a clean, readable standalone HTML page.

Usage:
    python render_report.py REPORT.md [YYYY-MM-DD]

If the date isn't given, today's date is used. Writes to reports/premarket_<date>.html
"""

import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import markdown

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(HERE, "reports")

CSS = """
:root {
    --bg: #f4f4f2;
    --page-bg: #ffffff;
    --text: #1f2328;
    --muted: #6b7280;
    --border: #e3e3e0;
    --header-bg: #f4f5f7;
    --zebra: #fafafa;
    --accent: #33383f;
}

* { box-sizing: border-box; }

body {
    margin: 0;
    padding: 40px 16px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.65;
    font-size: 16px;
}

.page {
    max-width: 900px;
    margin: 0 auto;
    background: var(--page-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 40px 48px 32px;
}

.report-header {
    border-bottom: 2px solid var(--border);
    padding-bottom: 18px;
    margin-bottom: 24px;
}

.report-header h1 {
    margin: 0 0 6px 0;
    font-size: 26px;
    font-weight: 700;
    letter-spacing: -0.01em;
}

.report-date {
    color: var(--muted);
    font-size: 14px;
}

.report-body h2 {
    font-size: 20px;
    font-weight: 700;
    color: var(--accent);
    margin-top: 36px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
}

.report-body h2:first-child {
    border-top: none;
    padding-top: 0;
    margin-top: 0;
}

.report-body h3 {
    font-size: 15px;
    font-weight: 600;
    color: var(--muted);
    margin-top: 4px;
}

.report-body p {
    margin: 12px 0;
}

.report-body blockquote {
    margin: 16px 0;
    padding: 12px 16px;
    background: var(--header-bg);
    border-left: 4px solid var(--border);
    color: var(--muted);
    font-size: 14px;
}

.report-body hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 32px 0;
}

.report-body table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 13.5px;
}

.report-body th,
.report-body td {
    border: 1px solid var(--border);
    padding: 8px 10px;
    text-align: left;
    vertical-align: top;
}

.report-body th {
    background: var(--header-bg);
    font-weight: 600;
}

.report-body tbody tr:nth-child(even) {
    background: var(--zebra);
}

.report-body ul,
.report-body ol {
    padding-left: 22px;
}

.report-body li {
    margin: 4px 0;
}

.report-body code {
    background: var(--header-bg);
    padding: 2px 5px;
    border-radius: 4px;
    font-size: 0.9em;
}

.report-body pre {
    background: var(--header-bg);
    padding: 12px 14px;
    border-radius: 6px;
    overflow-x: auto;
}

.report-body strong {
    font-weight: 600;
}

.report-footer {
    margin-top: 36px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 13px;
    text-align: center;
}

@media (max-width: 640px) {
    .page {
        padding: 24px 20px;
    }
}
"""


def split_title(md_text):
    """Pull the first H1 out as the page title, everything else is the body.

    Keeping it out of the body avoids showing the title twice, once in the
    custom header and once inside the rendered markdown.
    """
    lines = md_text.splitlines()
    title = None
    body_lines = []
    title_taken = False
    for line in lines:
        if not title_taken and re.match(r"^#(?!#)\s*", line):
            title = re.sub(r"^#(?!#)\s*", "", line).strip()
            title_taken = True
            continue
        body_lines.append(line)
    return title or "Premarket Report", "\n".join(body_lines)


def build_html(md_text, date_str, generated_str):
    title, body_md = split_title(md_text)
    body_html = markdown.markdown(
        body_md,
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
<header class="report-header">
<h1>{title}</h1>
<div class="report-date">{date_str}</div>
</header>
<main class="report-body">
{body_html}
</main>
<footer class="report-footer">
Generated {generated_str} &middot; Built by Claude + Codex &middot; Educational only, not financial advice
</footer>
</div>
</body>
</html>
"""


def output_prefix(md_path):
    """Name the output file after its source, so different reports don't collide.

    REPORT.md keeps its existing "premarket" prefix for backward compatibility,
    everything else gets a slug built from its own filename.
    """
    stem = os.path.splitext(os.path.basename(md_path))[0]
    if stem.upper() == "REPORT":
        return "premarket"
    slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    return slug or "report"


def main():
    if len(sys.argv) < 2:
        print("Usage: python render_report.py REPORT.md [YYYY-MM-DD]")
        sys.exit(1)

    md_path = sys.argv[1]
    if not os.path.exists(md_path):
        print(f"File not found: {md_path}")
        sys.exit(1)

    if len(sys.argv) >= 3:
        date_str = sys.argv[2]
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            print(f"Date must be in YYYY-MM-DD format, got: {date_str}")
            sys.exit(1)
    else:
        date_str = datetime.now(ET).strftime("%Y-%m-%d")

    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()

    generated_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    html = build_html(md_text, date_str, generated_str)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(REPORTS_DIR, f"{output_prefix(md_path)}_{date_str}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
