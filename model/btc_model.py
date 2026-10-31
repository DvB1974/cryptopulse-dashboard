#!/usr/bin/env python3
"""BTC regime model: own rule-based implementation of a cycle/trend methodology.

Runs daily in GitHub Actions (.github/workflows/btc-model.yml). Standard library only.
Writes data/btc-model.json. Every block carries its source and the date of the data.

What it computes
- Trend checklist (4 checks): weekly close vs 50-week SMA, higher high, higher low,
  direction of the 50-week SMA.
- Swing structure via zigzag pivots (20% for market structure, 12% for higher lows,
  45% for the multi-year market cycle).
- Cycle position: 4-year cycle, weekly cycle (ICL), daily cycle (DCL) with timing bands.
- Key levels: moving averages (daily/weekly/monthly), swing levels, anchored VWAP.
- Macro: US 2y/10y/30y yields, NFCI vs its 28-week EMA, Brent, broad dollar index,
  10y breakeven inflation (FRED).
- Flows and sentiment: US spot ETF net flows (data/etf-flows.json), Fear & Greed.
- A daily history of the model state (no look-ahead: each day is evaluated with data
  up to that day only), used to compare the model with external market updates.

If the price download fails, the previous JSON is left untouched.
"""
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "btc-model.json"
ETF_FILE = ROOT / "data" / "etf-flows.json"
DAILY_OUT = ROOT / "data" / "btc-daily.json"
UA = "Mozilla/5.0 (compatible; btc-model/1.0; +https://github.com/DvB1974/cryptopulse-dashboard)"

CONFIG = {
    "history_start": date(2017, 1, 1),
    "zigzag_structure": 0.20,   # swing highs/lows for higher-high / lower-high logic
    "zigzag_hl": 0.12,          # swing lows for the higher-low check
    "zigzag_major": 0.45,       # bull/bear market cycle lows and tops
    "acceptance_days": 3,       # consecutive daily closes above a level = acceptance
    "weekly_band": (22, 40),    # weeks between weekly cycle lows (avg ~26-32)
    "daily_band": (35, 75),     # days between daily cycle lows (avg ~60)
    "weekly_confirm": (21, 0.12),  # early recognition of a weekly cycle low: age (days), rally
    "daily_confirm": (5, 0.04),    # early recognition of a daily cycle low: age (days), rally
    "four_year_months": (47.5, 0.15),  # mean months between 4-year cycle lows, +/- band
    "weights": {"base": 5, "b1": 40, "b2": 20, "b3": 15, "b4": 15},
    "macro_weight": 2.5,        # bull-probability points per macro score point
    "failed_dcl_penalty": 5,
    "history_days": 460,
    "deep_bear_share": 0.25,    # share of the bear probability assigned to a deep bear
}


# ---------------------------------------------------------------- fetching

def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def http_json(url, timeout=40):
    return json.loads(http_get(url, timeout))


def retry(fn, tries=3, pause=4):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(pause * (i + 1))
    raise last


def fetch_coinbase_daily(product, start, end):
    """Daily UTC candles: list of (date, open, high, low, close, volume)."""
    rows = {}
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=299), end)
        url = ("https://api.exchange.coinbase.com/products/%s/candles?granularity=86400"
               "&start=%sT00:00:00Z&end=%sT23:59:59Z" % (product, cur.isoformat(), chunk_end.isoformat()))
        data = retry(lambda: http_json(url))
        for t, low, high, opn, close, vol in data:
            d = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
            rows[d] = (float(opn), float(high), float(low), float(close), float(vol))
        cur = chunk_end + timedelta(days=1)
        time.sleep(0.4)
    return [(d,) + rows[d] for d in sorted(rows)]


def fetch_bitstamp_daily(pair, start):
    rows = {}
    ts = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    now = int(time.time())
    while ts < now:
        url = "https://www.bitstamp.net/api/v2/ohlc/%s/?step=86400&limit=1000&start=%d" % (pair, ts)
        data = retry(lambda: http_json(url))["data"]["ohlc"]
        if not data:
            break
        for r in data:
            d = datetime.fromtimestamp(int(r["timestamp"]), tz=timezone.utc).date()
            rows[d] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]))
        last = int(data[-1]["timestamp"])
        if last <= ts:
            break
        ts = last + 86400
        time.sleep(0.4)
    return [(d,) + rows[d] for d in sorted(rows)]


def fetch_kraken_closes():
    data = http_json("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440")
    res = data.get("result", {})
    key = next(k for k in res if k != "last")
    return {datetime.fromtimestamp(int(r[0]), tz=timezone.utc).date(): float(r[4]) for r in res[key]}


def fetch_fred(series, start):
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (series, start.isoformat())
    txt = retry(lambda: http_get(url))
    out = []
    for line in txt.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2 or parts[1] in (".", ""):
            continue
        try:
            out.append((date.fromisoformat(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return out


def fetch_fear_greed():
    data = http_json("https://api.alternative.me/fng/?limit=60&format=json")["data"]
    out = []
    for r in data:
        d = datetime.fromtimestamp(int(r["timestamp"]), tz=timezone.utc).date()
        out.append((d, int(r["value"]), r.get("value_classification", "")))
    return sorted(out)


# ---------------------------------------------------------------- math helpers

def sma_last(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ema_series(vals, n):
    out, e, k = [], None, 2.0 / (n + 1)
    for i, v in enumerate(vals):
        if i < n - 1:
            out.append(None)
            continue
        e = (sum(vals[:n]) / n) if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi_last(vals, n=14):
    if len(vals) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = vals[i] - vals[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(vals)):
        ch = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def zigzag(H, L, thr):
    """Confirmed pivots [(kind, idx)] and the provisional extreme (kind, idx) or None."""
    piv, trend, hi_i, lo_i = [], 0, 0, 0
    for i in range(1, len(H)):
        if trend == 0:
            if H[i] > H[hi_i]:
                hi_i = i
            if L[i] < L[lo_i]:
                lo_i = i
            if hi_i > lo_i and H[hi_i] >= L[lo_i] * (1 + thr):
                piv.append(("L", lo_i))
                trend = 1
            elif lo_i > hi_i and L[lo_i] <= H[hi_i] * (1 - thr):
                piv.append(("H", hi_i))
                trend = -1
        elif trend == 1:
            if H[i] >= H[hi_i]:
                hi_i = i
            if L[i] <= H[hi_i] * (1 - thr):
                piv.append(("H", hi_i))
                trend, lo_i = -1, i
        else:
            if L[i] <= L[lo_i]:
                lo_i = i
            if H[i] >= L[lo_i] * (1 + thr):
                piv.append(("L", lo_i))
                trend, hi_i = 1, i
    prov = ("H", hi_i) if trend == 1 else (("L", lo_i) if trend == -1 else None)
    return piv, prov


def week_start(d):
    return d - timedelta(days=d.weekday())


def aggregate(days, keyfn):
    """Aggregate daily rows into periods. Returns list of dicts with complete flag."""
    out = []
    for row in days:
        d, o, h, l, c, v = row
        k = keyfn(d)
        if out and out[-1]["key"] == k:
            p = out[-1]
            p["h"] = max(p["h"], h)
            p["l"] = min(p["l"], l)
            p["c"] = c
            p["end"] = d
        else:
            out.append({"key": k, "start": d, "end": d, "o": o, "h": h, "l": l, "c": c})
    return out


def r0(x, nd=0):
    if x is None:
        return None
    return round(x, nd) if nd else int(round(x))


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a / b - 1) * 100, 2)


# ---------------------------------------------------------------- model core

class Series:
    def __init__(self, days):
        self.days = days
        self.D = [r[0] for r in days]
        self.O = [r[1] for r in days]
        self.H = [r[2] for r in days]
        self.L = [r[3] for r in days]
        self.C = [r[4] for r in days]
        self.V = [r[5] for r in days]
        self.idx = {d: i for i, d in enumerate(self.D)}


def acceptance_since(S, start_i, end_i, level, weekly):
    """First date (index) at/after start_i where price shows acceptance above level:
    a completed weekly close above it, or N consecutive daily closes above it."""
    n = CONFIG["acceptance_days"]
    run = 0
    for i in range(start_i, end_i + 1):
        run = run + 1 if S.C[i] > level else 0
        if run >= n:
            return i
        if S.D[i].weekday() == 6 and S.C[i] > level:  # Sunday close = weekly close
            return i
    return None


def cycle_chain(lows, highs, dates, start_i, end_i, band, unit_days, confirm_days, rally_pct):
    """Hurst-style chain: from a cycle low at start_i, the next cycle low is the lowest low
    within [start+band_min, start+band_max]. Once the band has fully elapsed that low is taken.
    While the band is still open, the lowest low so far is accepted early when it is at least
    `confirm_days` old and price has since rallied `rally_pct` above it (the turn is visible).
    Returns (list of low indices, current low index, band_open: bool)."""
    chain = [start_i]
    cur = start_i
    while True:
        lo_d = dates[cur] + timedelta(days=band[0] * unit_days)
        hi_d = dates[cur] + timedelta(days=band[1] * unit_days)
        cand = [i for i in range(cur + 1, end_i + 1) if lo_d <= dates[i] <= hi_d]
        if not cand:
            return chain, cur, True
        nxt = min(cand, key=lambda i: lows[i])
        if hi_d > dates[end_i]:
            old_enough = (dates[end_i] - dates[nxt]).days >= confirm_days
            rallied = max(highs[nxt:end_i + 1]) >= lows[nxt] * (1 + rally_pct)
            if not (old_enough and rallied):
                return chain, cur, True
        chain.append(nxt)
        cur = nxt


def evaluate(S, t, macro, flows_ctx=None, full=False):
    """Model state using daily data up to and including index t (no look-ahead)."""
    W = CONFIG["weights"]
    D, H, L, C = S.D[: t + 1], S.H[: t + 1], S.L[: t + 1], S.C[: t + 1]
    today = D[-1]
    days = S.days[: t + 1]

    # weekly series (complete weeks only for signals)
    weeks = aggregate(days, week_start)
    wk_done = [w for w in weeks if w["end"].weekday() == 6]
    wc = [w["c"] for w in wk_done]
    sma50w = sma_last(wc, 50)
    sma50w_prev = sma_last(wc[:-1], 50)
    sma50w_prev4 = sma_last(wc[:-4], 50)
    sma10w = sma_last(wc, 10)
    sma200w = sma_last(wc, 200)
    ema50w_s = ema_series(wc, 50)
    ema200w_s = ema_series(wc, 200)
    last_wc = wc[-1] if wc else None

    # checklist
    b1 = bool(last_wc and sma50w and last_wc > sma50w)
    b4 = bool(sma50w and sma50w_prev and sma50w_prev4 and sma50w > sma50w_prev and sma50w > sma50w_prev4)

    piv20, prov20 = zigzag(H, L, CONFIG["zigzag_structure"])
    lows20 = [i for k, i in piv20 if k == "L"]
    highs20 = [i for k, i in piv20 if k == "H"]
    b2, b2_detail = False, {}
    if lows20:
        l_last = lows20[-1]
        h_ref_cands = [i for i in highs20 if i < l_last]
        if h_ref_cands:
            h_ref = h_ref_cands[-1]
            acc = acceptance_since(S, l_last, t, H[h_ref], True)
            b2 = acc is not None
            b2_detail = {"ref_high": r0(H[h_ref]), "ref_high_date": D[h_ref].isoformat(),
                         "since_low": r0(L[l_last]), "since_low_date": D[l_last].isoformat(),
                         "accepted_on": D[acc].isoformat() if acc is not None else None}

    piv12, _ = zigzag(H, L, CONFIG["zigzag_hl"])
    lows12 = [i for k, i in piv12 if k == "L"]
    b3, b3_detail = False, {}
    if len(lows12) >= 2:
        a, b = lows12[-2], lows12[-1]
        b3 = L[b] > L[a]
        b3_detail = {"last_low": r0(L[b]), "last_low_date": D[b].isoformat(),
                     "prev_low": r0(L[a]), "prev_low_date": D[a].isoformat()}

    # major cycle (bull/bear) via 45% zigzag
    pivM, provM = zigzag(H, L, CONFIG["zigzag_major"])
    major_lows = [i for k, i in pivM if k == "L"]
    major_highs = [i for k, i in pivM if k == "H"]
    cyc_low_i = major_lows[-1] if major_lows else None

    # daily indicators
    sma10d = sma_last(C, 10)
    ema100d = ema_series(C, 100)[-1] if len(C) >= 100 else None
    ema200d = ema_series(C, 200)[-1] if len(C) >= 200 else None
    sma200d = sma_last(C, 200)

    # cycles: weekly (ICL) and daily (DCL), anchored at the last major cycle low
    icl_i = dcl_i = None
    weekly_info = daily_info = None
    if cyc_low_i is not None:
        chainW, icl_i, _ = cycle_chain(L, H, D, cyc_low_i, t, CONFIG["weekly_band"], 7,
                                       CONFIG["weekly_confirm"][0], CONFIG["weekly_confirm"][1])
        chainD, dcl_i, _ = cycle_chain(L, H, D, icl_i, t, CONFIG["daily_band"], 1,
                                       CONFIG["daily_confirm"][0], CONFIG["daily_confirm"][1])
        wk_age = (today - D[icl_i]).days / 7.0
        d_age = (today - D[dcl_i]).days
        closes_after = C[dcl_i + 1:]
        failed_dcl = any(c < L[dcl_i] for c in closes_after)
        intraday_breaks = sum(1 for x in L[dcl_i + 1:] if x < L[dcl_i])
        confirmed_dcl = False
        for j in range(dcl_i + 1, min(t, dcl_i + 15) + 1):
            if j >= 9 and C[j] > sum(C[j - 9:j + 1]) / 10:
                confirmed_dcl = True
                break
        failed_icl = any(c < L[icl_i] for c in C[icl_i + 1:])
        weekly_info = {
            "icl_date": D[icl_i].isoformat(), "icl_low": r0(L[icl_i]), "week": int(wk_age) + 1,
            "band_weeks": list(CONFIG["weekly_band"]),
            "next_window": [(D[icl_i] + timedelta(weeks=CONFIG["weekly_band"][0])).isoformat(),
                            (D[icl_i] + timedelta(weeks=CONFIG["weekly_band"][1])).isoformat()],
            "failed": failed_icl,
            "history": [D[i].isoformat() for i in chainW][-6:],
            "above_10w": bool(last_wc and sma10w and last_wc > sma10w),
        }
        daily_info = {
            "dcl_date": D[dcl_i].isoformat(), "dcl_low": r0(L[dcl_i]), "day": d_age,
            "band_days": list(CONFIG["daily_band"]),
            "next_window": [(D[dcl_i] + timedelta(days=CONFIG["daily_band"][0])).isoformat(),
                            (D[dcl_i] + timedelta(days=CONFIG["daily_band"][1])).isoformat()],
            "confirmed": confirmed_dcl, "failed_close": failed_dcl, "intraday_breaks": intraday_breaks,
            "above_10d": bool(sma10d and C[-1] > sma10d),
            "history": [D[i].isoformat() for i in chainD][-6:],
        }

    # four-year cycle
    four = None
    if len(major_lows) >= 2:
        prev_i, cur_i = major_lows[-2], major_lows[-1]
        months = (D[cur_i] - D[prev_i]).days / 30.44
        mean, band = CONFIG["four_year_months"]
        four = {"prev_low": r0(L[prev_i]), "prev_date": D[prev_i].isoformat(),
                "low": r0(L[cur_i]), "date": D[cur_i].isoformat(), "months_between": round(months, 1),
                "expected_months": mean,
                "expected_window": [(D[prev_i] + timedelta(days=mean * (1 - band) * 30.44)).isoformat(),
                                    (D[prev_i] + timedelta(days=mean * (1 + band) * 30.44)).isoformat()],
                "months_since_low": round((today - D[cur_i]).days / 30.44, 1)}
        top_cands = [i for i in major_highs if prev_i < i < cur_i]
        if top_cands:
            four["top"] = r0(H[top_cands[-1]])
            four["top_date"] = D[top_cands[-1]].isoformat()

    # macro score as of today
    m = macro_state(macro, today)

    # probabilities
    bull = W["base"] + W["b1"] * b1 + W["b2"] * b2 + W["b3"] * b3 + W["b4"] * b4
    bull += CONFIG["macro_weight"] * (m["score"] if m else 0)
    if daily_info and daily_info["failed_close"]:
        bull -= CONFIG["failed_dcl_penalty"]
    bull = max(5, min(95, 5 * round(bull / 5)))
    bear = 100 - bull
    deep = 5 * round(bear * CONFIG["deep_bear_share"] / 5)
    mild = bear - deep

    if b1 and b2 and b3 and b4:
        regime = "Bullmarkt"
    elif b1 and b2:
        regime = "Vroege bullmarkt"
    elif b1:
        regime = "Overgangsfase"
    elif b2 or b3:
        regime = "Handrem erop"
    else:
        regime = "Bearmarkt"

    # short-term bias
    bias_pts, bias_why = 0, []
    if daily_info:
        if daily_info["above_10d"]:
            bias_pts += 1; bias_why.append("+1 koers boven 10-daags gemiddelde")
        else:
            bias_pts -= 1; bias_why.append("-1 koers onder 10-daags gemiddelde")
        if daily_info["failed_close"]:
            bias_pts -= 2; bias_why.append("-2 dagslot onder bodem daily cycle (failed cycle)")
        if weekly_info["above_10w"]:
            bias_pts += 1; bias_why.append("+1 weekslot boven 10-weeks gemiddelde")
        else:
            bias_pts -= 1; bias_why.append("-1 weekslot onder 10-weeks gemiddelde")
    if m and abs(m["score"]) >= 2:
        s = 1 if m["score"] > 0 else -1
        bias_pts += s; bias_why.append("%+d macro %s" % (s, "rugwind" if s > 0 else "tegenwind"))
    bias = "bullish" if bias_pts >= 2 else ("bearish" if bias_pts <= -2 else "neutraal")

    state = {
        "date": today.isoformat(), "close": r0(C[-1]),
        "checks": {"b1": b1, "b2": b2, "b3": b3, "b4": b4}, "n_checks": int(b1) + int(b2) + int(b3) + int(b4),
        "regime": regime, "base_case": "bull" if bull >= 50 else "bear",
        "scenarios": {"bull": bull, "mild_bear": mild, "deep_bear": deep},
        "bias": bias, "bias_points": bias_pts,
        "sma50w": r0(sma50w), "sma10w": r0(sma10w),
        "dcl_date": daily_info["dcl_date"] if daily_info else None,
        "dcl_low": daily_info["dcl_low"] if daily_info else None,
        "icl_date": weekly_info["icl_date"] if weekly_info else None,
        "macro_score": m["score"] if m else None,
    }
    if not full:
        return state

    # ------- full detail for the latest day
    lw = wk_done[-1] if wk_done else None
    checklist = [
        {"id": "b1", "label": "Weekslot boven het 50-weekse gemiddelde", "ok": b1,
         "detail": "Weekslot %s (week t/m %s) vs 50W SMA %s" % (fmt(last_wc), lw["end"].isoformat() if lw else "?", fmt(sma50w))},
        {"id": "b2", "label": "Hogere top (acceptatie boven laatste lagere top)", "ok": b2,
         "detail": ("Referentietop %s (%s), geaccepteerd op %s" % (fmt(b2_detail.get("ref_high")), b2_detail.get("ref_high_date"), b2_detail.get("accepted_on") or "nog niet")) if b2_detail else "Geen referentie gevonden"},
        {"id": "b3", "label": "Hogere bodem (laatste swing low boven de vorige)", "ok": b3,
         "detail": ("Laatste bodem %s (%s) vs vorige %s (%s)" % (fmt(b3_detail.get("last_low")), b3_detail.get("last_low_date"), fmt(b3_detail.get("prev_low")), b3_detail.get("prev_low_date"))) if b3_detail else "Te weinig swing lows"},
        {"id": "b4", "label": "50-weeks gemiddelde stijgt", "ok": b4,
         "detail": "50W SMA nu %s, vorige week %s, 4 weken terug %s" % (fmt(sma50w), fmt(sma50w_prev), fmt(sma50w_prev4))},
    ]

    # handrem events: weekly close crossing the 50W SMA
    events = []
    for k in range(51, len(wc)):
        prev_above = wc[k - 1] > sum(wc[k - 50:k]) / 50 if k >= 50 else None
        cur_sma = sum(wc[k - 49:k + 1]) / 50
        cur_above = wc[k] > cur_sma
        if prev_above is not None and cur_above != prev_above:
            events.append({"week_end": wk_done[k]["end"].isoformat(), "type": "handrem eraf" if cur_above else "handrem erop",
                           "close": r0(wc[k]), "sma50w": r0(cur_sma)})

    # monthly
    months = aggregate(days, lambda d: (d.year, d.month))
    mdone = [mm for mm in months if (mm["end"] + timedelta(days=1)).month != mm["end"].month]
    mc = [mm["c"] for mm in mdone]

    # anchored VWAP from the cycle top (Coinbase volume, approximation)
    vwap = None
    top_i = None
    if four and four.get("top_date"):
        top_i = S.idx.get(date.fromisoformat(four["top_date"]))
    if top_i is not None and top_i <= t:
        num = sum((S.H[i] + S.L[i] + S.C[i]) / 3 * S.V[i] for i in range(top_i, t + 1))
        den = sum(S.V[i] for i in range(top_i, t + 1))
        vwap = num / den if den else None

    levels = [
        lv("sma10d", "10-daags gemiddelde", sma10d, "dag"),
        lv("ema100d", "100-daagse EMA", ema100d, "dag"),
        lv("ema200d", "200-daagse EMA", ema200d, "dag"),
        lv("sma200d", "200-daags gemiddelde", sma200d, "dag"),
        lv("sma10w", "10-weeks gemiddelde", sma10w, "week"),
        lv("sma50w", "50-weeks gemiddelde (handrem)", sma50w, "week"),
        lv("ema50w", "50-weekse EMA", ema50w_s[-1] if ema50w_s else None, "week"),
        lv("sma200w", "200-weeks gemiddelde (bodemzone bear)", sma200w, "week"),
        lv("ema200w", "200-weekse EMA", ema200w_s[-1] if ema200w_s else None, "week"),
        lv("sma10m", "10-maands gemiddelde (yearly cycle)", sma_last(mc, 10), "maand"),
        lv("sma12m", "12-maands gemiddelde", sma_last(mc, 12), "maand"),
        lv("sma20m", "20-maands gemiddelde (alternatieve handrem)", sma_last(mc, 20), "maand"),
        lv("vwap_top", "VWAP vanaf de cyclustop (Coinbase-volume)", vwap, "anker"),
    ]
    if b2_detail:
        levels.append(lv("ref_high", "Laatste lagere top (HH-grens)", b2_detail.get("ref_high"), "swing", b2_detail.get("ref_high_date")))
        levels.append(lv("bear_low", "Laatste swing low (HL-grens)", b2_detail.get("since_low"), "swing", b2_detail.get("since_low_date")))
    if prov20 and prov20[0] == "H":
        levels.append(lv("leg_high", "Top van de huidige opwaartse beweging", H[prov20[1]], "swing", D[prov20[1]].isoformat()))
    if daily_info:
        levels.append(lv("dcl_low", "Bodem daily cycle (rode lijn)", daily_info["dcl_low"], "cyclus", daily_info["dcl_date"]))
    if weekly_info:
        levels.append(lv("icl_low", "Bodem weekly cycle", weekly_info["icl_low"], "cyclus", weekly_info["icl_date"]))
    for x in levels:
        x["dist_pct"] = pct(C[-1], x["price"]) if x["price"] else None
    levels = [x for x in levels if x["price"]]

    weekly_chart = [{"w": wk_done[k]["end"].isoformat(), "c": r0(wc[k]),
                     "sma50": r0(sum(wc[k - 49:k + 1]) / 50) if k >= 49 else None,
                     "sma10": r0(sum(wc[k - 9:k + 1]) / 10) if k >= 9 else None}
                    for k in range(max(0, len(wc) - 110), len(wc))]

    state.update({
        "checklist": checklist,
        "b2_detail": b2_detail, "b3_detail": b3_detail,
        "cycles": {"daily": daily_info, "weekly": weekly_info, "four_year": four},
        "levels": levels,
        "macro": m,
        "handrem_events": events[-6:],
        "pivots": {
            "structure": [{"kind": k, "date": D[i].isoformat(), "price": r0(H[i] if k == "H" else L[i])} for k, i in piv20[-8:]],
            "structure_open": ({"kind": prov20[0], "date": D[prov20[1]].isoformat(), "price": r0(H[prov20[1]] if prov20[0] == "H" else L[prov20[1]])} if prov20 else None),
            "major": [{"kind": k, "date": D[i].isoformat(), "price": r0(H[i] if k == "H" else L[i])} for k, i in pivM[-6:]],
        },
        "rsi": {"daily": round(rsi_last(C), 1) if len(C) > 30 else None,
                "weekly": round(rsi_last(wc), 1) if len(wc) > 30 else None,
                "monthly": round(rsi_last(mc), 1) if len(mc) > 30 else None},
        "bias_why": bias_why,
        "weekly_chart": weekly_chart,
        "weekly_partial": ({"start": weeks[-1]["start"].isoformat(), "close_so_far": r0(weeks[-1]["c"])}
                           if weeks and weeks[-1]["end"].weekday() != 6 else None),
    })
    return state


def lv(key, label, price, kind, date_s=None):
    return {"key": key, "label": label, "price": r0(price), "kind": kind, "date": date_s}


def fmt(x):
    return "n.b." if x is None else "{:,.0f}".format(x).replace(",", ".")


# ---------------------------------------------------------------- macro

MACRO_SERIES = {
    "DGS2": "2-jaarsrente VS",
    "DGS10": "10-jaarsrente VS",
    "DGS30": "30-jaarsrente VS",
    "NFCI": "Chicago Fed NFCI",
    "DCOILBRENTEU": "Brent-olie ($/vat)",
    "DTWEXBGS": "Dollarindex (breed, Fed)",
    "T10YIE": "Inflatieverwachting 10j (breakeven)",
}


def asof(series, d):
    lo, hi = 0, len(series) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= d:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def macro_state(macro, d):
    if not macro:
        return None
    items, score = [], 0
    for sid, label in MACRO_SERIES.items():
        s = macro.get(sid)
        if not s:
            continue
        i = asof(s, d)
        if i is None:
            continue
        val_d, val = s[i]
        j = asof(s, val_d - timedelta(days=28))
        prev = s[j][1] if j is not None else None
        ch = None if prev is None else val - prev
        sig, why = 0, ""
        if sid in ("DGS2", "DGS10", "DGS30"):
            if ch is not None and ch <= -0.10:
                sig, why = 1, "rente daalt"
            elif ch is not None and ch >= 0.10:
                sig, why = -1, "rente stijgt"
            if sid == "DGS30":
                sig = 0  # shown, not scored (overlaps with 10y)
        elif sid == "NFCI":
            vals = [v for _, v in s[: i + 1]]
            e = ema_series(vals, 28)[-1] if len(vals) >= 28 else None
            if e is not None:
                sig, why = (1, "financiële condities verruimen (onder 28w EMA)") if val < e else (-1, "financiële condities verkrappen (boven 28w EMA)")
            items.append({"id": sid, "label": label, "value": round(val, 3), "date": val_d.isoformat(),
                          "change_4w": None if ch is None else round(ch, 3), "ema28": None if e is None else round(e, 3),
                          "signal": sig, "why": why})
            score += sig
            continue
        elif sid == "DCOILBRENTEU":
            r = None if not prev else val / prev - 1
            if r is not None and r <= -0.05:
                sig, why = 1, "olie daalt"
            elif r is not None and r >= 0.05:
                sig, why = -1, "olie stijgt"
        elif sid == "DTWEXBGS":
            r = None if not prev else val / prev - 1
            if r is not None and r <= -0.01:
                sig, why = 1, "dollar zwakker"
            elif r is not None and r >= 0.01:
                sig, why = -1, "dollar sterker"
        elif sid == "T10YIE":
            if ch is not None and ch <= -0.10:
                sig, why = 1, "inflatieverwachting daalt"
            elif ch is not None and ch >= 0.10:
                sig, why = -1, "inflatieverwachting stijgt"
        items.append({"id": sid, "label": label, "value": round(val, 3), "date": val_d.isoformat(),
                      "change_4w": None if ch is None else round(ch, 3), "signal": sig, "why": why})
        score += sig
    label = "rugwind" if score >= 2 else ("tegenwind" if score <= -2 else "neutraal")
    return {"score": score, "label": label, "items": items}


# ---------------------------------------------------------------- main

def main():
    errors, sources = [], {}
    now = datetime.now(timezone.utc)
    today = now.date()
    start = CONFIG["history_start"]

    days = None
    try:
        days = fetch_coinbase_daily("BTC-USD", start, today)
        sources["btc"] = "Coinbase Exchange daily candles (UTC)"
    except Exception as e:  # noqa: BLE001
        errors.append("Coinbase BTC: %s" % e)
    if not days or len(days) < 1500:
        try:
            days = fetch_bitstamp_daily("btcusd", start)
            sources["btc"] = "Bitstamp daily OHLC (UTC), fallback"
        except Exception as e:  # noqa: BLE001
            errors.append("Bitstamp BTC: %s" % e)
    if not days or len(days) < 1500:
        print("FATAL: no usable BTC price history; keeping previous file", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1

    live = days[-1] if days[-1][0] == today else None
    completed = [r for r in days if r[0] < today]
    S = Series(completed)
    t = len(completed) - 1

    cross = None
    try:
        kr = fetch_kraken_closes()
        diffs = [abs(S.C[S.idx[d]] / kr[d] - 1) * 100 for d in S.D[-5:] if d in kr]
        cross = {"source": "Kraken", "max_diff_pct": round(max(diffs), 3) if diffs else None,
                 "ok": bool(diffs) and max(diffs) < 1.5}
    except Exception as e:  # noqa: BLE001
        errors.append("Kraken cross-check: %s" % e)

    macro = {}
    for sid in MACRO_SERIES:
        try:
            macro[sid] = fetch_fred(sid, date(2023, 1, 1))
            time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            errors.append("FRED %s: %s" % (sid, e))
    sources["macro"] = "FRED (St. Louis Fed) CSV"

    latest = evaluate(S, t, macro, full=True)

    # history without look-ahead
    hist = []
    for i in range(max(400, t - CONFIG["history_days"] + 1), t + 1):
        try:
            hist.append(evaluate(S, i, macro))
        except Exception as e:  # noqa: BLE001
            errors.append("history %s: %s" % (S.D[i], e))
            break

    # ETH context
    eth = None
    try:
        ed = [r for r in fetch_coinbase_daily("ETH-USD", today - timedelta(days=420), today) if r[0] < today]
        ec = [r[4] for r in ed]
        e100 = ema_series(ec, 100)[-1]
        e200 = ema_series(ec, 200)[-1]
        ethbtc = [ec[-1] / S.C[S.idx[ed[-1][0]]]] if ed[-1][0] in S.idx else None
        eb30 = None
        if ed[-31][0] in S.idx:
            eb30 = pct(ec[-1] / S.C[S.idx[ed[-1][0]]], ec[-31] / S.C[S.idx[ed[-31][0]]])
        eth = {"date": ed[-1][0].isoformat(), "close": r0(ec[-1]), "ema100d": r0(e100), "ema200d": r0(e200),
               "above_ema100": ec[-1] > e100, "above_ema200": ec[-1] > e200,
               "eth_btc": round(ethbtc[0], 5) if ethbtc else None, "eth_btc_30d_pct": eb30,
               "source": "Coinbase Exchange"}
    except Exception as e:  # noqa: BLE001
        errors.append("ETH: %s" % e)

    flows = None
    try:
        ef = json.loads(ETF_FILE.read_text())
        btc_rows = ef.get("btc", [])
        vals = [r["total"] for r in btc_rows if r.get("total") is not None]
        flows = {"source": ef.get("source"), "updated": ef.get("updated"),
                 "last_date": btc_rows[-1]["date"] if btc_rows else None,
                 "last": vals[-1] if vals else None,
                 "sum_5d": round(sum(vals[-5:]), 1) if vals else None,
                 "sum_20d": round(sum(vals[-20:]), 1) if vals else None,
                 "days_available": len(vals)}
    except Exception as e:  # noqa: BLE001
        errors.append("ETF flows: %s" % e)

    sentiment = None
    try:
        fg = fetch_fear_greed()
        sentiment = {"fear_greed": fg[-1][1], "label": fg[-1][2], "date": fg[-1][0].isoformat(),
                     "avg_7d": round(sum(v for _, v, _ in fg[-7:]) / min(7, len(fg)), 1),
                     "source": "alternative.me Fear & Greed"}
    except Exception as e:  # noqa: BLE001
        errors.append("Fear & Greed: %s" % e)

    out = {
        "schema": 1,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": S.D[t].isoformat(),
        "price": {"last_close": r0(S.C[t]), "last_close_date": S.D[t].isoformat(),
                  "live": r0(live[4]) if live else None, "live_note": "lopende UTC-dagcandle" if live else None,
                  "change_1d_pct": pct(S.C[t], S.C[t - 1]), "change_7d_pct": pct(S.C[t], S.C[t - 7]),
                  "change_30d_pct": pct(S.C[t], S.C[t - 30]),
                  "ath": r0(max(S.H)), "ath_date": S.D[S.H.index(max(S.H))].isoformat(),
                  "from_ath_pct": pct(S.C[t], max(S.H)), "cross_check": cross},
        "model": latest,
        "eth": eth, "flows": flows, "sentiment": sentiment,
        "history": hist,
        "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in CONFIG.items() if k != "history_start"},
        "sources": sources, "errors": errors,
        "unavailable": ["realized price / MVRV (on-chain, geen gratis bron)", "FedWatch-renteverwachting (CME, geen gratis API)",
                        "open interest / funding (zit in CryptoPulse)", "S&P 500-breadth"],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":"), default=str))
    # raw completed daily candles, for calibration and audits
    DAILY_OUT.write_text(json.dumps({
        "source": sources.get("btc"), "generated_at": out["generated_at"],
        "fields": ["date", "open", "high", "low", "close", "volume"],
        "rows": [[r[0].isoformat(), round(r[1], 2), round(r[2], 2), round(r[3], 2), round(r[4], 2), round(r[5], 2)] for r in completed],
    }, separators=(",", ":")))
    print("ok: as_of %s close %s regime %s bull %s%% errors %d" % (out["as_of"], fmt(S.C[t]), latest["regime"], latest["scenarios"]["bull"], len(errors)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
