"""trader-lab tests: offline, on tests/fixtures/dt-cache (25 symbols x 60 sessions of 5-minute bars
plus daily rows), the upstream TSLA 60-day chart (the reel's known answer) and hand-built sessions."""

import gzip
import io
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

sys.path.insert(0, ROOT)
lab = load("trader-lab", "lab")
from dt import bars as dtbars, clock, net, setups, stats as dtstats  # noqa: E402

CACHE = os.path.join(FIX, "dt-cache")
TSLA60 = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")
CONFIG = os.path.join(ROOT, "trader-budget", "config.example.json")
STATS_KEYS = ["n", "target", "stop", "close", "green", "win_rate", "win_ci95", "exp_R", "exp_ci95", "pf", "avg_win_R",
              "avg_loss_R", "max_dd_R", "median_R", "n_symbols", "n_sessions"]
TRADE_KEYS = ["symbol", "date", "config_id", "setup", "params", "k", "entry", "stop", "target", "target_r", "stop_rule",
              "entry_time", "fill", "entry_fill", "outcome", "exit_slot", "exit_price", "R_gross", "R_net", "mae_R",
              "mfe_R", "bars_held", "gap_pct", "rvol", "hour", "dow", "tier", "sector", "regime", "hindsight"]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = lab.main(argv)
    return code, out.getvalue(), err.getvalue()


def session_dates(sym="SPY"):
    return [d for d, _ in dtbars.sessions_of(dtbars.load_store(sym, "bars5m", CACHE))]


def hand_session(n=78, base=100.0, v=1000):
    """A flat session: every bar 99.5 .. 100.5, closes at base."""
    return [{"t": 1000 + 300 * k, "o": base, "h": base + 0.5, "l": base - 0.5, "c": base, "v": v} for k in range(n)]


class KnownAnswer(unittest.TestCase):
    """The reel's TSLA chart: orb15 with a 1R target, plan fill, no costs, the upstream numbers."""

    @classmethod
    def setUpClass(cls):
        with gzip.open(TSLA60, "rt", encoding="utf-8") as fh:
            series = net.parse_chart(fh.read(), "TSLA")
        cls.sessions = dtbars.series_to_sessions(series, clock.parse_now("2026-10-19 10:16"), include_partial=True)
        cls.cfgs = [c for c in setups.configs() if c["id"] == "orb15|r=1"]

    def test_orb15_r1_on_the_59_sessions_before_the_reel(self):
        full = [d for d in sorted(self.sessions) if d < "2026-10-19" and len(self.sessions[d]) == 78]
        self.assertEqual(len(full), 59)
        trades = []
        for d in full:
            trades += lab.session_trades("TSLA", d, self.sessions[d], {"window": (3, 76)}, "close", 77, 0, self.cfgs)
        self.assertEqual(len(trades), 33)
        outcomes = {k: sum(1 for t in trades if t["outcome"] == k) for k in ("target", "stop", "close")}
        self.assertEqual(outcomes, {"target": 13, "stop": 15, "close": 5})
        self.assertEqual(round(statistics.mean(t["R_gross"] for t in trades), 3), -0.062)
        for t in trades:
            self.assertEqual(t["R_net"], t["R_gross"])          # no costs
            self.assertEqual(t["fill"], "close")
            self.assertEqual(t["entry_fill"], t["entry"])
            self.assertEqual(t["config_id"], "orb15|r=1")
            self.assertLessEqual(t["exit_slot"], 77)
        st = dtstats.stats_block(trades, "R_gross")
        self.assertEqual((st["n"], st["target"], st["stop"], st["close"]), (33, 13, 15, 5))
        self.assertAlmostEqual(st["exp_R"], -0.062, places=3)   # the upstream prints -0.062

    def test_the_reel_itself_fires_at_bar_8(self):
        today = self.sessions["2026-10-19"]
        self.assertEqual(len(today), 9)                          # 9:30 .. the 10:10 bar, closed by 10:16
        sig = lab.signals_on(today, {"window": (3, 76)}, self.cfgs)["orb15|r=1"]
        self.assertEqual((sig["k"], sig["entry"], sig["stop"], sig["target"]), (8, 364.18, 358.32, 370.04))
        self.assertEqual(sig["entry_time"], "10:15 AM")
        self.assertEqual(sig["opening_range"], {"high": 362.18, "low": 354.47, "minutes": 15})


class Run(unittest.TestCase):
    """lab.py run on three fixture symbols, one process."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.out = os.path.join(cls.d, "lab")
        cls.code, cls.text, cls.err = run(["run", "--cache-root", CACHE, "--symbols", "TSLA,NVDA,AAPL,SPY,^VIX",
                                           "--workers", "1", "--out-dir", cls.out, "--config", CONFIG, "--quiet",
                                           "--largecap", os.path.join(cls.d, "no-largecap.json")])
        with open(os.path.join(cls.out, "edge.json")) as fh:
            cls.edge = json.load(fh)
        cls.trades = []
        with gzip.open(os.path.join(cls.out, "trades.jsonl.gz"), "rt", encoding="utf-8") as fh:
            for ln in fh:
                cls.trades.append(json.loads(ln))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def test_exit_code_and_the_tables(self):
        self.assertEqual(self.code, 0, self.err)
        self.assertNotIn("Traceback", self.err)
        self.assertGreaterEqual(self.text.count("configs tested: 31"), 3)
        self.assertIn("Out of sample (the gate)", self.text)
        self.assertIn("Rolling walk-forward", self.text)
        self.assertIn("Most people who day trade lose money", self.text)
        self.assertIn("orb15|r=1 ", self.text)
        self.assertNotIn("a buy", self.text.lower())
        self.assertNotIn("$", self.text)                          # never a P&L

    def test_edge_json_carries_the_contract(self):
        e = self.edge
        self.assertEqual(e["created_by"], "trader-lab")
        self.assertEqual(e["symbols"], ["TSLA", "NVDA", "AAPL"])   # SPY and ^VIX are context, never traded
        self.assertEqual((e["fill"], e["cost_bps"], e["configs_tested"]), ("next_open", 4.0, 31))
        self.assertEqual(e["sessions"], {"first": "2026-07-31", "last": "2026-10-23", "n": 60})
        self.assertEqual((e["split"]["is"]["n"], e["split"]["oos"]["n"]), (40, 20))
        self.assertLess(e["split"]["is"]["last"], e["split"]["oos"]["first"])
        self.assertEqual(e["split"]["oos"]["last"], "2026-10-23")
        self.assertEqual(len(e["configs"]), 31)
        self.assertEqual([c["id"] for c in e["configs"]], [c["id"] for c in setups.configs()])
        for c in e["configs"]:
            for k in ("id", "setup", "params", "label", "all", "is", "oos", "by_regime", "by_hour", "by_tier",
                      "by_sector", "by_dow", "status"):
                self.assertIn(k, c, c["id"])
            for block in ("all", "is", "oos"):
                self.assertEqual(sorted(c[block]), sorted(STATS_KEYS), c["id"])
            self.assertIn(c["status"], ("approved", "watch", "rejected"))
            self.assertEqual(c["status"], dtstats.status_of(c["oos"]))
            self.assertEqual(c["all"]["n"], c["is"]["n"] + c["oos"]["n"])
            for fam in ("by_regime", "by_hour", "by_tier", "by_sector", "by_dow"):
                for key, st in c[fam].items():
                    self.assertGreaterEqual(st["n"], 10, (c["id"], fam, key))
                    self.assertEqual(sorted(st), sorted(STATS_KEYS))
            self.assertEqual(sorted(c["by_hour"]) , sorted(h for h in c["by_hour"] if h in lab.HOURS))
            self.assertIn("never a live filter", c["hindsight"]["note"])
        r = e["rolling"]
        for k in ("window", "test_days", "selected_per_day", "equity_R", "stats"):
            self.assertIn(k, r)
        self.assertEqual((r["window"], r["test_days"]), (30, 20))
        self.assertEqual(len(r["selected_per_day"]), 20)
        self.assertEqual(len(r["equity_R"]), 20)
        self.assertEqual(sorted(r["stats"]), sorted(STATS_KEYS))
        self.assertIn("lose money", e["note"])
        self.assertEqual(e["meta"]["skipped"], [])
        self.assertEqual(e["meta"]["entry_window"], [2, 71])

    def test_trades_file_lines(self):
        self.assertEqual(len(self.trades), self.edge["n_trades"])
        self.assertEqual(len(self.trades), sum(c["all"]["n"] for c in self.edge["configs"]))
        for t in self.trades:
            for k in TRADE_KEYS:
                self.assertIn(k, t, k)
            self.assertIn(t["symbol"], ("TSLA", "NVDA", "AAPL"))
            self.assertIn(t["hour"], lab.HOURS)
            self.assertIn(t["dow"], lab.DOW)
            self.assertEqual((t["tier"], t["sector"]), ("unknown", "unknown"))
            self.assertIsInstance(t["regime"], list)
            self.assertIn(t["hindsight"]["day_type"], ("trend_day", "range_day"))
            self.assertIn(t["outcome"], ("target", "stop", "close"))
            self.assertEqual(t["fill"], "next_open")
            self.assertAlmostEqual(t["R_net"], t["R_gross"] - t["entry_fill"] * 4 / 10000.0 / (t["entry"] - t["stop"]), 3)
            self.assertLess(t["R_net"], t["R_gross"])             # costs on every trade, win or lose
            self.assertGreaterEqual(t["k"], 2)
            self.assertLessEqual(t["k"], 71)
            self.assertLessEqual(t["exit_slot"], 76)              # flat by 15:55
            self.assertRegex(t["entry_time"], r"^\d{1,2}:\d\d (AM|PM)$")
            self.assertRegex(t["exit_time"], r"^\d{1,2}:\d\d (AM|PM)$")
            if t["rvol"] is not None:
                self.assertGreater(t["rvol"], 0)
        # the aggregate blocks are dt.stats over the file's own lines
        mine = [t for t in self.trades if t["config_id"] == "orb15|r=1"]
        c = [c for c in self.edge["configs"] if c["id"] == "orb15|r=1"][0]
        self.assertEqual(dtstats.stats_block(mine), c["all"])
        oos = [t for t in mine if t["date"] >= self.edge["split"]["oos"]["first"]]
        self.assertEqual(dtstats.stats_block(oos), c["oos"])
        ten = [t for t in mine if t["hour"] == "10"]
        self.assertEqual(dtstats.stats_block(ten), c["by_hour"]["10"])
        quiet = [t for t in mine if "prev_quiet" in t["regime"]]
        if len(quiet) >= 10:
            self.assertEqual(dtstats.stats_block(quiet), c["by_regime"]["prev_quiet"])
        # the regime tags come from closes before the date: the same tags dt.regime gives
        for t in self.trades[:50]:
            self.assertEqual(t["regime"], lab.regime_tags(t["date"], lab.load_context(CACHE)))

    def test_report_reads_it_back(self):
        code, text, err = run(["report", "--lab-dir", self.out])
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(text.count("configs tested: 31"), 3)
        self.assertIn("orb15|r=1 ", text)
        code, text, _ = run(["report", "--lab-dir", self.out, "--by", "hour", "--top", "2"])
        self.assertEqual(code, 0)
        self.assertIn("by hour", text)
        code, text, _ = run(["report", "--lab-dir", self.out, "--config", "orb15|r=1"])
        self.assertEqual(code, 0)
        for s in ("out of sample", "in sample", "all sessions", "by regime", "by dow", "hindsight"):
            self.assertIn(s, text)
        code, text, _ = run(["report", "--lab-dir", self.out, "--config", "orb15|r=1", "--json"])
        self.assertEqual(json.loads(text)["id"], "orb15|r=1")
        code, text, _ = run(["report", "--lab-dir", self.out, "--json"])
        doc = json.loads(text)
        self.assertNotIn("trades", doc["rolling"])
        code, _, err = run(["report", "--lab-dir", self.out, "--config", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("no config", err)


class RunFlags(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_sessions_largecap_symbols_file_fill_and_publish(self):
        with open("largecap.json", "w") as fh:
            json.dump({"symbols": [{"symbol": "TSLA", "market_cap": 1.2e12, "sector": "Consumer Discretionary"},
                                   {"symbol": "JPM", "market_cap": 8e11, "tier": "large", "sector": "Finance"}]}, fh)
        with open("list.txt", "w") as fh:
            fh.write("TSLA|Tesla\nJPM|JPMorgan\nSPY|context\nNOPE|not in the store\n")
        code, text, err = run(["run", "--cache-root", CACHE, "--symbols-file", "list.txt", "--largecap",
                               "largecap.json", "--sessions", "12", "--fill", "close", "--cost-bps", "0",
                               "--workers", "1", "--out-dir", "lab", "--config", CONFIG, "--publish",
                               "--quiet", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["symbols"], ["TSLA", "JPM"])
        self.assertEqual(doc["meta"]["not_in_store"], ["NOPE"])
        self.assertEqual(doc["sessions"]["n"], 12)
        self.assertEqual((doc["fill"], doc["cost_bps"]), ("close", 0.0))
        self.assertEqual((doc["split"]["is"]["n"], doc["split"]["oos"]["n"]), (8, 4))
        self.assertIn("note", doc["rolling"])                      # 12 sessions: no 30-session window
        self.assertEqual(doc["rolling"]["test_days"], 0)
        tiers = set()
        with gzip.open(os.path.join("lab", "trades.jsonl.gz"), "rt") as fh:
            for ln in fh:
                t = json.loads(ln)
                tiers.add((t["symbol"], t["tier"], t["sector"]))
                self.assertEqual(t["R_net"], t["R_gross"])
                self.assertEqual(t["entry_fill"], t["entry"])
        self.assertEqual(tiers, {("TSLA", "mega", "Consumer Discretionary"), ("JPM", "large", "Finance")})
        pub = os.path.join("trader", "publish", "edge.json")
        self.assertTrue(os.path.exists(pub))
        with open(pub) as fh:
            p = json.load(fh)
        for k in ("configs", "rolling", "split", "meta", "configs_tested"):
            self.assertIn(k, p)
        self.assertNotIn("trades", p["rolling"])
        self.assertEqual(len(p["configs"]), 31)
        for c in p["configs"]:
            self.assertIn("by_tier", c)
            for tier in c["by_tier"]:
                self.assertIn(tier, ("mega", "large"))

    def test_refusals_are_one_plain_line(self):
        cases = [["run", "--cache-root", os.path.join(self.d, "nowhere"), "--workers", "1", "--quiet"],
                 ["run", "--cache-root", CACHE, "--symbols", "SPY,QQQ", "--workers", "1", "--quiet"],
                 ["run", "--cache-root", CACHE, "--symbols", "TSLA", "--workers", "1", "--cost-bps", "-1", "--quiet"],
                 ["run", "--cache-root", CACHE, "--symbols", "TSLA", "--workers", "1", "--sessions", "0", "--quiet"],
                 ["run", "--cache-root", CACHE, "--symbols-file", "missing.txt", "--workers", "1", "--quiet"],
                 ["report"], ["why", "TSLA", "2026-10-19", "--cache-root", os.path.join(self.d, "nowhere")]]
        for argv in cases:
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertTrue(err.startswith("lab.py: "), err)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertNotIn("Traceback", err)
        code, _, _ = run([])
        self.assertEqual(code, 1)


class Stats(unittest.TestCase):
    def test_stats_block_of_a_hand_made_list(self):
        rs = [1.0, -1.0, 1.0, 1.0, -1.0, 1.0, 0.2, 1.0, -1.0, 1.0]
        outcomes = ["target", "stop", "target", "target", "stop", "target", "close", "target", "stop", "target"]
        trades = [{"symbol": "A" if i % 2 else "B", "date": "2026-10-%02d" % (1 + i // 3), "outcome": o, "R_net": r}
                  for i, (o, r) in enumerate(zip(outcomes, rs))]
        st = dtstats.stats_block(trades)
        self.assertEqual((st["n"], st["target"], st["stop"], st["close"], st["green"]), (10, 6, 3, 1, 7))
        self.assertEqual(st["win_rate"], 0.6)
        self.assertEqual(st["win_ci95"], [0.3127, 0.8318])        # Wilson, by hand
        self.assertEqual(st["exp_R"], 0.32)
        self.assertEqual(st["pf"], 2.067)                          # 6.2 / 3
        self.assertEqual(st["avg_win_R"], round(6.2 / 7, 4))
        self.assertEqual(st["avg_loss_R"], -1.0)
        self.assertEqual(st["max_dd_R"], 1.0)
        self.assertEqual(st["median_R"], 1.0)
        self.assertEqual((st["n_symbols"], st["n_sessions"]), (2, 4))
        self.assertLess(st["exp_ci95"][0], 0.32)
        self.assertGreater(st["exp_ci95"][1], 0.32)
        self.assertEqual(st, dtstats.stats_block(trades))         # seed 19: the same CI every time
        # the lab's slim rows give the very same block
        rows = [lab.slim(dict(t, hour="10", dow="Mon", tier="mega", sector="Tech", regime=["trend_up"],
                              hindsight={"day_type": "trend_day"})) for t in trades]
        self.assertEqual(lab.stats_of_rows(rows), st)
        self.assertEqual(dtstats.stats_block([])["n"], 0)

    def test_split_boundary(self):
        dates = session_dates()
        self.assertEqual(len(dates), 60)
        is_, oos = lab.split_sessions(dates)
        self.assertEqual((len(is_), len(oos)), (40, 20))
        self.assertLess(is_[-1], oos[0])
        self.assertEqual(is_ + oos, sorted(dates))
        self.assertEqual(oos[0], "2026-09-28")
        is_, oos = lab.split_sessions(dates[:59])
        self.assertEqual((len(is_), len(oos)), (39, 20))
        is_, oos = lab.split_sessions(dates[:3])
        self.assertEqual((len(is_), len(oos)), (2, 1))
        is_, oos = lab.split_sessions(dates[:2])
        self.assertEqual((len(is_), len(oos)), (2, 0))
        self.assertEqual(lab.split_sessions(list(reversed(dates)))[0], is_[:0] + sorted(dates)[:40])

    def test_status_rule(self):
        def oos(n, exp, lo):
            return {"n": n, "exp_R": exp, "exp_ci95": [lo, None if lo is None else lo + 0.5]}
        self.assertEqual(dtstats.status_of(oos(100, 0.2, 0.01)), "approved")
        self.assertEqual(dtstats.status_of(oos(99, 0.2, 0.01)), "watch")
        self.assertEqual(dtstats.status_of(oos(100, 0.2, -0.01)), "watch")
        self.assertEqual(dtstats.status_of(oos(100, 0.2, 0.0)), "watch")
        self.assertEqual(dtstats.status_of(oos(30, 0.01, -0.3)), "watch")
        self.assertEqual(dtstats.status_of(oos(30, 0.0, -0.3)), "rejected")
        self.assertEqual(dtstats.status_of(oos(29, 0.5, 0.2)), "rejected")
        self.assertEqual(dtstats.status_of(oos(0, None, None)), "rejected")
        self.assertEqual(dtstats.status_of(None), "rejected")

    def test_slices_need_ten_trades(self):
        rows = [("A", "2026-10-0%d" % (1 + i % 9), "target", 1.0, "10", "Mon", "mega", "Tech", ("trend_up",), "trend_day")
                for i in range(9)]
        self.assertEqual(lab.slices(rows, 4, lab.HOURS), {})
        self.assertEqual(lab.tag_slices(rows), {})
        rows.append(("A", "2026-10-10", "stop", -1.0, "10", "Tue", "mega", "Tech", ("trend_up", "vix_lt15"), "range_day"))
        self.assertEqual(list(lab.slices(rows, 4, lab.HOURS)), ["10"])
        self.assertEqual(lab.slices(rows, 4, lab.HOURS)["10"]["n"], 10)
        self.assertEqual(list(lab.tag_slices(rows)), ["trend_up"])   # vix_lt15 has one trade
        self.assertEqual(lab.slices(rows, 5, lab.DOW), {})           # 9 Mon, 1 Tue


class Rolling(unittest.TestCase):
    """The walk-forward selects on the 30 sessions before each test day and never on the day itself."""

    def setUp(self):
        self.sessions = session_dates()                             # 60 real session dates
        self.tests = self.sessions[40:]

    def rows(self, r_before, r_after, symbols=("AAA", "BBB", "CCC"), outcome=None):
        out = []
        for d in self.sessions:
            r = r_after if d in self.tests else r_before
            for s in symbols:
                out.append((s, d, outcome or ("target" if r > 0 else "stop"), r, "10", "Mon", "mega", "Tech",
                            ("trend_up",), "range_day"))
        return out

    def test_test_days_and_windows(self):
        self.assertEqual(lab.test_sessions(self.sessions), self.tests)
        self.assertEqual(lab.test_sessions(self.sessions[:30]), [])
        self.assertEqual(lab.test_sessions(self.sessions[:35]), self.sessions[30:35])
        self.assertEqual(lab.test_sessions(self.sessions, 10, 3), self.sessions[57:])

    def test_a_config_that_only_wins_on_the_test_days_is_never_selected(self):
        good = lab.rolling_for_config(self.rows(0.5, 0.5), self.sessions)
        cheat = lab.rolling_for_config(self.rows(-1.0, 5.0), self.sessions)
        self.assertEqual(sorted(good["windows"]), self.tests)
        # the cheat only wins on test days: the first test day's window (all training days) never selects it,
        # and no window ever reaches the day it is tested on (earlier test days are legitimately in later windows)
        self.assertNotIn(self.tests[0], cheat["windows"])
        for t, w in cheat["windows"].items():
            self.assertLess(w["last"], t)
        for t, w in good["windows"].items():
            i = self.sessions.index(t)
            self.assertEqual((w["first"], w["last"]), (self.sessions[i - 30], self.sessions[i - 1]))
            self.assertLess(w["last"], t)
            self.assertEqual(w["n"], 90)                           # 30 sessions x 3 symbols
            self.assertGreater(w["lo"], 0)
        block = lab.rolling_walk_forward({"good": good}, self.sessions)
        self.assertEqual(block["test_days"], 20)
        self.assertEqual(block["selected_per_day"], [["good"]] * 20)
        self.assertEqual([d["date"] for d in block["days"]], self.tests)
        for d in block["days"]:
            self.assertLess(d["window"]["last"], d["date"])
            self.assertEqual(d["window"]["n"], 30)
            self.assertEqual(d["n_trades"], 3)
            self.assertEqual(d["R"], 1.5)
        self.assertEqual(block["equity_R"][-1], 30.0)
        self.assertEqual(block["daily_R"], [1.5] * 20)
        self.assertEqual(block["stats"]["n"], 60)
        self.assertEqual(block["stats"]["win_rate"], 1.0)
        self.assertTrue(all(t["config_id"] == "good" for t in block["trades"]))
        self.assertEqual(block["best_days"]["n_days"], 4)
        self.assertEqual(block["best_days"]["share_of_total_R"], 0.2)
        self.assertIsNone(block["best_days"]["warning"])

    def test_one_trade_per_symbol_takes_the_higher_lower_bound(self):
        strong = lab.rolling_for_config(self.rows(1.0, 1.0), self.sessions)
        weak = lab.rolling_for_config(self.rows(0.3, 0.3), self.sessions)
        only_bbb = lab.rolling_for_config(self.rows(0.9, 0.9, symbols=("BBB", "DDD")), self.sessions)
        self.assertEqual(weak["windows"][self.tests[0]]["n"], 90)
        self.assertLess(weak["windows"][self.tests[0]]["lo"], strong["windows"][self.tests[0]]["lo"])
        block = lab.rolling_walk_forward({"weak": weak, "strong": strong, "bbb": only_bbb}, self.sessions)
        day = block["days"][0]
        self.assertEqual([s["config_id"] for s in day["selected"]], ["strong", "bbb", "weak"])
        picks = {t["symbol"]: t["config_id"] for t in block["trades"] if t["date"] == day["date"]}
        self.assertEqual(picks, {"AAA": "strong", "BBB": "strong", "CCC": "strong", "DDD": "bbb"})
        self.assertEqual(day["n_trades"], 4)

    def test_too_few_trades_or_a_non_positive_mean_is_not_selected(self):
        thin = lab.rolling_for_config(self.rows(1.0, 1.0, symbols=("AAA",)), self.sessions)   # 30 a window
        self.assertEqual(thin["windows"], {})
        flat = lab.rolling_for_config(self.rows(0.0, 0.0), self.sessions)
        self.assertEqual(flat["windows"], {})
        late_bloomer = lab.rolling_for_config(self.rows(0.0, 1.0), self.sessions)
        self.assertNotIn(self.tests[0], late_bloomer["windows"])   # nothing positive before the first test day
        block = lab.rolling_walk_forward({"thin": thin, "flat": flat}, self.sessions)
        self.assertEqual(block["stats"]["n"], 0)
        self.assertEqual(block["selected_per_day"], [[]] * 20)
        self.assertIn("not positive", block["best_days"]["warning"])
        short = lab.rolling_walk_forward({}, self.sessions[:30])
        self.assertIn("needs more than 30 sessions", short["note"])
        self.assertEqual(short["test_days"], 0)

    def test_best_days_warning(self):
        b = lab.best_days([5.0, 1.0, 1.0, 1.0, 1.0], ["d1", "d2", "d3", "d4", "d5"])
        self.assertEqual((b["n_days"], b["days"], b["share_of_total_R"]), (1, ["d1"], round(5.0 / 9.0, 4)))
        self.assertIn("1 of 5 test days", b["warning"])
        b = lab.best_days([-1.0, 2.0], ["a", "b"])
        self.assertIsNone(b["share_of_total_R"])
        self.assertEqual(lab.best_days([], [])["n_days"], 0)


class Why(unittest.TestCase):
    def test_why_prints_every_config_that_fired(self):
        code, text, err = run(["why", "TSLA", "2026-10-19", "--cache-root", CACHE, "--config", CONFIG])
        self.assertEqual(code, 0, err)
        ctxd = lab.load_context(CACHE)
        store = dtbars.load_store("TSLA", "bars5m", CACHE)
        b = dtbars.bars_of(store["sessions"]["2026-10-19"])
        ctx = lab.session_context("2026-10-19", dtbars.daily_rows("TSLA", CACHE), lab.spy_bars_of("2026-10-19", ctxd),
                                  ctxd["spy_daily"], {"no_entries_before": "09:45", "no_entries_after": "15:30"})
        fired = setups.run_all(b, ctx)
        self.assertGreater(len(fired), 5)
        self.assertIn("orb15|r=1", fired)
        for cid in fired:
            self.assertRegex(text, r"(?m)^%s\s+\d{1,2}:\d\d (AM|PM)\s" % re.escape(cid))
        for cid in (c["id"] for c in setups.configs()):
            if cid not in fired:
                self.assertIn(cid, text.split("Did not fire:")[1])
        self.assertIn("%d of 31 configs matched a rule" % len(fired), text)
        self.assertIn("TSLA Mon 2026-10-19", text)
        self.assertIn("364.18", text)
        self.assertIn("358.32", text)
        self.assertIn("370.04", text)
        self.assertIn("10:15 AM", text)
        self.assertIn("Regime before the open: trend_flat, vix_15_20, prev_wide", text)
        self.assertIn("never a live filter", text)
        self.assertIn("lose money", text)
        self.assertNotIn("a buy", text.lower())
        code, text, _ = run(["why", "tsla", "2026-10-19", "--cache-root", CACHE, "--json", "--fill", "close",
                             "--cost-bps", "0"])
        doc = json.loads(text)
        self.assertEqual(len(doc["trades"]), len(fired))
        self.assertEqual(len(doc["trades"]) + len(doc["did_not_fire"]), 31)
        self.assertEqual(doc["configs_tested"], 31)
        self.assertEqual(doc["hindsight"]["day_type"], "range_day")
        orb = [t for t in doc["trades"] if t["config_id"] == "orb15|r=1"][0]
        self.assertEqual((orb["k"], orb["entry_fill"], orb["R_net"], orb["R_gross"]), (8, 364.18, orb["R_gross"], orb["R_gross"]))

    def test_why_refusals(self):
        for argv, why in ([["why", "ZZZZ", "2026-10-19"], "no bar store"],
                          [["why", "TSLA", "2026-10-18"], "no session"],
                          [["why", "SPY", "2026-10-19"], "context symbol"],
                          [["why", "TSLA", "yesterday"], "YYYY-MM-DD"]):
            code, _, err = run(argv + ["--cache-root", CACHE])
            self.assertEqual(code, 1, argv)
            self.assertIn(why, err)
            self.assertEqual(len(err.strip().splitlines()), 1)


class Fields(unittest.TestCase):
    """The context on a hand-built session: gap, relative volume, hour, weekday, tier, regime."""

    def test_trade_record_context(self):
        b = hand_session()
        for k in range(3, 6):                                       # bars 3..5 above the opening range high
            b[k] = {"t": b[k]["t"], "o": 100.6, "h": 101.5, "l": 100.5, "c": 101.0, "v": 3000}
        b[6] = dict(b[6], h=103.0, l=100.6)                         # the target bar; its low stays above the stop
        ctx = {"window": (2, 71), "prev_close": 99.0, "daily_atr": 2.0, "avg_vol20": 78 * 1000.0, "spy": None}
        cfg = [c for c in setups.configs() if c["id"] == "orb15|r=1"][0]
        trades = lab.session_trades("ACME", "2026-10-21", b, ctx, "next_open", 76, 4.0, [cfg],
                                    {"tier": "mega", "sector": "Tech"}, ["trend_up", "vix_lt15"], "trend_day")
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["k"], 3)
        self.assertEqual(t["entry_time"], "9:50 AM")
        self.assertEqual(t["hour"], "9")
        self.assertEqual(t["dow"], "Wed")
        self.assertEqual(t["gap_pct"], round((100.0 / 99.0 - 1) * 100, 3))
        self.assertEqual(t["rvol"], round(6000.0 / (78000.0 * 4 / 78.0), 3))   # 3 flat bars + one at 3000
        self.assertEqual((t["tier"], t["sector"]), ("mega", "Tech"))
        self.assertEqual(t["regime"], ["trend_up", "vix_lt15"])
        self.assertEqual(t["hindsight"], {"day_type": "trend_day"})
        self.assertEqual((t["entry"], t["stop"], t["target"]), (101.0, 100.0, 102.0))
        self.assertEqual((t["entry_fill"], t["outcome"], t["exit_slot"]), (100.6, "target", 6))
        self.assertEqual(t["exit_time"], "10:05 AM")
        t2 = lab.session_trades("ACME", "2026-10-21", b, dict(ctx, prev_close=None, avg_vol20=None), "close", 76, 0,
                                [cfg])[0]
        self.assertEqual((t2["gap_pct"], t2["rvol"], t2["tier"], t2["regime"]), (None, None, "unknown", []))
        self.assertEqual(t2["hindsight"], {"day_type": None})

    def test_context_symbols_and_the_store_listing(self):
        for s in ("SPY", "QQQ", "IWM", "DIA", "^VIX", "^VIX3M", "XLK", "XLRE", "xlf"):
            self.assertTrue(lab.is_context(s), s)
        for s in ("TSLA", "XOM", "XLNX", "BRK.B", "X"):
            self.assertFalse(lab.is_context(s), s)
        have = lab.store_symbols(CACHE)
        self.assertIn("^VIX", have)
        self.assertIn("BRK.B", have)
        syms, missing = lab.pick_symbols(CACHE)
        self.assertEqual(len(syms), 18)
        self.assertNotIn("SPY", syms)
        self.assertEqual(missing, [])
        syms, missing = lab.pick_symbols(CACHE, "tsla, SPY,NOPE,TSLA")
        self.assertEqual((syms, missing), (["TSLA"], ["NOPE"]))
        self.assertEqual(lab.load_largecap(os.path.join(CACHE, "none.json")), {})
        self.assertGreaterEqual(lab.default_workers(), 1)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-lab", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            exit_md = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-lab\n"))
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        self.assertIn("description:", fm)
        self.assertIn("argument-hint:", fm)
        self.assertGreater(len(fm), 200)
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], exit_md[exit_md.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("matched a rule", "most people who day trade lose money", "configs tested", "${claude_skill_dir}",
                     "walk-forward", "trades.jsonl.gz", "edge.json", "33 times", "-0.062", "364.18", "358.32",
                     "370.04", "what it reads and writes", "what it never does", "## steps", "## rules",
                     "confidence interval", "not a prediction", "lab.py why", "lab.py report"):
            self.assertIn(must, low, must)
        for never in ("\u2014", "a buy", "guaranteed", "p&l"):
            self.assertNotIn(never, low, never)

    def test_python_3_8_and_no_em_dash_in_the_new_files(self):
        paths = [os.path.join(ROOT, "trader-lab", "lab.py"), os.path.join(ROOT, "trader-lab", "SKILL.md"),
                 os.path.join(ROOT, "tests", "test_lab.py")]
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("\u2014", src, path)
            if path.endswith(".py"):
                for bad in ("import " + "zoneinfo", "from " + "zoneinfo", ".remove" + "prefix(", ".remove" + "suffix("):
                    self.assertNotIn(bad, src, path)
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", path)
                for bad in ("list" + "[", "dict" + "[", "tuple" + "["):
                    self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), path)


if __name__ == "__main__":
    unittest.main()
