# AI Premarket Report Pipeline

A pipeline that pulls free, keyless market data before the open, screens it
against two backtested watchlist setups plus two extra quality filters, has
Claude write an actual analyst report on top of the rule based picks, renders
everything into a clean HTML page with a matching PDF, and emails it to you
automatically every weekday morning, unattended.

Nothing here is financial advice. It's a data, screening, and reporting tool,
not a signal generator, treat every output as a starting point for your own
research.

## 🔑 Requires a Claude Pro or Max subscription for the main report

**The centerpiece of this pipeline, the AI Premarket Report (`claude_analyst.py`),
needs a Claude.ai Pro or Max subscription (the $20/month `chat` plan), not a
separate paid API key.** It runs through the Claude Code CLI in headless mode,
authenticated with your normal Claude.ai login, and usage draws against your
subscription's own limits, not per-token API billing.

- Install: `npm install -g @anthropic-ai/claude-code`
- Log in once: `claude auth login` (opens a browser, signs in with your Claude.ai account)
- Check it worked: `claude auth status`, should show `"subscriptionType": "pro"` (or `"max"`)

If you don't have a Pro/Max subscription, or the CLI isn't logged in, that one
step just skips itself, `claude_analyst.py` prints a clear message and exits
cleanly. **Everything else in this repo works without it**: the two backtested
watchlist screens, the Stage 2 trend filter, the FinViz sector scan, rendering,
PDF export, and email delivery are all free and keyless, no subscription and
no API key of any kind.

## What it does

- Pulls a premarket snapshot: major indices, VIX, rates, oil, dollar, live top
  movers, market wide news, and the day's high impact US economic calendar.
- Filters that down to real gappers and enriches each one with catalyst
  headlines, intraday levels, daily trend data, and a next earnings date.
- Applies two validated, backtested rule sets in code (not AI judgment) to flag
  day trading and swing candidates.
- Applies two extra mechanical filters on top: a "Stage 2" trend quality gate,
  and a sector leadership and screening funnel modeled on a FinViz workflow.
- Runs an independent Claude analyst pass over that data and writes it up in a
  readable report, on top of (not instead of) the rule based picks. Requires
  the Pro/Max subscription above, skips itself cleanly if that's not set up.
- Renders any of these into a clean HTML page, plus a pixel-identical PDF via
  headless Chromium, and emails the combined result via Resend.
- Runs unattended on a schedule: weekdays only, works even when you're not
  logged into the machine, and waits out a dead internet connection instead of
  just failing silently.

## How it's built

```
scan.py                  pulls raw premarket data into packet.json, does no
                          analysis, just data collection plus the two
                          deterministic eligibility flags from the rules below

stage2_scan.py            mechanical "Stage 2 Rider" trend filter against
                          today's gappers, writes STAGE2_RIDER_REPORT.md

finviz_sector_scan.py     sector leadership ranking plus a FinViz style
                          mechanical screen, writes FINVIZ_SECTOR_SCAN_REPORT.md

claude_analyst.py         runs the Claude analyst + merge pass over packet.json
                          via the Claude Code CLI (Pro/Max subscription auth,
                          see above), writes claude_view.md and REPORT.md

render_report.py          turns one or several markdown reports into a clean,
                          styled HTML page, combining multiple into one page
                          with a divider between them when given more than one

html_to_pdf.py            converts a rendered HTML report to PDF with headless
                          Chromium (Playwright), pixel identical to the HTML

deliver.py                 emails a rendered HTML report (plus PDF) via Resend,
                          supports multiple recipients and multiple accounts

run_daily.py               the full unattended chain: scan, Claude analyst,
                          both mechanical reports, render, PDF, deliver, all
                          logged to logs/run_daily_<date>.log. Weekdays only,
                          waits for a lost internet connection to come back

WATCHLIST_CRITERIA.md     the source of truth for every rule, in plain English

REPORT_TEMPLATE.md        the section by section blueprint the AI report
                          prompts are built against

prompt_claude.md           the analyst + merge prompts claude_analyst.py runs.
prompt_merge.md           Also reusable manually for a deeper, two-brain
prompt_codex.md           report with Codex as a second opinion, see below.
```

## Setup

You need Python 3.10 or newer, and Node.js (for the Claude Code CLI, only
needed if you want the AI Premarket Report step).

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install yfinance feedparser markdown requests playwright
.venv\Scripts\python.exe -m playwright install chromium
```

For the AI Premarket Report (optional, see the callout above):

```
npm install -g @anthropic-ai/claude-code
claude auth login
```

Copy the env template and fill it in:

```
copy .env.example .env
```

Open `.env` and set:
- `RESEND_API_KEY`, get one free at https://resend.com/api-keys
- `EMAIL_TO`, the inbox(es) that should receive the reports, comma separated
  for more than one
- `EMAIL_FROM` is optional, it defaults to a Resend sandbox address that works
  without verifying your own domain
- Resend's free sandbox mode only lets an API key send to the email address
  that owns that Resend account. To send to more than one person without
  verifying a domain, see the `RESEND_API_KEY_2` / `EMAIL_TO_2` pattern
  documented in `.env.example`, one Resend account per extra recipient. Once
  you verify a domain, drop that and just comma separate `EMAIL_TO`.

`.env` is gitignored on purpose, never commit it, it holds real API keys.

## Running it

The simplest path, one command that does everything and emails the result:

```
.venv\Scripts\python.exe run_daily.py
```

Or run each step yourself:

```
.venv\Scripts\python.exe scan.py
.venv\Scripts\python.exe claude_analyst.py
.venv\Scripts\python.exe stage2_scan.py
.venv\Scripts\python.exe finviz_sector_scan.py
.venv\Scripts\python.exe render_report.py REPORT.md STAGE2_RIDER_REPORT.md FINVIZ_SECTOR_SCAN_REPORT.md
.venv\Scripts\python.exe deliver.py reports/combined_..._<date>.html
```

If `RESEND_API_KEY` or `EMAIL_TO` aren't set, `deliver.py` just prints a skip
message and exits cleanly. If the Claude Code CLI isn't installed or logged
in, `claude_analyst.py` does the same. Neither crashes the pipeline, it's
safe to run everything else without either configured.

## Automating it

`run_daily.py` handles its own scheduling logic: it only does real work on
weekdays (exits immediately on Saturday/Sunday regardless of when it's
triggered), and if there's no internet connection at trigger time, it checks
every 5 minutes and runs with live data as soon as one comes back, up to a
same day cutoff. All of that is in the script, not the OS scheduler, so it
holds regardless of what's triggering it.

On Windows, Task Scheduler works well. For it to run even when you're not
logged in (locked screen, no active session), set it up for "run whether user
is logged on or not":

```
$action = New-ScheduledTaskAction -Execute '"<path to .venv>\Scripts\python.exe"' -Argument 'run_daily.py' -WorkingDirectory '<path to this folder>'
$trigger = New-ScheduledTaskTrigger -Daily -At 9:00AM
Register-ScheduledTask -TaskName "PremarketDailyReports" -Action $action -Trigger $trigger -RunLevel Limited
```

Then in Task Scheduler's GUI, open the task's Properties → General tab →
switch "Run only when user is logged on" to **"Run whether user is logged on
or not"** → enter your Windows password when prompted. This is a one time
GUI step, `Register-ScheduledTask` alone can't set it non-interactively.

This only covers the machine being locked or logged out, not asleep or fully
powered off, nothing running on the machine can wake itself up from either of
those.

On macOS or Linux, a cron entry does the equivalent job:

```
0 9 * * 1-5 cd /path/to/this/folder && .venv/bin/python run_daily.py
```

## The two watchlist rules

Full detail is in `WATCHLIST_CRITERIA.md`, short version:

**Day Trading, "Trend Join Long"** (backtest: 54.6% win rate, profit factor
1.59): gap over 3%, price over $3, market cap over $1B, premarket RVOL over
1.5, price breaking above yesterday's high. Comes with a full intraday
management plan (trigger, stop, scale out, trail, flat time).

**Swing Watchlist** (backtest: 57.6% win rate on news catalysts, 44.7% on
earnings catalysts): gap of 8% or more, price over $3, open above yesterday's
high, open above the 200-day SMA, market cap of $800M or more, and a real
catalyst. Entry and exit management for swing isn't built yet, treat these as
starter ideas only.

Two more filters sit on top of those, not backtested, just quality gates:

**Stage 2 Rider**: an 8 point trend confirmation check (150/200-day MA
structure, 52-week range, relative strength) before a name is even considered
for the swing watchlist.

**FinViz Sector Scan**: ranks sector leadership and screens a universe by
market cap, volume, price, and ATR. The chart check step is intentionally left
to a human, this script narrows the field, it doesn't approve anything.

## The AI Premarket Report

`claude_analyst.py` runs an independent Claude analyst pass over `packet.json`
(`prompt_claude.md`), then a merge pass that assembles the final report
(`prompt_merge.md`, structured against `REPORT_TEMPLATE.md`). This is the part
that needs the Pro/Max subscription described at the top of this file. It's
wired into `run_daily.py` automatically, no separate step required, it just
skips itself if the CLI isn't logged in.

For an even deeper, two-brain version with an independent second opinion from
OpenAI's Codex CLI layered on top (`codex login` required, separate from
Claude entirely), run `prompt_codex.md` manually and merge both views with
`prompt_merge.md`. This part stays manual on purpose, a live two-model review
isn't something a scheduler can drive on its own, and it needs its own Codex
CLI session. The merge rules never average the two opinions, conviction only
reaches the top tier when both sides genuinely agree.

## Data sources

Free and keyless for everything except the two subscriptions above:
`yfinance` for prices and screeners, `feedparser` for news RSS, and the
ForexFactory calendar JSON feed for economic events (cached locally with a
TTL since it rate limits on rapid calls). Resend (free tier) for email
delivery, and your existing Claude Pro/Max subscription for the AI Premarket
Report specifically, nothing else needs a paid key.

## Disclaimer

This is an educational project. The rule based flags encode backtested
criteria, but a backtest is history, not a promise. RVOL in these reports is
full trading day relative volume, not true premarket RVOL, since the free data
source reports roughly zero volume before the open. Nothing in this repo's
output, AI generated or otherwise, is financial advice.
