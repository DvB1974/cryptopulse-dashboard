#!/usr/bin/env python3
"""Compare external market-update stances with the BTC regime model.

Usage: python3 compare.py btc-model.json editions.json > comparisons.json

editions.json is a list of stance records (one per market update) with optional fields:
  id, date (YYYY-MM-DD), base_case (bull|bear|transition), bull_prob (0-100),
  checklist {b1..b4: true|false|null}, short_term (bullish|neutraal|bearish),
  cycles {dcl_date, icl_date}, levels [{label, price, kind}]
Fields that are missing or null are skipped, never counted as a mismatch.

For each record the model state of the last completed day BEFORE the publication date is
used, so the model never benefits from data the author did not have.
Outcome scoring: after HORIZON days, did the short-term call match the price move?
"""
import json
import sys
from datetime import date, timedelta

HORIZON = 28
TOL = {"bull_prob": 15, "dcl_days": 7, "icl_days": 14, "level_pct": 1.5}
CHECK_LABELS = {"b1": "Weekslot boven 50W", "b2": "Hogere top", "b3": "Hogere bodem", "b4": "50W stijgt"}


def model_row(hist, d):
    best = None
    for row in hist:
        if row["date"] < d:
            best = row
        else:
            break
    return best


def close_on(hist, d):
    for row in hist:
        if row["date"] >= d:
            return row
    return None


def dir_hit(call, ret):
    if call is None or ret is None:
        return None
    if call == "bullish":
        return ret > 0
    if call == "bearish":
        return ret < 0
    return abs(ret) < 5


def days_between(a, b):
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def compare(model, ed):
    hist = model.get("history", [])
    d = ed["date"]
    row = model_row(hist, d)
    if row is None:
        return {"id": ed.get("id"), "date": d, "model_date": None, "items": [], "agreement_pct": None,
                "note": "geen modelhistorie voor deze datum"}
    items = []

    def add(key, label, a, m, match, **extra):
        items.append(dict({"key": key, "label": label, "alpha": a, "model": m, "match": match}, **extra))

    bc = ed.get("base_case")
    if bc in ("bull", "bear"):
        add("base_case", "Basisscenario", bc, row["base_case"], bc == row["base_case"])
    elif bc == "transition":
        add("base_case", "Basisscenario", bc, row["base_case"], row["n_checks"] in (1, 2))

    bp = ed.get("bull_prob")
    if isinstance(bp, (int, float)):
        diff = row["scenarios"]["bull"] - bp
        add("bull_prob", "Kans bullmarkt", bp, row["scenarios"]["bull"], abs(diff) <= TOL["bull_prob"], diff=diff)

    ck = ed.get("checklist") or {}
    for k in ("b1", "b2", "b3", "b4"):
        if isinstance(ck.get(k), bool):
            add(k, CHECK_LABELS[k], ck[k], row["checks"][k], ck[k] == row["checks"][k])

    st = ed.get("short_term")
    if st in ("bullish", "neutraal", "bearish"):
        add("bias", "Korte termijn", st, row["bias"], st == row["bias"])

    cy = ed.get("cycles") or {}
    if cy.get("dcl_date") and row.get("dcl_date"):
        dd = days_between(cy["dcl_date"], row["dcl_date"])
        add("dcl", "Start daily cycle", cy["dcl_date"], row["dcl_date"], dd <= TOL["dcl_days"], diff_days=dd)
    if cy.get("icl_date") and row.get("icl_date"):
        dd = days_between(cy["icl_date"], row["icl_date"])
        add("icl", "Start weekly cycle", cy["icl_date"], row["icl_date"], dd <= TOL["icl_days"], diff_days=dd)

    for lv in ed.get("levels") or []:
        kind = (lv.get("kind") or "").lower()
        if kind in ("rode lijn", "invalidatie", "redline") and row.get("dcl_low") and lv.get("price"):
            dp = round((row["dcl_low"] / lv["price"] - 1) * 100, 2)
            add("redline", "Rode lijn (bodem daily cycle)", lv["price"], row["dcl_low"], abs(dp) <= TOL["level_pct"], diff_pct=dp)
            break
    for lv in ed.get("levels") or []:
        if (lv.get("kind") or "").lower() in ("50w", "handrem") and row.get("sma50w") and lv.get("price"):
            dp = round((row["sma50w"] / lv["price"] - 1) * 100, 2)
            add("sma50w", "50-weeks gemiddelde", lv["price"], row["sma50w"], abs(dp) <= TOL["level_pct"], diff_pct=dp)
            break

    n = len(items)
    agree = round(100 * sum(1 for x in items if x["match"]) / n) if n else None

    outcome = None
    end_d = (date.fromisoformat(d) + timedelta(days=HORIZON)).isoformat()
    end_row = close_on(hist, end_d)
    if end_row and end_row["date"] >= end_d:
        ret = round((end_row["close"] / row["close"] - 1) * 100, 2)
        outcome = {"horizon_days": HORIZON, "start_close": row["close"], "end_close": end_row["close"],
                   "end_date": end_row["date"], "ret_pct": ret,
                   "alpha_hit": dir_hit(st, ret), "model_hit": dir_hit(row["bias"], ret)}
    return {"id": ed.get("id"), "date": d, "model_date": row["date"], "items": items,
            "agreement_pct": agree, "outcome": outcome}


def scorecard(comps):
    rated = [c for c in comps if c.get("agreement_pct") is not None]
    per = {}
    for c in rated:
        for it in c["items"]:
            p = per.setdefault(it["key"], {"label": it["label"], "n": 0, "match": 0})
            p["n"] += 1
            p["match"] += int(it["match"])
    for p in per.values():
        p["pct"] = round(100 * p["match"] / p["n"])
    out = [c["outcome"] for c in comps if c.get("outcome")]
    a = [o["alpha_hit"] for o in out if o["alpha_hit"] is not None]
    m = [o["model_hit"] for o in out if o["model_hit"] is not None]
    return {
        "editions": len(comps), "rated": len(rated),
        "avg_agreement": round(sum(c["agreement_pct"] for c in rated) / len(rated)) if rated else None,
        "per_item": per,
        "outcomes": len(out),
        "alpha_hit_pct": round(100 * sum(a) / len(a)) if a else None, "alpha_hits_n": len(a),
        "model_hit_pct": round(100 * sum(m) / len(m)) if m else None, "model_hits_n": len(m),
        "horizon_days": HORIZON, "tolerances": TOL,
    }


def main():
    model = json.load(open(sys.argv[1]))
    eds = sorted(json.load(open(sys.argv[2])), key=lambda e: e["date"])
    comps = [compare(model, e) for e in eds]
    json.dump({"comparisons": comps, "scorecard": scorecard(comps), "model_as_of": model.get("as_of")},
              sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
