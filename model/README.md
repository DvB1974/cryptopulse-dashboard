# BTC regime model

Rule-based trend and cycle model for bitcoin. It runs daily in GitHub Actions (`.github/workflows/btc-model.yml`) and writes `data/btc-model.json`.

## Data
- BTC and ETH daily candles: Coinbase Exchange (fallback Bitstamp), last closes cross-checked against Kraken
- Macro: FRED CSV (DGS2, DGS10, DGS30, NFCI, DCOILBRENTEU, DTWEXBGS, T10YIE). Each series is used from its approximate publication date (observation date + `MACRO_LAG_DAYS`), also in the history. The last good download is kept in `data/fred-cache.json` and used when FRED fails; the run then reports this in `errors`, and `model.macro.complete` is false when a series is missing or stale.
- ETF flows: `data/etf-flows.json` (Farside, refreshed by the ETF workflow in this repo)
- Fear & Greed: alternative.me

## Output
- `model`: trend checklist (weekly close vs 50W SMA, higher high, higher low, 50W slope), regime, scenario weights, short-term bias, cycle positions (4-year, weekly, daily) with timing bands, key levels, macro score, RSI
- `history`: daily model state for the last ~460 days, each day evaluated only with data up to that day (no look-ahead)
- `errors` lists any source that failed; on a total price-data failure the previous file is kept

## Rules (since 31 Oct 2026)
- Weekly close vs 50W SMA flips only on a close more than 2% beyond the average, or on 2 weekly closes in a row on the other side (`handrem_buffer`).
- Higher low counts only after an accepted higher high: the last 12% swing low is above the previous one, and the pullback low after the higher high stays above the low the move started from.
- Bull probability: 5 + 40 (50W) + 20 (higher high) + 15 (higher low) + 15 (50W rising) + 10 (monthly close above 10-month SMA) + 10 (close above 200-day SMA) + 2.5 per macro point - 5 after a close below the daily-cycle low; rounded to 5, capped 5-95. Exactly 50 keeps the previous base case.
- Backtest 2018-2026 (daily, no look-ahead): weekly Brier score for "weekly close above 50W SMA 13 weeks later" 0.162 (previous rules 0.180, always-62% 0.237); base-case flips 20 (previous 24). The bull probability is a regime measure, not a price forecast: as a forecast of a higher price 3-12 months later it is worse than a constant base rate.

## Compare
`compare.py` scores external stance records against `history` (see the docstring).

Parameters live in `CONFIG` at the top of `btc_model.py`.
