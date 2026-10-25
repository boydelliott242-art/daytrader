"""dt.bars: Yahoo series -> sessions on the grid, the bar store, VWAP, ATR, the rounding. Offline."""

import gzip
import json
import os
import shutil
import statistics
import tempfile
import unittest

from _dt_fixtures import CACHE, DAY, HALF_DAY, FIX, make_session, series_of, ts_of_slot, bar
from dt import bars, clock, net

DT = clock.datetime.datetime
TSLA_60D = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")
TSLA_LIVE = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")
NEXT = "2026-10-20"
AFTER_CLOSE = clock.parse_now("2026-10-20 16:05")


def one_day(closes=None, day=DAY):
    return make_session(day, closes or [100.0 + 0.1 * i for i in range(78)])


class SeriesToSessions(unittest.TestCase):
    def test_a_full_day_comes_back_whole_and_typed(self):
        full = one_day()
        out = bars.series_to_sessions(series_of({DAY: full}), AFTER_CLOSE)
        self.assertEqual(list(out), [DAY])
        self.assertEqual(len(out[DAY]), 78)
        self.assertEqual(out[DAY], full)
        b = out[DAY][8]
        self.assertIsInstance(b["t"], int)
        self.assertIsInstance(b["v"], int)
        for key in ("o", "h", "l", "c"):
            self.assertIsInstance(b[key], float)
        self.assertEqual(clock.to_eastern(clock.utc_of(b["t"])), DT(2026, 10, 19, 10, 10))

    def test_a_none_close_drops_the_whole_day_unless_partial(self):
        full = one_day()
        ser = series_of({DAY: full, NEXT: one_day(day=NEXT)})
        i = ser["t"].index(ts_of_slot(NEXT, 10))
        ser["c"][i] = None
        out = bars.series_to_sessions(ser, AFTER_CLOSE)
        self.assertEqual(list(out), [DAY])
        part = bars.series_to_sessions(ser, AFTER_CLOSE, include_partial=True)
        self.assertEqual(sorted(part), [DAY, NEXT])
        self.assertEqual(len(part[NEXT]), 10)                     # the contiguous prefix: slots 0..9
        self.assertEqual([clock.slot_of(clock.to_eastern(clock.utc_of(b["t"]))) for b in part[NEXT]], list(range(10)))
        self.assertFalse(bars.is_full(part[NEXT], NEXT))
        self.assertTrue(bars.is_full(part[DAY], DAY))

    def test_a_none_open_high_or_low_also_drops_the_bar(self):
        for col in ("o", "h", "l"):
            ser = series_of({DAY: one_day()})
            ser[col][40] = None
            self.assertEqual(bars.series_to_sessions(ser, AFTER_CLOSE), {}, col)
            part = bars.series_to_sessions(ser, AFTER_CLOSE, include_partial=True)
            self.assertEqual(len(part[DAY]), 40, col)

    def test_off_grid_ticks_the_16_00_print_and_pre_market_are_dropped(self):
        full = one_day()
        ser = series_of({DAY: full})
        tick = ts_of_slot(DAY, 15) + 285                           # 10:49:45, the live tick
        close_print = ts_of_slot(DAY, 78)                          # 16:00
        pre = ts_of_slot(DAY, 0) - 300                             # 9:25
        for t in (tick, close_print, pre):
            ser["t"].append(t)
            for col in ("o", "h", "l", "c"):
                ser[col].append(999.0)
            ser["v"].append(1)
        out = bars.series_to_sessions(ser, AFTER_CLOSE)
        self.assertEqual(len(out[DAY]), 78)
        self.assertEqual(out[DAY], full)
        self.assertNotIn(999.0, [b["c"] for b in out[DAY]])

    def test_an_incomplete_bar_is_never_used(self):
        ser = series_of({DAY: one_day()})
        at = lambda hhmm: clock.parse_now("%s %s" % (DAY, hhmm))  # noqa: E731
        self.assertEqual(bars.series_to_sessions(ser, at("10:17")), {})
        self.assertEqual(len(bars.series_to_sessions(ser, at("10:17"), include_partial=True)[DAY]), 9)   # 9:30..10:10
        self.assertEqual(len(bars.series_to_sessions(ser, at("10:15"), include_partial=True)[DAY]), 9)   # 10:10 bar closes at 10:15
        self.assertEqual(len(bars.series_to_sessions(ser, at("10:14"), include_partial=True)[DAY]), 8)
        self.assertEqual(len(bars.series_to_sessions(ser, at("09:34"), include_partial=True)), 0)
        self.assertEqual(len(bars.series_to_sessions(ser, at("16:00"), include_partial=True)[DAY]), 78)
        self.assertEqual(len(bars.series_to_sessions(ser, at("15:59"))), 0)
        self.assertEqual(len(bars.series_to_sessions(ser, at("16:00"))), 1)

    def test_include_partial_keeps_only_a_contiguous_prefix(self):
        full = one_day()
        ser = series_of({DAY: full[:10] + full[11:30]})              # a hole at slot 10
        out = bars.series_to_sessions(ser, AFTER_CLOSE, include_partial=True)
        self.assertEqual(len(out[DAY]), 10)
        self.assertEqual(out[DAY], full[:10])
        ser = series_of({DAY: full[1:30]})                           # no 9:30 bar at all
        self.assertEqual(bars.series_to_sessions(ser, AFTER_CLOSE, include_partial=True), {})

    def test_volume_defaults_to_zero_and_missing_columns(self):
        ser = series_of({DAY: one_day()})
        ser["v"][5] = None
        out = bars.series_to_sessions(ser, AFTER_CLOSE)
        self.assertEqual(out[DAY][5]["v"], 0)
        del ser["v"]
        out = bars.series_to_sessions(ser, AFTER_CLOSE)
        self.assertEqual(sum(b["v"] for b in out[DAY]), 0)
        del ser["o"]
        self.assertEqual(bars.series_to_sessions(ser, AFTER_CLOSE, include_partial=True), {})

    def test_an_early_close_is_full_at_42_bars(self):
        half = make_session(HALF_DAY, [100.0] * 42)
        self.assertEqual(len(half), 42)
        out = bars.series_to_sessions(series_of({HALF_DAY: half}), clock.parse_now("2026-11-27 13:05"))
        self.assertEqual(len(out[HALF_DAY]), 42)
        self.assertTrue(bars.is_full(out[HALF_DAY], HALF_DAY))
        self.assertFalse(bars.is_full(out[HALF_DAY][:41], HALF_DAY))
        self.assertFalse(bars.is_full(out[HALF_DAY], DAY))

    def test_weekend_and_holiday_bars_are_dropped(self):
        full = one_day()
        ser = series_of({DAY: full})
        for off in (-1 * 86400, -2 * 86400):                         # Sunday and Saturday before
            for b in full[:5]:
                ser["t"].append(b["t"] + off)
                for col in ("o", "h", "l", "c"):
                    ser[col].append(b[col])
                ser["v"].append(b["v"])
        xmas = ts_of_slot("2026-12-25", 0)
        ser["t"].append(xmas)
        for col in ("o", "h", "l", "c"):
            ser[col].append(1.0)
        ser["v"].append(1)
        out = bars.series_to_sessions(ser, clock.parse_now("2026-12-26 10:00"), include_partial=True)
        self.assertEqual(list(out), [DAY])

    def test_the_upstream_tsla_chart_gives_59_full_sessions(self):
        with gzip.open(TSLA_60D, "rt", encoding="utf-8") as fh:
            ser = net.parse_chart(fh.read(), "TSLA")
        now = clock.parse_now("2026-10-19 10:50")
        full = bars.series_to_sessions(ser, now)
        days = sorted(full)
        self.assertEqual(len(days), 59)
        self.assertEqual((days[0], days[-1]), ("2026-07-27", "2026-10-16"))
        self.assertNotIn("2026-10-19", full)
        self.assertTrue(all(len(full[d]) == 78 for d in days))
        part = bars.series_to_sessions(ser, now, include_partial=True)
        self.assertEqual(len(part["2026-10-19"]), 16)              # 9:30 .. 10:45, complete by 10:50
        self.assertEqual(len(bars.series_to_sessions(ser, clock.parse_now("2026-10-19 10:49"),
                                                     include_partial=True)["2026-10-19"]), 15)
        self.assertEqual(round(part["2026-10-19"][8]["c"], 2), 364.18)

    def test_the_live_tsla_chart_drops_its_tick(self):
        with open(TSLA_LIVE, encoding="utf-8") as fh:
            ser = net.parse_chart(fh.read(), "TSLA")
        self.assertEqual(len(ser["t"]), 17)
        self.assertEqual(ser["t"][-1] % 300, 285)                  # the 10:49:45 tick
        part = bars.series_to_sessions(ser, clock.parse_now("2026-10-19 10:50"), include_partial=True)
        self.assertEqual(len(part["2026-10-19"]), 16)
        self.assertEqual(bars.series_to_sessions(ser, clock.parse_now("2026-10-19 10:50")), {})


class Store(unittest.TestCase):
    def test_rows_of_and_bars_of_round_trip(self):
        sess = one_day()
        rows = bars.rows_of(sess)
        self.assertEqual(len(rows), 78)
        self.assertEqual(rows[8], [sess[8]["t"], sess[8]["o"], sess[8]["h"], sess[8]["l"], sess[8]["c"], sess[8]["v"]])
        self.assertEqual(bars.bars_of(rows), sess)
        store = bars.load_store("TSLA", "bars5m", CACHE)
        raw = store["sessions"][DAY]
        self.assertEqual(bars.rows_of(bars.bars_of(raw)), raw)

    def test_store_path_and_kinds(self):
        self.assertEqual(bars.store_path("^VIX", "bars5m", "/x"), os.path.join("/x", "bars5m", "_VIX.json.gz"))
        self.assertEqual(bars.store_path("brk.b", "daily", "/x"), os.path.join("/x", "daily", "BRK.B.json.gz"))
        self.assertTrue(bars.store_path("SPY").startswith(bars.CACHE))
        with self.assertRaises(ValueError):
            bars.store_path("SPY", "weekly", "/x")
        self.assertEqual(bars.STORE_KINDS, ("bars5m", "daily"))

    def test_load_store_on_the_fixture(self):
        store = bars.load_store("TSLA", "bars5m", CACHE)
        self.assertEqual((store["symbol"], store["interval"], store["source"]), ("TSLA", "5m", "yahoo"))
        self.assertEqual(len(store["sessions"]), 60)
        self.assertEqual(len(store["sessions"][DAY]), 78)
        self.assertIsNone(bars.load_store("NOPE", "bars5m", CACHE))
        daily = bars.load_store("TSLA", "daily", CACHE)
        self.assertEqual(daily["interval"], "1d")
        self.assertEqual(len(daily["rows"]), len(bars.daily_rows("TSLA", CACHE)))
        self.assertEqual(bars.daily_rows("NOPE", CACHE), [])
        vix = bars.load_store("^VIX", "bars5m", CACHE)
        self.assertEqual(vix["symbol"], "^VIX")
        self.assertEqual(len(vix["sessions"]), 59)

    def test_save_and_load_round_trip(self):
        d = tempfile.mkdtemp()
        try:
            sess = one_day()
            doc = {"symbol": "ZZ", "interval": "5m", "source": "test", "sessions": {DAY: bars.rows_of(sess)}}
            bars.save_store("zz", doc, "bars5m", d)
            path = os.path.join(d, "bars5m", "ZZ.json.gz")
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".tmp"))
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), doc)
            back = bars.load_store("ZZ", "bars5m", d)
            self.assertEqual(back, doc)
            self.assertEqual(bars.load_sessions("ZZ", root=d), [(DAY, sess)])
        finally:
            shutil.rmtree(d)

    def test_sessions_of_sorts_cuts_and_limits(self):
        store = bars.load_store("TSLA", "bars5m", CACHE)
        items = bars.sessions_of(store)
        self.assertEqual(len(items), 60)
        self.assertEqual([d for d, _ in items], sorted(d for d, _ in items))
        self.assertEqual(items[0][0], "2026-07-31")
        self.assertEqual(items[-1][0], "2026-10-23")
        self.assertEqual(len(items[-1][1]), 78)
        before = bars.sessions_of(store, before=DAY)
        self.assertEqual(before[-1][0], "2026-10-16")              # exclusive cutoff
        self.assertEqual(len(before), 55)
        last = bars.sessions_of(store, last_n=3)
        self.assertEqual([d for d, _ in last], ["2026-10-21", "2026-10-22", "2026-10-23"])
        self.assertEqual([d for d, _ in bars.sessions_of(store, before=DAY, last_n=1)], ["2026-10-16"])
        self.assertEqual(bars.sessions_of(None), [])
        self.assertEqual(bars.sessions_of({}), [])
        self.assertEqual(bars.sessions_of({"sessions": {}}), [])
        self.assertEqual(bars.load_sessions("TSLA", last_n=2, root=CACHE), items[-2:])
        self.assertEqual(bars.load_sessions("NOPE", root=CACHE), [])
        # the fixture holds the reel's sessions whole, every slot on the grid
        for day, sess in items:
            self.assertEqual(len(sess), clock.slots_of_day(day), day)
            for k, b in enumerate(sess):
                self.assertEqual(b["t"], ts_of_slot(day, k), (day, k))


class Daily(unittest.TestCase):
    ROWS = [["2026-10-14", 100.0, 101.0, 99.0, 100.0, 1000],
            ["2026-10-15", 100.0, 103.0, 100.0, 102.0, 2000],
            ["2026-10-16", 102.0, 102.5, 101.0, 101.5, 3000],
            ["2026-10-19", 101.5, 150.0, 50.0, 140.0, 999999],
            ["2026-10-20", 140.0, 141.0, 139.0, 140.5, 500]]

    def test_prev_close_is_strictly_before(self):
        self.assertEqual(bars.prev_close(self.ROWS, "2026-10-19"), 101.5)
        self.assertEqual(bars.prev_close(self.ROWS, "2026-10-17"), 101.5)      # a Saturday: the Friday close
        self.assertEqual(bars.prev_close(self.ROWS, "2026-10-16"), 102.0)
        self.assertEqual(bars.prev_close(self.ROWS, "2026-10-21"), 140.5)
        self.assertIsNone(bars.prev_close(self.ROWS, "2026-10-14"))
        self.assertIsNone(bars.prev_close([], "2026-10-14"))
        rows = [list(r) for r in self.ROWS]
        rows[2][4] = None
        self.assertEqual(bars.prev_close(rows, "2026-10-19"), 102.0)          # a None close is skipped

    def test_daily_before_and_sma(self):
        self.assertEqual([r[0] for r in bars.daily_before(self.ROWS, "2026-10-19", 2)], ["2026-10-15", "2026-10-16"])
        self.assertEqual(len(bars.daily_before(self.ROWS, "2026-10-19", 0)), 3)
        self.assertEqual(len(bars.daily_before(self.ROWS, "2026-10-19", 50)), 3)
        self.assertEqual(bars.daily_before(self.ROWS, "2026-10-14", 5), [])
        self.assertEqual(bars.sma([1, 2, 3, 4], 2), 3.5)
        self.assertEqual(bars.sma([1, 2, 3, 4], 4), 2.5)
        self.assertIsNone(bars.sma([1, 2, 3], 4))
        self.assertIsNone(bars.sma([1, 2, 3], 0))

    def test_daily_atr_uses_rows_before_the_date_only(self):
        # true ranges by hand: 10-15 max(3, 3, 0) = 3; 10-16 max(1.5, 0.5, 1.0) = 1.5
        self.assertAlmostEqual(bars.daily_atr(self.ROWS, "2026-10-19", 2), 2.25)
        self.assertAlmostEqual(bars.daily_atr(self.ROWS, "2026-10-19", 14), 2.25)
        self.assertAlmostEqual(bars.daily_atr(self.ROWS, "2026-10-19", 1), 1.5)
        self.assertAlmostEqual(bars.daily_atr(self.ROWS, "2026-10-20", 1), 100.0)  # now the wild 10-19 row counts
        self.assertIsNone(bars.daily_atr(self.ROWS, "2026-10-15", 14))              # one row: no true range
        self.assertIsNone(bars.daily_atr(self.ROWS, "2026-10-14", 14))
        self.assertIsNone(bars.daily_atr([], "2026-10-19"))
        rows = [list(r) for r in self.ROWS]
        rows[2][2] = None                                                          # a row without a high is skipped
        self.assertAlmostEqual(bars.daily_atr(rows, "2026-10-19", 14), 3.0)

    def test_daily_atr_on_the_fixture_ignores_later_rows(self):
        spy = bars.daily_rows("SPY", CACHE)
        a = bars.daily_atr(spy, DAY)
        self.assertIsNotNone(a)
        self.assertGreater(a, 0)
        cut = [r for r in spy if r[0] < DAY]
        self.assertEqual(bars.daily_atr(cut, DAY), a)
        wild = [list(r) for r in spy]
        for r in wild:
            if r[0] >= DAY:
                r[2], r[3] = 9999.0, 1.0
        self.assertEqual(bars.daily_atr(wild, DAY), a)
        self.assertEqual(bars.daily_atr(wild, DAY, 20), bars.daily_atr(cut, DAY, 20))

    def test_avg_volume_uses_rows_before_the_date_only(self):
        self.assertEqual(bars.avg_volume(self.ROWS, "2026-10-19", 20), 2000.0)
        self.assertEqual(bars.avg_volume(self.ROWS, "2026-10-19", 2), 2500.0)
        self.assertEqual(bars.avg_volume(self.ROWS, "2026-10-20", 1), 999999.0)
        self.assertIsNone(bars.avg_volume(self.ROWS, "2026-10-14"))
        rows = [list(r) for r in self.ROWS]
        rows[0][5], rows[1][5] = 0, None                                          # empty volumes are skipped
        self.assertEqual(bars.avg_volume(rows, "2026-10-19"), 3000.0)
        rows[2][5] = 0
        self.assertIsNone(bars.avg_volume(rows, "2026-10-19"))
        spy = bars.daily_rows("SPY", CACHE)
        self.assertEqual(bars.avg_volume(spy, DAY), bars.avg_volume([r for r in spy if r[0] < DAY], DAY))


class Intraday(unittest.TestCase):
    def test_opening_range(self):
        sess = make_session(DAY, rows=[(100, 101.0, 99.0, 100.5, 10), (100.5, 100.8, 99.5, 100.2, 10),
                                       (100.2, 100.9, 99.8, 100.6, 10), (100.6, 105.0, 95.0, 100.0, 10)])
        self.assertEqual(bars.opening_range(sess, 3), (101.0, 99.0))
        self.assertEqual(bars.opening_range(sess, 1), (101.0, 99.0))
        self.assertEqual(bars.opening_range(sess, 2), (101.0, 99.0))
        self.assertEqual(bars.opening_range(sess, 4), (105.0, 95.0))
        tsla = bars.bars_of(bars.load_store("TSLA", "bars5m", CACHE)["sessions"][DAY])
        oh, ol = bars.opening_range(tsla, 3)
        self.assertEqual((round(oh, 2), round(ol, 2)), (362.18, 354.47))        # the reel's range

    def test_vwap_is_cumulative_on_typical_price(self):
        sess = make_session(DAY, rows=[(100, 101, 99, 100, 100), (100, 102, 100, 101, 300), (101, 103, 101, 102, 0),
                                       (102, 104, 100, 103, 400)])
        vw = bars.vwap_series(sess)
        self.assertEqual(len(vw), 78)
        self.assertAlmostEqual(vw[0], 100.0)
        self.assertAlmostEqual(vw[1], (100 * 100 + 101 * 300) / 400.0)         # 100.75
        self.assertAlmostEqual(vw[2], 100.75)                                   # a zero-volume bar changes nothing
        self.assertAlmostEqual(vw[3], (100 * 100 + 101 * 300 + ((104 + 100 + 103) / 3.0) * 400) / 800.0)
        # vwap[k] only knows bars[:k+1]
        self.assertEqual(bars.vwap_series(sess[:2]), vw[:2])
        self.assertEqual(bars.vwap_series([dict(b, v=0) for b in sess])[:3], [100.0, 101.0, 102.0])
        self.assertEqual(bars.vwap_series([]), [])

    def test_vwap_on_the_fixture_matches_a_hand_loop(self):
        sess = bars.bars_of(bars.load_store("SPY", "bars5m", CACHE)["sessions"][DAY])
        vw = bars.vwap_series(sess)
        pv = vol = 0.0
        for k, b in enumerate(sess):
            pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
            vol += b["v"]
            self.assertAlmostEqual(vw[k], pv / vol, places=9)

    def test_atr_with_fewer_than_n_bars_averages_what_is_there(self):
        rows = [(100, 101, 99, 100.5, 1), (100.5, 102, 100, 101.5, 1), (101.5, 101.8, 99.5, 100, 1), (100, 100.4, 99.8, 100.2, 1)]
        sess = make_session(DAY, rows=rows, pad=False)
        # true ranges: 2.0 (first bar: high - low), max(2, 1.5, 0.5) = 2.0, max(2.3, 0.3, 2.0) = 2.3, max(0.6, 0.4, 0.2) = 0.6
        self.assertAlmostEqual(bars.atr(sess, 14), (2.0 + 2.0 + 2.3 + 0.6) / 4)
        self.assertAlmostEqual(bars.atr(sess, 2), (2.3 + 0.6) / 2)
        self.assertAlmostEqual(bars.atr(sess, 14, upto=1), 2.0)
        self.assertAlmostEqual(bars.atr(sess, 14, upto=0), 2.0)
        self.assertAlmostEqual(bars.atr(sess, 1, upto=2), 2.3)
        self.assertIsNone(bars.atr([], 14))
        tsla = bars.bars_of(bars.load_store("TSLA", "bars5m", CACHE)["sessions"][DAY])
        self.assertAlmostEqual(bars.atr(tsla, 14, upto=8), 3.2182, places=4)   # trader-exit's ATR(9) at the entry bar

    def test_returns_std_needs_eight_returns(self):
        closes = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101]
        sess = make_session(DAY, closes)
        self.assertIsNone(bars.returns_std(sess, 7))                            # 7 returns
        sd = bars.returns_std(sess, 8)                                          # 8 returns
        rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, 9)]
        self.assertAlmostEqual(sd, statistics.pstdev(rets))
        self.assertIsNone(bars.returns_std(sess, 0))
        self.assertAlmostEqual(bars.returns_std(sess, 9), statistics.pstdev([closes[i] / closes[i - 1] - 1.0 for i in range(1, 10)]))
        flat = make_session(DAY, [100.0] * 20)
        self.assertEqual(bars.returns_std(flat, 19), 0.0)
        zero = make_session(DAY, rows=[(1, 1, 1, 0, 1)] + [(1, 1, 1, 1, 1)] * 9)
        self.assertEqual(bars.returns_std(zero, 9), 0.0)                        # a zero close is skipped, 8 left

    def test_cents_rounding(self):
        self.assertEqual(bars.cents_down(358.325), 358.32)                     # the reel's stop
        self.assertEqual(bars.cents_up(358.325), 358.33)
        self.assertEqual(bars.half_up(370.035), 370.04)                        # the reel's target
        self.assertEqual(bars.half_up(364.1759948730469), 364.18)
        self.assertEqual(bars.cents_down(364.18), 364.18)                      # no float drift on a clean cent
        self.assertEqual(bars.cents_up(364.18), 364.18)
        self.assertEqual(bars.cents_down(1.005), 1.0)
        self.assertEqual(bars.cents_up(1.005), 1.01)
        self.assertEqual(bars.half_up(1.005), 1.01)
        self.assertEqual(bars.half_up(2.675), 2.68)                            # round() would say 2.67
        self.assertEqual(bars.half_up(2.674999), 2.67)
        self.assertEqual(bars.cents_down(0.0), 0.0)
        self.assertEqual(bars.half_up(100), 100.0)
        self.assertEqual(bars.cents_down((362.18 + 354.47) / 2.0), 358.32)
        self.assertEqual(bars.half_up(364.18 + 1.0 * (364.18 - 358.32)), 370.04)
        self.assertEqual(bars.half_up(364.18 + 2.0 * (364.18 - 358.32)), 375.90)

    def test_session_day_type(self):
        trend = make_session(DAY, rows=[(100, 101, 99, 100.5, 1)] + [(100.5, 111, 100, 110, 1)] + [(110, 110, 110, 110, 1)] * 76)
        self.assertEqual(bars.session_day_type(trend), "trend_day")              # |110 - 100| = 10 >= 0.6 x 12
        rng = make_session(DAY, rows=[(100, 101, 99, 100.5, 1)] + [(100.5, 111, 100, 103, 1)] + [(103, 103, 103, 103, 1)] * 76)
        self.assertEqual(bars.session_day_type(rng), "range_day")                # 3 < 7.2
        edge = make_session(DAY, rows=[(100, 100, 90, 100, 1), (100, 100, 90, 94, 1)] + [(94, 94, 94, 94, 1)] * 76)
        self.assertEqual(bars.session_day_type(edge), "trend_day")               # exactly 0.6 x 10 counts
        self.assertIsNone(bars.session_day_type([]))
        self.assertEqual(bars.session_day_type(make_session(DAY, [100.0] * 78)), "range_day")
        spy = bars.load_store("SPY", "bars5m", CACHE)["sessions"]
        self.assertEqual(bars.session_day_type(bars.bars_of(spy[DAY])), "range_day")
        labels = set(bars.session_day_type(bars.bars_of(rows)) for rows in spy.values())
        self.assertEqual(labels, {"trend_day", "range_day"})


if __name__ == "__main__":
    unittest.main()
