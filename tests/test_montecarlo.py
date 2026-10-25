"""trader-montecarlo tests: the 26,000-replay plan through the upstream time machine on the reel's chart, the
book resampled at the sizing rules on a hand-made trade book, and the futures overnight read on saved Yahoo
charts (tests/fixtures/trader-montecarlo). No network."""

import gzip
import io as _io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load, FIX, ROOT

mc = load("trader-montecarlo", "montecarlo")

TSLA60 = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")
PLAN = os.path.join(FIX, "trader-budget", "plan-tsla.json")
FUT = os.path.join(FIX, "trader-montecarlo")


def run(argv):
    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mc.main(argv)
    return code, out.getvalue(), err.getvalue()


class Plan(unittest.TestCase):
    def test_the_reel_at_26000_and_10000_replays(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "mc.json")
            code, text, err = run(["plan", "--plan", PLAN, "--offline", TSLA60, "--now", "2026-10-19 10:16", "--write",
                                   "--out", out, "--json"])
            self.assertEqual(code, 0, err)
            doc = json.loads(text)
            self.assertEqual((doc["created_by"], doc["kind"], doc["replays"], doc["seed"]), ("trader-montecarlo", "plan", 26000, 19))
            m = doc["tm"]["mc"]
            self.assertEqual(m["replays"], 26000)
            self.assertAlmostEqual(m["p_target"], 0.294, delta=0.015)
            self.assertAlmostEqual(m["p_stop"], 0.255, delta=0.015)
            self.assertAlmostEqual(m["p_neither"], 0.451, delta=0.015)
            self.assertEqual((doc["reel"]["p_target"], doc["reel"]["p_stop"], doc["reel"]["p_neither"]), (0.294, 0.255, 0.451))
            self.assertEqual((doc["tm"]["history"]["triggered"], doc["tm"]["history"]["target"], doc["tm"]["history"]["stop"]), (33, 13, 15))
            self.assertTrue(os.path.exists(out))
            code, text, err = run(["plan", "--plan", PLAN, "--offline", TSLA60, "--now", "2026-10-19 10:16", "--replays", "2000"])
            self.assertEqual(code, 0, err)
            for must in ("2,000 replays", "95 % CI", "History: the same rule on 59 real sessions fired 33 times",
                         "coin flip", "The reel's own run, 10,000 replays", "never a prediction"):
                self.assertIn(must, text, must)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_refusals(self):
        code, text, err = run(["plan", "--plan", os.path.join(FIX, "nowhere.json")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, text, err = run(["plan", "--plan", PLAN, "--timemachine", os.path.join(FIX, "nope.py")])
        self.assertEqual(code, 1)


class Book(unittest.TestCase):
    """A hand-made book: three configs, 30 sessions, 1 to 5 trades a day with known R."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.trades = os.path.join(cls.d, "trades.jsonl.gz")
        cls.edge = os.path.join(cls.d, "edge.json")
        cls.cfg = os.path.join(cls.d, "config.json")
        dates = ["2026-09-%02d" % d for d in range(1, 31) if d not in (5, 6, 12, 13, 19, 20, 26, 27)]  # 22 weekdays
        rows = []
        for i, date in enumerate(dates):
            n = (i % 5) + 1                              # 1..5 trades a day
            for j in range(n):
                r = [0.8, -1.0, 1.5, -1.0, 0.4][j]
                rows.append({"config_id": "good|r=1", "setup": "good", "symbol": "S%d" % j, "date": date, "k": 8 + j,
                             "entry_time": "10:%02d AM" % (15 + 5 * j), "outcome": "target" if r > 0 else "stop",
                             "R_net": r, "R_gross": r + 0.03, "fill": "next_open"})
            rows.append({"config_id": "bad|r=1", "setup": "bad", "symbol": "B", "date": date, "k": 9, "entry_time": "10:20 AM",
                         "outcome": "stop", "R_net": -1.0, "R_gross": -0.97, "fill": "next_open"})
            rows.append({"config_id": "good|r=1", "setup": "good", "symbol": "S9", "date": date, "k": 8, "entry_time": "10:15 AM",
                         "outcome": "target", "R_net": 1.0, "R_gross": 1.03, "fill": "close"})
        with gzip.open(cls.trades, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        with open(cls.edge, "w") as fh:
            json.dump({"configs_tested": 31, "split": {"is": {"first": dates[0], "last": dates[13]}, "oos": {"first": dates[14], "last": dates[-1]}},
                       "configs": [{"id": "good|r=1", "status": "approved"}, {"id": "bad|r=1", "status": "watch"},
                                   {"id": "empty|r=1", "status": "approved"}]}, fh)
        with open(cls.cfg, "w") as fh:
            json.dump({"budget": 5000, "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150, "max_trades_per_day": 3}, fh)
        cls.dates = dates

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_the_day_rules(self):
        rules = {"max_loss": 50.0, "max_trades": 3, "max_daily_loss": 150.0, "budget": 5000.0}
        day = [{"k": 8, "R_net": 0.8}, {"k": 9, "R_net": -1.0}, {"k": 10, "R_net": 1.5}, {"k": 11, "R_net": -1.0}]
        r = mc.day_result(day, rules)
        self.assertEqual((r["n"], r["skipped"], r["R"]), (3, 1, [0.8, -1.0, 1.5]))     # the 4th trade is over the count
        self.assertEqual(r["total_R"], 1.3)
        self.assertTrue(r["hit_daily_limit"])                                             # 3 x $50 fills the $150 day
        tight = dict(rules, max_daily_loss=100.0)
        r = mc.day_result(day, tight)
        self.assertEqual((r["n"], r["skipped"]), (2, 2))                                 # the 3rd would breach $100
        r = mc.day_result(day[:2], rules)
        self.assertFalse(r["hit_daily_limit"])
        self.assertEqual(r["path"], [0.8, -0.19999999999999996])

    def test_book_is_deterministic_and_honest(self):
        base = ["book", "--config", "good|r=1", "--edge", self.edge, "--trades", self.trades, "--config-file", self.cfg,
                "--resamples", "3000", "--days", "20", "--horizon-days", "40", "--now", "2026-10-01 18:00"]
        code, text, err = run(base + ["--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["kind"], doc["resamples"], doc["seed"], doc["configs_tested"]), ("book", 3000, 19, 31))
        b = doc["books"][0]
        self.assertTrue(b["oos_only"])
        self.assertEqual(b["oos_first"], self.dates[14])
        self.assertEqual(b["real"]["sessions"], 8)                                        # the 8 OOS sessions
        self.assertGreater(b["real"]["trades_skipped_by_rules"], 0)
        w = b["window"]
        self.assertEqual(w["days"], 20)
        for k in ("p5", "p25", "p50", "p75", "p95"):
            self.assertIn(k, w["total_R"])
        self.assertLessEqual(w["total_R"]["p5"], w["total_R"]["p50"])
        self.assertLessEqual(w["total_R"]["p50"], w["total_R"]["p95"])
        self.assertEqual(w["total_usd"]["p50"], round(w["total_R"]["p50"] * 50, 2))
        self.assertGreaterEqual(w["max_dd_R"]["p50"], 0)
        self.assertTrue(0 <= w["p_negative"] <= 1)
        self.assertIn("exp_R_per_trade", w)
        self.assertEqual(b["horizon"]["days"], 40)
        code, text2, _ = run(base + ["--json"])
        self.assertEqual(json.loads(text2)["books"][0]["window"], w)                      # same seed, same numbers
        code, text3, _ = run(base + ["--json", "--seed", "7"])
        self.assertNotEqual(json.loads(text3)["books"][0]["window"]["total_R"], w["total_R"])
        code, text, err = run(base)
        self.assertEqual(code, 0, err)
        for must in ("configs tested: 31", "cannot show what they never did", "3,000 resamples", "one R = $50",
                     "ends below zero", "max drawdown", "40-day horizon", "never a prediction"):
            self.assertIn(must, text, must)

    def test_all_sessions_watch_all_approved_and_refusals(self):
        code, text, _ = run(["book", "--config", "good|r=1", "--edge", self.edge, "--trades", self.trades, "--config-file", self.cfg,
                             "--resamples", "500", "--all-sessions", "--json"])
        self.assertEqual(json.loads(text)["books"][0]["real"]["sessions"], 22)
        code, text, err = run(["book", "--all-approved", "--edge", self.edge, "--trades", self.trades, "--config-file", self.cfg,
                               "--resamples", "500"])
        self.assertEqual(code, 0, err)
        self.assertIn("good|r=1", text)
        self.assertIn("skipped: no next_open trades for empty|r=1", text)
        self.assertNotIn("bad|r=1", text.split("skipped")[0])
        code, text, err = run(["book", "--all-approved", "--include-watch", "--edge", self.edge, "--trades", self.trades,
                               "--config-file", self.cfg, "--resamples", "500", "--json"])
        self.assertEqual(sorted(b["config_id"] for b in json.loads(text)["books"]), ["bad|r=1", "good|r=1"])
        code, text, err = run(["book", "--config", "good|r=1", "--fill", "close", "--edge", self.edge, "--trades", self.trades,
                               "--config-file", self.cfg, "--resamples", "500", "--json"])
        self.assertEqual(json.loads(text)["books"][0]["real"]["trades_taken"], 8)        # one close-fill trade a day
        code, text, err = run(["book", "--config", "none|r=1", "--edge", self.edge, "--trades", self.trades, "--config-file", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("no next_open trades for none|r=1", err)
        code, text, err = run(["book", "--edge", self.edge, "--trades", self.trades, "--config-file", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("give --config ID or --all-approved", err)
        code, text, err = run(["book", "--config", "good|r=1", "--trades", os.path.join(self.d, "none.gz"), "--config-file", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("no trade book", err)


class Futures(unittest.TestCase):
    def test_the_overnight_read_on_saved_charts(self):
        code, text, err = run(["futures", "--offline", FUT, "--now", "2026-10-23 09:25", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["created_by"], doc["date"], doc["cutoff"]), ("trader-montecarlo", "2026-10-23", "09:25"))
        nq, es = doc["nq"], doc["es"]
        self.assertEqual((nq["prev_settle"], nq["settle_time"]), (29146.5, "2026-10-22 15:55"))
        self.assertEqual(nq["last"], 29059.25)
        self.assertAlmostEqual(nq["gap_pct"], -0.299, places=2)
        self.assertEqual((nq["overnight_high"], nq["overnight_low"]), (29276.75, 29006.0))
        self.assertAlmostEqual(nq["range_vs_20d"], 0.51, places=1)
        self.assertEqual(nq["tags"], ["nq_gap_flat", "nq_quiet_overnight"])
        self.assertEqual(es["tags"], ["es_gap_flat", "es_quiet_overnight"])
        self.assertAlmostEqual(es["gap_pct"], -0.487, places=2)
        self.assertEqual(doc["tags"], nq["tags"] + es["tags"])
        self.assertEqual(doc["failed"], {})
        code, text, err = run(["futures", "--offline", FUT, "--now", "2026-10-23 09:25"])
        self.assertEqual(code, 0, err)
        for must in ("Nasdaq-100 futures (NQ) 29,059.25", "-0.30 % overnight", "4:00 PM settle 29,146.50", "0.51x its 20-day average",
                     "Tags: nq_gap_flat nq_quiet_overnight", "S&P 500 futures (ES)", "Context, not a forecast"):
            self.assertIn(must, text, must)

    def test_tags_at_the_thresholds_and_a_written_file(self):
        rows = [(mc.datetime.datetime(2026, 10, 22, 15, 55), 100.0, 100.5, 99.5, 100.0),
                (mc.datetime.datetime(2026, 10, 22, 18, 0), 100.0, 101.0, 99.0, 100.6)]
        drows = [["2026-10-%02d" % d, 100, 101.0, 100.0, 100.5, 0] for d in range(1, 22)]      # a 1-point daily range
        r = mc.overnight_read("NQ=F", rows, drows, mc.datetime.date(2026, 10, 23), mc.datetime.datetime(2026, 10, 23, 9, 25))
        self.assertEqual(r["tags"], ["nq_gap_up", "nq_wide_overnight"])                        # +0.6 %, 2x the range
        self.assertEqual((r["gap_pct"], r["range_vs_20d"]), (0.6, 2.0))
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "futures.json")
            code, text, err = run(["futures", "--offline", FUT, "--now", "2026-10-23 09:25", "--write", "--out", out])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.load(open(out))["nq"]["tags"], ["nq_gap_flat", "nq_quiet_overnight"])
        finally:
            shutil.rmtree(d, ignore_errors=True)
        code, text, err = run(["futures", "--offline", FUT, "--now", "2026-10-24 09:25"])
        self.assertEqual(code, 1)
        self.assertIn("not a trading day", err)
        code, text, err = run(["futures", "--offline", os.path.join(FIX, "nowhere"), "--now", "2026-10-23 09:25"])
        self.assertEqual(code, 1)
        self.assertIn("no offline chart", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-montecarlo", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            exit_md = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-montecarlo\n"))
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        self.assertGreater(len(fm[fm.index("description:"):fm.index("argument-hint:")]), 200)
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], exit_md[exit_md.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("${claude_skill_dir}", "what it reads and writes", "## steps", "## rules", "what it never does",
                     "26,000", "10,000", "seed 19", "29.4", "25.5", "45.1", "montecarlo.py plan", "montecarlo.py book",
                     "montecarlo.py futures", "trader/montecarlo.json", "trader/futures.json", "max_loss_per_trade_usd",
                     "cannot show what they never did", "configs tested", "nq", "overnight", "most people who day trade lose money",
                     "not a prediction", "not financial advice"):
            self.assertIn(must, low, must)
        for never in (chr(0x2014), "a buy", "guarantee" + "d", "real " + "time", "will go up"):
            self.assertNotIn(never, low, never)

    def test_python_3_8_and_no_em_dashes(self):
        for path in (os.path.join(ROOT, "trader-montecarlo", "montecarlo.py"), os.path.join(ROOT, "trader-montecarlo", "SKILL.md"),
                     os.path.join(ROOT, "tests", "test_montecarlo.py")):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn(chr(0x2014), src, path)
            if path.endswith(".py"):
                for bad in ("import " + "zoneinfo", "from " + "zoneinfo", ".remove" + "prefix(", ".remove" + "suffix("):
                    self.assertNotIn(bad, src, path)
                for bad in ("list" + "[", "dict" + "[", "tuple" + "["):
                    self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), path)


if __name__ == "__main__":
    unittest.main()
