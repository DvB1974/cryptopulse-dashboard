#!/usr/bin/env python3
"""Tests for the BTC regime model. Standard library only: python3 -m unittest discover -s model

They guard the properties the dashboard relies on:
- no look-ahead: the state of day t does not change when later days are added
- the 50W check only flips with the 2% / 2-week buffer
- data repairs (missing day, bad tick) are applied and reported
- the published probability stays inside the calibrated range
- past days keep the macro score that was published for them
"""
import json
import random
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import btc_model as M  # noqa: E402
import compare as C  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"


def synthetic(n=1400, seed=3):
    """Random walk with drift and regime changes; deterministic."""
    rnd = random.Random(seed)
    d, p, rows = date(2019, 1, 1), 10000.0, []
    for i in range(n):
        drift = 0.002 if (i // 250) % 2 == 0 else -0.002
        o = p
        c = max(100.0, o * (1 + drift + rnd.gauss(0, 0.03)))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.01)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.01)))
        rows.append((d + timedelta(days=i), o, h, l, c, 1000.0))
        p = c
    return rows


class ZigzagTest(unittest.TestCase):
    def test_pivots_alternate_and_respect_threshold(self):
        rows = synthetic()
        H, L = [r[2] for r in rows], [r[3] for r in rows]
        piv, _ = M.zigzag(H, L, 0.20)
        self.assertGreater(len(piv), 4)
        for (k1, i1), (k2, i2) in zip(piv, piv[1:]):
            self.assertNotEqual(k1, k2)
            self.assertLess(i1, i2)
            if k1 == "L":
                self.assertGreaterEqual(H[i2], L[i1] * 1.20)
            else:
                self.assertLessEqual(L[i2], H[i1] * 0.80)


class HandremTest(unittest.TestCase):
    def setUp(self):
        self.base = [100.0] * 50   # 50W SMA = 100, state starts as "not above"

    def state(self, extra):
        return M.handrem_states(self.base + extra)[-1][1]

    def test_small_close_above_does_not_flip(self):
        self.assertFalse(self.state([101.0]))

    def test_close_beyond_margin_flips(self):
        self.assertTrue(self.state([103.5]))

    def test_two_closes_in_a_row_flip(self):
        self.assertTrue(self.state([101.0, 101.5]))

    def test_flip_back_needs_buffer_too(self):
        up = [103.5]
        self.assertTrue(self.state(up + [99.9]))          # one close just below: still on
        self.assertFalse(self.state(up + [99.9, 99.8]))   # second close below: off


class IntegrityTest(unittest.TestCase):
    def test_missing_day_is_filled_and_reported(self):
        rows = synthetic(30)
        del rows[10]
        self.assertEqual(len(M.missing_days(rows)), 1)
        clean, rep = M.clean_days(rows)
        self.assertEqual(len(clean), 30)
        self.assertEqual(rep["filled_days"], [(date(2019, 1, 1) + timedelta(days=10)).isoformat()])
        self.assertEqual(clean[10][4], clean[9][4])       # previous close carried forward
        self.assertEqual(M.missing_days(clean), [])

    def test_bad_tick_is_cut_back(self):
        rows = synthetic(30)
        d, o, h, l, c, v = rows[5]
        rows[5] = (d, o, h, 0.06, c, v)
        clean, rep = M.clean_days(rows)
        self.assertEqual(clean[5][3], min(o, c))
        self.assertEqual(rep["bad_ticks"][0]["date"], d.isoformat())

    def test_farside_date(self):
        self.assertEqual(M.farside_date("06 Nov 2026"), "2026-11-06")


class EvaluateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.S = M.Series(synthetic())

    def test_no_look_ahead(self):
        """The state of day t is identical whether or not later days exist."""
        for t in (700, 900, 1100, 1300):
            cut = M.Series(self.S.days[: t + 1])
            self.assertEqual(M.evaluate(self.S, t, {}, full=True), M.evaluate(cut, t, {}, full=True))

    def test_probability_range_and_sum(self):
        lo, hi = M.CONFIG["prob_clamp"]
        for t in range(600, 1400, 37):
            st = M.evaluate(self.S, t, {})
            sc = st["scenarios"]
            self.assertTrue(lo <= sc["bull"] <= hi, sc)
            self.assertTrue(5 <= st["bull_raw"] <= 95)
            self.assertEqual(sc["bull"] + sc["mild_bear"] + sc["deep_bear"], 100)
            self.assertEqual(st["base_case"] == "bull", sc["bull"] >= 50)

    def test_frozen_macro_score_wins(self):
        a = M.evaluate(self.S, 1000, {}, macro_score_frozen=3)
        b = M.evaluate(self.S, 1000, {}, macro_score_frozen=-3)
        self.assertEqual(a["macro_score"], 3)
        self.assertEqual(b["macro_score"], -3)
        self.assertGreaterEqual(a["bull_raw"], b["bull_raw"])
        self.assertGreater(a["bias_points"], b["bias_points"])


class CompareTest(unittest.TestCase):
    def test_direction_hit(self):
        self.assertTrue(C.dir_hit("bullish", 3.0))
        self.assertFalse(C.dir_hit("bearish", 3.0))
        self.assertTrue(C.dir_hit("neutraal", -4.9))
        self.assertFalse(C.dir_hit("neutraal", 6.0))
        self.assertIsNone(C.dir_hit(None, 1.0))

    def test_model_row_is_the_day_before_publication(self):
        hist = [{"date": "2026-01-0%d" % i} for i in range(1, 6)]
        self.assertEqual(C.model_row(hist, "2026-01-04")["date"], "2026-01-03")


@unittest.skipUnless((DATA / "btc-daily.json").exists() and (DATA / "btc-model.json").exists(), "repo data not present")
class PublishedDataTest(unittest.TestCase):
    """Checks on the real files in data/."""

    @classmethod
    def setUpClass(cls):
        rows = json.loads((DATA / "btc-daily.json").read_text())["rows"]
        days = [(date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]
        cls.days, cls.report = M.clean_days(days)
        cls.out = json.loads((DATA / "btc-model.json").read_text())

    def test_price_history_has_no_holes(self):
        self.assertEqual(self.report["filled_days"], [])

    def test_no_look_ahead_on_real_prices(self):
        S = M.Series(self.days)
        rnd = random.Random(11)
        for t in rnd.sample(range(1500, len(self.days) - 1), 8):
            cut = M.Series(self.days[: t + 1])
            self.assertEqual(M.evaluate(S, t, {}, full=True), M.evaluate(cut, t, {}, full=True))

    def test_published_history_is_consistent(self):
        hist = self.out["history"]
        self.assertEqual(hist[-1]["date"], self.out["as_of"])
        dates = [date.fromisoformat(r["date"]) for r in hist]
        self.assertTrue(all((b - a).days == 1 for a, b in zip(dates, dates[1:])))
        lo, hi = self.out["config"]["prob_clamp"]
        for r in hist:
            self.assertTrue(lo <= r["scenarios"]["bull"] <= hi)

    def test_history_matches_ledger(self):
        led = json.loads((DATA / "btc-model-ledger.json").read_text())["macro_score"]
        for r in self.out["history"]:
            if r["date"] in led:
                self.assertEqual(r["macro_score"], led[r["date"]], r["date"])


if __name__ == "__main__":
    unittest.main()
