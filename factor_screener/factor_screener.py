"""
FACTOR CONT screener: a Python port of the signal logic in
"EMA + SMC Confluence [crpto] v23" (TradingView Pine script).

Scans a list of tickers and reports every Factor CONT signal (the flag markers,
"F#n" labels on the chart) that fired within the last LOOKBACK_DAYS on TIMEFRAME.

What is ported (bar-by-bar, same order of operations as the Pine script so the
stateful parts line up):
  PART A  EMA engine: cross -> windowed confirm -> activeDir, trend invalidation
          by N closes through EMA-mid, auto re-arm from the EMA stack, CONT with
          sep-rising / hold / mature-trend rules, and the FIX1 avg-move unlock
          whose Dynamic threshold is learned from past CONT peak moves.
  PART B  FVG creation / first-tap / fill, PDH/PDL, PWH/PWL, PMH/PML, killzones.
  PART B2 Displacement [X], swing pivots + Market Structure Shift [M], OTE [D].
  PART C  Confluence flags F/L/K/D/M/X on CONT bars, factor match (Any / All /
          Pairs) and the direction-flip sequence counter (F#1, F#2 ...).

Not ported (they don't affect Factor CONT): SETUP/ENTRY, RESUME, trend-ride,
pullback (PB) signals, sessions drawing, and the stats tables.

Nothing here is financial advice.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

# ============================================================================
# CONFIG: edit these
# ============================================================================

TIMEFRAME = "4h"          # "15m", "30m", "1h", "2h", "4h", "1d", "1wk", ...
LOOKBACK_DAYS = 5         # report signals fired within the last N days
LOOKBACK_MODE = "trading" # "trading" = last N sessions present in the data, "calendar" = now - N days
ONLY_CLOSED_BARS = True   # ignore the still-forming bar (Pine alerts fire on bar close)

TICKERS = [
    "FSLY", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL", "NFLX",
    "PLTR", "SHOP", "CRWD", "NET", "SNOW", "COIN", "MSTR", "UBER", "SMCI", "ARM",
]
TICKERS_FILE = None       # optional path to a .txt/.csv with one ticker per line (overrides TICKERS)

EXCHANGE_TZ = "America/New_York"
MARKET_CLOSE = "16:00"    # regular-session close, used to decide if the last bar is complete
MAX_WORKERS = 8           # parallel downloads

# --- EMA engine (PART A) ---
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
CONFIRM_WINDOW = 8
USE_BREAK_SIGNAL = True
BIG_CANDLE_THRESH_PCT = 0.3
SEP_PCT_MIN = 0.04
SEP_LOOKBACK = 2
HOLD_BARS = 2
REQUIRE_EMA_SLOW_ALIGN = True   # Pine default is off; on matches the reference chart's F# flags
USE_WICKS_FOR_HOLD = False
CONT_INV_MODE = "closes"  # "closes" | "cross" | "either"
CONT_INV_CLOSES = 4
CONT_AUTO_REARM = True
PERF_LOOKFORWARD = 40

# FIX1 avg-move unlock
USE_AVG_MOVE_UNLOCK = True
THRESH_MODE = "Dynamic"   # "Dynamic" | "TimeframAuto" | "Manual"
MANUAL_THRESH_PCT = 0.48
DYN_MULTIPLIER = 0.45
DYN_MIN_SAMPLES = 5

# FIX2 mature trend
USE_MATURE_TREND = True
MATURE_SEP_MULTIPLIER = 2.0
MATURE_AFTER_COUNT = 2

# --- SMC / ICT factors (PART B / B2) ---
FVG_DAYS = 30             # Pine only builds FVGs within the last N days ("Days Back")
DISP_BODY_MIN = 0.62
DISP_RANGE_MULT = 1.4
DISP_LOOKBACK = 4
ATR_LEN = 14
MSS_PIVOT_LEN = 5
MSS_LOOKBACK = 15
OTE_LO, OTE_HI = 0.618, 0.786
LIQ_SWEEP_LOOKBACK = 10
KILLZONES_UTC = [("06:00", "09:00"), ("11:00", "13:00")]  # London, NY (bar open time, UTC)

# Which factors are counted at all (Pine "Count ... confluence" toggles)
FACTOR_ENABLED = {"F": True, "L": True, "K": True, "D": True, "M": True, "X": True}

# --- Factor CONT filter (PART C) ---
# "any"   = CONT carries at least one of REQUIRED_FACTORS  (Pine default: M or X)
# "all"   = CONT carries every one of REQUIRED_FACTORS     (e.g. X AND M)
# "pairs" = CONT carries both factors of any pair in FACTOR_PAIRS
MATCH_MODE = "any"
REQUIRED_FACTORS = {"M", "X"}
FACTOR_PAIRS = [("X", "M")]

# --- Output ---
EXPORT_FORMAT = None      # None | "csv" | "xlsx"
EXPORT_DIR = Path(__file__).resolve().parent / "results"

FACTOR_ORDER = "FLKDMX"

# ============================================================================
# TIMEFRAME HANDLING / DATA FETCHING
# ============================================================================

_NATIVE_MINUTES = {1, 2, 5, 15, 30, 60, 90}


@dataclass
class TfSpec:
    label: str            # "4H", "15M", "1D"
    minutes: float        # bar length in minutes (Pine timeframe.in_seconds / 60)
    intraday: bool
    fetch_interval: str   # yfinance interval to download
    fetch_period: str     # yfinance period to download
    resample_n: int       # combine N fetched bars into one (1 = native)


def parse_timeframe(tf: str) -> TfSpec:
    m = re.fullmatch(r"(\d+)\s*(m|min|h|d|wk|w)", tf.strip().lower())
    if not m:
        raise ValueError(f"Unsupported TIMEFRAME {tf!r}; use e.g. '15m', '1h', '4h', '1d', '1wk'")
    n, unit = int(m.group(1)), m.group(2)
    if unit in ("m", "min"):
        if n in _NATIVE_MINUTES:
            return TfSpec(f"{n}M", n, True, f"{n}m", "7d" if n == 1 else "60d", 1)
        base = 5 if n % 5 == 0 else 1
        return TfSpec(f"{n}M", n, True, f"{base}m", "7d" if base == 1 else "60d", n // base)
    if unit == "h":
        # Yahoo keeps ~730 days of hourly data; resampling it gives far more
        # history than native multi-hour intervals, which the stateful engine needs.
        return TfSpec(f"{n}H", n * 60, True, "1h", "730d", n)
    if unit == "d":
        if n != 1:
            raise ValueError("Only '1d' is supported for daily timeframes")
        return TfSpec("1D", 1440, False, "1d", "5y", 1)
    if n != 1:
        raise ValueError("Only '1wk' is supported for weekly timeframes")
    return TfSpec("1W", 10080, False, "1wk", "10y", 1)


def resample_session_anchored(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Combine every n intraday bars within a session, anchored at the session
    open (09:30 -> 09:30/13:30 for 4h), the way TradingView builds them."""
    if n <= 1:
        return df
    day = df.index.normalize()
    idx_in_day = df.groupby(day).cumcount().to_numpy()
    key = pd.Series(day.astype("int64") // 10**9 * 1000 + idx_in_day // n, index=df.index)
    out = df.groupby(key.values).agg(
        Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
        Close=("Close", "last"), Volume=("Volume", "sum"),
    )
    out.index = pd.DatetimeIndex(df.index.to_series().groupby(key.values).first())
    return out


def fetch_bars(ticker: str, spec: TfSpec) -> pd.DataFrame:
    """Download OHLCV and return bars on the requested timeframe in exchange time."""
    raw = yf.Ticker(ticker).history(
        period=spec.fetch_period, interval=spec.fetch_interval,
        auto_adjust=False, actions=False, prepost=False,
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    raw = raw[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])
    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    raw.index = raw.index.tz_convert(EXCHANGE_TZ)
    bars = resample_session_anchored(raw, spec.resample_n) if spec.intraday else raw
    if ONLY_CLOSED_BARS and not bars.empty:
        bars = drop_open_bar(bars, spec)
    return bars


def drop_open_bar(bars: pd.DataFrame, spec: TfSpec) -> pd.DataFrame:
    tz = ZoneInfo(EXCHANGE_TZ)
    now = datetime.now(tz)
    start = bars.index[-1].to_pydatetime()
    hh, mm = map(int, MARKET_CLOSE.split(":"))
    if spec.intraday:
        close_dt = start.replace(hour=hh, minute=mm, second=0, microsecond=0)
        end = min(start + timedelta(minutes=spec.minutes), close_dt)
    elif spec.label == "1D":
        end = start.replace(hour=hh, minute=mm, second=0, microsecond=0)
    else:  # weekly bar starts Monday, ends Friday close
        end = (start + timedelta(days=4)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return bars.iloc[:-1] if now < end else bars


def load_tickers(tickers_arg: str | None, file_arg: str | None) -> list[str]:
    if tickers_arg:
        items = tickers_arg.split(",")
    elif file_arg or TICKERS_FILE:
        text = Path(file_arg or TICKERS_FILE).read_text(encoding="utf-8")
        items = re.split(r"[,\s]+", text)
    else:
        items = TICKERS
    seen, out = set(), []
    for t in (x.strip().upper() for x in items):
        if t and not t.startswith("#") and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ============================================================================
# INDICATORS (Pine-equivalent)
# ============================================================================

def pine_ema(x: np.ndarray, length: int) -> np.ndarray:
    """ta.ema: SMA seed, then alpha = 2 / (len + 1)."""
    out = np.full(len(x), np.nan)
    if len(x) < length:
        return out
    alpha = 2.0 / (length + 1)
    out[length - 1] = x[:length].mean()
    for i in range(length, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def pine_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, length: int) -> np.ndarray:
    """ta.atr: RMA of true range, SMA seed."""
    prev_c = np.concatenate(([np.nan], c[:-1]))
    tr = np.where(np.isnan(prev_c), h - l,
                  np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c))))
    out = np.full(len(tr), np.nan)
    if len(tr) < length:
        return out
    out[length - 1] = tr[:length].mean()
    for i in range(length, len(tr)):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


def pine_pivots(h: np.ndarray, l: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """ta.pivothigh/pivotlow(n, n): value reported on the bar n bars AFTER the pivot."""
    size = len(h)
    ph, pl = np.full(size, np.nan), np.full(size, np.nan)
    for i in range(2 * n, size):
        p = i - n
        left_h, right_h = h[p - n:p], h[p + 1:i + 1]
        if h[p] >= left_h.max() and h[p] > right_h.max():
            ph[i] = h[p]
        left_l, right_l = l[p - n:p], l[p + 1:i + 1]
        if l[p] <= left_l.min() and l[p] < right_l.min():
            pl[i] = l[p]
    return ph, pl


def barssince_within(cond: np.ndarray, lookback: int) -> np.ndarray:
    """ta.barssince(cond) <= lookback (False when cond never happened)."""
    out = np.zeros(len(cond), dtype=bool)
    last = -1
    for i, v in enumerate(cond):
        if v:
            last = i
        out[i] = last >= 0 and (i - last) <= lookback
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).min().to_numpy()


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).max().to_numpy()


def _hhmm(s: str) -> int:
    hh, mm = map(int, s.split(":"))
    return hh * 60 + mm


# ============================================================================
# SIGNAL ENGINE
# ============================================================================

def detect_factor_signals(bars: pd.DataFrame, spec: TfSpec) -> list[dict]:
    """Run the Pine logic over every bar; return all Factor CONT signals."""
    n = len(bars)
    if n < EMA_SLOW + 5:
        return []
    o = bars["Open"].to_numpy(float)
    h = bars["High"].to_numpy(float)
    l = bars["Low"].to_numpy(float)
    c = bars["Close"].to_numpy(float)
    idx = bars.index

    ef, em, es = pine_ema(c, EMA_FAST), pine_ema(c, EMA_MID), pine_ema(c, EMA_SLOW)

    # --- Displacement [X] ---
    rng = h - l
    body_rat = np.where(rng > 0, np.abs(c - o) / np.where(rng > 0, rng, 1), 0.0)
    atr = pine_atr(h, l, c, ATR_LEN)
    with np.errstate(invalid="ignore"):
        big = (body_rat >= DISP_BODY_MIN) & (rng >= DISP_RANGE_MULT * atr)
    recent_disp_up = barssince_within(big & (c > o), DISP_LOOKBACK)
    recent_disp_dn = barssince_within(big & (c < o), DISP_LOOKBACK)

    # --- Pivots -> last swing high/low, MSS [M] ---
    ph, pl = pine_pivots(h, l, MSS_PIVOT_LEN)
    last_ph = pd.Series(ph).ffill().to_numpy()
    last_pl = pd.Series(pl).ffill().to_numpy()
    prev_c = np.concatenate(([np.nan], c[:-1]))
    with np.errstate(invalid="ignore"):
        mss_up = ~np.isnan(last_ph) & (c > last_ph) & (prev_c <= last_ph)
        mss_dn = ~np.isnan(last_pl) & (c < last_pl) & (prev_c >= last_pl)
    recent_mss_up = barssince_within(mss_up, MSS_LOOKBACK)
    recent_mss_dn = barssince_within(mss_dn, MSS_LOOKBACK)

    # --- OTE [D] ---
    with np.errstate(invalid="ignore"):
        leg_ok = ~np.isnan(last_ph) & ~np.isnan(last_pl) & (last_ph > last_pl)
        leg = last_ph - last_pl
        in_ote_long = leg_ok & (l <= last_ph - OTE_LO * leg) & (h >= last_ph - OTE_HI * leg)
        in_ote_short = leg_ok & (h >= last_pl + OTE_LO * leg) & (l <= last_pl + OTE_HI * leg)

    # --- Liquidity levels: previous day / week / month H-L built from chart bars ---
    dates = idx.date
    iso = idx.isocalendar()
    week_key = (iso["year"].to_numpy() * 100 + iso["week"].to_numpy())
    month_key = idx.year * 100 + idx.month
    pdh = np.full(n, np.nan); pdl = np.full(n, np.nan)
    pwh = np.full(n, np.nan); pwl = np.full(n, np.nan)
    pmh = np.full(n, np.nan); pml = np.full(n, np.nan)
    levels = {"d": [np.nan] * 4, "w": [np.nan] * 4, "m": [np.nan] * 4}  # prevH, prevL, curH, curL
    keys = {"d": [d.toordinal() for d in dates], "w": week_key, "m": month_key}
    for i in range(n):
        for k, st in levels.items():
            new_period = i > 0 and keys[k][i] != keys[k][i - 1]
            if new_period:
                st[0], st[1], st[2], st[3] = st[2], st[3], h[i], l[i]
            else:
                st[2] = h[i] if math.isnan(st[2]) else max(st[2], h[i])
                st[3] = l[i] if math.isnan(st[3]) else min(st[3], l[i])
        pdh[i], pdl[i] = levels["d"][0], levels["d"][1]
        pwh[i], pwl[i] = levels["w"][0], levels["w"][1]
        pmh[i], pml[i] = levels["m"][0], levels["m"][1]
    recent_low = rolling_min(l, LIQ_SWEEP_LOOKBACK)
    recent_high = rolling_max(h, LIQ_SWEEP_LOOKBACK)

    def _swept_low(i):
        return any(not math.isnan(lv) and recent_low[i] < lv and c[i] > lv for lv in (pdl[i], pwl[i], pml[i]))

    def _swept_high(i):
        return any(not math.isnan(lv) and recent_high[i] > lv and c[i] < lv for lv in (pdh[i], pwh[i], pmh[i]))

    # --- Killzone [K] (intraday only, by bar open time in UTC) ---
    if spec.intraday:
        utc = idx.tz_convert("UTC")
        mins = utc.hour * 60 + utc.minute
        in_kz = np.zeros(n, dtype=bool)
        for a, b in KILLZONES_UTC:
            in_kz |= (mins >= _hhmm(a)) & (mins < _hhmm(b))
    else:
        in_kz = np.zeros(n, dtype=bool)

    # --- FVG range cutoff ---
    fvg_cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=FVG_DAYS)
    fvg_in_range = (idx.tz_convert("UTC") >= fvg_cutoff)

    tf_mins = spec.minutes
    auto_thresh = (0.15 if tf_mins <= 1 else 0.48 if tf_mins <= 3 else 0.55 if tf_mins <= 5 else
                   0.75 if tf_mins <= 15 else 1.20 if tf_mins <= 30 else 1.80 if tf_mins <= 60 else 2.50)

    # ---- state (Pine "var") ----
    pending_long = pending_short = False
    sig_bar = None
    sig_high = sig_low = math.nan
    sig_big = False
    active_dir = 0
    break_count = 0
    cont_count = 0
    last_cont_price = math.nan
    unlocked = False
    avg_move_thresh = auto_thresh
    cont_ready_long_prev = cont_ready_short_prev = False
    sep_hist: list[float] = []
    cont_long_arr = np.zeros(n, dtype=bool)
    cont_short_arr = np.zeros(n, dtype=bool)
    perf_cnt = 0
    perf_sum = 0.0
    fvgs: list[list] = []   # [top, bot, created_bar, dir] (unfilled only)
    fac_seq_num = fac_seq_dir = 0
    signals = []

    for i in range(n):
        f, m, s = ef[i], em[i], es[i]
        ema_ok = not (math.isnan(f) or math.isnan(m) or math.isnan(s))
        fp, mp = (ef[i - 1], em[i - 1]) if i > 0 else (math.nan, math.nan)

        # ===== FVG creation / tap / fill (PART B) =====
        if fvg_in_range[i] and i >= 2:
            if l[i] > h[i - 2]:
                fvgs.append([l[i], h[i - 2], i, 1])
            if l[i - 2] > h[i]:
                fvgs.append([l[i - 2], h[i], i, -1])
        tap_long = any(z[3] == 1 and z[2] < i and l[i] <= z[0] and h[i] >= z[1] for z in fvgs)
        tap_short = any(z[3] == -1 and z[2] < i and l[i] <= z[0] and h[i] >= z[1] for z in fvgs)
        fvgs = [z for z in fvgs if not (i > z[2] and l[i] <= z[0] and h[i] >= z[1])]

        if not ema_ok:
            sep_hist.append(math.nan)
            continue

        # ===== cross + pending confirm =====
        long_sig = not math.isnan(fp) and not math.isnan(mp) and f > m and fp <= mp
        short_sig = not math.isnan(fp) and not math.isnan(mp) and f < m and fp >= mp
        if long_sig or short_sig:
            pending_long, pending_short = long_sig, short_sig
            sig_bar, sig_high, sig_low = i, h[i], l[i]
            sig_big = abs(h[i] - l[i]) / c[i] * 100.0 >= BIG_CANDLE_THRESH_PCT
        bars_from = None if sig_bar is None else i - sig_bar
        inval_l = pending_long and f <= m
        inval_s = pending_short and f >= m
        expire = (pending_long or pending_short) and bars_from is not None and bars_from > CONFIRM_WINDOW
        if inval_l or inval_s or expire:
            pending_long = pending_short = False
            sig_bar, sig_high, sig_low, sig_big = None, math.nan, math.nan, False
        in_window = bars_from is not None and 1 <= bars_from <= CONFIRM_WINDOW
        up_bar = i > 0 and c[i] > c[i - 1]
        dn_bar = i > 0 and c[i] < c[i - 1]
        base_l = pending_long and in_window and f > m and up_bar
        base_s = pending_short and in_window and f < m and dn_bar
        lvl_l = m if sig_big else sig_high
        lvl_s = m if sig_big else sig_low
        confirm_l = base_l and (not USE_BREAK_SIGNAL or c[i] > lvl_l)
        confirm_s = base_s and (not USE_BREAK_SIGNAL or c[i] < lvl_s)
        if confirm_l or confirm_s:
            pending_long = pending_short = False
            sig_bar, sig_high, sig_low, sig_big = None, math.nan, math.nan, False

        # ===== activeDir =====
        if confirm_l or confirm_s:
            active_dir = 1 if confirm_l else -1
            cont_count, last_cont_price, unlocked = 0, math.nan, False
        for d in (1, -1):
            if active_dir == d:
                wrong = c[i] < m if d == 1 else c[i] > m
                break_count = break_count + 1 if wrong else 0
                by_closes = break_count >= CONT_INV_CLOSES
                by_cross = f < m if d == 1 else f > m
                clear = {"closes": by_closes, "cross": by_cross}.get(CONT_INV_MODE, by_closes or by_cross)
                if clear:
                    active_dir, break_count, cont_count = 0, 0, 0
                    last_cont_price, unlocked = math.nan, False
        if CONT_AUTO_REARM and active_dir == 0:
            rearm_l = f > m > s and c[i] > f
            rearm_s = f < m < s and c[i] < f
            if rearm_l or rearm_s:
                active_dir = 1 if rearm_l else -1
                break_count, cont_count, last_cont_price, unlocked = 0, 0, math.nan, False

        # ===== CONT =====
        sep = abs(f - m) / c[i] * 100.0
        prev_seps = sep_hist[-SEP_LOOKBACK:]
        sep_rising = (len(prev_seps) == SEP_LOOKBACK and not any(math.isnan(x) for x in prev_seps)
                      and sep > max(prev_seps))
        sep_hist.append(sep)
        if i >= HOLD_BARS - 1:
            if USE_WICKS_FOR_HOLD:
                hold_l = l[i - HOLD_BARS + 1:i + 1].min() > m
                hold_s = h[i - HOLD_BARS + 1:i + 1].max() < m
            else:
                hold_l = c[i - HOLD_BARS + 1:i + 1].min() > m
                hold_s = c[i - HOLD_BARS + 1:i + 1].max() < m
        else:
            hold_l = hold_s = False
        align_l = (f > m > s) if REQUIRE_EMA_SLOW_ALIGN else f > m
        align_s = (f < m < s) if REQUIRE_EMA_SLOW_ALIGN else f < m

        if USE_AVG_MOVE_UNLOCK and not math.isnan(last_cont_price) and not unlocked:
            mv = ((h[i] - last_cont_price) if active_dir == 1 else (last_cont_price - l[i])) / last_cont_price * 100.0
            if mv >= avg_move_thresh:
                unlocked = True
        allowed = cont_count == 0 or not USE_AVG_MOVE_UNLOCK or unlocked
        mature = USE_MATURE_TREND and cont_count >= MATURE_AFTER_COUNT
        if mature:
            cond_l = active_dir == 1 and align_l and sep >= SEP_PCT_MIN * MATURE_SEP_MULTIPLIER and hold_l
            cond_s = active_dir == -1 and align_s and sep >= SEP_PCT_MIN * MATURE_SEP_MULTIPLIER and hold_s
        else:
            cond_l = active_dir == 1 and align_l and sep >= SEP_PCT_MIN and sep_rising and hold_l
            cond_s = active_dir == -1 and align_s and sep >= SEP_PCT_MIN and sep_rising and hold_s
        ready_l, ready_s = cond_l and allowed, cond_s and allowed
        cont_l = ready_l and not cont_ready_long_prev
        cont_s = ready_s and not cont_ready_short_prev
        cont_ready_long_prev, cont_ready_short_prev = ready_l, ready_s
        if cont_l or cont_s:
            cont_count += 1
            last_cont_price = c[i]
            unlocked = False
        cont_long_arr[i], cont_short_arr[i] = cont_l, cont_s

        # ===== perf stats -> Dynamic unlock threshold (end of bar, used next bar) =====
        k = PERF_LOOKFORWARD
        if i >= k:
            ep = c[i - k]
            if cont_long_arr[i - k]:
                perf_cnt += 1
                perf_sum += (h[i - k + 1:i + 1].max() - ep) / ep * 100.0
            if cont_short_arr[i - k]:
                perf_cnt += 1
                perf_sum += (ep - l[i - k + 1:i + 1].min()) / ep * 100.0
        dyn_ready = THRESH_MODE == "Dynamic" and perf_cnt >= DYN_MIN_SAMPLES
        if THRESH_MODE == "Manual":
            avg_move_thresh = MANUAL_THRESH_PCT
        elif dyn_ready:
            avg_move_thresh = max(perf_sum / perf_cnt * DYN_MULTIPLIER, 0.05)
        else:
            avg_move_thresh = auto_thresh

        if not (cont_l or cont_s):
            continue

        # ===== confluence factors on the CONT bar (PART C) =====
        long_ = cont_l
        present = {
            "F": tap_long if long_ else tap_short,
            "L": _swept_low(i) if long_ else _swept_high(i),
            "K": bool(in_kz[i]),
            "D": bool(in_ote_long[i] if long_ else in_ote_short[i]),
            "M": bool(recent_mss_up[i] if long_ else recent_mss_dn[i]),
            "X": bool(recent_disp_up[i] if long_ else recent_disp_dn[i]),
        }
        present = {kf: v and FACTOR_ENABLED.get(kf, True) for kf, v in present.items()}
        if not factor_match(present):
            continue

        direction = 1 if long_ else -1
        fac_seq_num = 1 if direction != fac_seq_dir else fac_seq_num + 1
        fac_seq_dir = direction
        tags = [kf for kf in FACTOR_ORDER if present[kf]]
        signals.append({
            "time": idx[i],
            "direction": "LONG" if long_ else "SHORT",
            "factors": "+".join(tags),
            "score": len(tags),
            "seq": f"F#{fac_seq_num}",
            "close": c[i],
        })
    return signals


def factor_match(present: dict[str, bool]) -> bool:
    if MATCH_MODE == "pairs":
        return any(present.get(a) and present.get(b) for a, b in FACTOR_PAIRS)
    if not REQUIRED_FACTORS:
        return False
    if MATCH_MODE == "all":
        return all(present.get(kf) for kf in REQUIRED_FACTORS)
    return any(present.get(kf) for kf in REQUIRED_FACTORS)


# ============================================================================
# SCANNING / OUTPUT
# ============================================================================

def lookback_cutoff(bars: pd.DataFrame) -> pd.Timestamp:
    if LOOKBACK_MODE == "calendar":
        return pd.Timestamp.now(tz=EXCHANGE_TZ) - pd.Timedelta(days=LOOKBACK_DAYS)
    sessions = pd.Index(bars.index.normalize().unique())
    return sessions[-min(LOOKBACK_DAYS, len(sessions))]


def apply_settings(overrides: dict) -> None:
    """Set CONFIG constants by name (used by the GUI). Unknown names raise."""
    g = globals()
    for name, value in overrides.items():
        if not name.isupper() or name not in g:
            raise KeyError(f"Unknown setting {name!r}")
        g[name] = value


def avg_daily_volume(bars: pd.DataFrame, spec: TfSpec, sessions: int = 20) -> float:
    """Average volume per session over the last N sessions of the fetched bars."""
    if bars.empty:
        return 0.0
    if spec.label == "1W":
        return float(bars["Volume"].tail(max(sessions // 5, 1)).mean() / 5)
    daily = bars["Volume"].groupby(bars.index.normalize()).sum()
    return float(daily.tail(sessions).mean())


def analyze_ticker(ticker: str, spec: TfSpec) -> dict:
    """Fetch, run the engine, and return signals inside the lookback window
    plus the last price and average daily volume (for GUI filters)."""
    bars = fetch_bars(ticker, spec)
    if bars.empty:
        return {"ticker": ticker, "signals": [], "price": None, "avg_volume": None, "error": "no data"}
    cutoff = lookback_cutoff(bars)
    sigs = [s for s in detect_factor_signals(bars, spec) if s["time"] >= cutoff]
    return {"ticker": ticker, "signals": sigs, "price": float(bars["Close"].iloc[-1]),
            "avg_volume": avg_daily_volume(bars, spec), "error": None}


def scan_ticker(ticker: str, spec: TfSpec) -> tuple[str, list[dict], str | None]:
    try:
        res = analyze_ticker(ticker, spec)
        return ticker, res["signals"], res["error"]
    except Exception as e:  # keep scanning the rest of the list
        return ticker, [], f"{type(e).__name__}: {e}"


def run_scan(tickers: list[str], spec: TfSpec) -> tuple[pd.DataFrame, dict[str, str]]:
    rows, errors = [], {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for ticker, sigs, err in pool.map(lambda t: scan_ticker(t, spec), tickers):
            if err:
                errors[ticker] = err
            for s in sigs:
                rows.append({
                    "Ticker": ticker,
                    "Signal Time": s["time"],
                    "Timeframe": spec.label,
                    "Direction": s["direction"],
                    "Factors": s["factors"],
                    "Score": s["score"],
                    "Seq": s["seq"],
                    "Close": round(float(s["close"]), 4),
                })
    df = pd.DataFrame(rows, columns=["Ticker", "Signal Time", "Timeframe", "Direction",
                                     "Factors", "Score", "Seq", "Close"])
    if not df.empty:
        df = df.sort_values(["Signal Time", "Ticker"], ascending=[False, True]).reset_index(drop=True)
    return df, errors


def format_table(df: pd.DataFrame, spec: TfSpec) -> str:
    if df.empty:
        return "No FACTOR CONT signals in the lookback window."
    out = df.copy()
    fmt = "%Y-%m-%d %H:%M" if spec.intraday else "%Y-%m-%d"
    out["Signal Time"] = out["Signal Time"].dt.strftime(fmt)
    return out.to_string(index=False)


def export_results(df: pd.DataFrame, fmt: str, spec: TfSpec) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = df.copy()
    out["Signal Time"] = out["Signal Time"].dt.tz_localize(None)  # Excel can't store tz-aware
    path = EXPORT_DIR / f"factor_signals_{spec.label}_{stamp}.{fmt}"
    if fmt == "xlsx":
        try:
            out.to_excel(path, index=False)
            return path
        except ImportError:
            print("openpyxl is not installed; writing CSV instead (pip install openpyxl).")
            path = path.with_suffix(".csv")
    out.to_csv(path, index=False)
    return path


def main(argv: list[str] | None = None) -> int:
    global LOOKBACK_DAYS, MATCH_MODE, REQUIRED_FACTORS, FACTOR_PAIRS
    ap = argparse.ArgumentParser(description="Scan tickers for FACTOR CONT signals (EMA + SMC Confluence).")
    ap.add_argument("--tickers", help="comma-separated list, e.g. AAPL,MSFT,FSLY")
    ap.add_argument("--file", help="text/CSV file with tickers")
    ap.add_argument("--timeframe", "-t", default=TIMEFRAME, help=f"bar timeframe (default {TIMEFRAME})")
    ap.add_argument("--lookback", "-l", type=int, default=LOOKBACK_DAYS, help=f"days to look back (default {LOOKBACK_DAYS})")
    ap.add_argument("--mode", choices=["any", "all", "pairs"], default=MATCH_MODE, help="factor match mode")
    ap.add_argument("--factors", help="required factors for any/all, e.g. MX or XML")
    ap.add_argument("--pairs", help="pairs for pairs mode, e.g. XM,XL")
    ap.add_argument("--export", choices=["csv", "xlsx"], default=EXPORT_FORMAT, help="also save results to a file")
    args = ap.parse_args(argv)

    LOOKBACK_DAYS = args.lookback
    MATCH_MODE = args.mode
    if args.factors:
        REQUIRED_FACTORS = set(args.factors.upper()) & set(FACTOR_ORDER)
    if args.pairs:
        FACTOR_PAIRS = [(p[0], p[1]) for p in args.pairs.upper().split(",") if len(p) == 2]

    spec = parse_timeframe(args.timeframe)
    tickers = load_tickers(args.tickers, args.file)
    rule = (" or ".join(f"{a}+{b}" for a, b in FACTOR_PAIRS) if MATCH_MODE == "pairs"
            else (" OR " if MATCH_MODE == "any" else " AND ").join(sorted(REQUIRED_FACTORS)))
    print(f"Scanning {len(tickers)} tickers | TF {spec.label} | last {LOOKBACK_DAYS} "
          f"{LOOKBACK_MODE} days | factor rule: {rule}\n")

    df, errors = run_scan(tickers, spec)
    print(format_table(df, spec))
    if errors:
        print("\nSkipped:", ", ".join(f"{t} ({e})" for t, e in errors.items()))
    if args.export and not df.empty:
        print(f"\nSaved: {export_results(df, args.export, spec)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
