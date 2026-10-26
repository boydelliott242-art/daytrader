"""dt/regimes.py: the seven classes, rule by rule, and the tag on the daily context."""

import sys
import unittest

from _dt_fixtures import daily_rows, ROOT, CACHE

sys.path.insert(0, ROOT)
from dt import regimes, regime, bars  # noqa: E402


class Rules(unittest.TestCase):
    def test_each_rule_in_order(self):
        c = regimes.classify
        self.assertEqual(c(vix=31)[0], "crisis")
        self.assertEqual(c(trend="trend_down", drawdown_pct=-16)[0], "crisis")
        self.assertEqual(c(trend="trend_up", drawdown_pct=-16)[0], "weak_bull")           # a drawdown alone is not a crisis
        self.assertEqual(c(vix=26, trend="trend_up")[0], "high_vol")
        self.assertEqual(c(vol_pctile=0.85, trend="trend_up")[0], "high_vol")
        self.assertEqual(c(trend="trend_down", vix=18)[0], "bear")
        self.assertEqual(c(trend="trend_flat", crossed_within=True)[0], "transitional")
        self.assertEqual(c(trend="trend_up", sma50_rising=True)[0], "strong_bull")
        self.assertEqual(c(trend="trend_up", sma50_rising=True, breadth_pct=55)[0], "weak_bull")
        self.assertEqual(c(trend="trend_up", sma50_rising=True, breadth_pct=70)[0], "strong_bull")
        self.assertEqual(c(trend="trend_up", sma50_rising=False)[0], "weak_bull")
        self.assertEqual(c(trend="trend_flat")[0], "range")
        self.assertEqual(c()[0], "range")
        for cls in regimes.CLASSES:
            self.assertTrue(regimes.tag(cls).startswith("regime_"))
        self.assertIn("VIX", c(vix=31)[1])

    def test_fit_by_side(self):
        self.assertEqual(regimes.fit("short", "strong_bull"), "against")
        self.assertEqual(regimes.fit("long", "strong_bull"), "for")
        self.assertEqual(regimes.fit("long", "crisis"), "against")
        self.assertEqual(regimes.fit("short", "bear"), "for")
        self.assertEqual(regimes.fit("short", "range"), "neutral")
        self.assertEqual(regimes.fit(None, "weak_bull"), "for")


class FromRows(unittest.TestCase):
    def test_inputs_from_a_rising_line(self):
        rows = daily_rows(100.0, 320, 0.5, 1.0)
        inp = regimes.inputs_from_rows("2027-12-31", rows)
        self.assertEqual(inp["trend"], "trend_up")
        self.assertTrue(inp["sma50_rising"])
        self.assertFalse(inp["crossed_within_10"])
        self.assertAlmostEqual(inp["drawdown_pct"], (rows[-1][4] / (rows[-1][4] + 0.5) - 1) * 100, 2)
        self.assertEqual(regimes.classify_rows("2027-12-31", rows)["class"], "strong_bull")

    def test_a_cross_is_transitional_and_a_fall_is_bear(self):
        rows = daily_rows(100.0, 200, 0.5, 1.0) + daily_rows(200.0, 60, -0.3, 1.0, first="2027-04-12")
        doc = regimes.classify_rows("2027-12-31", rows)
        self.assertEqual(doc["class"], "bear")                                     # down 9 %: a bear, not a crisis
        crash = daily_rows(100.0, 200, 0.5, 1.0) + daily_rows(200.0, 60, -1.0, 1.0, first="2027-04-12")
        self.assertEqual(regimes.classify_rows("2027-12-31", crash)["class"], "crisis")   # down 30 % in a downtrend
        turn = daily_rows(100.0, 200, 0.5, 1.0) + daily_rows(200.0, 12, -3.0, 1.0, first="2027-04-12")
        doc = regimes.classify_rows("2027-12-31", turn)
        self.assertIn(doc["class"], ("transitional", "bear", "range", "high_vol"))
        self.assertTrue(doc["inputs"]["crossed_within_10"] or doc["class"] == "bear")

    def test_short_history_is_range_by_default(self):
        doc = regimes.classify_rows("2027-12-31", daily_rows(100.0, 10, 0.5))
        self.assertEqual((doc["class"], doc["inputs"]["trend"]), ("range", None))
        self.assertIn("not enough", doc["rule"])


class OnTheContext(unittest.TestCase):
    def test_the_daily_context_carries_the_class_and_its_tag(self):
        spy = bars.daily_rows("SPY", CACHE)
        vix = bars.daily_rows("^VIX", CACHE)
        dc = regime.daily_context("2026-10-19", spy, vix)
        self.assertIn(dc["class"], regimes.CLASSES)
        self.assertEqual(dc["tags"][-1], "regime_" + dc["class"])
        self.assertTrue(dc["class_rule"])
        self.assertEqual(dc["class_inputs"]["vix"], dc["vix"])
        self.assertEqual(regime.session_tags("2026-10-19", {"SPY": spy, "^VIX": vix})[-1], "regime_" + dc["class"])


if __name__ == "__main__":
    unittest.main()
