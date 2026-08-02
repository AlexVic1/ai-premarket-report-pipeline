"""
scan.py

Data gatherer for the premarket pipeline. Pulls raw market data only, into packet.json.
No conviction, no buckets, no opinions, that judgment happens later in AI prompts.

The only computed logic in here is the deterministic eligibility flags (day_eligible,
swing_eligible). Those are a direct code encoding of the fixed rules in
WATCHLIST_CRITERIA.md, not AI judgment, so they stay in scope for a "zero analysis" file.

Uses only free, keyless libraries: yfinance, feedparser, requests.
zoneinfo is stdlib on Python 3.9+.

Run with: python scan.py
Writes: packet.json in the current directory.
"""

import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf

feedparser.USER_AGENT = "Mozilla/5.0 (compatible; PremarketScanner/1.0)"

ET = ZoneInfo("America/New_York")

INSTRUMENTS = {
    "S&P 500": "^GSPC",
    "Dow": "^DJI",
    "Nasdaq": "^IXIC",
    "Russell 2000": "^RUT",
    "VIX": "^VIX",
    "US 10Y": "^TNX",
    "US 3M": "^IRX",
    "WTI Oil": "CL=F",
    "Dollar (DXY)": "DX-Y.NYB",
}

STATIC_UNIVERSE = [
    "NVDA", "AMD", "AVGO", "SMCI", "MRVL", "TSLA", "AAPL", "MSFT", "META", "AMZN",
    "GOOGL", "NFLX", "DELL", "SNOW", "PLTR", "COIN", "MSTR", "SOFI", "RIVN", "NIO",
    "MARA", "RIOT", "BA", "DIS", "JPM", "BAC", "XOM", "CVX", "HOOD", "UBER",
    "CRWD", "PANW", "CELH", "LULU", "NKE", "CAVA", "DKNG", "ARM", "INTC", "MU",
]

NEWS_FEEDS = {
    "MarketWatch Top": "http://feeds.marketwatch.com/marketwatch/topstories/",
    "MarketWatch RealTime": "http://feeds.marketwatch.com/marketwatch/realtimeheadlines/",
    "CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "Google News Markets": "https://news.google.com/rss/search?q=markets%20OR%20earnings%20when:1d&hl=en-US&gl=US&ceid=US:en",
}

SPAM_PATTERNS = [
    re.compile(r"price prediction", re.I),
    re.compile(r"\b20\d{2}-20\d{2}\b"),
]

# Generic company-name words that must never match a headline on their own. Words
# like "Applied" or "Digital" sit in dozens of unrelated company names (Applied
# Optoelectronics vs Applied Digital, for example), so treating them as a valid
# catalyst token would cross-match the wrong ticker. They only count when paired
# with the ticker itself or a genuinely distinctive word from the same name.
NAME_STOP = {
    "the", "inc", "corp", "corporation", "holdings", "technologies", "technology",
    "group", "digital", "applied", "advanced", "strategy", "strategies", "motors",
    "energy", "platforms", "systems", "industries", "international", "global",
    "solutions", "sciences", "therapeutics", "resources", "partners", "company",
    "co", "ltd", "plc", "class", "common", "stock", "shares", "enterprises",
    "brands", "labs", "laboratories", "capital", "financial", "industrial",
}

PRIMARY_PUBLISHERS = [
    "bloomberg", "reuters", "cnbc", "marketwatch", "barron", "yahoo finance",
    "wsj", "wall street journal", "associated press", "ap news", "financial times",
]

ECON_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ECON_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ff_calendar_cache.json")
ECON_CACHE_TTL_SECONDS = 4 * 60 * 60

GAP_MIN_PCT = 4.0
GAP_MIN_PRICE = 3.0
TOP_N_GAPPERS = 12


def r(x, nd=4):
    """Round to a plain python float, or return None if it can't be converted."""
    try:
        return round(float(x), nd)
    except Exception:
        return None


def get_fast_info_value(ticker_obj, *keys):
    try:
        fi = ticker_obj.fast_info
    except Exception:
        return None
    for key in keys:
        try:
            val = fi[key]
            if val is not None:
                return val
        except Exception:
            pass
        try:
            val = getattr(fi, key)
            if val is not None:
                return val
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# 1. Market snapshot
# ---------------------------------------------------------------------------

def fetch_market_snapshot():
    print("Fetching market snapshot...")
    snapshot = {}
    for name, symbol in INSTRUMENTS.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d", interval="1d")
            closes = hist["Close"].dropna()
            if len(closes) < 2:
                snapshot[name] = {"symbol": symbol, "error": "not enough data"}
                continue
            last = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])
            change_pct = (last - prev_close) / prev_close * 100 if prev_close else None
            snapshot[name] = {
                "symbol": symbol,
                "last": r(last),
                "prev_close": r(prev_close),
                "change_pct": r(change_pct, 2),
            }
            print(f"  {name}: last {r(last, 2)}, change {r(change_pct, 2)}%")
        except Exception as e:
            snapshot[name] = {"symbol": symbol, "error": str(e)}
            print(f"  {name}: failed, {e}")
    return snapshot


# ---------------------------------------------------------------------------
# 2. Live top movers, with static universe fallback
# ---------------------------------------------------------------------------

def fetch_screener_movers():
    quotes = []
    for screen_name in ("day_gainers", "most_actives"):
        try:
            try:
                result = yf.screen(screen_name, count=50)
            except TypeError:
                result = yf.screen(screen_name)
            found = result.get("quotes", []) if isinstance(result, dict) else []
            quotes.extend(found)
            print(f"  screener {screen_name}: {len(found)} quotes")
        except Exception as e:
            print(f"  screener {screen_name} failed: {e}")
    return quotes


def normalize_quote(q):
    symbol = q.get("symbol")
    if not symbol:
        return None
    price = q.get("regularMarketPrice")
    prev_close = q.get("regularMarketPreviousClose")
    gap_pct = q.get("regularMarketChangePercent")
    if gap_pct is None and price is not None and prev_close:
        try:
            gap_pct = (price - prev_close) / prev_close * 100
        except Exception:
            gap_pct = None
    return {
        "ticker": symbol,
        "name": q.get("shortName") or q.get("longName") or symbol,
        "price": r(price),
        "prev_close": r(prev_close),
        "gap_pct": r(gap_pct, 2),
        "market_cap": q.get("marketCap"),
        "volume": q.get("regularMarketVolume"),
    }


def fetch_static_universe_movers():
    movers = []
    for ticker in STATIC_UNIVERSE:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d")
            closes = hist["Close"].dropna()
            if len(closes) < 2:
                continue
            last = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])
            gap_pct = (last - prev_close) / prev_close * 100 if prev_close else None
            volume = None
            if "Volume" in hist and not hist["Volume"].dropna().empty:
                volume = int(hist["Volume"].dropna().iloc[-1])
            market_cap = get_fast_info_value(t, "marketCap", "market_cap")
            movers.append({
                "ticker": ticker,
                "name": ticker,
                "price": r(last),
                "prev_close": r(prev_close),
                "gap_pct": r(gap_pct, 2),
                "market_cap": market_cap,
                "volume": volume,
            })
        except Exception as e:
            print(f"  static universe {ticker} failed: {e}")
    return movers


def gather_candidates():
    print("Fetching live top movers...")
    raw = fetch_screener_movers()
    normalized = []
    seen = set()
    for q in raw:
        n = normalize_quote(q)
        if n and n["ticker"] not in seen:
            seen.add(n["ticker"])
            normalized.append(n)
    if len(normalized) >= 5:
        print(f"  using live screeners, {len(normalized)} unique names")
        return normalized, "live_screeners"
    print("  live screeners returned fewer than 5 names, falling back to static universe")
    fallback = fetch_static_universe_movers()
    return fallback, "static_universe_fallback"


# ---------------------------------------------------------------------------
# 3. Gap filter
# ---------------------------------------------------------------------------

def filter_gappers(movers):
    filtered = []
    for m in movers:
        gap = m.get("gap_pct")
        price = m.get("price")
        if gap is None or price is None:
            continue
        if abs(gap) >= GAP_MIN_PCT and price >= GAP_MIN_PRICE:
            filtered.append(m)
    filtered.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
    return filtered[:TOP_N_GAPPERS]


# ---------------------------------------------------------------------------
# 4. Market wide news
# ---------------------------------------------------------------------------

def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def is_spam(title):
    for pat in SPAM_PATTERNS:
        if pat.search(title or ""):
            return True
    return False


def fetch_market_news():
    print("Fetching market wide news...")
    items = []
    for source, url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            kept = 0
            for entry in feed.entries:
                title = (entry.get("title") or "").strip()
                if not title or is_spam(title):
                    continue
                summary = strip_html(entry.get("summary") or entry.get("description") or "")
                items.append({
                    "source": source,
                    "title": title,
                    "summary": summary,
                    "link": entry.get("link"),
                    "published": entry.get("published") or entry.get("updated") or "",
                })
                kept += 1
            print(f"  {source}: kept {kept} of {len(feed.entries)} entries")
        except Exception as e:
            print(f"  {source} failed: {e}")
    return items


# ---------------------------------------------------------------------------
# 5. Economic calendar
# ---------------------------------------------------------------------------

def load_econ_cache():
    try:
        if os.path.exists(ECON_CACHE_FILE):
            with open(ECON_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def save_econ_cache(raw_events):
    try:
        payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "events": raw_events}
        with open(ECON_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def fetch_econ_calendar():
    print("Fetching economic calendar...")
    base = {
        "source": ECON_CALENDAR_URL,
        "filter": "USD, High impact",
        "today_date": None,
        "tomorrow_date": None,
        "today": [],
        "tomorrow": [],
    }
    try:
        raw_events = None
        used_cache = False
        note = None

        cache = load_econ_cache()
        if cache:
            try:
                fetched_at = datetime.fromisoformat(cache["fetched_at"])
                age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
                if age < ECON_CACHE_TTL_SECONDS:
                    raw_events = cache.get("events")
                    used_cache = True
                    print(f"  using cached calendar, {int(age)}s old")
            except Exception:
                pass

        if raw_events is None:
            try:
                resp = requests.get(
                    ECON_CALENDAR_URL,
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; PremarketScanner/1.0)"},
                )
                resp.raise_for_status()
                raw_events = resp.json()
                save_econ_cache(raw_events)
                print(f"  live fetch ok, {len(raw_events)} raw events")
            except Exception as e:
                print(f"  live econ calendar fetch failed: {e}")
                if cache and cache.get("events"):
                    raw_events = cache["events"]
                    note = "live fetch failed, used last cached week"
                else:
                    base["error"] = f"live fetch failed and no cache available: {e}"
                    return base

        today_date = datetime.now(ET).date()
        tomorrow_date = today_date + timedelta(days=1)
        today_events = []
        tomorrow_events = []

        for ev in raw_events or []:
            if ev.get("country") != "USD" or ev.get("impact") != "High":
                continue
            date_str = ev.get("date")
            if not date_str:
                continue
            try:
                dt = datetime.fromisoformat(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ET)
                dt_et = dt.astimezone(ET)
            except Exception:
                continue
            record = {
                "time_et": dt_et.strftime("%H:%M"),
                "title": ev.get("title"),
                "forecast": ev.get("forecast"),
                "previous": ev.get("previous"),
            }
            if dt_et.date() == today_date:
                today_events.append((dt_et, record))
            elif dt_et.date() == tomorrow_date:
                tomorrow_events.append((dt_et, record))

        today_events.sort(key=lambda x: x[0])
        tomorrow_events.sort(key=lambda x: x[0])

        result = dict(base)
        result["today_date"] = str(today_date)
        result["tomorrow_date"] = str(tomorrow_date)
        result["today"] = [rec for _, rec in today_events]
        result["tomorrow"] = [rec for _, rec in tomorrow_events]
        if used_cache:
            result["note"] = "served from local cache, within TTL"
        elif note:
            result["note"] = note
        print(f"  econ calendar: {len(result['today'])} today, {len(result['tomorrow'])} tomorrow")
        return result
    except Exception as e:
        base["error"] = f"econ calendar failed: {e}"
        return base


# ---------------------------------------------------------------------------
# 6. Per gapper enrichment
# ---------------------------------------------------------------------------

def build_company_tokens(name, ticker):
    tokens = set()
    if ticker:
        tokens.add(ticker.upper())
    if name:
        for word in re.split(r"[^A-Za-z0-9]+", name):
            w = word.strip()
            # Require 4+ letters so a token is distinctive enough to stand alone.
            # Short generic fragments are the ones most likely to land in another
            # company's name too.
            if len(w) < 4:
                continue
            if w.lower() in NAME_STOP:
                continue
            tokens.add(w)
    return tokens


def headline_matches_ticker(title, ticker, tokens):
    if not title:
        return False
    if re.search(r"\b" + re.escape(ticker) + r"\b", title):
        return True
    for tok in tokens:
        if tok.upper() == ticker.upper():
            continue
        if re.search(r"\b" + re.escape(tok) + r"\b", title, re.I):
            return True
    return False


def rank_publisher(name):
    if not name:
        return 99
    low = name.lower()
    for i, pub in enumerate(PRIMARY_PUBLISHERS):
        if pub in low:
            return i
    return 99


def fetch_catalyst_headlines(ticker, name, market_news):
    headlines = []
    try:
        t = yf.Ticker(ticker)
        for item in (t.news or [])[:10]:
            content = item.get("content") or {}
            title = item.get("title") or content.get("title")
            publisher = item.get("publisher") or (content.get("provider") or {}).get("displayName")
            link = item.get("link") or (content.get("canonicalUrl") or {}).get("url")
            if title:
                headlines.append({"title": title, "publisher": publisher, "link": link, "source": "yfinance"})
    except Exception as e:
        print(f"    yfinance news failed for {ticker}: {e}")

    tokens = build_company_tokens(name, ticker)
    for n in market_news:
        if headline_matches_ticker(n["title"], ticker, tokens):
            headlines.append({"title": n["title"], "publisher": n["source"], "link": n["link"], "source": "rss"})

    seen_titles = set()
    deduped = []
    for h in headlines:
        key = (h["title"] or "").strip().lower()
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        deduped.append(h)

    deduped.sort(key=lambda h: rank_publisher(h.get("publisher")))
    return deduped


def fetch_intraday_levels(ticker):
    empty = {"vwap": None, "hod": None, "lod": None, "premarket_high": None, "premarket_volume": None}
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="5m", prepost=True)
        if hist.empty:
            empty["error"] = "no intraday bars"
            return empty

        idx = hist.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        idx_et = idx.tz_convert(ET)

        typical_price = (hist["High"] + hist["Low"] + hist["Close"]) / 3
        vol = hist["Volume"].fillna(0)
        total_vol = float(vol.sum())
        vwap = float((typical_price * vol).sum() / total_vol) if total_vol > 0 else None

        hod = float(hist["High"].max())
        lod = float(hist["Low"].min())

        premarket_mask = [(t.hour, t.minute) < (9, 30) for t in idx_et]
        pm_high, pm_volume = None, None
        if any(premarket_mask):
            pm_highs = hist["High"][premarket_mask]
            pm_vols = hist["Volume"][premarket_mask]
            pm_high = float(pm_highs.max()) if len(pm_highs) else None
            pm_volume = float(pm_vols.sum()) if len(pm_vols) else None

        return {
            "vwap": r(vwap),
            "hod": r(hod),
            "lod": r(lod),
            "premarket_high": r(pm_high),
            "premarket_volume": pm_volume,
        }
    except Exception as e:
        empty["error"] = str(e)
        return empty


def fetch_daily_metrics(ticker):
    empty = {"sma_200": None, "prior_day_high": None, "prior_close": None, "today_open": None, "avg_volume_20": None}
    try:
        hist = yf.Ticker(ticker).history(period="1y", interval="1d")
        if hist.empty:
            empty["error"] = "no daily bars"
            return empty

        today_local = datetime.now(ET).date()
        last_date = hist.index[-1].date()

        today_open = None
        if last_date == today_local:
            today_open = float(hist["Open"].iloc[-1])
            hist_prior = hist.iloc[:-1]
        else:
            hist_prior = hist

        if hist_prior.empty:
            empty["today_open"] = r(today_open)
            empty["error"] = "not enough prior history"
            return empty

        return {
            "sma_200": r(hist_prior["Close"].tail(200).mean()),
            "prior_day_high": r(hist_prior["High"].iloc[-1]),
            "prior_close": r(hist_prior["Close"].iloc[-1]),
            "today_open": r(today_open),
            "avg_volume_20": r(hist_prior["Volume"].tail(20).mean(), 2),
        }
    except Exception as e:
        empty["error"] = str(e)
        return empty


def fetch_next_earnings_date(ticker):
    try:
        t = yf.Ticker(ticker)
        try:
            cal = t.calendar
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date")
                if dates:
                    return str(dates[0])
        except Exception:
            pass
        try:
            df = t.get_earnings_dates(limit=4)
            if df is not None and not df.empty:
                return str(df.index[0].date())
        except Exception:
            pass
    except Exception as e:
        print(f"    earnings date failed for {ticker}: {e}")
    return None


def compute_eligibility(gapper, daily, rvol, catalyst_found):
    """Pure rule checks from WATCHLIST_CRITERIA.md, no judgment involved."""
    price = gapper.get("price")
    gap = gapper.get("gap_pct")
    market_cap = gapper.get("market_cap")
    prior_high = daily.get("prior_day_high")
    today_open = daily.get("today_open")
    sma_200 = daily.get("sma_200")

    def gt(a, b):
        return a is not None and b is not None and a > b

    day_eligible = bool(
        gap is not None and gap > 3
        and price is not None and price > 3
        and market_cap is not None and market_cap > 1_000_000_000
        and rvol is not None and rvol > 1.5
        and gt(price, prior_high)
    )

    swing_eligible = bool(
        gap is not None and gap >= 8
        and price is not None and price > 3
        and gt(today_open, prior_high)
        and gt(today_open, sma_200)
        and market_cap is not None and market_cap >= 800_000_000
        and catalyst_found
    )

    return day_eligible, swing_eligible


def enrich_gapper(gapper, market_news):
    ticker = gapper["ticker"]
    print(f"  enriching {ticker}...")

    catalyst_headlines = fetch_catalyst_headlines(ticker, gapper.get("name"), market_news)
    catalyst_found = len(catalyst_headlines) > 0

    intraday = fetch_intraday_levels(ticker)
    daily = fetch_daily_metrics(ticker)

    if daily.get("today_open") is None:
        open_px = get_fast_info_value(yf.Ticker(ticker), "open", "regularMarketOpen")
        if open_px:
            daily["today_open"] = r(open_px)

    avg_vol_20 = daily.get("avg_volume_20")
    today_volume = gapper.get("volume")
    # yfinance reports roughly 0 volume during premarket hours, so this is full
    # trading day relative volume, not true premarket RVOL. A true premarket RVOL
    # needs a premarket data feed such as Alpaca. This is the keyless stand-in.
    rvol = (today_volume / avg_vol_20) if (today_volume and avg_vol_20) else None

    next_earnings = fetch_next_earnings_date(ticker)
    day_eligible, swing_eligible = compute_eligibility(gapper, daily, rvol, catalyst_found)

    result = dict(gapper)
    result.update({
        "catalyst_found": catalyst_found,
        "catalyst_headlines": catalyst_headlines,
        "intraday": intraday,
        "daily": daily,
        "rvol": r(rvol, 2),
        "next_earnings_date": next_earnings,
        "day_eligible": day_eligible,
        "swing_eligible": swing_eligible,
    })
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Premarket scan starting ===")
    generated_at = datetime.now(timezone.utc).isoformat()

    market_snapshot = fetch_market_snapshot()

    candidates, candidate_source = gather_candidates()
    print(f"Candidate source: {candidate_source}, {len(candidates)} names before filtering")

    gappers_raw = filter_gappers(candidates)
    print(f"Gap filter kept {len(gappers_raw)} names (gap >= {GAP_MIN_PCT}%, price >= ${GAP_MIN_PRICE})")

    market_news = fetch_market_news()
    econ_calendar = fetch_econ_calendar()

    print("Enriching gappers...")
    gappers = []
    for g in gappers_raw:
        try:
            gappers.append(enrich_gapper(g, market_news))
        except Exception as e:
            print(f"  failed to enrich {g.get('ticker')}: {e}")
            g["enrichment_error"] = str(e)
            gappers.append(g)

    et_now = datetime.now(ET)
    trading_day_note = f"Scan run at {et_now.strftime('%Y-%m-%d %H:%M')} ET, {et_now.strftime('%A')}"

    packet = {
        "generated_at": generated_at,
        "candidate_source": candidate_source,
        "trading_day_note": trading_day_note,
        "scan_params": {
            "gap_min_pct": GAP_MIN_PCT,
            "gap_min_price": GAP_MIN_PRICE,
            "top_n_gappers": TOP_N_GAPPERS,
        },
        "criteria": {
            "day_trading_trend_join_long": (
                "Gap over 3 percent, price over 3 dollars, market cap over 1 billion, "
                "premarket RVOL over 1.5, price breaking above yesterday's high. "
                "Intraday window is 10am to 3:30pm ET, trigger is price above premarket high "
                "and above the prior high of day, stop is 1 percent below premarket high or "
                "the low of day whichever is lower, that's 1R. Scale 1/3 off at 1R and 1/3 at "
                "2R, trail the last 1/3 on the 21-EMA, flat by 3:51pm."
            ),
            "swing_watchlist": (
                "Gap of 8 percent or more, price over 3 dollars, open above yesterday's high, "
                "open above the 200-day SMA, market cap of 800 million or more, and a real "
                "catalyst such as earnings on the gap day or news with no earnings attached. "
                "Entry and exit management is still being built, so these are starter ideas only."
            ),
        },
        "market_snapshot": market_snapshot,
        "econ_calendar": econ_calendar,
        "gappers": gappers,
        "market_news": market_news[:20],
        "gaps_to_fill": [
            "Market wide earnings calendar is only partial, this packet has a next earnings "
            "date per ticker, not a full earnings calendar for the week.",
            "Intraday levels (VWAP, HOD, LOD, premarket high) come from yfinance 5 minute bars, "
            "not a dedicated intraday feed, so gaps in bar coverage are possible.",
            "RVOL here is full trading day relative volume, not true premarket RVOL, because "
            "yfinance reports roughly 0 volume premarket. A true premarket RVOL needs a "
            "premarket data feed such as Alpaca.",
        ],
    }

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packet.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2, default=str)

    print(f"=== Done, wrote {out_path} with {len(gappers)} gappers ===")


if __name__ == "__main__":
    main()
