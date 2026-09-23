"""
Ticker universes (index / sector presets) and ticker -> sector lookup for the
FACTOR CONT screener GUI.

S&P 500 (GICS sectors) and Nasdaq-100 (ICB industries) constituents are pulled
from Wikipedia and cached for CACHE_DAYS. If that fails and there's no cache,
small built-in fallback lists are used so the app still works offline.
Sectors for tickers outside both indexes come from yfinance and are cached too.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

HERE = Path(__file__).resolve().parent
UNIVERSE_CACHE = HERE / "cache" / "universe_cache.json"
SECTOR_CACHE = HERE / "cache" / "sector_cache.json"
CACHE_DAYS = 7

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (factor-screener; personal use)"}

MEGACAP_TECH = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "ORCL", "NFLX", "AMD", "PLTR"]
EXTRA_SEMIS = ["TSM", "ASML", "ARM", "SMCI", "MRVL", "ONTO", "COHR", "WOLF", "CRUS", "SITM"]

_FALLBACK_SP500 = {
    "AAPL": "Information Technology", "MSFT": "Information Technology", "NVDA": "Information Technology",
    "AVGO": "Information Technology", "AMD": "Information Technology", "ORCL": "Information Technology",
    "CRM": "Information Technology", "ADBE": "Information Technology", "QCOM": "Information Technology",
    "TXN": "Information Technology", "MU": "Information Technology", "AMAT": "Information Technology",
    "GOOGL": "Communication Services", "META": "Communication Services", "NFLX": "Communication Services",
    "DIS": "Communication Services", "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "MCD": "Consumer Discretionary", "NKE": "Consumer Discretionary",
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials", "GS": "Financials", "MS": "Financials",
    "V": "Financials", "MA": "Financials", "BRK-B": "Financials", "XOM": "Energy", "CVX": "Energy",
    "COP": "Energy", "SLB": "Energy", "EOG": "Energy", "UNH": "Health Care", "LLY": "Health Care",
    "JNJ": "Health Care", "ABBV": "Health Care", "MRK": "Health Care", "PFE": "Health Care",
    "CAT": "Industrials", "GE": "Industrials", "BA": "Industrials", "HON": "Industrials", "UPS": "Industrials",
    "PG": "Consumer Staples", "KO": "Consumer Staples", "PEP": "Consumer Staples", "WMT": "Consumer Staples",
    "COST": "Consumer Staples", "LIN": "Materials", "FCX": "Materials", "NEE": "Utilities", "DUK": "Utilities",
    "PLD": "Real Estate", "AMT": "Real Estate",
}
_FALLBACK_SEMI_SUBIND = {"NVDA", "AVGO", "AMD", "QCOM", "TXN", "MU", "AMAT"}

# Yahoo's sector names -> GICS names, so custom tickers group with the index ones
YAHOO_TO_GICS = {
    "Technology": "Information Technology", "Healthcare": "Health Care",
    "Financial Services": "Financials", "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples", "Basic Materials": "Materials",
}

_lock = threading.Lock()


def _yf_symbol(sym: str) -> str:
    return str(sym).strip().upper().replace(".", "-")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _fetch_tables(url: str) -> list[pd.DataFrame]:
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def _download_universe() -> dict:
    sp = next(t for t in _fetch_tables(SP500_URL) if "Symbol" in t.columns and "GICS Sector" in t.columns)
    ndx = next(t for t in _fetch_tables(NDX_URL) if "Ticker" in t.columns)
    ind_col = next((c for c in ndx.columns if str(c).startswith("ICB Industry")), None)
    return {
        "fetched": time.time(),
        "sp500": {_yf_symbol(r["Symbol"]): {"sector": r["GICS Sector"], "sub": r["GICS Sub-Industry"]}
                  for _, r in sp.iterrows()},
        "ndx": {_yf_symbol(r["Ticker"]): (str(r[ind_col]) if ind_col else "") for _, r in ndx.iterrows()},
    }


def load_universe(force_refresh: bool = False) -> tuple[dict, str]:
    """Return (universe, source) where source is 'cache', 'web', or 'built-in'."""
    cached = _read_json(UNIVERSE_CACHE)
    fresh = cached and time.time() - cached.get("fetched", 0) < CACHE_DAYS * 86400
    if fresh and not force_refresh:
        return cached, "cache"
    try:
        data = _download_universe()
        _write_json(UNIVERSE_CACHE, data)
        return data, "web"
    except Exception:
        if cached:
            return cached, "cache (stale)"
        return {
            "fetched": 0,
            "sp500": {t: {"sector": s, "sub": "Semiconductors" if t in _FALLBACK_SEMI_SUBIND else ""}
                      for t, s in _FALLBACK_SP500.items()},
            "ndx": {t: "" for t in _FALLBACK_SP500 if t in MEGACAP_TECH},
        }, "built-in"


def preset_names(universe: dict) -> list[str]:
    sectors = sorted({v["sector"] for v in universe["sp500"].values()})
    return (["Custom list only", "S&P 500", "Nasdaq 100", "All US (S&P 500 + Nasdaq 100)",
             "MegaCap Tech", "Semiconductors"] + [f"Sector: {s}" for s in sectors])


def preset_tickers(name: str, universe: dict) -> list[str]:
    sp, ndx = universe["sp500"], universe["ndx"]
    if name == "S&P 500":
        return sorted(sp)
    if name == "Nasdaq 100":
        return sorted(ndx)
    if name.startswith("All US"):
        return sorted(set(sp) | set(ndx))
    if name == "MegaCap Tech":
        return list(MEGACAP_TECH)
    if name == "Semiconductors":
        semis = {t for t, v in sp.items() if "Semiconductor" in str(v.get("sub", ""))}
        semis |= {t for t, ind in ndx.items() if "Semiconductor" in ind}
        return sorted(semis | set(EXTRA_SEMIS))
    if name.startswith("Sector: "):
        sector = name[len("Sector: "):]
        return sorted(t for t, v in sp.items() if v["sector"] == sector)
    return []


def sector_of(ticker: str, universe: dict, allow_network: bool = True) -> str:
    """GICS sector from the S&P table, else ICB industry, else yfinance (cached)."""
    t = _yf_symbol(ticker)
    if t in universe["sp500"]:
        return universe["sp500"][t]["sector"]
    with _lock:
        cache = _read_json(SECTOR_CACHE)
    if t in cache:
        return cache[t]
    if not allow_network:
        return universe["ndx"].get(t, "") or ""
    try:
        sector = yf.Ticker(t).info.get("sector") or ""
        sector = YAHOO_TO_GICS.get(sector, sector) or universe["ndx"].get(t, "") or ""
    except Exception:
        sector = universe["ndx"].get(t, "") or ""
    if sector:
        with _lock:
            cache = _read_json(SECTOR_CACHE)
            cache[t] = sector
            _write_json(SECTOR_CACHE, cache)
    return sector
