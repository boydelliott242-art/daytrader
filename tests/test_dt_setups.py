"""dt.setups: every one of the 12 setups on a hand-built session that fires it (exact k, entry,
stop, target), one that does not, and a look-ahead guard; the registry; the TSLA fixture. Offline."""

import copy
import unittest

from _dt_fixtures import CACHE, DAY, make_session, rows_from_closes, VOL
from dt import bars, clock, setups

CONFIGS = {c["id"]: c for c in setups.configs()}
TSLA = bars.bars_of(bars.load_store("TSLA", "bars5m", CACHE)["sessions"][DAY])
SPY = bars.bars_of(bars.load_store("SPY", "bars5m", CACHE)["sessions"][DAY])
TSLA_PREV_CLOSE = bars.prev_close(bars.daily_rows("TSLA", CACHE), DAY)


def run(cid, sess, ctx=None, **params):
    cfg = CONFIGS[cid]
    if params:
        cfg = dict(cfg, params=dict(cfg["params"], **params))
    return setups.run_config(cfg, sess, dict(ctx or {}))


def wreck(sess, after_k):
    """The same session with every bar after k turned into nonsense (a look-ahead would notice)."""
    out = copy.deepcopy(sess)
    for j in range(after_k + 1, len(out)):
        out[j].update({"o": 1.0, "h": 99999.0, "l": 0.01, "c": 50000.0, "v": 10 ** 9})
    return out


class Guard(unittest.TestCase):
    """Shared assertions: a signal must not change when the bars after k change, and the
    prefix bars[:k+1] (what a live run knows at the time) must give the same signal."""

    def assertNoLookAhead(self, cid, sess, ctx=None, **params):
        sig = run(cid, sess, ctx, **params)
        self.assertIsNotNone(sig, cid)
        k = sig["k"]
        self.assertEqual(run(cid, wreck(sess, k), ctx, **params), sig, "%s read bars after k" % cid)
        self.assertEqual(run(cid, sess[:k + 1], ctx, **params), sig, "%s differs on the live prefix" % cid)
        self.assertIsNone(run(cid, sess[:k], ctx, **params), "%s fired before its bar closed" % cid)
        return sig

    def assertSignalShape(self, sig, setup):
        for key in ("setup", "params", "k", "entry", "stop", "target", "target_r", "stop_rule", "entry_time",
                    "risk_per_share", "opening_range", "level"):
            self.assertIn(key, sig, key)
        self.assertEqual(sig["setup"], setup)
        self.assertLess(sig["stop"], sig["entry"])
        self.assertGreater(sig["target"], sig["entry"])
        self.assertAlmostEqual(sig["risk_per_share"], round(sig["entry"] - sig["stop"], 2))
        self.assertEqual(sig["entry_time"], clock.clock12(clock.slot_close_minutes(sig["k"])))
        self.assertEqual(sig["stop"], bars.cents_down(sig["stop"]))
        self.assertEqual(sig["target"], bars.half_up(sig["target"]))
        self.assertGreaterEqual(sig["entry"] - sig["stop"], sig["entry"] * setups.MIN_RISK_FRAC)


# ---------------------------------------------------------------- the opening-range family

ORB_ROWS = [(100, 101.0, 99.0, 100.5), (100.5, 100.8, 99.5, 100.2), (100.2, 100.9, 99.8, 100.6),   # the 15-minute range
            (100.6, 101.2, 100.3, 100.7), (100.7, 101.4, 100.5, 101.0),                              # highs poke above, closes do not
            (101.0, 101.6, 100.8, 101.5)]                                                            # the 9:55 bar closes above


def orb_session(vols=None, rows=None):
    rows = rows or ORB_ROWS
    vols = vols or [VOL] * len(rows)
    return make_session(DAY, rows=[r + (v,) for r, v in zip(rows, vols)])


class Orb15(Guard):
    def test_fires_on_the_first_close_above_the_range(self):
        sess = orb_session()
        sig = self.assertNoLookAhead("orb15|r=1", sess)
        self.assertSignalShape(sig, "orb15")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (5, 101.5, 100.0, 103.0))
        self.assertEqual(sig["opening_range"], {"high": 101.0, "low": 99.0, "minutes": 15})
        self.assertEqual((sig["stop_rule"], sig["entry_time"], sig["risk_per_share"]), ("or_mid", "10:00 AM", 1.5))
        self.assertEqual(run("orb15|r=1.5", sess)["target"], 103.75)
        self.assertEqual(run("orb15|r=2", sess)["target"], 104.5)
        low = run("orb15|r=1|stop=or_low", sess)
        self.assertEqual((low["stop"], low["target"], low["risk_per_share"]), (99.0, 104.0, 2.5))
        self.assertEqual(run("orb15|r=2|stop=or_low", sess)["target"], 106.5)

    def test_a_high_above_the_range_is_not_a_close_above(self):
        rows = ORB_ROWS[:5] + [(101.0, 101.9, 100.8, 101.0)]      # closes exactly on the high: not above
        self.assertIsNone(run("orb15|r=1", orb_session(rows=rows)))
        rows = [(100, 101.0, 99.0, 100.5), (100.5, 100.8, 99.5, 100.2), (100.2, 100.9, 99.8, 100.6)]
        sess = make_session(DAY, rows=[r + (VOL,) for r in rows] + [(100.6, 100.6, 100.6, 100.6, VOL)] * 75)
        self.assertIsNone(run("orb15|r=1", sess))
        self.assertIsNone(run("orb15|r=1", sess[:3]))                 # the range alone is never a signal
        self.assertIsNone(run("orb15|r=1", []))

    def test_the_range_bars_themselves_never_trigger(self):
        rows = [(100, 101.0, 99.0, 100.5), (100.5, 102.0, 99.5, 101.9), (101.9, 102.5, 101.5, 102.4)]
        sess = make_session(DAY, rows=[r + (VOL,) for r in rows])     # then flat at 102.4, inside the 102.5 high
        self.assertIsNone(run("orb15|r=1", sess))

    def test_the_min_risk_rule_drops_a_too_tight_stop(self):
        rows = [(100, 100.02, 99.98, 100.0), (100.0, 100.02, 99.98, 100.0), (100.0, 100.02, 99.98, 100.0),
                (100.0, 100.05, 99.99, 100.03)]                       # mid 100.00, entry 100.03: risk 0.03 < 0.05
        self.assertIsNone(run("orb15|r=1", orb_session(rows=rows)))
        rows[3] = (100.0, 100.08, 99.99, 100.06)                      # risk 0.06 >= 0.05: fine
        sig = run("orb15|r=1", orb_session(rows=rows))
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (3, 100.06, 100.0, 100.12))

    def test_the_entry_window_is_respected(self):
        sess = orb_session()
        self.assertEqual(run("orb15|r=1", sess, {"window": clock.entry_window(DAY)})["k"], 5)
        self.assertIsNone(run("orb15|r=1", sess, {"window": (2, 4)}))
        later = run("orb15|r=1", sess, {"window": (6, 71)})            # the 10:00 close is before the window;
        self.assertEqual(later["k"], 6)                                 # the flat padding still closes above, so
        self.assertEqual(later["entry"], 101.5)                         # the first bar inside the window fires
        self.assertIsNone(run("orb15|r=1", sess[:6], {"window": (6, 71)}))   # nothing inside the window yet
        rows = ORB_ROWS + [(101.5, 101.8, 101.2, 101.7)]
        self.assertEqual(run("orb15|r=1", orb_session(rows=rows), {"window": (6, 71)})["k"], 6)
        # a close above the range after 15:30 is never an entry (the default window ends at slot 71)
        rows = [(100, 101.0, 99.0, 100.5), (100.5, 100.8, 99.5, 100.2), (100.2, 100.9, 99.8, 100.6)]
        rows += [(100.6, 100.9, 100.4, 100.6)] * 69 + [(100.6, 102.0, 100.5, 101.8)] * 6     # slots 72..77
        sess = make_session(DAY, rows=[r + (VOL,) for r in rows])
        self.assertIsNone(run("orb15|r=1", sess))
        self.assertEqual(run("orb15|r=1", sess, {"window": (2, 76)})["k"], 72)
        self.assertEqual(run("orb15|r=1", sess, {"window": (2, 76)})["entry_time"], "3:35 PM")

    def test_the_reel_on_the_tsla_fixture(self):
        sig = self.assertNoLookAhead("orb15|r=1", TSLA)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (8, 364.18, 358.32, 370.04))
        self.assertEqual(sig["opening_range"], {"high": 362.18, "low": 354.47, "minutes": 15})
        self.assertEqual((sig["entry_time"], sig["risk_per_share"], sig["target_r"]), ("10:15 AM", 5.86, 1.0))
        self.assertEqual(run("orb15|r=1.5", TSLA)["target"], 372.97)
        self.assertEqual(run("orb15|r=2", TSLA)["target"], 375.90)           # trader-exit's default 2R
        low = run("orb15|r=1|stop=or_low", TSLA)
        self.assertEqual((low["stop"], low["risk_per_share"], low["target"]), (354.47, 9.71, 373.89))
        self.assertIsNone(run("orb15|r=1", TSLA[:8]))                         # not yet at 10:10
        self.assertEqual(run("orb15|r=1", TSLA[:9]), sig)                     # the 10:10 bar has closed
        self.assertEqual(run("orb15|r=1", TSLA, {"window": (9, 71)})["entry"], 365.05)   # the next close above


class Orb30(Guard):
    def test_fires_after_a_30_minute_range(self):
        rows = [(100, 101.0, 99.0, 100.5)] + [(100.5, 100.9, 99.4, 100.3)] * 5 + [(100.3, 100.9, 100.0, 100.8), (100.8, 101.3, 100.6, 101.2)]
        sess = orb_session(rows=rows)
        sig = self.assertNoLookAhead("orb30|r=1", sess)
        self.assertSignalShape(sig, "orb30")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (7, 101.2, 100.0, 102.4))
        self.assertEqual(sig["opening_range"], {"high": 101.0, "low": 99.0, "minutes": 30})
        self.assertEqual(sig["entry_time"], "10:10 AM")
        self.assertEqual(run("orb30|r=2", sess)["target"], 103.6)

    def test_does_not_fire_inside_the_range(self):
        rows = [(100, 101.0, 99.0, 100.5)] + [(100.5, 100.9, 99.4, 100.3)] * 5 + [(100.3, 100.9, 100.0, 100.8)]
        self.assertIsNone(run("orb30|r=1", orb_session(rows=rows)))
        self.assertIsNone(run("orb30|r=1", orb_session()[:6]))

    def test_on_the_tsla_fixture(self):
        sig = self.assertNoLookAhead("orb30|r=1", TSLA)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (8, 364.18, 358.69, 369.67))
        self.assertEqual(sig["opening_range"], {"high": 362.92, "low": 354.47, "minutes": 30})


class Orb15Vol(Guard):
    def test_fires_only_with_volume_at_least_the_ranges_average(self):
        rows = ORB_ROWS[:4] + [(100.7, 101.4, 100.5, 101.3), (101.3, 101.6, 100.8, 101.5)]
        sess = orb_session(vols=[1000, 1000, 1000, 500, 900, 1000], rows=rows)
        sig = self.assertNoLookAhead("orb15_vol|r=1", sess)
        self.assertSignalShape(sig, "orb15_vol")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (5, 101.5, 100.0, 103.0))   # not bar 4 (0.9x)
        self.assertEqual(sig["vol_x_actual"], 1.0)
        self.assertEqual(sig["params"]["vol_x"], 1.0)
        sess = make_session(DAY, rows=[r + (v,) for r, v in zip(rows, [1000, 1000, 1000, 500, 900, 999])], vol=100)
        self.assertIsNone(run("orb15_vol|r=1", sess))                   # 0.999x, then thin padding (0.1x): nothing
        self.assertEqual(run("orb15|r=1", sess)["k"], 4)                                  # the plain rule fires at bar 4
        sess = orb_session(vols=[1200, 800, 1000, 500, 1000, 999], rows=rows)
        self.assertEqual(run("orb15_vol|r=1", sess)["k"], 4)
        self.assertEqual(run("orb15_vol|r=1", sess)["vol_x_actual"], 1.0)
        sess = orb_session(vols=[0, 0, 0, 500, 1000, 999], rows=rows)
        self.assertIsNone(run("orb15_vol|r=1", sess))                                   # no range volume: never

    def test_on_the_tsla_fixture_the_filter_never_passes(self):
        # The upstream trader-entry says 10:35 AM at 365.75, "7.98x", measured against a different
        # baseline. dt measures the trigger bar against the AVERAGE of the three range bars, and
        # the 9:30 bar's 3.08M lifts that average to 1.66M; no later bar on 2026-10-19 reaches it
        # (the best is 0.75x, the 15:55 bar), so dt's orb15_vol gives no signal on the reel's day.
        range_vol = sum(b["v"] for b in TSLA[:3]) / 3.0
        self.assertEqual(round(range_vol), 1658747)
        self.assertEqual(round(TSLA[12]["c"], 2), 365.75)                 # the upstream's 10:30 bar
        self.assertEqual(round(TSLA[12]["v"] / range_vol, 2), 0.33)
        self.assertLess(max(b["v"] for b in TSLA[3:]) / range_vol, 1.0)
        self.assertIsNone(run("orb15_vol|r=1", TSLA))
        self.assertIsNone(run("orb15_vol|r=1.5", TSLA))
        self.assertIsNone(run("orb15_vol|r=2", TSLA))
        # with the bar lowered to 0.6x the same rule fires on the reel's bar, so the gate is the only difference
        sig = run("orb15_vol|r=1", TSLA, vol_x=0.6)
        self.assertEqual((sig["k"], sig["entry"], sig["vol_x_actual"]), (8, 364.18, 0.6))
        self.assertIsNone(run("orb15_vol|r=1", TSLA, vol_x=0.61))


class Orb15Rs(Guard):
    def test_needs_spy_and_relative_strength(self):
        sess = orb_session()                                           # stock +1.5 % from 100 to 101.5 at bar 5
        spy = make_session(DAY, [100, 100.1, 100.2, 100.3, 100.4, 100.5])   # SPY +0.5 %
        sig = self.assertNoLookAhead("orb15_rs|r=1", sess, {"spy": spy})
        self.assertSignalShape(sig, "orb15_rs")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (5, 101.5, 100.0, 103.0))
        self.assertIsNone(run("orb15_rs|r=1", sess, {}))                         # no SPY: no signal
        self.assertIsNone(run("orb15_rs|r=1", sess, {"spy": None}))
        self.assertIsNone(run("orb15_rs|r=1", sess, {"spy": spy[:5]}))           # SPY's bar 5 not in yet
        strong = make_session(DAY, [100, 100.3, 100.6, 100.9, 101.1, 101.25])   # SPY +1.25 %: edge 0.25 % < 0.3 %
        self.assertIsNone(run("orb15_rs|r=1", sess, {"spy": strong}))
        edge = make_session(DAY, [100, 100.3, 100.6, 100.9, 101.1, 101.2])      # exactly 0.3 %: fires
        self.assertEqual(run("orb15_rs|r=1", sess, {"spy": edge})["k"], 5)
        self.assertEqual(run("orb15_rs|r=1", sess, {"spy": strong}, rs_min=0.002)["k"], 5)

    def test_spy_only_matters_at_the_trigger_bar(self):
        sess = orb_session()
        spy = make_session(DAY, [100, 100.1, 100.2, 100.3, 100.4, 100.5])
        sig = run("orb15_rs|r=1", sess, {"spy": spy})
        self.assertEqual(run("orb15_rs|r=1", sess, {"spy": wreck(spy, 5)}), sig)

    def test_on_the_tsla_fixture(self):
        sig = self.assertNoLookAhead("orb15_rs|r=1", TSLA, {"spy": SPY})
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (8, 364.18, 358.32, 370.04))
        edge = (TSLA[8]["c"] / TSLA[0]["o"] - 1) - (SPY[8]["c"] / SPY[0]["o"] - 1)
        self.assertGreater(edge, 0.003)
        self.assertAlmostEqual(edge, 0.0173, places=4)
        self.assertIsNone(run("orb15_rs|r=1", TSLA))


class Orb15Gap(Guard):
    def test_needs_a_gap_up_from_the_prior_close(self):
        sess = orb_session()                                           # opens at 100
        sig = self.assertNoLookAhead("orb15_gap|r=1", sess, {"prev_close": 99.4})   # +0.60 %
        self.assertSignalShape(sig, "orb15_gap")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (5, 101.5, 100.0, 103.0))
        self.assertEqual(sig["gap_pct"], 0.6)
        self.assertIsNone(run("orb15_gap|r=1", sess, {}))                            # no prior close: no signal
        self.assertIsNone(run("orb15_gap|r=1", sess, {"prev_close": None}))
        self.assertIsNone(run("orb15_gap|r=1", sess, {"prev_close": 99.6}))          # +0.40 %
        self.assertIsNone(run("orb15_gap|r=1", sess, {"prev_close": 100.6}))         # a gap down
        self.assertEqual(run("orb15_gap|r=1", sess, {"prev_close": 100.0 / 1.005})["gap_pct"], 0.5)   # exactly 0.5 %
        self.assertEqual(run("orb15_gap|r=2", sess, {"prev_close": 99.4})["target"], 104.5)

    def test_on_the_tsla_fixture(self):
        self.assertAlmostEqual(TSLA_PREV_CLOSE, 354.72, places=2)
        sig = self.assertNoLookAhead("orb15_gap|r=1", TSLA, {"prev_close": TSLA_PREV_CLOSE})
        self.assertEqual((sig["k"], sig["entry"], sig["gap_pct"]), (8, 364.18, 0.78))
        self.assertIsNone(run("orb15_gap|r=1", TSLA))


# ---------------------------------------------------------------- gap and go

GAP_ROWS = [(101.2, 101.8, 100.9, 101.5, VOL),   # gap +1.2 %, green, high 101.8
            (101.5, 101.7, 101.0, 101.4, VOL),
            (101.4, 101.7, 101.1, 101.6, VOL),
            (101.6, 102.0, 101.3, 101.9, VOL)]    # the 9:50 close above 101.8


class GapGo(Guard):
    def test_fires_on_the_first_close_above_the_first_bars_high(self):
        sess = make_session(DAY, rows=GAP_ROWS)
        sig = self.assertNoLookAhead("gap_go|r=1", sess, {"prev_close": 100.0})
        self.assertSignalShape(sig, "gap_go")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (3, 101.9, 100.9, 102.9))
        self.assertEqual((sig["stop_rule"], sig["level"], sig["gap_pct"], sig["entry_time"]), ("first_bar_low", 101.8, 1.2, "9:50 AM"))
        self.assertEqual(run("gap_go|r=2", sess, {"prev_close": 100.0})["target"], 103.9)

    def test_does_not_fire_without_the_gap_or_a_green_first_bar(self):
        sess = make_session(DAY, rows=GAP_ROWS)
        self.assertIsNone(run("gap_go|r=1", sess, {}))
        self.assertIsNone(run("gap_go|r=1", sess, {"prev_close": 100.5}))      # +0.7 %
        self.assertEqual(run("gap_go|r=1", sess, {"prev_close": 101.2 / 1.01})["k"], 3)   # exactly 1 %
        red = [(101.2, 101.8, 100.9, 101.1, VOL)] + GAP_ROWS[1:]
        self.assertIsNone(run("gap_go|r=1", make_session(DAY, rows=red), {"prev_close": 100.0}))
        self.assertIsNone(run("gap_go|r=1", sess[:1], {"prev_close": 100.0}))

    def test_the_break_must_come_by_10_30(self):
        rows = GAP_ROWS[:3] + [(101.6, 101.8, 101.3, 101.7, VOL)] * 9 + [(101.7, 102.0, 101.5, 101.9, VOL)]   # slot 12
        sess = make_session(DAY, rows=rows)
        self.assertIsNone(run("gap_go|r=1", sess, {"prev_close": 100.0}))
        self.assertEqual(run("gap_go|r=1", sess, {"prev_close": 100.0}, last_k=12)["k"], 12)
        rows = GAP_ROWS[:3] + [(101.6, 101.8, 101.3, 101.7, VOL)] * 8 + [(101.7, 102.0, 101.5, 101.9, VOL)]   # slot 11
        self.assertEqual(run("gap_go|r=1", make_session(DAY, rows=rows), {"prev_close": 100.0})["k"], 11)
        # a break on the 9:35 bar is before the entry window (first allowed close 9:45)
        early = [GAP_ROWS[0], (101.5, 102.0, 101.3, 101.9, VOL), (101.9, 102.0, 101.6, 101.7, VOL)]
        sess = make_session(DAY, rows=early)
        self.assertIsNone(run("gap_go|r=1", sess, {"prev_close": 100.0}))
        self.assertEqual(run("gap_go|r=1", sess, {"prev_close": 100.0, "window": (1, 71)})["k"], 1)

    def test_on_the_tsla_fixture(self):
        self.assertIsNone(run("gap_go|r=1", TSLA, {"prev_close": TSLA_PREV_CLOSE}))      # +0.78 % is under 1 %


# ---------------------------------------------------------------- VWAP rules

RECLAIM_ROWS = [(100, 100, 100, 100, VOL)] * 6 + [(100, 100, 99, 99.5, VOL)] * 3 + [(99.5, 100.5, 99.4, 100.3, VOL)]


class VwapReclaim(Guard):
    def test_fires_after_three_closes_below_then_one_above(self):
        sess = make_session(DAY, rows=RECLAIM_ROWS)
        sig = self.assertNoLookAhead("vwap_reclaim|r=1", sess)
        self.assertSignalShape(sig, "vwap_reclaim")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (9, 100.3, 99.0, 101.6))
        self.assertEqual((sig["stop_rule"], sig["entry_time"]), ("low_of_last_4", "10:20 AM"))
        vw = bars.vwap_series(sess)
        self.assertEqual(sig["level"], round(vw[9], 2))
        self.assertAlmostEqual(vw[9], (6 * 100 * VOL + 3 * 99.5 * VOL + (100.5 + 99.4 + 100.3) / 3.0 * VOL) / (10.0 * VOL))
        self.assertTrue(all(sess[j]["c"] < vw[j] for j in (6, 7, 8)))
        self.assertGreater(sess[9]["c"], vw[9])
        self.assertEqual(run("vwap_reclaim|r=2", sess)["target"], 102.9)

    def test_two_bars_below_are_not_enough(self):
        rows = [(100, 100, 100, 100, VOL)] * 7 + [(100, 100, 99, 99.5, VOL)] * 2 + [(99.5, 100.5, 99.4, 100.3, VOL)]
        self.assertIsNone(run("vwap_reclaim|r=1", make_session(DAY, rows=rows)))
        self.assertEqual(run("vwap_reclaim|r=1", make_session(DAY, rows=rows), below_bars=2)["k"], 9)
        self.assertIsNone(run("vwap_reclaim|r=1", make_session(DAY, [100.0] * 78)))
        self.assertIsNone(run("vwap_reclaim|r=1", make_session(DAY, rows=RECLAIM_ROWS[:9])))

    def test_on_the_tsla_fixture(self):
        self.assertIsNone(run("vwap_reclaim|r=1", TSLA))


def pullback_rows(touch_low=100.0):
    rows = []
    prev = 100.0
    for i in range(8):
        c = 100 + 0.2 * i
        rows.append((prev, c + 0.1, prev - 0.1, c, VOL))
        prev = c
    rows.append((101.4, 101.7, touch_low, 101.5, VOL))
    return rows


class VwapPullback(Guard):
    def test_fires_when_a_low_touches_a_rising_vwap_and_closes_above(self):
        sess = make_session(DAY, rows=pullback_rows())
        sig = self.assertNoLookAhead("vwap_pullback|r=1", sess)
        self.assertSignalShape(sig, "vwap_pullback")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (8, 101.5, 100.0, 103.0))
        self.assertEqual((sig["stop_rule"], sig["entry_time"]), ("bar_low", "10:15 AM"))
        vw = bars.vwap_series(sess)
        self.assertEqual(sig["level"], round(vw[8], 2))
        self.assertGreater(vw[8], vw[5])
        self.assertLessEqual(sess[8]["l"], vw[8] + 0.1 * bars.atr(sess, 14, 8))
        self.assertEqual(run("vwap_pullback|r=2", sess)["target"], 104.5)

    def test_no_touch_no_signal(self):
        sess = make_session(DAY, rows=pullback_rows(touch_low=101.3))          # the low stays well above VWAP
        self.assertIsNone(run("vwap_pullback|r=1", sess))
        rows = pullback_rows()
        rows[8] = (101.4, 101.7, 100.0, 100.2, VOL)                             # touches but closes under VWAP
        self.assertIsNone(run("vwap_pullback|r=1", make_session(DAY, rows=rows)))
        falling = [(100 - 0.2 * i, 100.1 - 0.2 * i, 99.7 - 0.2 * i, 99.8 - 0.2 * i, VOL) for i in range(12)]
        self.assertIsNone(run("vwap_pullback|r=1", make_session(DAY, rows=falling)))   # VWAP falling

    def test_on_the_tsla_fixture(self):
        sig = self.assertNoLookAhead("vwap_pullback|r=1", TSLA)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (6, 361.47, 360.02, 362.92))
        self.assertEqual(sig["level"], round(bars.vwap_series(TSLA)[6], 2))


def revert_rows():
    rows = []
    prev = 100.0
    for i in range(11):
        c = 100.0 if i % 2 == 0 else 100.1
        rows.append((prev, max(prev, c) + 0.05, min(prev, c) - 0.05, c, VOL))
        prev = c
    rows.append((99.0, 99.3, 98.8, 99.2, VOL))      # the 10:25 bar: a drop, closed green
    return rows


class VwapRevert(Guard):
    def test_fires_on_a_green_bar_two_sigma_under_vwap(self):
        sess = make_session(DAY, rows=revert_rows())
        sig = self.assertNoLookAhead("vwap_revert", sess)
        self.assertSignalShape(sig, "vwap_revert")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (11, 99.2, 98.66, 99.97))
        self.assertEqual((sig["stop_rule"], sig["entry_time"], sig["risk_per_share"]), ("bar_low_minus_atr", "10:30 AM", 0.54))
        vw = bars.vwap_series(sess)
        sd = bars.returns_std(sess, 11)
        a = bars.atr(sess, 14, 11)
        self.assertAlmostEqual(a, 0.275)                                          # by hand: (0.1 + 10 x 0.2 + 1.2) / 12
        self.assertEqual(sig["stop"], bars.cents_down(98.8 - 0.5 * a))
        self.assertEqual(sig["target"], bars.half_up(vw[11]))
        self.assertEqual(sig["level"], round(vw[11], 2))
        self.assertLessEqual(sess[11]["c"], vw[11] * (1 - 2 * sd))
        self.assertEqual(sig["target_r"], round((sig["target"] - sig["entry"]) / 0.54, 4))
        self.assertEqual(sig["sigma_pct"], round(2 * sd * 100, 3))
        self.assertEqual(sig["params"], {"sigma": 2.0, "stop_atr": 0.5, "min_r": 0.5})

    def test_a_red_bar_or_a_quiet_tape_does_not_fire(self):
        rows = revert_rows()
        rows[11] = (99.3, 99.3, 98.8, 99.2, VOL)                                  # red (close under open): no
        self.assertIsNone(run("vwap_revert", make_session(DAY, rows=rows)))
        rows[11] = (99.3, 99.3, 98.8, 99.3, VOL)                                  # a doji is not green
        self.assertIsNone(run("vwap_revert", make_session(DAY, rows=rows)))
        self.assertIsNone(run("vwap_revert", make_session(DAY, rows=revert_rows()[:11])))
        self.assertIsNone(run("vwap_revert", make_session(DAY, [100.0] * 78)))
        # the drop at slot 10 would be before 10:30: the rule starts at the 10:30 close
        rows = revert_rows()[:10] + [(99.0, 99.3, 98.8, 99.2, VOL)]
        self.assertIsNone(run("vwap_revert", make_session(DAY, rows=rows)))

    def test_the_min_r_rule(self):
        sess = make_session(DAY, rows=revert_rows())
        self.assertIsNone(run("vwap_revert", sess, min_r=1.5))                    # 1.43R is under 1.5
        self.assertEqual(run("vwap_revert", sess, min_r=1.4)["k"], 11)

    def test_on_the_tsla_fixture(self):
        self.assertIsNone(run("vwap_revert", TSLA))


# ---------------------------------------------------------------- breakouts later in the day

def hod_rows(breakout=(105.1, 105.6, 105.0, 105.5), cons=(105.1, 105.2, 104.9, 105.1)):
    rows = []
    prev = 100.0
    for i in range(11):
        c = 100 + 0.5 * i
        rows.append((prev, c + 0.3, prev - 0.3, c, VOL))      # the run-up: day high 105.3 on bar 10
        prev = c
    rows.append((105.0, 105.2, 104.9, 105.1, VOL))
    rows += [cons + (VOL,)] * 5                                 # six tight bars (slots 11..16)
    rows.append(breakout + (VOL,))                              # slot 17, the 11:00 close
    return rows


class HodBreak(Guard):
    def test_fires_on_a_close_above_the_day_high_after_a_tight_base(self):
        sess = make_session(DAY, rows=hod_rows())
        sig = self.assertNoLookAhead("hod_break|r=1", sess)
        self.assertSignalShape(sig, "hod_break")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (17, 105.5, 104.9, 106.1))
        self.assertEqual((sig["stop_rule"], sig["level"], sig["entry_time"]), ("consolidation_low", 105.3, "11:00 AM"))
        a = bars.atr(sess, 14, 17)
        self.assertAlmostEqual(a, (7 * 1.1 + 6 * 0.3 + 0.6) / 14)
        self.assertLessEqual(105.2 - 104.9, 0.6 * a)
        self.assertLessEqual(105.3 - 105.2, 0.5 * a)
        self.assertEqual(run("hod_break|r=2", sess)["target"], 106.7)

    def test_a_wide_base_or_an_early_break_does_not_fire(self):
        wide = make_session(DAY, rows=hod_rows(cons=(105.1, 105.2, 104.2, 105.1)))   # 1.0 range > 0.6 x ATR
        self.assertIsNone(run("hod_break|r=1", wide))
        far = make_session(DAY, rows=hod_rows(cons=(104.0, 104.1, 103.9, 104.0), breakout=(104.0, 105.6, 103.9, 105.5)))
        self.assertIsNone(run("hod_break|r=1", far))                                   # base too far under the high
        rows = hod_rows()[:17]
        rows[16] = (105.1, 105.6, 105.0, 105.5, VOL)                                   # the break at 10:55: too early
        self.assertIsNone(run("hod_break|r=1", make_session(DAY, rows=rows)))
        self.assertIsNone(run("hod_break|r=1", make_session(DAY, rows=hod_rows()[:17])))
        under = make_session(DAY, rows=hod_rows(breakout=(105.1, 105.6, 105.0, 105.3)))   # closes on the high, not above
        self.assertIsNone(run("hod_break|r=1", under))

    def test_on_the_tsla_fixture(self):
        self.assertIsNone(run("hod_break|r=1", TSLA))


def pm_rows(break_slot=54):
    rows = [(100, 100.5, 99.5, 100, VOL)] * 5 + [(100, 101.0, 99.5, 100.2, VOL)]       # the day high 101.0 on bar 5
    rows += [(100.2, 100.6, 99.8, 100.1, VOL)] * (break_slot - 12)
    rows += [(100.1, 100.4, 99.7, 100.0, VOL)] * 6                                      # the six bars before the break
    rows.append((100.0, 101.3, 99.9, 101.2, VOL))
    return rows


class PmBreak(Guard):
    def test_fires_between_2_00_and_3_15(self):
        sess = make_session(DAY, rows=pm_rows())
        sig = self.assertNoLookAhead("pm_break|r=1", sess)
        self.assertSignalShape(sig, "pm_break")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (54, 101.2, 99.7, 102.7))
        self.assertEqual((sig["stop_rule"], sig["level"], sig["entry_time"]), ("low_of_last_6", 101.0, "2:05 PM"))
        self.assertEqual(run("pm_break|r=2", sess)["target"], 104.2)
        late = make_session(DAY, rows=pm_rows(68))
        self.assertEqual((run("pm_break|r=1", late)["k"], run("pm_break|r=1", late)["entry_time"]), (68, "3:15 PM"))

    def test_outside_the_afternoon_window_it_does_not_fire(self):
        self.assertIsNone(run("pm_break|r=1", make_session(DAY, rows=pm_rows(53))))     # 2:00 close is slot 53
        self.assertIsNone(run("pm_break|r=1", make_session(DAY, rows=pm_rows(69))))     # 3:20 close is slot 69
        self.assertEqual(run("pm_break|r=1", make_session(DAY, rows=pm_rows(69)), last_k=69)["k"], 69)
        self.assertIsNone(run("pm_break|r=1", make_session(DAY, rows=pm_rows()[:54])))
        self.assertIsNone(run("pm_break|r=1", make_session(DAY, [100.0] * 78)))

    def test_on_the_tsla_fixture(self):
        self.assertIsNone(run("pm_break|r=1", TSLA))


INSIDE_ROWS = [(100, 100.5 + 0.1 * i, 99.5 - 0.1 * i, 100, VOL) for i in range(7)]   # expanding bars: none inside
INSIDE_ROWS += [(100, 100.6, 99.6, 100.2, VOL),       # slot 7: inside slot 6 (101.1 / 98.9)
                (100.2, 101.0, 100.0, 100.8, VOL)]    # slot 8: closes above 100.6


class InsideBreak(Guard):
    def test_fires_on_the_close_above_an_inside_bar(self):
        sess = make_session(DAY, rows=INSIDE_ROWS)
        sig = self.assertNoLookAhead("inside_break|r=1", sess)
        self.assertSignalShape(sig, "inside_break")
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (8, 100.8, 99.6, 102.0))
        self.assertEqual((sig["stop_rule"], sig["level"], sig["entry_time"]), ("inside_bar_low", 100.6, "10:15 AM"))
        self.assertEqual(run("inside_break|r=2", sess)["target"], 103.2)

    def test_no_inside_bar_or_no_close_above_means_no_signal(self):
        rows = INSIDE_ROWS[:8] + [(100.2, 101.0, 100.0, 100.6, VOL)]                 # closes on the high
        self.assertIsNone(run("inside_break|r=1", make_session(DAY, rows=rows)))
        rows = INSIDE_ROWS[:7] + [(100, 101.2, 99.6, 100.2, VOL), (100.2, 101.5, 100.0, 101.4, VOL)]   # not inside
        self.assertIsNone(run("inside_break|r=1", make_session(DAY, rows=rows)))
        early = INSIDE_ROWS[:3] + INSIDE_ROWS[7:9]                                   # the break at slot 4: before 10:00
        self.assertIsNone(run("inside_break|r=1", make_session(DAY, rows=early)))
        self.assertIsNone(run("inside_break|r=1", make_session(DAY, rows=INSIDE_ROWS[:8])))

    def test_on_the_tsla_fixture(self):
        sig = self.assertNoLookAhead("inside_break|r=1", TSLA)
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (8, 364.18, 360.42, 367.94))
        self.assertEqual(sig["level"], round(TSLA[7]["h"], 2))
        self.assertTrue(TSLA[7]["h"] <= TSLA[6]["h"] and TSLA[7]["l"] >= TSLA[6]["l"])


# ---------------------------------------------------------------- the signal builder and the registry

class SignalBuilder(unittest.TestCase):
    def test_rounding_and_the_min_risk_rule(self):
        sig = setups._signal("orb15", {"target_r": 1.0}, 8, 364.1759948730469, 358.325, 1.0, stop_rule="or_mid")
        self.assertEqual((sig["entry"], sig["stop"], sig["target"], sig["risk_per_share"]), (364.18, 358.32, 370.04, 5.86))
        self.assertEqual(sig["entry_time"], "10:15 AM")
        self.assertIsNone(setups._signal("x", {}, 5, 100.0, 99.96, 1.0))             # risk 0.04 < 0.05
        self.assertIsNotNone(setups._signal("x", {}, 5, 100.0, 99.95, 1.0))          # exactly 0.05 % is allowed
        self.assertIsNone(setups._signal("x", {}, 5, 100.0, 100.0, 1.0))             # no risk
        self.assertIsNone(setups._signal("x", {}, 5, 100.0, 100.5, 1.0))             # stop above the entry
        self.assertEqual(setups.MIN_RISK_FRAC, 0.0005)

    def test_a_given_target_sets_target_r(self):
        sig = setups._signal("vwap_revert", {}, 11, 99.2, 98.66, target=99.9667)
        self.assertEqual((sig["target"], sig["target_r"]), (99.97, round(0.77 / 0.54, 4)))
        self.assertIsNone(setups._signal("x", {}, 11, 99.2, 98.66, target=99.2))      # at the entry
        self.assertIsNone(setups._signal("x", {}, 11, 99.2, 98.66, target=99.0))
        sig = setups._signal("x", {"a": 1}, 11, 99.2, 98.66, 2.0, extra={"gap_pct": 1.2}, level=99.0,
                             opening_range_={"high": 1, "low": 0, "minutes": 15})
        self.assertEqual((sig["gap_pct"], sig["level"], sig["opening_range"]["minutes"], sig["target"]), (1.2, 99.0, 15, 100.28))
        self.assertEqual(sig["params"], {"a": 1})

    def test_scan_honours_the_window(self):
        seen = []

        def cond(k):
            seen.append(k)
            return {"k": k} if k == 10 else None
        self.assertEqual(setups._scan([None] * 78, {}, 3, cond), {"k": 10})
        self.assertEqual(seen, list(range(3, 11)))
        self.assertIsNone(setups._scan([None] * 78, {"window": (2, 9)}, 3, cond))
        self.assertEqual(setups._scan([None] * 78, {"window": (10, 10)}, 3, cond)["k"], 10)
        self.assertIsNone(setups._scan([None] * 5, {}, 3, cond))                     # fewer bars than the window
        self.assertEqual(setups._scan([None] * 78, {}, 0, cond)["k"], 10)
        seen[:] = []
        setups._scan([None] * 78, {}, 0, cond)
        self.assertEqual(seen[0], 2)                                                  # first_k 2 wins over start 0


class Registry(unittest.TestCase):
    def test_twelve_setups_and_31_configs(self):
        self.assertEqual(sorted(setups.SETUPS), sorted(["orb15", "orb30", "orb15_vol", "orb15_rs", "orb15_gap", "gap_go",
                                                        "vwap_reclaim", "vwap_pullback", "hod_break", "pm_break",
                                                        "vwap_revert", "inside_break"]))
        cfgs = setups.configs()
        self.assertEqual(len(cfgs), 31)
        ids = [c["id"] for c in cfgs]
        self.assertEqual(len(set(ids)), 31)
        for name, spec in setups.SETUPS.items():
            self.assertTrue(callable(spec["fn"]), name)
            self.assertTrue(spec["label"], name)
            self.assertIsInstance(spec["base"], dict)
            self.assertIsInstance(spec["grid"], list)
            self.assertEqual(len(spec["grid"]), sum(1 for c in cfgs if c["setup"] == name), name)
        self.assertEqual(sum(1 for c in cfgs if c["setup"] == "orb15"), 6)
        self.assertEqual([c["id"] for c in cfgs if c["setup"] == "vwap_revert"], ["vwap_revert"])
        self.assertEqual(len(setups.configs(["orb15", "gap_go"])), 8)
        self.assertEqual(setups.configs(["nope"]), [])

    def test_config_id_round_trips(self):
        for c in setups.configs():
            self.assertEqual(setups.config_id(c["setup"], c["params"]), c["id"], c["id"])
            self.assertTrue(c["label"].startswith(setups.SETUPS[c["setup"]]["label"]), c["id"])
            if "target_r" in c["params"]:
                self.assertIn("target %gR" % c["params"]["target_r"], c["label"], c["id"])
        self.assertEqual(setups.config_id("orb15", {"target_r": 1.5, "stop_rule": "or_mid"}), "orb15|r=1.5")
        self.assertEqual(setups.config_id("orb15", {"target_r": 1.0, "stop_rule": "or_low"}), "orb15|r=1|stop=or_low")
        self.assertEqual(setups.config_id("vwap_revert", {}), "vwap_revert")
        self.assertEqual(setups.config_id("gap_go", {"target_r": 2.0, "stop_rule": "or_low"}), "gap_go|r=2")   # only orb setups carry the stop
        self.assertIn("stop at the range low", CONFIGS["orb15|r=1|stop=or_low"]["label"])
        self.assertIn("stop at the range midpoint", CONFIGS["orb15|r=1"]["label"])
        for word in setups.STOP_WORDS.values():
            self.assertIn("stop", word)

    def test_run_all_keys_on_the_tsla_fixture(self):
        ctx = {"prev_close": TSLA_PREV_CLOSE, "spy": SPY, "window": clock.entry_window(DAY)}
        out = setups.run_all(TSLA, ctx)
        self.assertEqual(sorted(out), ["inside_break|r=1", "inside_break|r=2", "orb15_gap|r=1", "orb15_gap|r=1.5",
                                       "orb15_gap|r=2", "orb15_rs|r=1", "orb15_rs|r=1.5", "orb15_rs|r=2", "orb15|r=1",
                                       "orb15|r=1.5", "orb15|r=1.5|stop=or_low", "orb15|r=1|stop=or_low", "orb15|r=2",
                                       "orb15|r=2|stop=or_low", "orb30|r=1", "orb30|r=1.5", "orb30|r=2",
                                       "vwap_pullback|r=1", "vwap_pullback|r=2"])
        for cid, sig in out.items():
            self.assertEqual(sig["setup"], CONFIGS[cid]["setup"], cid)
            self.assertEqual(sig["params"], CONFIGS[cid]["params"], cid)
            self.assertLessEqual(sig["k"], 71, cid)
            self.assertGreaterEqual(sig["k"], 2, cid)
        self.assertEqual(out["orb15|r=1"]["entry"], 364.18)
        self.assertNotIn("orb15_vol|r=1", out)
        self.assertNotIn("gap_go|r=1", out)
        few = setups.run_all(TSLA, ctx, [CONFIGS["orb15|r=1"], CONFIGS["gap_go|r=1"]])
        self.assertEqual(list(few), ["orb15|r=1"])
        self.assertEqual(setups.run_all([], ctx), {})
        self.assertEqual(setups.run_all(make_session(DAY, [100.0] * 78), ctx), {})

    def test_run_all_on_the_hand_built_sessions(self):
        out = setups.run_all(make_session(DAY, rows=INSIDE_ROWS), {})
        self.assertIn("inside_break|r=1", out)
        self.assertIn("inside_break|r=2", out)
        self.assertEqual(out["inside_break|r=1"]["k"], 8)
        out = setups.run_all(orb_session(), {"prev_close": 99.4, "spy": make_session(DAY, [100] * 6)})
        self.assertEqual(sorted(k.split("|")[0] for k in out), sorted(["orb15"] * 6 + ["orb15_vol"] * 3 + ["orb15_rs"] * 3
                                                                      + ["orb15_gap"] * 3))

    def test_a_cache_in_ctx_never_leaks_across_sessions(self):
        ctx = {}
        a = run("vwap_reclaim|r=1", make_session(DAY, rows=RECLAIM_ROWS), ctx)
        self.assertIsNotNone(a)
        self.assertNotIn("_cache", ctx)                       # run() copies the ctx: the cache stays private
        shared = {}
        setups.run_config(CONFIGS["vwap_reclaim|r=1"], make_session(DAY, rows=RECLAIM_ROWS), shared)
        self.assertIn("_cache", shared)
        b = setups.run_config(CONFIGS["vwap_reclaim|r=1"], make_session(DAY, rows=RECLAIM_ROWS)[:10], shared)
        self.assertEqual(a, b)                                 # the cache is keyed by length


if __name__ == "__main__":
    unittest.main()
