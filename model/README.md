# BTC regime model

Rule-based trend and cycle model for bitcoin. It runs daily in GitHub Actions (`.github/workflows/btc-model.yml`) and writes `data/btc-model.json`.

## Data
- BTC and ETH daily candles: Coinbase Exchange (fallback Bitstamp), last closes cross-checked against Kraken
- Macro: FRED CSV (DGS2, DGS10, DGS30, NFCI, DCOILBRENTEU, DTWEXBGS, T10YIE)
- ETF flows: `data/etf-flows.json` (Farside, refreshed by the ETF workflow in this repo)
- Fear & Greed: alternative.me

## Output
- `model`: trend checklist (weekly close vs 50W SMA, higher high, higher low, 50W slope), regime, scenario weights, short-term bias, cycle positions (4-year, weekly, daily) with timing bands, key levels, macro score, RSI
- `history`: daily model state for the last ~460 days, each day evaluated only with data up to that day (no look-ahead)
- `errors` lists any source that failed; on a total price-data failure the previous file is kept

## Compare
`compare.py` scores external stance records against `history` (see the docstring).

Parameters live in `CONFIG` at the top of `btc_model.py`.
