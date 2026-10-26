"""The short side: dt/shorts.py on hand-built sessions, dt/sim.py played mirrored, the config ids, the scorecard."""

import os
import sys
import unittest

from _dt_fixtures import make_session, DAY, VOL, ROOT, CACHE
from _load import load

sys.path.insert(0, ROOT)
from dt import setups, shorts, sim, bars  # noqa: E402

scorecard = load("trader-scorecard", "scorecard")

CTX = {"window": (2, 71)}
SHORT = {c["id"]: c for c in shorts.configs()}
TSLA = bars.bars_of(bars.load_store("TSLA", "bars5m", CACHE)["sessions"][DAY])


def run(cid, sess, ctx=None):
    return setups.run_config(SHORT[cid], sess, dict(CTX, **(ctx or {})))


# the 15-minute range 99.0 .. 101.0 (mid 100.0), then a quiet bar, then a close below the low
DOWN_ROWS = [(100, 101.0, 99.0, 100.5, VOL), (100.5, 100.8, 99.5, 100.2, VOL), (100.2, 100.9, 99.8, 100.6, VOL),
             (100.6, 100.7, 99.4, 99.6, VOL), (99.6, 99.7, 98.6, 98.8, VOL)]


class Registry(unittest.TestCase):
    def test_nineteen_short_configs_beside_the_thirty_one_long_ones(self):
        self.assertEqual(len(setups.configs()), 31)                     # the long library, untouched
        self.assertEqual(len(shorts.configs()), 19)
        every = setups.all_configs()
        self.assertEqual(len(every), 50)
        self.assertEqual(len(set(c["id"] for c in every)), 50)
        self.assertTrue(all(c["id"].endswith("|short") and c["side"] == "short" for c in shorts.configs()))
        self.assertTrue(all("side" not in c for c in setups.configs()))
        self.assertEqual(setups.configs(sides=("short",)), shorts.configs())
        self.assertEqual([c["id"] for c in every[:31]], [c["id"] for c in setups.configs()])
        self.assertIn("orb15_down|r=1|stop=or_high|short", SHORT)
        self.assertEqual(setups.side_of(SHORT["gap_fade|r=1|short"]), "short")
        self.assertEqual(setups.side_of({"id": "orb15|r=1"}), "long")
        self.assertIn("(short)", SHORT["lod_break|r=2|short"]["label"])


class OrbDown(unittest.TestCase):
    def test_fires_on_the_first_close_below_the_range_low(self):
        sess = make_session(rows=DOWN_ROWS)
        sig = run("orb15_down|r=1|short", sess)
        self.assertIsNotNone(sig)
        self.assertEqual((sig["side"], sig["k"], sig["entry"], sig["stop"], sig["target"]), ("short", 4, 98.8, 100.0, 97.6))
        self.assertEqual(sig["risk_per_share"], 1.2)
        self.assertEqual(sig["opening_range"], {"high": 101.0, "low": 99.0, "minutes": 15})
        self.assertEqual(sig["entry_time"], "9:55 AM")
        self.assertTrue(sig["target"] < sig["entry"] < sig["stop"])

    def test_the_range_high_stop_and_the_target_multiples(self):
        sess = make_session(rows=DOWN_ROWS)
        hi = run("orb15_down|r=1|stop=or_high|short", sess)
        self.assertEqual((hi["stop"], hi["target"]), (101.0, 96.6))
        two = run("orb15_down|r=2|short", sess)
        self.assertEqual(two["target"], round(98.8 - 2 * 1.2, 2))

    def test_does_not_fire_without_a_close_below_the_low(self):
        rows = DOWN_ROWS[:3] + [(100.6, 100.7, 99.0, 99.3, VOL)]        # touches but closes above 99.0
        self.assertIsNone(run("orb15_down|r=1|short", make_session(rows=rows)))
        self.assertIsNone(run("orb15_down|r=1|short", TSLA))               # the reel's day broke OUT, not down

    def test_orb30_waits_for_six_bars(self):
        sess = make_session(rows=DOWN_ROWS)
        self.assertIsNone(run("orb30_down|r=1|short", sess))             # the 30-minute range is not done at bar 4
        rows = [(100, 101.0, 99.0, 100.5, VOL)] * 6 + [(100.5, 100.6, 98.5, 98.7, VOL)]
        sig = run("orb30_down|r=1|short", make_session(rows=rows))
        self.assertEqual((sig["k"], sig["entry"], sig["stop"]), (6, 98.7, 100.0))

    def test_a_stop_too_tight_is_no_signal(self):
        rows = [(100, 100.02, 99.99, 100.0, VOL)] * 3 + [(100.0, 100.01, 99.97, 99.98, VOL)]
        self.assertIsNone(run("orb15_down|r=1|short", make_session(rows=rows)))


class VwapReject(unittest.TestCase):
    def rows(self):
        flat = [(100, 100, 100, 100, VOL)] * 6                            # VWAP sits at 100
        below = [(100, 100, 99, 99.5, VOL)] * 3                           # three closes under it
        attempt = [(99.5, 100.6, 99.4, 100.3, VOL)]                       # one close above
        reject = [(100.3, 100.4, 99.2, 99.4, VOL)]                        # and back below
        return flat + below + attempt + reject

    def test_fires_on_the_close_back_below(self):
        sig = run("vwap_reject|r=1|short", make_session(rows=self.rows()))
        self.assertIsNotNone(sig)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["stop_rule"]), (10, 99.4, 100.6, "reject_high"))
        self.assertAlmostEqual(sig["target"], round(99.4 - (100.6 - 99.4), 2), 2)

    def test_needs_the_attempt(self):
        rows = self.rows()
        rows[9] = (99.5, 99.9, 99.4, 99.6, VOL)                           # no close above VWAP
        self.assertIsNone(run("vwap_reject|r=1|short", make_session(rows=rows)))


class FailedBreakout(unittest.TestCase):
    def test_a_close_above_then_a_close_back_below_the_range_high(self):
        rows = DOWN_ROWS[:3] + [(100.6, 101.6, 100.4, 101.4, VOL), (101.4, 101.9, 100.5, 100.7, VOL)]
        sig = run("failed_breakout|r=1|short", make_session(rows=rows))
        self.assertIsNotNone(sig)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["stop_rule"], sig["level"]), (4, 100.7, 101.9, "day_high", 101.0))

    def test_no_breakout_no_failure(self):
        self.assertIsNone(run("failed_breakout|r=1|short", make_session(rows=DOWN_ROWS)))


class GapFade(unittest.TestCase):
    def test_gap_up_red_bar_then_a_close_below_its_low(self):
        rows = [(101.5, 101.8, 100.9, 101.1, VOL), (101.1, 101.3, 100.6, 100.7, VOL)]
        sig = run("gap_fade|r=1|short", make_session(rows=rows), {"prev_close": 100.0})
        self.assertIsNotNone(sig)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["stop_rule"], sig["gap_pct"]), (2, 100.7, 101.8, "first_bar_high", 1.5))

    def test_needs_the_gap_and_the_red_bar_and_the_clock(self):
        rows = [(101.5, 101.8, 100.9, 101.1, VOL), (101.1, 101.3, 100.6, 100.7, VOL)]
        self.assertIsNone(run("gap_fade|r=1|short", make_session(rows=rows), {"prev_close": 101.2}))   # gap 0.3 %
        green = [(101.5, 101.8, 100.9, 101.7, VOL), (101.7, 101.8, 100.6, 100.7, VOL)]
        self.assertIsNone(run("gap_fade|r=1|short", make_session(rows=green), {"prev_close": 100.0}))
        late = [(101.5, 101.8, 100.9, 101.1, VOL)] + [(101.1, 101.4, 101.0, 101.2, VOL)] * 11 + [(101.2, 101.3, 100.5, 100.6, VOL)]
        self.assertIsNone(run("gap_fade|r=1|short", make_session(rows=late), {"prev_close": 100.0}))    # bar 12 is after 10:30


class LodBreak(unittest.TestCase):
    def test_breakdown_after_a_tight_consolidation_near_the_low(self):
        early = [(100, 100.5, 99.5, 100.0, VOL)] * 12                      # the low of day is 99.5
        cons = [(99.65, 99.7, 99.55, 99.6, VOL)] * 6                       # tight, near the low
        rows = early + cons + [(99.6, 99.65, 99.2, 99.3, VOL)]
        sig = run("lod_break|r=1|short", make_session(rows=rows))
        self.assertIsNotNone(sig)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["stop_rule"], sig["level"]), (18, 99.3, 99.7, "consolidation_high", 99.5))

    def test_a_wide_consolidation_does_not_count(self):
        early = [(100, 100.5, 99.5, 100.0, VOL)] * 12
        cons = [(100.0, 103.0, 99.6, 100.5, VOL)] * 6                    # 3.4 wide against an ATR near 2
        rows = early + cons + [(100.5, 100.6, 99.2, 99.3, VOL)]
        self.assertIsNone(run("lod_break|r=1|short", make_session(rows=rows)))


class PmBreakdown(unittest.TestCase):
    def test_fires_from_two_pm(self):
        rows = [(100, 100.5, 99.5, 100.0, VOL)] * 54 + [(100.0, 100.1, 99.0, 99.2, VOL)]
        sig = run("pm_breakdown|r=1|short", make_session(rows=rows))
        self.assertIsNotNone(sig)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["stop_rule"]), (54, 99.2, 100.5, "high_of_last_6"))
        early = [(100, 100.5, 99.5, 100.0, VOL)] * 30 + [(100.0, 100.1, 99.0, 99.2, VOL)]
        self.assertIsNone(run("pm_breakdown|r=1|short", make_session(rows=early)))


class PlayedMirrored(unittest.TestCase):
    def signal(self):
        return {"setup": "orb15_down", "side": "short", "params": {}, "k": 4, "entry": 98.8, "stop": 100.0, "target": 97.6,
                "target_r": 1.0, "entry_time": "9:55 AM", "risk_per_share": 1.2}

    def test_stop_above_hit_on_the_high_before_the_target(self):
        rows = DOWN_ROWS + [(98.8, 99.0, 98.5, 98.9, VOL), (98.9, 100.3, 97.5, 98.0, VOL)]   # a bar touching both: the stop
        out = sim.play(make_session(rows=rows), self.signal(), "next_open", cost_bps=0)
        self.assertEqual((out["side"], out["outcome"], out["exit_slot"], out["exit_price"], out["entry_fill"]), ("short", "stop", 6, 100.0, 98.8))
        self.assertAlmostEqual(out["R_gross"], -1.0, 4)
        self.assertEqual(out["R_net"], out["R_gross"])
        self.assertLessEqual(out["mae_R"], -1.0)

    def test_target_below_hit_on_the_low(self):
        rows = DOWN_ROWS + [(98.8, 98.9, 98.5, 98.6, VOL), (98.6, 98.7, 97.4, 97.5, VOL)]
        out = sim.play(make_session(rows=rows), self.signal(), "next_open", cost_bps=4)
        self.assertEqual((out["outcome"], out["exit_slot"], out["exit_price"]), ("target", 6, 97.6))
        self.assertAlmostEqual(out["R_gross"], 1.0, 4)
        self.assertAlmostEqual(out["R_net"], 1.0 - 98.8 * 4 / 10000.0 / 1.2, 4)
        self.assertGreaterEqual(out["mfe_R"], 1.0)

    def test_flat_at_the_close_and_the_borrow_assumption(self):
        rows = DOWN_ROWS + [(98.8, 99.0, 98.4, 98.5, VOL)]
        sess = make_session(rows=rows)
        out = sim.play(sess, self.signal(), "next_open", cost_bps=0)
        self.assertEqual((out["outcome"], out["exit_slot"]), ("close", 76))
        self.assertAlmostEqual(out["R_gross"], (98.8 - sess[76]["c"]) / 1.2, 4)
        with_borrow = sim.play(sess, self.signal(), "next_open", cost_bps=0, borrow_bps_day=50)
        self.assertLess(with_borrow["R_net"], out["R_net"])
        self.assertEqual(with_borrow["borrow_bps_day"], 50)
        self.assertAlmostEqual(with_borrow["R_net"], out["R_gross"] - 98.8 * 50 / 10000.0 * (72 / 78.0) / 1.2, 4)

    def test_plan_fill_and_no_bar_left(self):
        rows = DOWN_ROWS + [(98.8, 98.9, 98.5, 98.6, VOL)]
        sess = make_session(rows=rows)
        out = sim.play(sess, self.signal(), "close", cost_bps=0)
        self.assertEqual(out["entry_fill"], 98.8)
        self.assertIsNone(sim.play(sess, dict(self.signal(), k=76), "next_open"))
        self.assertIsNone(sim.play(sess, dict(self.signal(), stop=98.0), "next_open"))   # a stop under the entry is no short

    def test_long_signals_are_untouched(self):
        long = {"setup": "orb15", "params": {}, "k": 3, "entry": 100.6, "stop": 100.0, "target": 101.2, "target_r": 1.0,
                "entry_time": "9:50 AM", "risk_per_share": 0.6}
        rows = [(100, 101.0, 99.0, 100.5, VOL)] * 3 + [(100.5, 100.7, 100.3, 100.6, VOL), (100.6, 101.3, 100.5, 101.1, VOL)]
        out = sim.play(make_session(rows=rows), long, "next_open", cost_bps=0)
        self.assertNotIn("side", out)
        self.assertEqual((out["outcome"], out["exit_price"]), ("target", 101.2))


class ScorecardReplaysShorts(unittest.TestCase):
    def test_normalize_keeps_the_side_and_the_mirrored_risk(self):
        sig = scorecard.normalize_signal({"side": "short", "entry": 129.2, "stop": 130.09, "target": 128.31, "k": 7})
        self.assertEqual((sig["side"], sig["risk_per_share"], sig["target_r"], sig["entry_time"]), ("short", 0.89, 1.0, "10:10 AM"))
        with self.assertRaises(scorecard.ScorecardError):
            scorecard.normalize_signal({"side": "short", "entry": 129.2, "stop": 128.0, "target": 130.0, "k": 7})
        with self.assertRaises(scorecard.ScorecardError):
            scorecard.normalize_signal({"entry": 129.2, "stop": 130.09, "target": 128.31, "k": 7})        # a long the wrong way round


if __name__ == "__main__":
    unittest.main()
