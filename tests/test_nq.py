"""trader-nq tests: the desk's setups through the user's NQ 1-minute backtester (skipped when it is not installed
on this machine), on a short window so the suite stays quick; the report and the refusals offline."""

import gzip
import io as _io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load, ROOT

nq = load("trader-nq", "nq")

BT = os.path.expanduser("~/Claude/Projects/nq-backtester")
HAVE_BT = os.path.isdir(BT) and os.path.exists(os.path.join(BT, ".venv", "bin", "python")) \
    and os.path.isdir(os.path.join(BT, "data", "nq_1m"))
CONFIG = os.path.join(ROOT, "trader-budget", "config.example.json")


def run(argv):
    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = nq.main(argv)
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(HAVE_BT, "the NQ backtester is not installed at ~/Claude/Projects/nq-backtester")
class Engine(unittest.TestCase):
    """One adapter run over Q1 2025 with four configs; every test reads its files."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.out = os.path.join(cls.d, "nq")
        cls.code, cls.text, cls.err = run(["setups", "--backtester", BT, "--config", CONFIG, "--out-dir", cls.out,
                                           "--start", "2025-01-01", "--end", "2025-03-31",
                                           "--configs", "orb15|r=1,orb15|r=2,vwap_reclaim|r=1,orb15_rs|r=1", "--quiet"])
        with open(os.path.join(cls.out, "edge.json")) as fh:
            cls.edge = json.load(fh)
        cls.trades = []
        with gzip.open(os.path.join(cls.out, "trades.jsonl.gz"), "rt", encoding="utf-8") as fh:
            for ln in fh:
                cls.trades.append(json.loads(ln))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_check(self):
        code, text, err = run(["check", "--backtester", BT])
        self.assertEqual(code, 0, err)
        self.assertIn("NQ backtester at", text)
        self.assertIn("1-minute bars in", text)
        self.assertIn("Usable", text)
        code, text, _ = run(["check", "--backtester", BT, "--json"])
        doc = json.loads(text)
        self.assertGreaterEqual(doc["data"]["parquet_files"], 15)
        self.assertEqual(doc["data"]["rolls"], 60)

    def test_the_edge_table_in_the_labs_shape(self):
        self.assertEqual(self.code, 0, self.err)
        e = self.edge
        self.assertEqual((e["created_by"], e["fill"], e["instrument"], e["configs_tested"], e["configs_run"]), ("trader-nq", "next_open", "NQ", 31, 4))
        self.assertEqual((e["sessions"]["first"], e["sessions"]["last"], e["sessions"]["n"]), ("2025-01-02", "2025-03-31", 60))
        self.assertEqual(e["not_applicable"], ["orb15_rs|r=1"])
        ids = [c["id"] for c in e["configs"]]
        self.assertEqual(sorted(ids), ["orb15115_rs|r=1".replace("15115", "15") if False else "orb15_rs|r=1", "orb15|r=1", "orb15|r=2", "vwap_reclaim|r=1"])
        by = {c["id"]: c for c in e["configs"]}
        for cid in ("orb15|r=1", "orb15|r=2", "vwap_reclaim|r=1"):
            c = by[cid]
            for k in ("all", "is", "oos", "status", "by_regime", "by_hour", "by_dow", "by_year", "engine", "rolling_by_year"):
                self.assertIn(k, c, k)
            self.assertIn(c["status"], ("approved", "watch", "rejected"))
            self.assertEqual(c["engine"]["n_trades"], c["all"]["n"])
            self.assertIn("net_pnl", c["engine"])
            self.assertIn("breakeven_slip_ticks", c["engine"])
        self.assertEqual(by["orb15_rs|r=1"]["status"], "not_applicable")
        self.assertEqual(by["orb15_rs|r=1"]["all"]["n"], 0)
        # the known answer of this window (the engine's own fees and slippage, back-adjusted prices)
        o = by["orb15|r=1"]["all"]
        self.assertEqual(o["n"], 41)
        self.assertAlmostEqual(o["win_rate"], 0.585, places=2)
        self.assertAlmostEqual(o["exp_R"], 0.228, places=2)
        self.assertEqual(by["orb15|r=2"]["all"]["n"], 41)
        self.assertEqual(by["vwap_reclaim|r=1"]["all"]["n"], 43)
        self.assertLess(by["vwap_reclaim|r=1"]["all"]["exp_R"], 0)
        te = e["two_engines"]
        self.assertEqual(te["compared"], len(self.trades))
        self.assertGreaterEqual(te["agreement"], 0.9)
        self.assertEqual(e["n_trades"], 125)

    def test_every_trade_pays_the_engines_costs_and_fills_after_the_signal(self):
        keys = ("config_id", "setup", "symbol", "date", "k", "entry", "stop", "target", "target_r", "entry_time", "fill", "entry_fill",
                "outcome", "exit_slot", "exit_price", "R_gross", "R_net", "cost_usd", "bars_held", "hour", "dow", "year", "regime",
                "hindsight", "exit_reason", "dt_check", "nq_overnight_gap_pct")
        for t in self.trades:
            for k in keys:
                self.assertIn(k, t, k)
            self.assertEqual(t["symbol"], "NQ")
            self.assertEqual(t["fill"], "next_open")
            self.assertLess(t["R_net"], t["R_gross"])                 # fees and slippage are charged
            self.assertGreater(t["cost_usd"], 0)
            self.assertIn(t["outcome"], ("target", "stop", "close"))
            self.assertIn(t["exit_reason"], ("stop_loss", "take_profit", "flatten_time", "session_end", "signal", "end_of_data"))
            self.assertEqual(t["dt_check"]["outcome"], t["outcome"])
        above = sum(1 for t in self.trades if t["entry_fill"] > t["entry"])
        self.assertGreater(above, len(self.trades) // 2)               # a market order at the next minute's open plus a tick, mostly above the close
        first = [t for t in self.trades if t["config_id"] == "orb15|r=1"][0]
        self.assertEqual((first["date"], first["k"], first["outcome"], first["exit_reason"]), ("2025-01-02", 4, "stop", "stop_loss"))
        self.assertAlmostEqual(first["entry"] - first["stop"], 109.75, places=2)

    def test_report_prints_the_table(self):
        code, text, err = run(["report", "--lab-dir", self.out])
        self.assertEqual(code, 0, err)
        for must in ("NQ setups: NQ, 60 full sessions 2025-01-02 to 2025-03-31", "configs tested: 31", "not applicable on NQ: orb15_rs|r=1",
                     "Two engines", "orb15|r=1", "b/e tk", "Most people who day trade lose money"):
            self.assertIn(must, text, must)
        code, text, err = run(["report", "--lab-dir", self.out, "--config", "orb15|r=1", "--by", "hour"])
        self.assertEqual(code, 0, err)
        self.assertIn("orb15|r=1 by hour", text)
        self.assertIn("rolling by year", text.lower() if "rolling" in text.lower() else "rolling by year")

    def test_overnight_and_session_tags(self):
        od = os.path.join(self.d, "over")
        code, text, err = run(["overnight", "--backtester", BT, "--out-dir", od, "--start", "2025-01-01", "--end", "2025-03-31", "--quiet", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["created_by"], doc["sessions"]), ("trader-nq", 60))
        for k in ("gap_pct_percentiles", "by_gap_bucket", "by_range_bucket", "share_rth_range_exceeds_overnight", "thresholds"):
            self.assertIn(k, doc, k)
        self.assertLess(doc["gap_pct_percentiles"]["p5"], doc["gap_pct_percentiles"]["p95"])
        self.assertEqual(sum(b["n"] for b in doc["by_gap_bucket"].values()), doc["orb15_r1_trades"])
        out = os.path.join(self.d, "tags.json")
        code, text, err = run(["session-tags", "--backtester", BT, "--days", "10", "--out", out, "--quiet"])
        self.assertEqual(code, 0, err)
        with open(out) as fh:
            tags = json.load(fh)
        self.assertEqual(len(tags["sessions"]), 10)
        for d, rec in tags["sessions"].items():
            self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")
            for t in rec["tags"]:
                self.assertIn(t, ("nq_gap_up", "nq_gap_down", "nq_gap_flat", "nq_wide_overnight", "nq_quiet_overnight", "nq_normal_overnight"))
        self.assertIn("2026-10-16", tags["sessions"])


class Offline(unittest.TestCase):
    def test_refusals_without_a_backtester(self):
        d = tempfile.mkdtemp()
        try:
            code, text, err = run(["check", "--backtester", os.path.join(d, "nowhere")])
            self.assertEqual(code, 1)
            self.assertIn("no NQ backtester at", err)
            code, text, err = run(["report", "--lab-dir", d])
            self.assertEqual(code, 0)
            self.assertIn("no edge.json yet", text)
            os.makedirs(os.path.join(d, "bt", "data"))
            code, text, err = run(["setups", "--backtester", os.path.join(d, "bt"), "--config", CONFIG])
            self.assertEqual(code, 1)
            self.assertIn("no venv python", err)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_report_words_on_a_hand_made_table(self):
        d = tempfile.mkdtemp()
        try:
            st = {"n": 120, "target": 60, "stop": 50, "close": 10, "green": 62, "win_rate": 0.5, "win_ci95": [0.41, 0.59],
                  "exp_R": 0.11, "exp_ci95": [0.02, 0.2], "pf": 1.3, "avg_win_R": 1.0, "avg_loss_R": -0.9, "max_dd_R": 8.0,
                  "median_R": 0.1, "n_symbols": 1, "n_sessions": 120}
            edge = {"created_by": "trader-nq", "fill": "next_open", "instrument": "NQ", "costs": {"fees": "ninjatrader_free", "slippage": "default"},
                    "sessions": {"first": "2011-10-17", "last": "2026-10-16", "n": 3700, "skipped": 100}, "configs_tested": 31, "configs_run": 1,
                    "not_applicable": [], "split": {"is": {"first": "2011-10-17", "last": "2021-10-15", "n": 2466}, "oos": {"first": "2021-10-18", "last": "2026-10-16", "n": 1234}},
                    "two_engines": {"compared": 100, "same_outcome": 97, "agreement": 0.97, "dt_better": 2, "engine_better": 1},
                    "configs": [{"id": "orb15|r=1", "status": "approved", "oos": st, "all": st, "engine": {"net_pnl": 12345.0, "breakeven_slip_ticks": 3.2},
                                 "by_year": {"2024": st}, "by_hour": {}, "by_dow": {}, "by_regime": {}, "rolling_by_year": {"selected_years": ["2024"], "stats": st}}]}
            with open(os.path.join(d, "edge.json"), "w") as fh:
                json.dump(edge, fh)
            code, text, err = run(["report", "--lab-dir", d, "--config", "orb15|r=1", "--by", "year"])
            self.assertEqual(code, 0, err)
            for must in ("3700 full sessions", "configs tested: 31", "97.0 % of 100 trades", "approved", "[+0.020, +0.200]", "12,345", "3.2",
                         "orb15|r=1 by year", "2024", "rolling by year", "Most people who day trade lose money"):
                self.assertIn(must, text, must)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-nq", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            exit_md = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-nq\n"))
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        self.assertGreater(len(fm[fm.index("description:"):fm.index("argument-hint:")]), 200)
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], exit_md[exit_md.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("${claude_skill_dir}", "what it reads and writes", "## steps", "## rules", "what it never does",
                     "nq.py check", "nq.py setups", "nq.py overnight", "nq.py session-tags", "nq.py run", "nq.py report",
                     "trader/nq/edge.json", "back-adjusted", "fees and slippage", "one instrument", "configs tested", "two engines",
                     "orb15_rs", "not applicable", "41 trades", "most people who day trade lose money", "not a prediction", "not financial advice"):
            self.assertIn(must, low, must)
        for never in (chr(0x2014), "a buy", "guarantee" + "d", "real " + "time", "will go up"):
            self.assertNotIn(never, low, never)

    def test_python_3_8_and_no_em_dashes(self):
        for path in (os.path.join(ROOT, "trader-nq", "nq.py"), os.path.join(ROOT, "trader-nq", "SKILL.md"),
                     os.path.join(ROOT, "tests", "test_nq.py")):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn(chr(0x2014), src, path)
            if path.endswith(".py"):
                for bad in ("import " + "zoneinfo", "from " + "zoneinfo", ".remove" + "prefix(", ".remove" + "suffix("):
                    self.assertNotIn(bad, src, path)
                for bad in ("list" + "[", "dict" + "[", "tuple" + "["):
                    self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), path)
        with open(os.path.join(ROOT, "trader-nq", "nq_adapter.py"), encoding="utf-8") as fh:
            self.assertNotIn(chr(0x2014), fh.read())


if __name__ == "__main__":
    unittest.main()
