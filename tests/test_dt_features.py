"""dt/features.py on hand-built daily rows: every value derived from the rows in the test, nothing from the date or after."""

import math
import sys
import unittest

from _dt_fixtures import daily_rows, make_session, ROOT, CACHE

sys.path.insert(0, ROOT)
from dt import features, bars  # noqa: E402


def closes(rows):
    return [r[4] for r in rows]


class Windows(unittest.TestCase):
    def test_rate_of_change_and_averages_on_a_straight_line(self):
        rows = daily_rows(100.0, 320, 0.5, 0.5)                        # +0.5 a day, 320 weekdays, a 0.5 range
        date = "2027-12-31"                                            # after every row
        f = features.features_for("T", date, rows)
        c = closes(rows)
        for n in (5, 20, 63, 126, 252):
            self.assertAlmostEqual(f["roc_%d" % n], (c[-1] / c[-1 - n] - 1) * 100, 3, n)
        self.assertEqual(f["ma_structure"], "up")
        self.assertEqual(f["weekly_structure"], "up")
        self.assertGreater(f["sma20_slope_pct"], 0)
        self.assertEqual(f["rsi_14"], 100.0)                           # only gains
        self.assertTrue(f["breakout_20d"])
        self.assertFalse(f["breakdown_20d"])
        self.assertEqual(f["up_days_20"], 1.0)
        self.assertAlmostEqual(f["trend_r2_63"], 1.0, 2)
        self.assertEqual(f["mtf_agreement"], "agree_up")
        self.assertEqual(f["as_of"], rows[-1][0])
        self.assertAlmostEqual(f["atr_14"], 0.75, 6)                    # true range: today's high over yesterday's close
        self.assertAlmostEqual(f["atr_pct"], 0.75 / c[-1] * 100, 4)
        self.assertAlmostEqual(f["adv_usd_20"], sum(r[4] * r[5] for r in rows[-20:]) / 20.0, 0)
        self.assertEqual(f["dist_52w_high_pct"], round((c[-1] / (c[-1] + 0.25) - 1) * 100, 4))
        self.assertEqual(f["missing"], ["rs_spy_20", "rs_spy_63", "rs_sector_20", "gap_pct", "beta_spy_63"])

    def test_nothing_from_the_date_or_after(self):
        rows = daily_rows(100.0, 320, 0.5, 1.0)
        mid = rows[200][0]
        f = features.features_for("T", mid, rows)
        g = features.features_for("T", mid, rows[:200])                # the same rows, cut at the date
        self.assertEqual(f["as_of"], rows[199][0])
        for k in features.NUMERIC:
            self.assertEqual(f[k], g[k], k)

    def test_a_falling_line_and_the_relative_strength(self):
        rows = daily_rows(200.0, 320, -0.5, 0.5)
        spy = daily_rows(100.0, 320, 0.1, 1.0)
        f = features.features_for("T", "2027-12-31", rows, None, spy, spy)
        self.assertEqual(f["ma_structure"], "down")
        self.assertEqual(f["rsi_14"], 0.0)
        self.assertTrue(f["breakdown_20d"])
        self.assertEqual(f["mtf_agreement"], "agree_down")
        c, s = closes(rows), closes(spy)
        self.assertAlmostEqual(f["rs_spy_20"], (c[-1] / c[-21] - 1) * 100 - (s[-1] / s[-21] - 1) * 100, 3)
        self.assertEqual(f["rs_sector_20"], f["rs_spy_20"])
        self.assertLess(f["rs_spy_63"], 0)

    def test_beta_is_one_against_itself_and_aligned_by_date(self):
        rows = daily_rows(100.0, 120, 0.3, 2.0)
        for i, r in enumerate(rows):                                   # a wobble so the returns are not constant
            r[4] = r[4] + (1.0 if i % 3 == 0 else -0.7)
        f = features.features_for("T", "2027-12-31", rows, None, rows)
        self.assertAlmostEqual(f["beta_spy_63"], 1.0, 3)
        shifted = [[rows[i + 1][0]] + rows[i][1:] for i in range(len(rows) - 1)]   # the same prices a day late
        g = features.features_for("T", "2027-12-31", rows, None, shifted)
        self.assertNotAlmostEqual(g["beta_spy_63"] or 0.0, 1.0, 1)        # misaligned returns are not a beta of 1
        a, b = features.aligned_closes(rows[:5], rows[2:7])
        self.assertEqual((len(a), a, b), (3, closes(rows[2:5]), closes(rows[2:5])))

    def test_too_little_history_is_missing_not_guessed(self):
        f = features.features_for("T", "2027-12-31", daily_rows(100.0, 10, 0.5))
        self.assertIsNone(f["roc_20"])
        self.assertIsNone(f["adx_14"])
        self.assertIn("roc_20", f["missing"])
        self.assertIn("weekly_roc_13", f["missing"])
        self.assertEqual(features.features_for("T", "2027-12-31", [])["missing"], list(features.NAMES))
        self.assertEqual(features.features_for("T", "2026-07-01", daily_rows(100.0, 10))["missing"], list(features.NAMES))


class Indicators(unittest.TestCase):
    def test_rsi_macd_adx_by_hand(self):
        up = [100 + i for i in range(30)]
        self.assertEqual(features.rsi(up, 14), 100.0)
        alt = [100 + (1 if i % 2 else 0) for i in range(30)]
        self.assertAlmostEqual(features.rsi(alt, 14), 50.0, delta=5)    # gains and losses alternate
        self.assertIsNone(features.rsi(up[:10], 14))
        m, s, h = features.macd([100.0] * 60)
        self.assertEqual((m, s, h), (0.0, 0.0, 0.0))                     # a flat line has no momentum
        self.assertEqual(features.macd([100.0] * 10), (None, None, None))
        rows = daily_rows(100.0, 60, 1.0, 0.5)
        self.assertGreater(features.adx(rows, 14), 50)                   # a straight trend is a strong trend
        self.assertIsNone(features.adx(rows[:20], 14))
        flat = daily_rows(100.0, 60, 0.0, 1.0)
        self.assertEqual(features.adx(flat, 14), 0.0)

    def test_realized_vol_and_r_squared(self):
        self.assertIsNone(features.realized_vol([100.0] * 5, 20))
        self.assertAlmostEqual(features.realized_vol([100.0] * 25, 20), 0.0, 9)
        self.assertAlmostEqual(features.r_squared([math.exp(i * 0.01) for i in range(40)]), 1.0, 9)
        self.assertIsNone(features.r_squared([1.0, 1.0, 1.0]))

    def test_weekly_closes_take_the_last_row_of_each_week(self):
        rows = daily_rows(100.0, 12, 1.0, 1.0, first="2026-07-01")      # Wed Jul 1 .. Thu Jul 16
        wk = features.weekly_closes(rows)
        self.assertEqual(len(wk), 3)
        self.assertEqual(wk[0], rows[2][4])                             # Fri Jul 3 closes the first week
        self.assertEqual(wk[-1], rows[-1][4])


class Intraday(unittest.TestCase):
    def test_the_session_through_bar_k_only(self):
        rows = [(100, 101.0, 99.0, 100.5, 1000), (100.5, 100.8, 99.5, 100.2, 1000), (100.2, 100.9, 99.8, 100.6, 1000),
                (100.6, 101.4, 100.5, 101.2, 2000), (101.2, 101.5, 100.9, 101.0, 500)]
        sess = make_session(rows=rows)
        f = features.features_intraday(sess, 3, {"prev_close": 100.0, "daily_atr": 2.0, "avg_vol20": 78000})
        self.assertEqual((f["k"], f["bars"], f["close"], f["or_position"]), (3, 4, 101.2, "above"))
        self.assertAlmostEqual(f["move_from_open_pct"], 1.2, 4)
        self.assertAlmostEqual(f["move_from_prev_close_pct"], 1.2, 4)
        self.assertAlmostEqual(f["range_used_atr"], (101.4 - 99.0) / 2.0, 4)
        self.assertAlmostEqual(f["relvol_so_far"], 5000 / (78000 * 4 / 78.0), 4)
        self.assertTrue(f["above_vwap"])
        self.assertEqual(f["missing"], [])
        g = features.features_intraday(sess, 1, {})
        self.assertNotIn("or_position", g)                              # the range is not done at bar 1
        self.assertIn("range_used_atr", g["missing"])


class RealStore(unittest.TestCase):
    def test_the_fixture_cache_computes_every_feature_fast(self):
        import time
        aapl = bars.daily_rows("TSLA", CACHE)
        spy = bars.daily_rows("SPY", CACHE)
        t = time.time()
        f = features.features_for("TSLA", "2026-10-19", aapl, None, spy)
        self.assertLess(time.time() - t, 0.5)
        self.assertEqual(f["as_of"], "2026-10-16")
        self.assertTrue(len(f["missing"]) <= 3, f["missing"])          # the fixture has a year of rows
        lines = features.summary_lines(f)
        self.assertTrue(any("vs SPY" in x for x in lines))


if __name__ == "__main__":
    unittest.main()
