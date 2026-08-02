"""
stage2_scan.py

Applies the "Stage 2 Rider" trend filter from WATCHLIST_CRITERIA.md against
today's gappers in packet.json. This is a mechanical quality gate, not a trade
setup and not a backtested rule set, so there's no conviction scoring here, just
pass or fail on all 8 boxes. A name only counts as a confirmed Stage 2 uptrend if
every box is checked, no partial credit.

Note on relative strength: a true IBD-style RS rating needs a full market
universe to rank against, which isn't available keyless. This script reports a
proxy instead, the stock's trailing 1 year return versus SPY's, plus whether the
RS line (ticker price divided by SPY price) has been rising over 6 and 13 weeks.
That's directional, not a real 1-99 percentile rank, the report says so.

Uses only free, keyless yfinance data. Writes STAGE2_RIDER_REPORT.md.
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
PACKET_PATH = os.path.join(HERE, "packet.json")
REPORT_PATH = os.path.join(HERE, "STAGE2_RIDER_REPORT.md")

TRADING_DAYS_1MO = 21
TRADING_DAYS_6WK = 30
TRADING_DAYS_13WK = 65
TRADING_DAYS_1YR = 252

CHECK_LABELS = [
    ("price_above_150_200", "Price above the 150-day and 200-day MAs"),
    ("150_above_200", "150-day MA above the 200-day MA"),
    ("200_trending_up_1mo", "200-day MA trending up for at least 1 month"),
    ("50_above_150_200", "50-day MA above both the 150-day and 200-day MAs"),
    ("above_25pct_of_52wk_low", "Price at least 25% above its 52-week low"),
    ("within_25pct_of_52wk_high", "Price within 25% of its 52-week high"),
    ("rs_outperforming_and_line_up", "Outperforming SPY over 1yr, RS line rising 6wk+"),
    ("price_above_50", "Price above the 50-day MA"),
]


def r(x, nd=2):
    try:
        return round(float(x), nd)
    except Exception:
        return None


def load_gappers():
    with open(PACKET_PATH, "r", encoding="utf-8") as f:
        packet = json.load(f)
    return packet.get("gappers", [])


def drop_todays_partial_bar(hist):
    if hist.empty:
        return hist
    today_local = datetime.now(ET).date()
    if hist.index[-1].date() == today_local:
        return hist.iloc[:-1]
    return hist


def trailing_return_pct(closes, days):
    if len(closes) <= days:
        return None
    end = float(closes.iloc[-1])
    start = float(closes.iloc[-1 - days])
    if start == 0:
        return None
    return (end - start) / start * 100


def fetch_spy_history():
    print("Fetching SPY history for the relative strength proxy...")
    hist = yf.Ticker("SPY").history(period="2y", interval="1d")
    return drop_todays_partial_bar(hist)


def analyze_ticker(ticker, live_price, spy_hist):
    result = {"ticker": ticker, "error": None}
    try:
        hist = yf.Ticker(ticker).history(period="2y", interval="1d")
        hist = drop_todays_partial_bar(hist)
        closes = hist["Close"].dropna()
        if len(closes) < 200 + TRADING_DAYS_1MO:
            result["error"] = "not enough daily history for a 200-day read"
            return result

        sma_50 = closes.tail(50).mean()
        sma_150 = closes.tail(150).mean()
        sma_200 = closes.tail(200).mean()
        sma_200_1mo_ago = closes.iloc[:-TRADING_DAYS_1MO].tail(200).mean()

        lookback = closes.tail(TRADING_DAYS_1YR)
        wk52_high = float(lookback.max())
        wk52_low = float(lookback.min())

        price = float(live_price) if live_price is not None else float(closes.iloc[-1])

        aligned = hist[["Close"]].join(spy_hist["Close"].rename("spy_close"), how="inner")
        rs_line = (aligned["Close"] / aligned["spy_close"]).dropna()
        rs_line_now = float(rs_line.iloc[-1]) if len(rs_line) else None
        rs_line_6wk_ago = float(rs_line.iloc[-1 - TRADING_DAYS_6WK]) if len(rs_line) > TRADING_DAYS_6WK else None
        rs_line_13wk_ago = float(rs_line.iloc[-1 - TRADING_DAYS_13WK]) if len(rs_line) > TRADING_DAYS_13WK else None
        rs_line_up_6wk = rs_line_now is not None and rs_line_6wk_ago is not None and rs_line_now > rs_line_6wk_ago
        rs_line_up_13wk = rs_line_now is not None and rs_line_13wk_ago is not None and rs_line_now > rs_line_13wk_ago

        ticker_1yr_return = trailing_return_pct(closes, min(TRADING_DAYS_1YR, len(closes) - 1))
        spy_closes = spy_hist["Close"].dropna()
        spy_1yr_return = trailing_return_pct(spy_closes, min(TRADING_DAYS_1YR, len(spy_closes) - 1))
        rs_vs_spy_pct = None
        if ticker_1yr_return is not None and spy_1yr_return is not None:
            rs_vs_spy_pct = ticker_1yr_return - spy_1yr_return

        checks = {
            "price_above_150_200": price > sma_150 and price > sma_200,
            "150_above_200": sma_150 > sma_200,
            "200_trending_up_1mo": sma_200 > sma_200_1mo_ago,
            "50_above_150_200": sma_50 > sma_150 and sma_50 > sma_200,
            "above_25pct_of_52wk_low": wk52_low > 0 and price >= wk52_low * 1.25,
            "within_25pct_of_52wk_high": wk52_high > 0 and price >= wk52_high * 0.75,
            "rs_outperforming_and_line_up": bool(rs_vs_spy_pct is not None and rs_vs_spy_pct > 0 and rs_line_up_6wk),
            "price_above_50": price > sma_50,
        }

        result.update({
            "price": r(price),
            "sma_50": r(sma_50),
            "sma_150": r(sma_150),
            "sma_200": r(sma_200),
            "sma_200_1mo_ago": r(sma_200_1mo_ago),
            "wk52_high": r(wk52_high),
            "wk52_low": r(wk52_low),
            "pct_above_52wk_low": r((price / wk52_low - 1) * 100, 1) if wk52_low else None,
            "pct_below_52wk_high": r((1 - price / wk52_high) * 100, 1) if wk52_high else None,
            "rs_vs_spy_pct_1yr": r(rs_vs_spy_pct, 1),
            "rs_line_up_6wk": rs_line_up_6wk,
            "rs_line_up_13wk": rs_line_up_13wk,
            "checks": checks,
            "checks_passed": sum(1 for v in checks.values() if v),
            "stage2_confirmed": all(checks.values()),
        })
    except Exception as e:
        result["error"] = str(e)
    return result


def format_check_cell(passed):
    return "PASS" if passed else "fail"


def build_report(analyses, generated_note):
    lines = []
    lines.append("# Stage 2 Rider Trend Filter Report")
    lines.append("")
    lines.append(f"### {generated_note}")
    lines.append("")
    lines.append(
        "> This is a quality gate, not a trade setup and not backtested. A name only "
        "counts as a confirmed Stage 2 uptrend if it clears all 8 boxes, one miss and "
        "it's out, no partial credit. Relative strength here is a proxy (trailing 1yr "
        "return vs SPY, plus RS line direction), not a true IBD-style 1-99 rank, that "
        "needs a full market universe this scan doesn't have. Not financial advice."
    )
    lines.append("")

    confirmed = [a for a in analyses if not a.get("error") and a.get("stage2_confirmed")]
    not_confirmed = [a for a in analyses if not a.get("error") and not a.get("stage2_confirmed")]
    errored = [a for a in analyses if a.get("error")]

    lines.append("## Summary")
    lines.append("")
    lines.append(f"{len(analyses)} tickers checked, from today's premarket gappers.")
    lines.append(f"{len(confirmed)} confirmed Stage 2 uptrends (all 8 boxes checked).")
    lines.append(f"{len(not_confirmed)} did not confirm.")
    if errored:
        lines.append(f"{len(errored)} could not be checked (not enough history or a fetch error).")
    lines.append("")

    lines.append("## Confirmed Stage 2 Uptrends")
    lines.append("")
    if confirmed:
        for a in confirmed:
            lines.append(f"- **{a['ticker']}**, 8 of 8 boxes checked, price {a['price']}, "
                          f"{a['pct_above_52wk_low']}% above its 52-week low, "
                          f"{a['pct_below_52wk_high']}% off its 52-week high, "
                          f"beating SPY by {a['rs_vs_spy_pct_1yr']} points over the trailing year.")
    else:
        lines.append("None of today's gappers confirmed all 8 boxes. That's normal, most "
                      "single-day earnings gaps haven't built a multi-month trend yet.")
    lines.append("")

    lines.append("## Full Checklist By Ticker")
    lines.append("")
    header = "| Ticker | " + " | ".join(label for _, label in CHECK_LABELS) + " | Boxes | Confirmed |"
    sep = "|---" * (len(CHECK_LABELS) + 3) + "|"
    lines.append(header)
    lines.append(sep)
    for a in analyses:
        if a.get("error"):
            row = f"| {a['ticker']} | " + " | ".join("n/a" for _ in CHECK_LABELS) + f" | - | error: {a['error']} |"
            lines.append(row)
            continue
        cells = [format_check_cell(a["checks"][key]) for key, _ in CHECK_LABELS]
        row = f"| {a['ticker']} | " + " | ".join(cells) + f" | {a['checks_passed']}/8 | {'YES' if a['stage2_confirmed'] else 'no'} |"
        lines.append(row)
    lines.append("")

    lines.append("## Raw Numbers")
    lines.append("")
    lines.append("| Ticker | Price | 50-SMA | 150-SMA | 200-SMA | 200-SMA 1mo ago | 52wk Low | 52wk High | RS vs SPY 1yr |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for a in analyses:
        if a.get("error"):
            continue
        lines.append(
            f"| {a['ticker']} | {a['price']} | {a['sma_50']} | {a['sma_150']} | {a['sma_200']} | "
            f"{a['sma_200_1mo_ago']} | {a['wk52_low']} | {a['wk52_high']} | {a['rs_vs_spy_pct_1yr']} |"
        )
    lines.append("")

    lines.append(
        "This filter only ever says whether a name qualifies to be considered for the "
        "swing watchlist. It doesn't pick entries, stops, or targets, and passing this "
        "screen doesn't override the swing_eligible rules in packet.json, both still "
        "have to line up before a name is actually tradeable."
    )

    return "\n".join(lines) + "\n"


def main():
    print("=== Stage 2 Rider scan starting ===")
    gappers = load_gappers()
    print(f"Loaded {len(gappers)} gappers from packet.json")

    spy_hist = fetch_spy_history()

    analyses = []
    for g in gappers:
        ticker = g["ticker"]
        print(f"  checking {ticker}...")
        analyses.append(analyze_ticker(ticker, g.get("price"), spy_hist))

    et_now = datetime.now(ET)
    generated_note = f"Run against today's gappers, {et_now.strftime('%Y-%m-%d %H:%M')} ET"
    report = build_report(analyses, generated_note)

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"=== Done, wrote {REPORT_PATH} ===")


if __name__ == "__main__":
    main()
