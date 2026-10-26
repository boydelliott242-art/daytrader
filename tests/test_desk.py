"""trader-desk tests: the board replayed from the bar store (tests/fixtures/dt-cache) with a hand-made edge
table (tests/fixtures/trader-desk/edge.json), exclusions, the late flag, one candidate a symbol, the scorecard
log, pick (the upstream exit plan), the offline morning and the refusals. The upstream time machine runs once
through the desk's subprocess path on the store-built chart. No network."""

import io as _io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load, FIX, ROOT

desk = load("trader-desk", "desk")

CACHE = os.path.join(FIX, "dt-cache")
DFIX = os.path.join(FIX, "trader-desk")
EDGE = os.path.join(DFIX, "edge.json")
REGIME = os.path.join(DFIX, "regime.json")
EXCL = os.path.join(DFIX, "exclude-tsla.json")
MORNING = os.path.join(DFIX, "morning")
EXAMPLE_CONFIG = os.path.join(ROOT, "trader-budget", "config.example.json")
TSLA_ID = "2026-10-19|TSLA|orb15_rs|r=1|10:15 AM"


def run(argv):
    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = desk.main(argv)
    return code, out.getvalue(), err.getvalue()


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Desk(unittest.TestCase):
    """A temp working folder with trader/config.json, like a project the skills run in."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs("trader", exist_ok=True)
        shutil.copy(EXAMPLE_CONFIG, os.path.join("trader", "config.json"))

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d, ignore_errors=True)

    def replay(self, *extra, **kw):
        at = kw.get("at", "10:15")
        argv = ["replay", "--date", "2026-10-19", "--at", at, "--cache-root", CACHE, "--edge", EDGE, "--regime", REGIME,
                "--no-timemachine", "--top", str(kw.get("top", 20))] + list(extra)
        return run(argv)


class Replay(Desk):
    def test_the_reel_day_at_10_15(self):
        code, text, err = self.replay("--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["created_by"], doc["mode"], doc["date"], doc["as_of"]), ("trader-desk", "live", "2026-10-19", "2026-10-19 10:15 ET"))
        self.assertEqual(doc["source"], "cache")
        self.assertEqual(doc["regime_tags"], ["trend_flat", "vix_20_30", "prev_normal"])
        self.assertEqual(sorted(doc["configs_run"]), ["orb15_down|r=1|short", "orb15_rs|r=1", "orb15|r=1"])   # approved only, both sides; watch and rejected out
        tsla = [c for c in doc["candidates"] if c["symbol"] == "TSLA"]
        self.assertEqual(len(tsla), 1)                                   # one candidate a symbol
        t = tsla[0]
        sig = t["signal"]
        self.assertEqual((sig["entry"], sig["stop"], sig["target"], sig["target_r"], sig["entry_time"], sig["k"]),
                         (364.18, 358.32, 370.04, 1.0, "10:15 AM", 8))
        self.assertEqual(sig["opening_range"], {"high": 362.18, "low": 354.47, "minutes": 15})
        self.assertEqual(t["config_id"], "orb15_rs|r=1")                 # the higher OOS lower bound (0.06 vs 0.05 in the regime slice)
        self.assertEqual([a["config_id"] for a in t["also_fired"]], ["orb15|r=1"])
        self.assertEqual(t["also_fired"][0]["entry"], 364.18)
        self.assertEqual(t["lab"]["status"], "approved")
        self.assertEqual(t["lab"]["oos"]["n"], 150)
        self.assertEqual(t["score"], 0.06)
        self.assertNotIn("late", t["flags"])
        self.assertIn("bars_since_entry", sig)
        self.assertEqual(sig["bars_since_entry"], 0)
        for c in doc["candidates"]:
            self.assertIn(c["config_id"], ("orb15_rs|r=1", "orb15|r=1"))
            self.assertIsNotNone(c["rank"])
        self.assertIn("Most people who day trade lose money", doc["note"])

    def test_the_board_text(self):
        code, text, err = self.replay()
        self.assertEqual(code, 0, err)
        for must in ("TSLA", "364.18", "358.32", "370.04", "10:15 AM", "n 150", "configs tested" if "configs tested" in text else "lab OOS",
                     "matched a rule", "Most people who day trade lose money", "Nothing was placed", "trend_flat"):
            self.assertIn(must, text, must)
        self.assertNotIn("Traceback", err)

    def test_one_candidate_a_symbol_with_the_rest_under_it(self):
        code, text, _ = self.replay("--json")
        doc = json.loads(text)
        syms = [c["symbol"] for c in doc["candidates"]]
        self.assertEqual(len(syms), len(set(syms)))
        for c in doc["candidates"]:
            for a in c.get("also_fired", []):
                self.assertNotEqual(a["config_id"], c["config_id"])
                self.assertIn("entry", a)

    def test_late_after_the_bar(self):
        code, text, _ = self.replay("--json", at="10:50", top=40)
        doc = json.loads(text)
        tsla = [c for c in doc["candidates"] if c["symbol"] == "TSLA"][0]
        self.assertIn("late", tsla["flags"])
        self.assertEqual(tsla["signal"]["bars_since_entry"], 7)
        self.assertEqual(tsla["signal"]["entry_time"], "10:15 AM")       # the trigger does not move
        fresh = [c for c in doc["candidates"] if "late" not in c["flags"]]
        late = [c for c in doc["candidates"] if "late" in c["flags"]]
        self.assertTrue(all(c["rank"] < l["rank"] for c in fresh for l in late))   # fresh ranks first

    def test_exclusions_remove_a_name(self):
        code, text, _ = self.replay("--json", "--exclusions", EXCL)
        doc = json.loads(text)
        self.assertFalse(any(c["symbol"] == "TSLA" for c in doc["candidates"]))
        self.assertEqual([e["symbol"] for e in doc["excluded"]["earnings"]], ["TSLA"])
        code, text, _ = self.replay("--json", "--exclusions", EXCL, "--allow-earnings")
        doc = json.loads(text)
        tsla = [c for c in doc["candidates"] if c["symbol"] == "TSLA"]
        self.assertEqual(len(tsla), 1)
        self.assertIn("earnings", tsla[0]["flags"])

    def test_the_short_board_runs_apart_from_the_long_one(self):
        code, text, err = self.replay("--json", top=40)
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        shorts = doc["candidates_short"]
        self.assertTrue(shorts)
        self.assertFalse(any(c.get("side") == "short" for c in doc["candidates"]))
        for c in shorts:
            self.assertEqual((c["side"], c["config_id"]), ("short", "orb15_down|r=1|short"))
            sig = c["signal"]
            self.assertEqual(sig["side"], "short")
            self.assertTrue(sig["target"] < sig["entry"] < sig["stop"], sig)
            self.assertIn("short", c["flags"])
            self.assertEqual(c["lab"]["status"], "approved")
        orcl = [c for c in shorts if c["symbol"] == "ORCL"][0]
        self.assertNotIn("late", orcl["flags"])
        self.assertEqual((orcl["signal"]["k"], orcl["signal"]["entry"], orcl["signal"]["stop"], orcl["signal"]["target"]),
                         (7, 129.2, 130.09, 128.31))
        meta = [c for c in shorts if c["symbol"] == "META"][0]
        self.assertIn("late", meta["flags"])                             # bar 3 closed long before 10:15
        self.assertIn("research only", doc["short_note"])
        self.assertEqual(doc["counts"]["candidates_short"], len(shorts))
        code, text, err = self.replay(top=40)
        self.assertIn("The short board", text)
        self.assertIn("SHORT  ORCL", text)
        self.assertIn("sell short 129.20 at 10:10 AM (bar 7), stop 130.09 above, target 128.31 below", text)
        self.assertIn("lose more than the stake", text)

    def test_pick_refuses_a_short_and_the_log_keeps_it(self):
        code, _, err = self.replay("--write", "--quiet", top=40)
        self.assertEqual(code, 0, err)
        code, text, err = run(["pick", "ORCL", "--write"])
        self.assertNotEqual(code, 0)
        self.assertIn("SHORT candidate", err)
        self.assertFalse(os.path.exists(os.path.join("trader", "entry.json")))
        with open(os.path.join("trader", "scorecard", "calls.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        shorts = [r for r in rows if r["config_id"].endswith("|short")]
        self.assertTrue(shorts)
        self.assertTrue(all(r["signal"]["side"] == "short" for r in shorts))
        self.assertIn("2026-10-19|ORCL|orb15_down|r=1|short|10:10 AM", [r["id"] for r in shorts])

    def test_no_edge_runs_every_config_and_says_so(self):
        code, text, err = run(["replay", "--date", "2026-10-19", "--at", "10:15", "--cache-root", CACHE,
                               "--edge", os.path.join(self.d, "none.json"), "--no-timemachine", "--top", "3"])
        self.assertEqual(code, 0, err)
        self.assertIn("the lab has not run", text)
        self.assertIn("untested", text)

    def test_write_logs_each_call_once(self):
        calls = os.path.join("trader", "scorecard", "calls.jsonl")
        code, _, err = self.replay("--write", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join("trader", "desk.json")))
        n1 = len(open(calls).read().splitlines())
        self.assertGreater(n1, 0)
        ids = [json.loads(l)["id"] for l in open(calls)]
        self.assertIn(TSLA_ID, ids)
        self.assertEqual(len(ids), len(set(ids)))
        row = [json.loads(l) for l in open(calls) if json.loads(l)["id"] == TSLA_ID][0]
        self.assertEqual((row["symbol"], row["setup"], row["config_id"], row["lab_oos_exp_R"]), ("TSLA", "orb15_rs", "orb15_rs|r=1", 0.2))
        self.assertEqual(row["signal"]["entry"], 364.18)
        self.assertFalse(row["late"])
        self.replay("--write", "--quiet")
        self.assertEqual(len(open(calls).read().splitlines()), n1)   # the same board twice: no duplicates
        doc = read(os.path.join("trader", "desk.json"))
        self.assertEqual(doc["mode"], "live")

    def test_refusals_are_one_plain_line(self):
        code, text, err = run(["replay", "--date", "2026-10-19", "--at", "09:45", "--cache-root", CACHE, "--no-timemachine"])
        self.assertEqual(code, 1)
        self.assertIn("first bar after it closes at 9:50 AM", err + text)
        self.assertNotIn("Traceback", err)
        code, text, err = run(["replay", "--date", "2026-10-18", "--at", "10:15", "--cache-root", CACHE, "--no-timemachine"])
        self.assertEqual(code, 1)
        self.assertIn("weekend", err + text)
        code, text, err = run(["replay", "--date", "2026-10-19", "--at", "15:45", "--cache-root", CACHE, "--no-timemachine"])
        self.assertEqual(code, 1)
        self.assertIn("no_entries_after", err + text)
        code, text, err = run(["pick", "TSLA"])
        self.assertEqual(code, 1)
        self.assertIn("no live board", err + text)


class TimeMachine(Desk):
    def test_the_upstream_time_machine_runs_on_the_store_built_chart(self):
        code, text, err = run(["replay", "--date", "2026-10-19", "--at", "10:15", "--cache-root", CACHE, "--edge", EDGE,
                               "--regime", REGIME, "--top", "3", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        tsla = [c for c in doc["candidates"] if c["symbol"] == "TSLA"][0]
        tm = tsla["timemachine"]
        self.assertIsNotNone(tm, "the time machine block is missing")
        self.assertNotIn("error", tm, tm)
        self.assertEqual(tm["seed"], 19)
        # the reel: 29.4 / 25.5 / 45.1 at seed 19 on Yahoo's own chart; the store-built chart holds the same bars
        self.assertAlmostEqual(tm["p_target"], 0.294, delta=0.03)
        self.assertAlmostEqual(tm["p_stop"], 0.255, delta=0.03)
        self.assertAlmostEqual(tm["p_neither"], 0.451, delta=0.03)
        # the test store starts 2026-07-31 (55 sessions before the reel day); Yahoo's own 60-day chart had 59 and 33 fires
        self.assertEqual(tm["history"]["sessions"], 55)
        self.assertEqual(tm["history"]["triggered"], 31)
        self.assertIn("verdict", tm)
        self.assertIn("coin flip", tm["verdict"].lower())


class Pick(Desk):
    def test_pick_writes_the_upstream_entry_and_plan(self):
        self.replay("--write", "--quiet")
        code, text, err = run(["pick", "TSLA", "--write", "--now", "2026-10-19 10:16"])
        self.assertEqual(code, 0, err)
        entry = read(os.path.join("trader", "entry.json"))
        self.assertEqual((entry["symbol"], entry["setup"], entry["status"], entry["price"], entry["bar_index"], entry["time"]),
                         ("TSLA", "orb", "triggered", 364.18, 8, "10:15 AM"))
        self.assertEqual(entry["opening_range"], {"high": 362.18, "low": 354.47, "minutes": 15})
        plan = read(os.path.join("trader", "plan.json"))
        self.assertEqual((plan["created_by"], plan["symbol"], plan["entry"], plan["stop"], plan["target"], plan["target_r"]),
                         ("trader-exit", "TSLA", 364.18, 358.32, 370.04, 1.0))
        self.assertEqual((plan["entry_time"], plan["flat_by"], plan["risk_per_share"]), ("10:15 AM", "15:55", 5.86))
        for must in ("timemachine.py run", "size.py --write", "check.py", "ticket.py show", "Nothing was placed",
                     "never sizes, checks or places"):
            self.assertIn(must, text, must)
        code, text, err = run(["pick", "TSLA", "--write", "--now", "2026-10-19 10:16", "--target", "2R"])
        self.assertEqual(code, 0, err)
        self.assertEqual(read(os.path.join("trader", "plan.json"))["target"], 375.90)
        code, text, err = run(["pick", "ZZZZ", "--now", "2026-10-19 10:16"])
        self.assertEqual(code, 1)
        self.assertIn("not on the board", err + text)


class Morning(Desk):
    def test_offline_morning_builds_the_exclusions_and_the_plan(self):
        code, text, err = run(["morning", "--offline", MORNING, "--cache-root", CACHE, "--now", "2026-10-19 08:30", "--write", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["created_by"], doc["mode"], doc["date"], doc["next_session"]), ("trader-desk", "morning", "2026-10-19", "2026-10-20"))
        ex = {e["symbol"]: e for e in doc["exclusions"]["earnings"]}
        self.assertEqual(sorted(ex), ["AAPL", "NVDA"])                  # NVDA tonight, AAPL tomorrow pre-market
        self.assertIn("after the close", ex["NVDA"]["reason"])
        self.assertIn("tomorrow pre-market", ex["AAPL"]["reason"])
        self.assertNotIn("MSFT", ex)                                     # tomorrow after the close is not an exclusion
        self.assertIn("MSFT", [w["symbol"] for w in doc["earnings_week"]])
        self.assertEqual([h["symbol"] for h in doc["daily_hits"]], ["XOM", "TSLA"])
        self.assertEqual(doc["news"]["TSLA"]["tags"].get("earnings"), 1)
        self.assertTrue(doc["news"]["TSLA"]["moving"])
        self.assertEqual(doc["regime_tags"], ["trend_flat", "vix_20_30", "prev_normal"])
        self.assertTrue(os.path.exists(os.path.join("trader", "desk.json")))
        # the live board then reads the morning file as its exclusions
        code, text, _ = self.replay("--json", "--exclusions", os.path.join("trader", "desk.json"))
        board = json.loads(text)
        self.assertFalse(any(c["symbol"] in ("NVDA", "AAPL") for c in board["candidates"]))
        self.assertEqual(sorted(e["symbol"] for e in board["excluded"]["earnings"]), ["AAPL", "NVDA"])

    def test_offline_wants_a_folder(self):
        code, text, err = run(["morning", "--offline", os.path.join(self.d, "nowhere"), "--cache-root", CACHE])
        self.assertEqual(code, 1)
        self.assertIn("--offline wants a folder", err + text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-desk", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            exit_md = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-desk\n"))
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        self.assertGreater(len(fm[fm.index("description:"):fm.index("argument-hint:")]), 200)
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], exit_md[exit_md.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("${claude_skill_dir}", "what it reads and writes", "## steps", "## rules", "what it never does",
                     "desk.py morning", "desk.py live", "desk.py replay", "desk.py pick", "desk.py grade", "trader/desk.json",
                     "edge.json", "calls.jsonl", "364.18", "358.32", "370.04", "late", "approved", "one candidate a symbol",
                     "matched a rule", "most people who day trade lose money", "not a prediction", "not financial advice",
                     "never sizes", "exit.py plan", "timemachine"):
            self.assertIn(must, low, must)
        for never in (chr(0x2014), "a buy", "guarantee" + "d", "real " + "time", "will go up"):
            self.assertNotIn(never, low, never)

    def test_python_3_8_and_no_em_dashes(self):
        for path in (os.path.join(ROOT, "trader-desk", "desk.py"), os.path.join(ROOT, "trader-desk", "SKILL.md"),
                     os.path.join(ROOT, "tests", "test_desk.py")):
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
