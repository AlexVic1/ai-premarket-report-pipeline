<!--
WATCHLIST_CRITERIA.md
Source of truth for the scanner. These two setups are backtested and validated.
Do not loosen or tighten the numbers below without re-running the backtest.
-->

# Watchlist Criteria

Two validated setups. The scanner's job is to find premarket names that pass every required condition, nothing more, nothing less. If a name is missing even one condition, it doesn't make the list.

---

## Day Trading Watchlist: "Trend Join Long"

Backtest: 54.6% win rate, profit factor 1.59, 280 trades.

### Premarket screen, all required

- Gap % vs prev close > 3%
- Price > $3
- Market cap > $1B
- Premarket RVOL > 1.5
- Price breaking above yesterday's high

If it doesn't clear all five, it's not a day trade candidate. No exceptions for a "good story."

### Intraday plan

- **Window:** 10:00am to 3:30pm ET. Nothing before, nothing after.
- **Trigger:** price > premarket high AND > prior high-of-day
- **Stop:** 1% below premarket high, or the LOD, whichever is lower. That's your 1R.
- **Scale out:** 1/3 off at +1R, 1/3 off at +2R
- **Runner:** trail the last 1/3 on the 21-EMA
- **Flat by:** 3:51pm, no holding into the close

---

## Swing Watchlist

Backtest: 57.6% win rate / PF 5.34 on news catalysts, 44.7% win rate / PF 2.57 on earnings catalysts.

### Premarket screen, all required

- Gap % >= 8%
- Price > $3
- Open > yesterday's high
- Open > 200-day SMA
- Market cap >= $800M
- Real catalyst: either earnings on the gap day, or news with no earnings attached

### Entry and exit

Entry and exit management is still being built out. Treat every swing name as a starter idea, not a full trade plan. No stops, no targets, don't make numbers up just to fill a table. When the management rules are ready, this section gets updated.

---

## Trend Filter: "Stage 2 Rider"

No backtest on this one yet, it's a fresh filter, not a standalone trade setup. Think of it as a quality gate you run before a name even gets considered for the swing watchlist.

### Selection criteria, all 8 required to call it a confirmed Stage 2 uptrend

- Price is above both the 150-day and 200-day moving averages
- The 150-day MA is above the 200-day MA
- The 200-day MA has been trending up for at least 1 month, ideally 4-5 months or more
- The 50-day MA is above both the 150-day and 200-day MAs
- Price is at least 25% above its 52-week low
- Price is within 25% of its 52-week high, closer is better
- Relative strength ranking is 70+, ideally in the 90s, and the RS line itself has been trending up for at least 6 weeks, ideally 13+
- Price is trading above the 50-day MA, meaning it's coming out of a base, not stuck in one

No entry, stop, or target logic here, this is a filter, not a trigger. A name only qualifies for the swing watchlist scanner if it clears all 8 boxes. One miss and it's out, no partial credit.

---

## Candidate Generator: "FinViz Sector Scan"

No backtest here either, this isn't a trade rule, it's the funnel that feeds candidates into everything above.

### Screening steps, FinViz

- Market cap: Mid and up
- Average volume > 300,000
- Price range: $5 to $50 (or $50 to $100 for the more advanced version)
- Technical filter: Average True Range > 0.75
- Filter down to the sector that's actually leading right now, pick this from a real sector rotation read, not randomly
- Sort results by market cap, high to low
- Click into the Charts tab on every candidate and actually look at the chart before it goes anywhere near the watchlist

This one's a human-in-the-loop step, not something the scanner auto-approves. The chart check matters, that's where you're eyeballing pattern and structure before a name earns a spot. A tight rectangle here means a channel, not a random consolidation, read it that way. Anything that clears this scan still has to pass the Stage 2 filter and/or the day or swing selection criteria before it's actually tradeable.

---

## Notes for the scanner build

- Both watchlist setups run off the premarket screen only. Confirm each condition with real data, don't eyeball it.
- Day trading setup has a full intraday management plan attached. Swing setup does not, yet, so don't force one.
- The Stage 2 filter and the FinViz scan aren't trade setups, don't score them or attach conviction to them the way the two watchlists get scored.
- The FinViz chart check is human-in-the-loop, the scanner surfaces candidates but a person still looks at the chart before anything earns a watchlist spot.
- On any chart label in this pipeline's analysis, an "Xxx" marking means a liquidity zone or area, not a placeholder and not a deleted label.
- These win rates and profit factors are backtest results, not a promise. Keep that framing anywhere this file's numbers get quoted downstream.
