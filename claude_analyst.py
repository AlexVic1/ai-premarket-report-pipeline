"""
claude_analyst.py

Runs the Claude-only analyst and merge passes against packet.json via the
Claude Code CLI in headless mode, authenticated through the user's Claude Pro
subscription login (`claude auth login`), not a separate ANTHROPIC_API_KEY. No
Codex involved. This is what lets the main AI Premarket Report run inside the
daily automated pipeline instead of only ever being a manual, interactive
Claude Code session.

Usage draws against the Pro subscription's usage limits, not per-token API
billing, as long as the CLI is logged in via `claude auth login` (not --bare
and not ANTHROPIC_API_KEY, both of which force API key billing instead). Run
`claude auth login` once interactively before this will work; `claude auth
status` shows the current state.

The prompt goes in over stdin, not as a command line argument. packet.json
alone runs to tens of thousands of tokens, and Windows caps command line
length at roughly 32KB, so passing it as an argv string fails with "Argument
list too long" (the same issue the Codex wrapper in this project hit early
on).

Two calls per run: one for the analyst pass (prompt_claude.md against
packet.json), one for the merge pass (prompt_merge.md against packet.json
plus the analyst pass's own output). Codex never runs here, the merge prompt
already has honest handling built in for a missing Codex pass, same as the
manual runs before it.

If the CLI isn't installed or isn't logged in, prints a clear skip message and
exits cleanly, so run_daily.py can carry on with just the two mechanical
reports.

Writes claude_view.md and REPORT.md.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = "sonnet"
TIMEOUT_SECONDS = 600


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def logged_in(cli):
    try:
        result = subprocess.run(
            [cli, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        status = json.loads(result.stdout)
        return bool(status.get("loggedIn")) and status.get("authMethod") != "none"
    except Exception:
        return False


def ask_claude(cli, prompt_text, label):
    try:
        result = subprocess.run(
            [cli, "-p", "--model", MODEL, "--tools", ""],
            input=prompt_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{label} timed out after {TIMEOUT_SECONDS}s") from e

    if result.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {result.returncode}): {result.stderr.strip()[:500]}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError(f"{label} returned no output, stderr: {result.stderr.strip()[:500]}")

    return output


def main():
    cli = shutil.which("claude")
    if not cli:
        print("Claude analyst pass skipped, claude CLI not installed")
        sys.exit(0)

    if not logged_in(cli):
        print("Claude analyst pass skipped, run 'claude auth login' first")
        sys.exit(0)

    packet_path = os.path.join(HERE, "packet.json")
    if not os.path.exists(packet_path):
        print("packet.json not found, run scan.py first")
        sys.exit(1)

    packet_json = read_file(packet_path)
    prompt_claude = read_file(os.path.join(HERE, "prompt_claude.md"))
    prompt_merge = read_file(os.path.join(HERE, "prompt_merge.md"))

    print("=== Claude analyst pass ===")
    analyst_input = f"{prompt_claude}\n\n=== INPUT: packet.json ===\n{packet_json}"
    try:
        claude_view = ask_claude(cli, analyst_input, "Analyst pass")
    except Exception as e:
        print(f"{e}")
        sys.exit(1)

    claude_view_path = os.path.join(HERE, "claude_view.md")
    write_file(claude_view_path, claude_view)
    print(f"Wrote {claude_view_path}")

    print("=== Claude merge pass ===")
    et_now = datetime.now(ET)
    date_line = f"{et_now.strftime('%A, %B %d, %Y')} · {et_now.strftime('%H:%M')} ET"

    merge_input = (
        f"{prompt_merge}\n\n"
        f"For the H3 date line, use exactly this date and time: {date_line}\n\n"
        f"=== INPUT 1: packet.json ===\n{packet_json}\n\n"
        f"=== INPUT 2: claude_view.md ===\n{claude_view}\n\n"
        f"=== INPUT 3: codex_view.md ===\n"
        f"(empty, Codex did not run this cycle, this pipeline runs Claude only, "
        f"follow your instructions for when the Codex pass is unavailable)\n"
    )
    try:
        report = ask_claude(cli, merge_input, "Merge pass")
    except Exception as e:
        print(f"{e}")
        sys.exit(1)

    report_path = os.path.join(HERE, "REPORT.md")
    write_file(report_path, report)
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
