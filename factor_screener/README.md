# FACTOR CONT Screener

Scans a list of tickers for **Factor CONT** signals, the flag / `F#n` markers
from the TradingView script *EMA + SMC Confluence [crpto] v23*, and lists the
ones that fired within the last few days.

It's a bar-by-bar Python port of the Pine logic that produces those flags:
the EMA engine (cross, confirm, active trend, CONT, avg-move unlock, mature
trend) plus the six confluence factors:

| Tag | Factor | Rule on the CONT bar |
|-----|--------|----------------------|
| F | FVG | first tap of an unfilled same-direction FVG (FVGs built only in the last 30 days, like the Pine "Days Back") |
| L | Liquidity sweep | the 10-bar low/high swept PDL/PWL/PML (PDH/PWH/PMH) and the close reclaimed it |
| K | Killzone | bar opens inside 06:00-09:00 or 11:00-13:00 UTC (intraday only) |
| D | OTE | bar trades inside the 0.618-0.786 retrace of the last swing leg |
| M | Structure shift | a close broke the last swing pivot within the last 15 bars |
| X | Displacement | a big-body candle ≥ 1.4 × ATR within the last 4 bars |

Nothing here is financial advice.

## Setup

From the repo root, with the project venv:

```bash
.venv\Scripts\python -m pip install -r factor_screener\requirements.txt
```

## Desktop app (GUI)

Double-click **`Run Factor Screener.bat`** in this folder, or run:

```bash
.venv\Scripts\python factor_screener\app_gui.py
```

The window has a settings panel on the left and three tabs on the right.
Settings are remembered between sessions.

**Settings panel**

- **Scan**: timeframe (15m to 1wk), lookback days (1 to 30, slider or box),
  trading vs calendar days, and whether to ignore the bar still forming.
- **Factor rule**: match mode *Any (OR)*, *All (AND)* or *Pairs*. Tick the
  required factors, or type them in *Factor string* (`MX`, `XLM` …); the two
  stay in sync. *Pairs* takes a list like `XM,XL`.
- **EMA & strategy**: EMA set (the same presets as the Pine script) and the
  CONT switches: *Require EMA-Mid above/below EMA-Slow*, break-of-signal
  confirm, auto re-arm, avg-move unlock, mature trend, wick hold test, and
  how the trend is cleared.
- **Tickers**: a preset (S&P 500, Nasdaq 100, S&P 500 + Nasdaq 100,
  MegaCap Tech, Semiconductors, or any of the 11 GICS sectors) plus your own
  tickers in *Custom* (commas or new lines). Both lists are combined. Pick
  *Custom list only* to scan just your own. Index lists come from Wikipedia
  and are cached for 7 days; **Reload** downloads them again.
- **Filters**: min/max price (last close) and minimum average daily volume
  (20 sessions, in millions). 0 turns a filter off. *Parallel workers* sets
  how many tickers download at once; lower it if Yahoo starts rejecting
  requests.

Press **Scan**. The scan runs on background threads, so the window stays
usable. The progress bar shows *Scanning ticker X of Y*, with the speed and
time left in the status bar, and hits appear in the table as they're found.
**Stop** cancels the scan and keeps what's been found so far.

**Results tab**

- Long rows are tinted green and short rows red. The first signal after a
  direction flip (`F#1`) is in bold. Hover over *Factors* to see what each
  letter means.
- Click a column header to sort. The filter bar narrows results as you type,
  without rescanning: free text across all columns (every word must match,
  e.g. `semis long` or `M+X`), plus Direction, minimum Score and *F#1 only*.
- **Double-click a row** to open that ticker on TradingView at the scan's
  timeframe. Right-click to copy one ticker or every ticker shown (handy for
  a TradingView watchlist).
- **Export Excel… / Export CSV…** save the rows currently shown (after
  filters) to a folder you choose. It defaults to `factor_screener/results/`.

**History tab**

Lists every saved scan, newest first, with timeframe, universe, factor rule,
lookback and signal count. Click one to preview it, and double-click it (or
use **Load into Results**) to filter and sort it again in the Results tab.
With *Auto-save every scan* on (the default), each finished scan is saved
there as CSV automatically.

**Log tab**: scan settings, skipped tickers and errors.

## Command line

```bash
.venv\Scripts\python factor_screener\factor_screener.py
```

Useful flags (each one overrides the matching constant at the top of the file):

```bash
.venv\Scripts\python factor_screener\factor_screener.py --tickers FSLY,NVDA,AAPL --timeframe 1h --lookback 3 --export xlsx
```

- `--tickers A,B,C` or `--file tickers.txt` (one per line, `#` comments allowed)
- `--timeframe` / `-t`: `15m`, `30m`, `1h`, `2h`, `4h`, `1d`, `1wk`
- `--lookback` / `-l`: days to look back
- `--mode any|all|pairs`, `--factors MX`, `--pairs XM,XL`: factor rule
- `--export csv|xlsx`: saves to `factor_screener/results/`

Output columns: Ticker, Signal Time (bar open, exchange time), Timeframe,
Direction, Factors (e.g. `L+M+X`), Score (number of factors), Seq (`F#1` =
first factor signal since the direction flipped), Close.

## Changing settings

Everything lives in the `CONFIG` block at the top of `factor_screener.py`:
`TIMEFRAME`, `LOOKBACK_DAYS`, `LOOKBACK_MODE` (`trading` sessions or
`calendar` days), `TICKERS`, the EMA lengths, and every Pine input that
affects the signal. Keep them in sync with your TradingView inputs if you
change them there.

Two defaults are worth knowing about:

- `REQUIRE_EMA_SLOW_ALIGN = True`. The Pine default is off, but on is what
  reproduces the FSLY 4H chart's flags, so it looks like your chart has it
  enabled. Set it to `False` if you turn it off in TradingView.
- `MATCH_MODE = "any"` with `REQUIRED_FACTORS = {"M", "X"}` is the Pine
  default (fire on M **or** X). For strict X **and** M use `--mode all --factors MX`.

## How data is built

- Hour-based timeframes are built from Yahoo's 1h bars (about 730 days),
  combined per session from the 09:30 open, so 4H gives 09:30 and 13:30 bars
  like TradingView. Minute timeframes use Yahoo's intraday data (60 days, or 7
  days for 1m). Daily uses 5 years and weekly uses 10.
- The still-forming bar is dropped (`ONLY_CLOSED_BARS`), since Pine alerts
  fire on bar close.

## Known differences from TradingView

- The engine is stateful (active trend, CONT counts, the Dynamic unlock
  threshold learned from past CONT moves), so a different history length or
  small price differences between Yahoo and TradingView can occasionally add
  or drop a signal. Spot-check new setups on the chart.
- `F#n` counts only the history that was downloaded, so on long trends the
  number can differ from the chart.
- Killzones are in UTC and fall before the US cash open, so **K** never fires
  on regular-hours US stock bars. That matches the chart, where K shows 0.
