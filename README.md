# Premarket Trading Routine

A small pipeline that pulls free, keyless market data before the open, screens it
against two backtested watchlist setups plus two extra quality filters, builds a
readable report, and can email it to you automatically every morning.

Nothing here is financial advice. It's a data and screening tool, not a signal
generator, treat every output as a starting point for your own research.

## What it does

- Pulls a premarket snapshot: major indices, VIX, rates, oil, dollar, live top
  movers, market wide news, and the day's high impact US economic calendar.
- Filters that down to real gappers and enriches each one with catalyst
  headlines, intraday levels, daily trend data, and a next earnings date.
- Applies two validated, backtested rule sets in code (not AI judgment) to flag
  day trading and swing candidates.
- Applies two extra mechanical filters on top: a "Stage 2" trend quality gate,
  and a sector leadership and screening funnel modeled on a FinViz workflow.
- Renders any of these into a clean HTML report and can email it via Resend.
- Optionally, runs an independent two brain review (Claude and Codex, run
  separately, then merged) that adds AI judgment on top of the rule based
  picks. This part is manual and off by default, see below.

## How it's built

```
scan.py                 pulls raw premarket data into packet.json, does no
                         analysis, just data collection plus the two
                         deterministic eligibility flags from the rules below

stage2_scan.py           mechanical "Stage 2 Rider" trend filter against
                         today's gappers, writes STAGE2_RIDER_REPORT.md

finviz_sector_scan.py    sector leadership ranking plus a FinViz style
                         mechanical screen, writes FINVIZ_SECTOR_SCAN_REPORT.md

render_report.py         turns any of the markdown reports into a clean,
                         styled standalone HTML page

deliver.py                emails a rendered HTML report via Resend

run_daily.py              runs the full unattended chain: scan, both
                         mechanical reports, render, deliver, in one go

WATCHLIST_CRITERIA.md    the source of truth for every rule, in plain English

REPORT_TEMPLATE.md       the section by section blueprint the AI report
                         prompts are built against

prompt_claude.md          the two brain (manual) analyst pipeline, not run by
prompt_codex.md          run_daily.py, kept for when you want a deeper,
prompt_merge.md          AI reviewed report instead of the mechanical ones
```

## Setup

You need Python 3.10 or newer.

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install yfinance feedparser markdown requests
```

Copy the env template and fill it in:

```
copy .env.example .env
```

Open `.env` and set:
- `RESEND_API_KEY`, get one free at https://resend.com/api-keys
- `EMAIL_TO`, the inbox that should receive the reports
- `EMAIL_FROM` is optional, it defaults to a Resend sandbox address that works
  without verifying your own domain

`.env` is gitignored on purpose, never commit it, it holds a real API key.

## Running it

The simplest path, one command that does everything and emails both reports:

```
.venv\Scripts\python.exe run_daily.py
```

Or run each step yourself:

```
.venv\Scripts\python.exe scan.py
.venv\Scripts\python.exe stage2_scan.py
.venv\Scripts\python.exe finviz_sector_scan.py
.venv\Scripts\python.exe render_report.py STAGE2_RIDER_REPORT.md
.venv\Scripts\python.exe render_report.py FINVIZ_SECTOR_SCAN_REPORT.md
.venv\Scripts\python.exe deliver.py reports/stage2_rider_report_<date>.html
.venv\Scripts\python.exe deliver.py reports/finviz_sector_scan_report_<date>.html
```

If `RESEND_API_KEY` or `EMAIL_TO` aren't set, `deliver.py` just prints a skip
message and exits cleanly, it never crashes the pipeline over missing email
config, so it's safe to run the scans and rendering without email set up yet.

## Automating it

`run_daily.py` is a fully self contained Python script, no AI reasoning needed
at runtime, which makes it safe to run on a plain OS scheduler.

On Windows, Task Scheduler works well:

```
$action = New-ScheduledTaskAction -Execute '"<path to .venv>\Scripts\python.exe"' -Argument 'run_daily.py' -WorkingDirectory '<path to this folder>'
$trigger = New-ScheduledTaskTrigger -Daily -At 9:00AM
Register-ScheduledTask -TaskName "PremarketDailyReports" -Action $action -Trigger $trigger -RunLevel Limited
```

On macOS or Linux, a cron entry does the same job:

```
0 9 * * * cd /path/to/this/folder && .venv/bin/python run_daily.py
```

Either way, this runs independently of any AI app being open, it's just a
scheduled script.

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

## The optional two brain AI review

For a deeper, judgment layered report (not just the rule based picks), there's
a separate manual workflow:

1. `prompt_claude.md` runs an independent analyst pass over `packet.json`.
2. `prompt_codex.md` runs a second, independent pass via the OpenAI Codex CLI
   (`codex login` required first, plus a small shell wrapper, see the prompt
   file for the exact command).
3. `prompt_merge.md` combines both into a final report using
   `REPORT_TEMPLATE.md` as the structural blueprint. It never averages the two
   opinions, conviction only goes to the top tier when both sides genuinely
   agree.

This isn't part of `run_daily.py` on purpose, it needs a real LLM reasoning
pass each time, not something a scheduler can do unattended.

## Data sources

Everything is free and keyless: `yfinance` for prices and screeners,
`feedparser` for news RSS, and the ForexFactory calendar JSON feed for
economic events (cached locally with a TTL since it rate limits on rapid
calls). No paid API keys required anywhere except Resend for email delivery,
which has a free tier.

## Disclaimer

This is an educational project. The rule based flags encode backtested
criteria, but a backtest is history, not a promise. RVOL in these reports is
full trading day relative volume, not true premarket RVOL, since the free data
source reports roughly zero volume before the open. Nothing in this repo's
output is financial advice.
