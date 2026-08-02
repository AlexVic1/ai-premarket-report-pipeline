"""
finviz_sector_scan.py

Automates the parts of the "FinViz Sector Scan" candidate generator from
WATCHLIST_CRITERIA.md that can actually be scripted keyless. The real thing runs
on FinViz's website and ends with a human looking at every chart before a name
earns a watchlist spot, this script does NOT replace that step, it just narrows
the field and says plainly what still needs eyes on it.

What's automated:
- Sector leadership: ranks the 11 SPDR sector ETFs by trailing 1-month return
  and picks the top one as "the sector that's actually leading right now,"
  instead of picking one randomly.
- The mechanical FinViz filters: market cap mid and up ($2B+), average volume
  over 300,000 (20-day average, this pipeline's existing convention), price in
  $5-$50 (and a second pass at $50-$100 for the advanced version), Average True
  Range over 0.75, sorted by market cap high to low.

What's NOT automated, and never claimed to be:
- The chart check. Every candidate this script surfaces still needs a human to
  click into the chart and read the pattern before it goes anywhere near a
  watchlist.
- Anything that clears this scan still has to pass the Stage 2 filter and/or the
  day/swing eligibility rules before it's actually tradeable.

Uses only free, keyless yfinance data. Writes FINVIZ_SECTOR_SCAN_REPORT.md.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from scan import STATIC_UNIVERSE

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(HERE, "FINVIZ_SECTOR_SCAN_REPORT.md")

SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financial Services",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLY": "Consumer Cyclical",
    "XLP": "Consumer Defensive",
    "XLI": "Industrials",
    "XLB": "Basic Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

MARKET_CAP_MIN = 2_000_000_000  # FinViz "Mid and up" bucket starts at $2B
AVG_VOLUME_MIN = 300_000
ATR_MIN = 0.75
PRICE_RANGES = {
    "primary ($5-$50)": (5.0, 50.0),
    "advanced ($50-$100)": (50.0, 100.0),
}


def r(x, nd=2):
    try:
        return round(float(x), nd)
    except Exception:
        return None


def fetch_sector_leadership():
    print("Ranking sector ETFs by trailing 1-month return...")
    rows = []
    for etf, sector_name in SECTOR_ETFS.items():
        try:
            hist = yf.Ticker(etf).history(period="4mo", interval="1d")
            closes = hist["Close"].dropna()
            if len(closes) < 65:
                rows.append({"etf": etf, "sector": sector_name, "error": "not enough history"})
                continue
            last = float(closes.iloc[-1])
            mo1 = float(closes.iloc[-22]) if len(closes) > 22 else None
            mo3 = float(closes.iloc[-64]) if len(closes) > 64 else None
            ret_1mo = (last - mo1) / mo1 * 100 if mo1 else None
            ret_3mo = (last - mo3) / mo3 * 100 if mo3 else None
            rows.append({
                "etf": etf,
                "sector": sector_name,
                "ret_1mo": r(ret_1mo),
                "ret_3mo": r(ret_3mo),
                "error": None,
            })
            print(f"  {etf} ({sector_name}): 1mo {r(ret_1mo)}%, 3mo {r(ret_3mo)}%")
        except Exception as e:
            rows.append({"etf": etf, "sector": sector_name, "error": str(e)})
            print(f"  {etf} failed: {e}")

    ranked = sorted(
        [row for row in rows if row.get("ret_1mo") is not None],
        key=lambda x: x["ret_1mo"],
        reverse=True,
    )
    return rows, ranked


def compute_atr14(hist):
    high = hist["High"]
    low = hist["Low"]
    prev_close = hist["Close"].shift(1)
    tr = (high - low).combine((high - prev_close).abs(), max).combine((low - prev_close).abs(), max)
    atr = tr.rolling(14).mean()
    if atr.dropna().empty:
        return None
    return float(atr.dropna().iloc[-1])


def screen_ticker(ticker, leading_sector):
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="6mo", interval="1d")
        if hist.empty or len(hist) < 20:
            return None

        price = float(hist["Close"].dropna().iloc[-1])
        avg_volume_20 = float(hist["Volume"].dropna().tail(20).mean())
        atr14 = compute_atr14(hist)

        try:
            info = t.info
        except Exception:
            info = {}
        sector = info.get("sector")
        market_cap = info.get("marketCap")

        return {
            "ticker": ticker,
            "sector": sector,
            "price": r(price),
            "avg_volume_20": r(avg_volume_20, 0),
            "atr14": r(atr14),
            "market_cap": market_cap,
            "sector_match": sector == leading_sector,
            "market_cap_ok": market_cap is not None and market_cap >= MARKET_CAP_MIN,
            "avg_volume_ok": avg_volume_20 >= AVG_VOLUME_MIN,
            "atr_ok": atr14 is not None and atr14 >= ATR_MIN,
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


def build_sector_top5(screened):
    """Top 5 by market cap, per sector, split into <=$50 and >$50 buckets.

    This covers every sector represented in the fixed universe, not just the
    leading one. The universe is only 40 tickers spread across 11 sectors, so
    most sector/bucket combos will have fewer than 5 names, that's the universe
    being honest about its own size, not a bug.
    """
    valid = [s for s in screened if s and not s.get("error") and s.get("sector") and s.get("price") is not None]
    by_sector = {}
    for s in valid:
        by_sector.setdefault(s["sector"], []).append(s)

    lines = []
    lines.append("## Top 5 By Sector")
    lines.append("")
    lines.append("Every sector represented in the fixed universe, ranked by market cap, "
                  "split into stocks priced up to $50 and stocks priced over $50. This is "
                  "informational, it is not filtered by the cap/volume/ATR screen above, "
                  "the Cap/Vol/ATR OK columns show whether a name would also clear that "
                  "screen. Still needs the chart check before any of this touches a watchlist.")
    lines.append("")

    for sector in sorted(by_sector.keys()):
        names = by_sector[sector]
        low_bucket = sorted([n for n in names if n["price"] <= 50], key=lambda n: n["market_cap"] or 0, reverse=True)[:5]
        high_bucket = sorted([n for n in names if n["price"] > 50], key=lambda n: n["market_cap"] or 0, reverse=True)[:5]

        lines.append(f"### {sector}")
        lines.append("")

        lines.append("Up to $50:")
        lines.append("")
        if low_bucket:
            lines.append("| Ticker | Price | Market Cap | Avg Vol (20d) | ATR14 | Cap OK | Vol OK | ATR OK |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for m in low_bucket:
                cap_b = m["market_cap"] / 1e9 if m["market_cap"] else None
                cap_str = f"${cap_b:.2f}B" if cap_b is not None else "n/a"
                lines.append(
                    f"| {m['ticker']} | {m['price']} | {cap_str} | {int(m['avg_volume_20']):,} | "
                    f"{m['atr14']} | {'yes' if m['market_cap_ok'] else 'no'} | "
                    f"{'yes' if m['avg_volume_ok'] else 'no'} | {'yes' if m['atr_ok'] else 'no'} |"
                )
        else:
            lines.append(f"None in this fixed universe, {sector} has no names priced $50 or under here.")
        lines.append("")

        lines.append("Over $50:")
        lines.append("")
        if high_bucket:
            lines.append("| Ticker | Price | Market Cap | Avg Vol (20d) | ATR14 | Cap OK | Vol OK | ATR OK |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for m in high_bucket:
                cap_b = m["market_cap"] / 1e9 if m["market_cap"] else None
                cap_str = f"${cap_b:.2f}B" if cap_b is not None else "n/a"
                lines.append(
                    f"| {m['ticker']} | {m['price']} | {cap_str} | {int(m['avg_volume_20']):,} | "
                    f"{m['atr14']} | {'yes' if m['market_cap_ok'] else 'no'} | "
                    f"{'yes' if m['avg_volume_ok'] else 'no'} | {'yes' if m['atr_ok'] else 'no'} |"
                )
        else:
            lines.append(f"None in this fixed universe, {sector} has no names priced over $50 here.")
        lines.append("")

    uncategorized = [s for s in screened if s and not s.get("error") and not s.get("sector")]
    if uncategorized:
        lines.append("Couldn't tag a sector for: " + ", ".join(s["ticker"] for s in uncategorized))
        lines.append("")

    no_coverage = sorted(set(SECTOR_ETFS.values()) - set(by_sector.keys()))
    if no_coverage:
        lines.append(f"Zero coverage in this fixed universe: {', '.join(no_coverage)}. "
                      f"These sectors just aren't represented by any of the 40 tickers "
                      f"scan.py's static universe carries, not a screening failure.")
        lines.append("")

    return lines


def build_report(leaderboard_rows, ranked, leading, screened, generated_note):
    lines = []
    lines.append("# FinViz Sector Scan Candidate Report")
    lines.append("")
    lines.append(f"### {generated_note}")
    lines.append("")
    lines.append(
        "> This is the candidate funnel, not a trade setup and not backtested. It "
        "narrows a universe down by sector leadership and FinViz-style filters, it "
        "does not approve anything for a watchlist. The chart check is a human step "
        "and it did not happen here, nothing below has been eyeballed. Anything that "
        "clears this scan still needs to pass the Stage 2 filter and/or the day or "
        "swing eligibility rules before it's actually tradeable. Not financial advice."
    )
    lines.append("")

    lines.append("## Sector Leaderboard")
    lines.append("")
    lines.append("Ranked by trailing 1-month return, this is the \"pick the sector that's "
                  "actually leading right now\" step, not a random pick.")
    lines.append("")
    lines.append("| Rank | Sector | ETF | 1mo Return | 3mo Return |")
    lines.append("|---|---|---|---|---|")
    for i, row in enumerate(ranked, start=1):
        lines.append(f"| {i} | {row['sector']} | {row['etf']} | {row['ret_1mo']}% | {row['ret_3mo']}% |")
    errored = [row for row in leaderboard_rows if row.get("error")]
    if errored:
        lines.append("")
        lines.append("Couldn't rank: " + ", ".join(f"{row['etf']} ({row['error']})" for row in errored))
    lines.append("")

    if leading:
        lines.append(f"**Leading sector: {leading['sector']} ({leading['etf']}), "
                      f"{leading['ret_1mo']}% over the trailing month.**")
    else:
        lines.append("**Could not determine a leading sector, sector screen skipped.**")
    lines.append("")

    lines.append("## Screen Applied")
    lines.append("")
    lines.append(f"- Market cap: mid and up (${MARKET_CAP_MIN / 1e9:.0f}B or more)")
    lines.append(f"- Average volume (20-day): over {AVG_VOLUME_MIN:,}")
    lines.append(f"- Average True Range (14-day): over {ATR_MIN}")
    lines.append(f"- Sector: matches the leading sector above" if leading else "- Sector filter skipped, no leader")
    lines.append("- Sorted by market cap, high to low")
    lines.append(f"- Universe screened: the {len(STATIC_UNIVERSE)} tickers in scan.py's static universe, "
                  "this is a fixed liquid-name list, not a full market scan")
    lines.append("")

    lines.extend(build_sector_top5(screened))

    sector_hits = [s for s in screened if s and not s.get("error") and s["sector_match"]]

    for label, (lo, hi) in PRICE_RANGES.items():
        lines.append(f"## Candidates, {label}")
        lines.append("")
        matches = [
            s for s in sector_hits
            if s["market_cap_ok"] and s["avg_volume_ok"] and s["atr_ok"]
            and s["price"] is not None and lo <= s["price"] <= hi
        ]
        matches.sort(key=lambda s: s["market_cap"] or 0, reverse=True)
        if matches:
            lines.append("| Ticker | Sector | Price | Market Cap | Avg Vol (20d) | ATR14 | Chart check |")
            lines.append("|---|---|---|---|---|---|---|")
            for m in matches:
                cap_b = m["market_cap"] / 1e9 if m["market_cap"] else None
                lines.append(
                    f"| {m['ticker']} | {m['sector']} | {m['price']} | "
                    f"${cap_b:.2f}B | {int(m['avg_volume_20']):,} | {m['atr14']} | pending, human step |"
                )
        else:
            lines.append(f"No matches in this price range today. There are {len(sector_hits)} "
                          f"name(s) in the leading sector inside this fixed universe, none landed "
                          f"in ${lo:.0f}-${hi:.0f} after the cap/volume/ATR checks, see the sector "
                          f"detail below for why.")
        lines.append("")

    if leading:
        lines.append(f"## {leading['sector']} Names In This Universe")
        lines.append("")
        lines.append("Every ticker in the fixed universe that's tagged to the leading sector, "
                      "price range checks included, so you can see exactly why each one did or "
                      "didn't make a candidate list above.")
        lines.append("")
        if sector_hits:
            lines.append("| Ticker | Price | Market Cap | Avg Vol (20d) | ATR14 | Cap OK | Vol OK | ATR OK | In a price window |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for m in sector_hits:
                cap_b = m["market_cap"] / 1e9 if m["market_cap"] else None
                in_window = any(lo <= (m["price"] or -1) <= hi for lo, hi in PRICE_RANGES.values())
                lines.append(
                    f"| {m['ticker']} | {m['price']} | ${cap_b:.2f}B | {int(m['avg_volume_20']):,} | "
                    f"{m['atr14']} | {'yes' if m['market_cap_ok'] else 'no'} | "
                    f"{'yes' if m['avg_volume_ok'] else 'no'} | {'yes' if m['atr_ok'] else 'no'} | "
                    f"{'yes' if in_window else 'no'} |"
                )
        else:
            lines.append("No tickers in this fixed universe are tagged to this sector at all, "
                          "the universe just doesn't have coverage there.")
        lines.append("")

    skipped = [s for s in screened if s and s.get("error")]
    if skipped:
        lines.append("## Couldn't Screen")
        lines.append("")
        for s in skipped:
            lines.append(f"- {s['ticker']}: {s['error']}")
        lines.append("")

    lines.append(
        "Every name above still needs the chart check, click into it and actually look "
        "before it goes on a watchlist. A tight rectangle means a channel, not a random "
        "consolidation, read it that way. Then run it through the Stage 2 filter and the "
        "day/swing eligibility rules, clearing this scan alone isn't enough."
    )

    return "\n".join(lines) + "\n"


def main():
    print("=== FinViz Sector Scan starting ===")
    leaderboard_rows, ranked = fetch_sector_leadership()
    leading = ranked[0] if ranked else None
    leading_sector = leading["sector"] if leading else None

    print(f"Leading sector: {leading_sector}")
    print(f"Screening {len(STATIC_UNIVERSE)} tickers...")
    screened = []
    for ticker in STATIC_UNIVERSE:
        print(f"  screening {ticker}...")
        screened.append(screen_ticker(ticker, leading_sector))

    et_now = datetime.now(ET)
    generated_note = f"Run at {et_now.strftime('%Y-%m-%d %H:%M')} ET"
    report = build_report(leaderboard_rows, ranked, leading, screened, generated_note)

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"=== Done, wrote {REPORT_PATH} ===")


if __name__ == "__main__":
    main()
