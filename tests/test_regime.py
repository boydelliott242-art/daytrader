"""trader-regime tests: offline, on tests/fixtures/dt-cache (the daily and 5-minute stores) and
tests/fixtures/trader-regime (saved Yahoo answers for Fri 2026-10-23: SPY, QQQ, the 11 sector ETFs
and one spark pass over 40 large caps). The network is never touched: dt.net.http_get is swapped
for a function that fails the test, and dt.net.SLEEP for a no-op."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

regime = load("trader-regime", "regime")
from dt import bars, clock, net  # noqa: E402  (ROOT is on sys.path once the tool is loaded)
from dt import regime as dtregime  # noqa: E402

DT = os.path.join(FIX, "dt-cache")
OFF = os.path.join(FIX, "trader-regime")
SYMS = os.path.join(OFF, "largecap.txt")
DAY = "2026-10-23"
CUT = "2026-10-23 10:30"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = regime.main(argv)
    return code, out.getvalue(), err.getvalue()


def no_network(*a, **k):
    raise AssertionError("the network was touched: %r" % (a[:1],))


class Offline(unittest.TestCase):
    """Every test class inherits this: Yahoo is unreachable, pauses are free."""

    def setUp(self):
        self._saved = (net.http_get, net.SLEEP)
        net.http_get = no_network
        net.SLEEP = lambda s: None

    def tearDown(self):
        net.http_get, net.SLEEP = self._saved


# ---------------------------------------------------------------- daily

class Daily(Offline):
    def test_the_block_for_2026_10_23_by_hand(self):
        stores = regime.load_stores(DT)
        d, tags = regime.daily_block(DAY, stores)
        # by hand, from the rows strictly before the date
        rows = bars.daily_rows("SPY", DT)
        before = [r for r in rows if r[0] < DAY]
        closes = [r[4] for r in before]
        self.assertEqual(before[-1][0], "2026-10-22")
        self.assertEqual(d["through"], "2026-10-22")
        self.assertEqual(d["spy_close"], round(closes[-1], 2))
        self.assertEqual(d["spy_sma20"], round(sum(closes[-20:]) / 20.0, 2))
        self.assertEqual(d["spy_sma50"], round(sum(closes[-50:]) / 50.0, 2))
        self.assertEqual(d["ret5d_pct"], round((closes[-1] / closes[-6] - 1) * 100, 3))
        vix = [r for r in bars.daily_rows("^VIX", DT) if r[0] < DAY][-1]
        v3 = [r for r in bars.daily_rows("^VIX3M", DT) if r[0] < DAY][-1]
        self.assertEqual(d["vix"], round(vix[4], 2))
        self.assertEqual(d["vix3m"], round(v3[4], 2))
        self.assertEqual(d["backwardation"], vix[4] > v3[4])
        # the exact numbers (checked by hand against dt.bars.daily_rows("SPY", root))
        self.assertEqual(d["spy_close"], 763.96)
        self.assertEqual(d["spy_sma20"], 770.72)
        self.assertEqual(d["spy_sma50"], 768.17)
        self.assertEqual(d["trend"], "flat")              # 763.96 under the 20-day, 20-day over the 50-day
        self.assertEqual(d["ret5d_pct"], -1.015)
        self.assertEqual(d["vix"], 19.03)
        self.assertEqual(d["vix_bucket"], "15-20")
        self.assertEqual(d["vix3m"], 20.12)
        self.assertIs(d["backwardation"], False)
        self.assertEqual(d["prev_range_atr"], 0.913)
        self.assertEqual(d["prev_range"], 6.54)            # 767.71 - 761.17
        self.assertEqual(d["spy_atr20"], 7.16)
        self.assertEqual(tags, ["trend_flat", "vix_15_20", "prev_normal"])
        self.assertEqual(tags, dtregime.session_tags(DAY, stores))
        for k in regime.DAILY_KEYS:
            self.assertIn(k, d)

    def test_never_uses_a_row_on_or_after_the_date(self):
        date = "2026-09-15"                                # the middle of the store
        tmp = tempfile.mkdtemp()
        try:
            for sym in ("SPY", "^VIX", "^VIX3M"):
                doc = bars.load_store(sym, "daily", DT)
                rows = []
                for r in doc["rows"]:
                    if r[0] >= date:                       # garble everything from the date on
                        r = [r[0], 1.0, 9999.0, 0.5, 5000.0 if sym == "SPY" else 99.0, 1]
                    rows.append(r)
                bars.save_store(sym, dict(doc, rows=rows), "daily", tmp)
            real = regime.daily_block(date, regime.load_stores(DT))
            garbled = regime.daily_block(date, regime.load_stores(tmp))
            self.assertEqual(real, garbled)
            self.assertEqual(real[0]["through"], "2026-09-14")
            self.assertEqual(real[0]["spy_close"], 760.88)
            self.assertEqual(real[1], dtregime.session_tags(date, regime.load_stores(DT)))
            # and the rows after the date would have moved every number had they been read
            later = regime.daily_block("2026-09-16", regime.load_stores(tmp))
            self.assertEqual(later[0]["spy_close"], 5000.0)
        finally:
            shutil.rmtree(tmp)

    def test_default_date_is_the_next_session_or_today_before_the_open(self):
        rows = bars.daily_rows("SPY", DT)                   # last stored session: Fri 2026-10-23
        self.assertEqual(regime.default_date(rows, clock.parse_now("2026-10-25 13:00")), datetime.date(2026, 10, 26))
        self.assertEqual(regime.default_date(rows, clock.parse_now("2026-10-26 08:30")), datetime.date(2026, 10, 26))
        self.assertEqual(regime.default_date(rows, clock.parse_now("2026-10-26 10:00")), datetime.date(2026, 10, 26))
        self.assertEqual(regime.default_date(rows, clock.parse_now("2026-10-23 17:00")), datetime.date(2026, 10, 26))
        self.assertEqual(regime.next_trading_day(datetime.date(2026, 11, 25)), datetime.date(2026, 11, 27))
        self.assertEqual(regime.prev_trading_day(datetime.date(2026, 11, 27)), datetime.date(2026, 11, 25))

    def test_cli_default_date_and_the_plain_read(self):
        code, text, err = run(["daily", "--cache-root", DT, "--now", "2026-10-25 13:00"])
        self.assertEqual(code, 0, err)
        self.assertIn("Regime for Mon 2026-10-26, from daily closes through Fri 2026-10-23", text)
        self.assertIn("SPY 759.97 below its 20-day 770.2, the 20-day above the 50-day 767.8: trend flat", text)
        self.assertIn("VIX 19.45 (15-20). VIX3M 20.48: no backwardation.", text)
        self.assertIn("0.67 x its 20-day ATR 7.13: quiet", text)
        self.assertIn("Tags: trend_flat vix_15_20 prev_quiet", text)
        self.assertIn("Most people who day trade lose money", text)
        self.assertNotIn("buy", text.lower())
        code, text, _ = run(["daily", "--cache-root", DT, "--date", DAY, "--now", "2026-10-25 13:00", "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["created_by"], "trader-regime")
        self.assertEqual(doc["date"], DAY)
        self.assertEqual(doc["as_of"], "2026-10-25T17:00:00Z")
        self.assertIsNone(doc["live"])
        self.assertEqual(doc["daily"]["spy_close"], 763.96)
        self.assertEqual(doc["tags"], ["trend_flat", "vix_15_20", "prev_normal"])
        self.assertNotIn("warnings", doc)
        code, text, _ = run(["daily", "--cache-root", DT, "--date", "2026-11-10", "--now", "2026-10-25 13:00", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("refresh the store", json.loads(text)["warnings"][0])

    def test_refusals_in_one_plain_line(self):
        cases = [(["daily", "--cache-root", DT, "--date", "2026-10-25"], "weekend"),
                 (["daily", "--cache-root", DT, "--date", "2026-11-26"], "Thanksgiving"),
                 (["daily", "--cache-root", DT, "--date", "2024-10-24"], "no SPY daily rows before 2024-10-24"),
                 (["daily", "--cache-root", os.path.join(FIX, "nowhere")], "no SPY daily store"),
                 (["daily", "--cache-root", DT, "--date", "yesterday"], "YYYY-MM-DD"),
                 (["tags", "2026-10-24", "--cache-root", DT], "weekend"),
                 (["tags", "2024-10-24", "--cache-root", DT], "no SPY daily rows")]
        for argv, why in cases:
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(why, err, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(text, "")


class Tags(Offline):
    def test_tags_for_a_date(self):
        code, text, err = run(["tags", DAY, "--cache-root", DT])
        self.assertEqual(code, 0, err)
        self.assertEqual(text.strip(), "2026-10-23: trend_flat vix_15_20 prev_normal")
        code, text, _ = run(["tags", "2026-09-15", "--cache-root", DT, "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["tags"], dtregime.session_tags("2026-09-15", regime.load_stores(DT)))
        self.assertEqual(doc["daily"]["through"], "2026-09-14")
        # every stored session gets the same tags the lab computes
        stores = regime.load_stores(DT)
        for day, _ in bars.load_sessions("SPY", root=DT)[-10:]:
            self.assertEqual(regime.daily_block(day, stores)[1], dtregime.session_tags(day, stores), day)


# ---------------------------------------------------------------- live

def spy_by_hand(cut=CUT):
    series = net.parse_chart(json.load(open(os.path.join(OFF, "SPY.json"))), "SPY")
    return series, bars.series_to_sessions(series, clock.parse_now(cut), include_partial=True)[DAY]


class Live(Offline):
    def setUp(self):
        Offline.setUp(self)
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "trader", "regime.json")

    def tearDown(self):
        shutil.rmtree(self.d)
        Offline.tearDown(self)

    def live(self, *extra):
        return run(["live", "--at", "10:30", "--now", CUT, "--offline", OFF, "--cache-root", DT,
                    "--symbols-file", SYMS, "--out", self.out, "--json"] + list(extra))

    def test_the_read_at_10_30_on_the_offline_fixture(self):
        code, text, err = self.live("--write")
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        live = doc["live"]
        self.assertEqual((doc["date"], live["at"], live["source"]), (DAY, "10:30", "offline"))
        # by hand from the saved SPY chart: 12 completed bars by 10:30, the 9:30 to 9:40 bars are the range
        series, sess = spy_by_hand()
        self.assertEqual(len(sess), 12)
        self.assertEqual(live["bars"], 12)
        oh = max(b["h"] for b in sess[:3])
        ol = min(b["l"] for b in sess[:3])
        last = sess[-1]["c"]
        prev = [r for r in bars.daily_rows("SPY", DT) if r[0] == "2026-10-22"][0][4]
        self.assertEqual(live["spy"]["prev_close"], round(prev, 2))
        self.assertEqual(live["spy"]["prev_close_source"], "daily store")
        self.assertEqual(live["spy_gap_pct"], round((sess[0]["o"] / prev - 1) * 100, 3))
        self.assertEqual(live["spy_gap_pct"], -0.532)
        self.assertEqual(live["spy_or"], "above" if last > oh else "below" if last < ol else "inside")
        self.assertEqual(live["spy_or"], "inside")
        self.assertEqual(live["spy"]["opening_range"], {"high": round(oh, 2), "low": round(ol, 2), "minutes": 15})
        vw = bars.vwap_series(sess)[-1]
        self.assertEqual(live["spy_vs_vwap_pct"], round((last / vw - 1) * 100, 3))
        self.assertLess(live["spy_vs_vwap_pct"], 0)                  # 759.65 under VWAP 760.13
        self.assertEqual(live["spy"]["vwap"], round(vw, 2))
        self.assertEqual(live["spy_move_pct"], round((last / sess[0]["o"] - 1) * 100, 3))
        self.assertEqual(live["qqq"]["or"], "above")
        self.assertGreater(live["qqq"]["vs_vwap_pct"], 0)
        # sectors: all 11, best first, each move from its own 9:30 open
        etfs = [s["etf"] for s in live["sectors"]]
        self.assertEqual(sorted(etfs), sorted(e for e, _ in regime.SECTORS))
        moves = [s["move_pct"] for s in live["sectors"]]
        self.assertEqual(moves, sorted(moves, reverse=True))
        self.assertEqual(etfs[0], "XLE")
        self.assertEqual(live["sectors_failed"], {})
        xlk = net.parse_chart(json.load(open(os.path.join(OFF, "XLK.json"))), "XLK")
        kb = bars.series_to_sessions(xlk, clock.parse_now(CUT), include_partial=True)[DAY]
        row = [s for s in live["sectors"] if s["etf"] == "XLK"][0]
        self.assertEqual(row["move_pct"], round((kb[-1]["c"] / kb[0]["o"] - 1) * 100, 3))
        self.assertEqual(row["vs_spy_pct"], round(row["move_pct"] - live["spy_move_pct"], 3))
        # breadth from one spark pass over the 40 symbols in the file
        br = live["breadth"]
        self.assertEqual((br["symbols"], br["answered"], br["with_bars"], br["failed"]), (40, 40, 40, 0))
        for k in ("breadth_above_open_pct", "breadth_up_pct"):
            self.assertGreaterEqual(live[k], 0.0)
            self.assertLessEqual(live[k], 100.0)
        self.assertEqual(live["breadth_above_open_pct"], round(100.0 * br["above_open"] / br["with_bars"], 1))
        self.assertEqual(live["breadth_up_pct"], round(100.0 * br["up"] / br["with_prev_close"], 1))
        self.assertEqual((live["breadth_above_open_pct"], live["breadth_up_pct"]), (52.5, 35.0))
        self.assertIn("closes only", br["note"])
        self.assertEqual(live["labels"], ["spy_gap_down", "spy_or_inside", "spy_below_vwap", "breadth_mixed"])
        # the daily block for the same date rides along, from rows before it
        self.assertEqual(doc["daily"]["through"], "2026-10-22")
        self.assertEqual(doc["tags"], ["trend_flat", "vix_15_20", "prev_normal"])
        self.assertAlmostEqual(live["spy"]["range_used_atr"],
                               round((max(b["h"] for b in sess) - min(b["l"] for b in sess)) / 7.1620025634765625, 3), 3)

    def test_the_written_json_has_every_contract_key(self):
        code, text, err = self.live("--write")
        self.assertEqual(code, 0, err)
        with open(self.out) as fh:
            doc = json.load(fh)
        for k in ("created_by", "as_of", "date", "daily", "live", "tags"):
            self.assertIn(k, doc)
        self.assertEqual(doc["created_by"], "trader-regime")
        for k in regime.DAILY_KEYS:
            self.assertIn(k, doc["daily"])
        for k in regime.LIVE_KEYS:
            self.assertIn(k, doc["live"])
        self.assertIn("spy_or", doc["live"])
        self.assertIn(doc["live"]["spy_or"], ("above", "inside", "below"))
        for s in doc["live"]["sectors"]:
            self.assertIn("etf", s)
            self.assertIn("move_pct", s)
        self.assertEqual(doc["as_of"], "2026-10-23T14:30:00Z")
        self.assertIsInstance(doc["tags"], list)

    def test_the_plain_read(self):
        code, text, err = run(["live", "--at", "10:30", "--now", CUT, "--offline", OFF, "--cache-root", DT,
                               "--symbols-file", SYMS, "--out", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("Live read, Fri 2026-10-23 at 10:30 AM ET: 12 completed 5-minute bars, the last closing at 10:30 AM", text)
        self.assertIn("SPY 759.65: gap -0.53 % from the prior close 763.96 (daily store), inside its 15-minute opening range (759.14 to 760.77), -0.06 % vs VWAP 760.13", text)
        self.assertIn("QQQ 704.87: gap -0.34 %", text)
        self.assertIn("above its 15-minute opening range (701.16 to 704.50)", text)
        self.assertIn("Breadth, 40 of 40 large caps with bars (spark closes only): 52.5 % above their 9:30 close, 35.0 % up on the day", text)
        self.assertIn("XLE  Energy", text)
        self.assertIn("Daily context (through 2026-10-22): trend flat, VIX 19.03 (15-20), prior day normal. Tags: trend_flat vix_15_20 prev_normal", text)
        self.assertIn("Live labels: spy_gap_down spy_or_inside spy_below_vwap breadth_mixed", text)
        self.assertIn("Most people who day trade lose money", text)
        self.assertNotIn("Wrote", text)
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("Breadth: one spark pass over 40 symbols", err)      # progress goes to stderr

    def test_refused_before_9_50_the_range_is_still_forming(self):
        for argv in (["live", "--at", "09:45", "--now", "2026-10-23 09:45"],
                     ["live", "--now", "2026-10-23 09:49"],
                     ["live", "--at", "09:30", "--now", "2026-10-23 11:00"]):
            code, text, err = run(argv + ["--offline", OFF, "--cache-root", DT, "--symbols-file", SYMS, "--out", self.out])
            self.assertEqual(code, 1, argv)
            self.assertIn("9:50 AM", err)
            self.assertIn("still forming", err)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(text, "")
        code, _, err = run(["live", "--now", "2026-10-23 09:50", "--offline", OFF, "--cache-root", DT,
                            "--symbols-file", SYMS, "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)                       # 9:50 is the first allowed cutoff

    def test_other_refusals(self):
        base = ["--offline", OFF, "--cache-root", DT, "--symbols-file", SYMS, "--out", self.out]
        code, _, err = run(["live", "--at", "10:30", "--now", "2026-10-25 10:30"] + base)
        self.assertEqual(code, 1)
        self.assertIn("weekend", err)
        code, _, err = run(["live", "--at", "10:30", "--now", "2026-11-26 10:30"] + base)
        self.assertIn("Thanksgiving", err)
        code, _, err = run(["live", "--at", "11:00", "--now", "2026-10-23 10:30"] + base)
        self.assertIn("later than now", err)
        code, _, err = run(["live", "--at", "16:05", "--now", "2026-10-23 17:00"] + base)
        self.assertIn("after the 4:00 PM close", err)
        code, _, err = run(["live", "--at", "10:30", "--now", "2026-10-23 10:30", "--offline", OFF, "--from-cache"] + base[2:])
        self.assertIn("not both", err)
        code, _, err = run(["live", "--offline", OFF] + base[2:])
        self.assertIn("give --at", err)
        code, _, err = run(["live", "--at", "10:30", "--now", "2026-10-23 10:30", "--offline", os.path.join(FIX, "nowhere")] + base[2:])
        self.assertIn("no offline folder", err)
        code, _, err = run(["live", "--at", "10:30", "--now", "2026-10-23 10:30", "--cache-root", DT,
                            "--symbols-file", SYMS, "--out", self.out])
        self.assertEqual(code, 1)                            # no --offline: the network, which is off here
        self.assertIn("replay 2026-10-23 with --from-cache", err)
        self.assertNotIn("Traceback", err)

    def test_offline_without_now_takes_the_day_from_the_data(self):
        code, text, err = run(["live", "--at", "15:00", "--offline", OFF, "--cache-root", DT, "--symbols-file", SYMS,
                               "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["date"], doc["live"]["at"], doc["live"]["bars"]), (DAY, "15:00", 66))
        self.assertEqual(doc["live"]["through"], "3:00 PM")

    def test_after_the_close_without_at_is_the_full_session(self):
        code, text, err = run(["live", "--now", "2026-10-23 17:30", "--offline", OFF, "--cache-root", DT,
                               "--symbols-file", SYMS, "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["live"]["at"], doc["live"]["bars"]), ("16:00", 78))
        self.assertIn("full-session read", doc["live"]["note"])

    def test_live_merges_into_the_daily_block_for_the_same_date(self):
        code, _, err = run(["daily", "--cache-root", DT, "--date", DAY, "--now", "2026-10-23 08:30", "--write", "--out", self.out])
        self.assertEqual(code, 0, err)
        with open(self.out) as fh:
            before = json.load(fh)
        self.assertIsNone(before["live"])
        code, _, err = self.live("--write")
        self.assertEqual(code, 0, err)
        with open(self.out) as fh:
            after = json.load(fh)
        self.assertEqual(after["daily"], before["daily"])
        self.assertEqual(after["tags"], before["tags"])
        self.assertEqual(after["live"]["at"], "10:30")
        # daily again for the same date keeps the live read; for another date it drops it
        code, text, err = run(["daily", "--cache-root", DT, "--date", DAY, "--now", "2026-10-23 11:00", "--write", "--out", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("Kept the live read at 10:30 ET", text)
        with open(self.out) as fh:
            self.assertEqual(json.load(fh)["live"]["at"], "10:30")
        run(["daily", "--cache-root", DT, "--date", "2026-10-22", "--now", "2026-10-23 11:00", "--write", "--out", self.out])
        with open(self.out) as fh:
            self.assertIsNone(json.load(fh)["live"])

    def test_without_a_spy_store_the_daily_block_comes_from_the_file_or_stays_empty(self):
        empty = os.path.join(self.d, "empty-cache")
        os.makedirs(empty)
        code, text, err = run(["live", "--at", "10:30", "--now", CUT, "--offline", OFF, "--cache-root", empty,
                               "--symbols-file", SYMS, "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertIsNone(doc["daily"]["spy_close"])
        self.assertEqual(doc["tags"], [])
        self.assertEqual(doc["live"]["spy"]["prev_close_source"], "chart meta")   # Yahoo's chartPreviousClose
        self.assertEqual(doc["live"]["spy"]["prev_close"], 763.96)
        self.assertIn("daily block is empty", doc["warnings"][0])
        for k in regime.DAILY_KEYS:
            self.assertIn(k, doc["daily"])
        run(["daily", "--cache-root", DT, "--date", DAY, "--now", "2026-10-23 08:30", "--write", "--out", self.out])
        code, text, err = run(["live", "--at", "10:30", "--now", CUT, "--offline", OFF, "--cache-root", empty,
                               "--symbols-file", SYMS, "--out", self.out, "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["daily"]["spy_close"], 763.96)
        self.assertEqual(doc["tags"], ["trend_flat", "vix_15_20", "prev_normal"])
        self.assertIn("kept the daily block", doc["warnings"][0])

    def test_publish_copies_to_docs_data(self):
        docs = os.path.join(self.d, "docs", "data", "regime.json")
        code, _, err = self.live("--publish", docs)
        self.assertEqual(code, 0, err)
        with open(self.out) as fh:
            a = json.load(fh)
        with open(docs) as fh:
            b = json.load(fh)
        self.assertEqual(a, b)
        self.assertEqual(a["live"]["spy_or"], "inside")
        code, text, _ = run(["daily", "--cache-root", DT, "--date", DAY, "--now", "2026-10-23 08:30", "--out", self.out,
                             "--publish", docs])
        self.assertIn("Wrote %s." % docs, text)

    def test_max_symbols_and_a_missing_symbols_file(self):
        code, text, err = self.live("--max-symbols", "10")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)["live"]["breadth"]["symbols"], 10)
        code, text, err = run(["live", "--at", "10:30", "--now", CUT, "--offline", OFF, "--cache-root", DT,
                               "--symbols-file", os.path.join(self.d, "none.txt"), "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)                       # offline: every symbol in spark.json
        self.assertEqual(json.loads(text)["live"]["breadth"]["symbols"], 40)

    def test_replay_from_the_bar_store(self):
        code, text, err = run(["live", "--at", "10:30", "--now", "2026-10-20 10:30", "--from-cache", "--cache-root", DT,
                               "--symbols-file", os.path.join(self.d, "none.txt"), "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        live = doc["live"]
        self.assertEqual((doc["date"], live["source"], live["bars"]), ("2026-10-20", "cache", 12))
        sess = bars.load_sessions("SPY", root=DT)
        spy = dict(sess)["2026-10-20"][:12]
        oh = max(b["h"] for b in spy[:3])
        self.assertEqual(live["spy_or"], "above" if spy[-1]["c"] > oh else "not above")
        prev = bars.prev_close(bars.daily_rows("SPY", DT), "2026-10-20")
        self.assertEqual(live["spy_gap_pct"], round((spy[0]["o"] / prev - 1) * 100, 3))
        self.assertEqual([s["etf"] for s in live["sectors"]], ["XLK", "XLF"])      # the only sector ETFs in the fixture
        self.assertEqual(len(live["sectors_failed"]), 9)
        self.assertEqual(live["breadth"]["symbols"], 18)                             # 25 stored minus the context symbols
        self.assertEqual(live["breadth"]["with_prev_close"], 18)
        self.assertEqual(doc["daily"]["through"], "2026-10-19")
        code, _, err = run(["live", "--at", "10:30", "--now", "2026-10-26 10:30", "--from-cache", "--cache-root", DT,
                            "--out", self.out, "--json"])
        self.assertEqual(code, 1)
        self.assertIn("no SPY session for 2026-10-26", err)


# ---------------------------------------------------------------- hand-built sessions

def mk(o, h, l, c, v=1000, k=0):
    t = clock.ts_of(clock.eastern_to_utc(clock.slot_time(datetime.date(2026, 10, 23), k)))
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


class HandBuilt(Offline):
    def test_index_read_above_inside_below_and_vwap(self):
        day = datetime.date(2026, 10, 23)
        cutoff = datetime.datetime(2026, 10, 23, 9, 50)
        rng = [mk(100, 101, 99, 100.5, k=0), mk(100.5, 101, 99.5, 100, k=1), mk(100, 100.8, 99.2, 100.2, k=2)]
        above = rng + [mk(100.2, 101.5, 100.1, 101.2, k=3)]
        inside = rng + [mk(100.2, 100.9, 99.9, 100.4, k=3)]
        below = rng + [mk(100.2, 100.3, 98.5, 98.8, k=3)]
        for sess, want in ((above, "above"), (inside, "inside"), (below, "below")):
            r = regime.index_read("SPY", sess, day, cutoff, 99.0, "daily store", atr20=2.0)
            self.assertEqual(r["or"], want)
            self.assertEqual(r["opening_range"], {"high": 101.0, "low": 99.0, "minutes": 15})
            self.assertEqual(r["gap_pct"], round((100 / 99.0 - 1) * 100, 3))
            self.assertEqual(r["through"], "9:50 AM")
            self.assertEqual(r["bars"], 4)
        r = regime.index_read("SPY", above, day, cutoff, 99.0, "daily store", atr20=2.0)
        self.assertGreater(r["vs_vwap_pct"], 0)
        self.assertEqual(r["vwap"], round(bars.vwap_series(above)[-1], 2))
        self.assertEqual(r["range_used_atr"], round((101.5 - 98.5 + 0.0) / 2.0, 3) if False else round((101.5 - 99.0) / 2.0, 3))
        r = regime.index_read("SPY", below, day, cutoff, None, None)
        self.assertIsNone(r["gap_pct"])
        self.assertLess(r["vs_vwap_pct"], 0)
        with self.assertRaises(regime.RegimeError):
            regime.index_read("SPY", rng, day, cutoff, 99.0, "daily store")      # only the range: nothing after it

    def test_breadth_from_closes_only(self):
        day = datetime.date(2026, 10, 23)
        cutoff = datetime.datetime(2026, 10, 23, 10, 0)

        def series(closes, prev):
            t = [clock.ts_of(clock.eastern_to_utc(clock.slot_time(day, k))) for k in range(len(closes))]
            s = {"t": t, "o": [None] * len(closes), "h": [None] * len(closes), "l": [None] * len(closes),
                 "c": closes, "v": [None] * len(closes), "meta": {"chartPreviousClose": prev}}
            return s
        data = {"A": series([10, 10.5, 11, 11.2, 11.5, 11.4, 11.6], 11.0),    # above its 9:30 close, up on the day
                "B": series([20, 19.8, 19.5, 19.4, 19.6, 19.3, 19.2], 19.0),  # below the 9:30 close, up on the day
                "C": series([30, 30.1, 30.2, 30.3, 30.4, 30.5, 30.6], None),  # above; no prior close: skipped for up
                "D": series([40, 40.1], 41.0)}                                # only two bars: still counts (last 40.1 > 40)
        br = regime.breadth_read(data, {"E": "not in Yahoo's answer"}, day, cutoff)
        self.assertEqual((br["symbols"], br["answered"], br["with_bars"], br["failed"]), (5, 4, 4, 1))
        self.assertEqual((br["above_open"], br["with_prev_close"], br["up"]), (3, 3, 2))
        self.assertEqual((br["above_open_pct"], br["up_pct"]), (75.0, 66.7))
        # a bar still in progress at the cutoff never counts: at 9:45 only the first three closes exist
        early = regime.breadth_read({"A": data["A"]}, {}, day, datetime.datetime(2026, 10, 23, 9, 45))
        self.assertEqual(regime.session_closes(data["A"], day, datetime.datetime(2026, 10, 23, 9, 45)),
                         [(0, 10.0), (1, 10.5), (2, 11.0)])
        self.assertEqual(early["above_open"], 1)
        # a series without the 9:30 bar is left out
        gone = dict(data["A"])
        gone["t"], gone["c"] = gone["t"][1:], gone["c"][1:]
        self.assertEqual(regime.breadth_read({"A": gone}, {}, day, cutoff)["with_bars"], 0)
        self.assertEqual(regime.breadth_read({}, {}, day, cutoff)["above_open_pct"], None)

    def test_labels(self):
        spy = {"gap_pct": 0.31, "or": "above", "vs_vwap_pct": 0.1}
        self.assertEqual(regime.live_labels(spy, {"up_pct": 70.0}), ["spy_gap_up", "spy_or_above", "spy_above_vwap", "breadth_broad_up"])
        spy = {"gap_pct": -0.29, "or": "below", "vs_vwap_pct": -0.1}
        self.assertEqual(regime.live_labels(spy, {"up_pct": 30.0}), ["spy_gap_flat", "spy_or_below", "spy_below_vwap", "breadth_broad_down"])
        self.assertEqual(regime.live_labels({"gap_pct": None, "or": "inside", "vs_vwap_pct": 0.0}, {"up_pct": None}),
                         ["spy_or_inside", "spy_at_vwap"])

    def test_live_window(self):
        now = clock.parse_now("2026-10-23 10:31")
        day, cutoff, note = regime.live_window(now, None)
        self.assertEqual((day, cutoff, note), (datetime.date(2026, 10, 23), datetime.datetime(2026, 10, 23, 10, 31), ""))
        day, cutoff, _ = regime.live_window(now, clock.parse_hhmm("10:00"))
        self.assertEqual(cutoff, datetime.datetime(2026, 10, 23, 10, 0))
        day, cutoff, note = regime.live_window(clock.parse_now("2026-10-23 18:00"), None)
        self.assertEqual(cutoff, datetime.datetime(2026, 10, 23, 16, 0))
        self.assertIn("closed at 4:00 PM", note)
        # an early close: the 1:00 PM close caps the read
        day, cutoff, _ = regime.live_window(clock.parse_now("2026-11-27 15:00"), None)
        self.assertEqual(cutoff, datetime.datetime(2026, 11, 27, 13, 0))
        with self.assertRaises(regime.RegimeError):
            regime.live_window(clock.parse_now("2026-11-27 12:00"), clock.parse_hhmm("13:05"))
        with self.assertRaises(regime.RegimeError):
            regime.live_window(clock.parse_now("2026-10-23 09:49"), None)
        # an explicit day (offline replay) does not care what time it is now
        day, cutoff, _ = regime.live_window(clock.parse_now("2026-10-25 09:00"), clock.parse_hhmm("10:15"),
                                            datetime.date(2026, 10, 23))
        self.assertEqual((day, cutoff.time()), (datetime.date(2026, 10, 23), datetime.time(10, 15)))

    def test_trend_words(self):
        self.assertEqual(regime.trend_words({"spy_close": 759.97, "spy_sma20": 751.2, "spy_sma50": 740.8, "trend": "up"}),
                         "SPY 759.97 above its 20-day 751.2 and 50-day 740.8: trend up.")
        self.assertEqual(regime.trend_words({"spy_close": 700.0, "spy_sma20": 751.2, "spy_sma50": 760.8, "trend": "down"}),
                         "SPY 700.00 below its 20-day 751.2 and 50-day 760.8: trend down.")
        self.assertIn("not enough history", regime.trend_words({"spy_close": 700.0, "spy_sma20": None, "spy_sma50": None,
                                                                "trend": None, "spy_rows": 7}))


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-regime", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            exit_md = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-regime\n"))
        self.assertGreater(len(text.splitlines()), 60)
        fm = text.split("---\n")[1]
        self.assertGreater(len(fm), 200)
        self.assertIn("argument-hint:", fm)
        self.assertEqual(text[text.index("## Safety"):], exit_md[exit_md.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("## what it reads and writes", "## steps", "## rules", "## what it never does", "matched a rule",
                     "most people who day trade lose money", "no look-ahead", "trader/regime.json", "docs/data/regime.json",
                     "9:50", "closes only", "${claude_skill_dir}", "759.65", "52.5 %", "trend_flat vix_15_20 prev_normal",
                     "not a forecast", "never a buy"):
            self.assertIn(must, low, must)
        for never in ("\u2014", "pro" + "fit", "guarantee" + "d", "will " + "go up"):
            self.assertNotIn(never, low, never)

    def test_python_3_8_and_no_em_dashes_in_the_new_files(self):
        import re
        for rel in ("trader-regime/regime.py", "trader-regime/SKILL.md", "tests/test_regime.py", "scripts/cut_regime_fixture.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("\u2014", src, rel)
            if rel.endswith(".py"):
                for bad in ("import " + "zoneinfo", "from " + "zoneinfo", ".remove" + "prefix(", ".remove" + "suffix("):
                    self.assertNotIn(bad, src, rel)
                for bad in ("list" + "[", "dict" + "[", "tuple" + "["):
                    self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), rel)
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", rel)


if __name__ == "__main__":
    unittest.main()
