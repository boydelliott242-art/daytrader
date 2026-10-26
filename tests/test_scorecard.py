"""trader-scorecard tests. The boards are hand-made from real signals in the fixture bar store
(tests/fixtures/dt-cache): TSLA 2026-10-19 is the reel's entry, 364.18 at 10:15 AM (bar 8), stop 358.32,
target 370.04. Everything offline; the one code path that would reach Yahoo is monkeypatched."""

import copy
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

scorecard = load("trader-scorecard", "scorecard")
bars = scorecard.bars
stats = scorecard.stats
net = scorecard.net

CACHE = os.path.join(FIX, "dt-cache")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scorecard.main(argv)
    return code, out.getvalue(), err.getvalue()


def sig(setup, k, entry, stop, target, entry_time, stop_rule="or_mid", orr=None, target_r=1.0):
    params = {"or_minutes": 15, "target_r": target_r, "stop_rule": stop_rule} if setup.startswith("orb") \
        else {"below_bars": 3, "target_r": target_r}
    return {"setup": setup, "params": params, "k": k, "entry": entry, "stop": stop, "target": target,
            "target_r": target_r, "stop_rule": stop_rule, "entry_time": entry_time, "opening_range": orr, "level": None}


def cand(rank, symbol, config_id, signal, exp=None, p_target=None, status="approved", flags=None):
    lab = {"status": status, "oos": {"n": 120, "exp_R": exp, "exp_ci95": [None, None]}, "regime_slice": None,
           "fill_note": "next_open"}
    tm = {"p_target": p_target, "p_stop": 0.3, "p_neither": 0.4, "seed": 19} if p_target is not None else None
    return {"rank": rank, "symbol": symbol, "name": symbol + " Inc.", "sector": "Technology", "tier": "mega",
            "setup": config_id.split("|")[0], "config_id": config_id, "label": "a rule", "signal": signal,
            "lab": lab, "timemachine": tm, "flags": flags or [], "score": 0.1}


def board(as_of, cands, mode="live"):
    return {"created_by": "trader-desk", "mode": mode, "as_of": as_of, "regime": {"date": as_of[:10]}, "universe": 900,
            "scanned": 900, "excluded": {"earnings": [], "blocked": []}, "candidates": cands,
            "note": "Candidates matched a rule with a measured record. None is a prediction."}


# the reel's TSLA entry and two other 10:15 AM signals from the fixture store, all on Monday 2026-10-19
TSLA = cand(1, "TSLA", "orb15|r=1", sig("orb15", 8, 364.18, 358.32, 370.04, "10:15 AM", "or_mid",
                                         {"high": 362.18, "low": 354.47, "minutes": 15}), 0.12, 0.294)
MSFT = cand(2, "MSFT", "orb15|r=1", sig("orb15", 8, 534.15, 530.68, 537.62, "10:15 AM", "or_mid",
                                         {"high": 532.68, "low": 528.73, "minutes": 15}), 0.08, None)
JPM = cand(3, "JPM", "vwap_reclaim|r=1", sig("vwap_reclaim", 8, 338.53, 336.70, 340.36, "10:15 AM", "low_of_last_4"),
           0.05, 0.31)
DESK_A = board("2026-10-19 10:15 ET", [TSLA, MSFT, JPM])
# a 10:10 AM trigger shown on the 10:45 board: late
AMZN = cand(1, "AMZN", "orb15|r=1", sig("orb15", 7, 258.21, 256.48, 259.94, "10:10 AM", "or_mid",
                                         {"high": 258.00, "low": 254.96, "minutes": 15}), 0.15, 0.41, flags=["late"])
DESK_LATE = board("2026-10-19 10:45 ET", [AMZN])
# the next day and the Friday before (another week)
TSLA_B = cand(1, "TSLA", "orb15|r=1", sig("orb15", 5, 370.58, 367.06, 374.10, "10:00 AM", "or_mid",
                                           {"high": 370.49, "low": 363.64, "minutes": 15}), 0.12, 0.33)
DESK_B = board("2026-10-20 10:00 ET", [TSLA_B])
NVDA_C = cand(1, "NVDA", "orb15|r=1", sig("orb15", 9, 216.94, 215.93, 217.95, "10:20 AM", "or_mid",
                                           {"high": 216.80, "low": 215.07, "minutes": 15}), 0.10, 0.28)
DESK_C = board("2026-10-16 10:20 ET", [NVDA_C])

TSLA_ID = "2026-10-19|TSLA|orb15|r=1|10:15 AM"
# what dt.sim.play says on the fixture bars (next-open fill, flat by slot 76, 4 bps)
EXPECT = {TSLA_ID: ("close", 76, 0.4896, 364.215, 367.2299, 0.4956),
          "2026-10-19|MSFT|orb15|r=1|10:15 AM": ("close", 76, -0.246, 534.16, 533.52, -0.2431),
          "2026-10-19|JPM|vwap_reclaim|r=1|10:15 AM": ("stop", 25, -1.1041, 338.585, 336.70, -1.074),
          "2026-10-19|AMZN|orb15|r=1|10:10 AM": ("target", 10, 0.9461, 258.20, 259.94, 0.9403),
          "2026-10-20|TSLA|orb15|r=1|10:00 AM": ("target", 8, 0.9488, 370.612, 374.10, 0.9579),
          "2026-10-16|NVDA|orb15|r=1|10:20 AM": ("target", 12, 1.0032, 216.85, 217.95, 0.9141)}


def fixture_series(symbol, day, as_symbol=None):
    """A Yahoo-like series (the dt.net.parse_chart shape) of one fixture session."""
    store = bars.load_store(symbol, "bars5m", CACHE)
    rows = store["sessions"][day]
    return {"t": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows], "l": [r[3] for r in rows],
            "c": [r[4] for r in rows], "v": [r[5] for r in rows], "meta": {"symbol": as_symbol or symbol,
                                                                            "dataGranularity": "5m"}}


def chart_doc(series):
    """The raw v8 chart document for a saved-chart file."""
    return {"chart": {"result": [{"meta": series["meta"], "timestamp": series["t"],
                                  "indicators": {"quote": [{"open": series["o"], "high": series["h"], "low": series["l"],
                                                            "close": series["c"], "volume": series["v"]}]}}],
                      "error": None}}


class Temp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        self._chart = net.chart

    def tearDown(self):
        net.chart = self._chart
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def read_lines(self, name):
        p = os.path.join("trader", "scorecard", name)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def log(self, desk, now, name="desk.json"):
        self.write(os.path.join("trader", name), desk)
        return run(["log", "--from-desk", "--desk", os.path.join("trader", name), "--now", now])

    def grade(self, now, *args):
        return run(["grade", "--cache-root", CACHE, "--now", now] + list(args))

    def no_network(self):
        def refuse(*a, **k):
            raise AssertionError("the network was touched: net.chart%r" % (a,))
        net.chart = refuse

    def log_everything(self):
        self.log(DESK_C, "2026-10-16 10:21", "c.json")
        self.log(DESK_A, "2026-10-19 10:17", "a.json")
        self.log(DESK_LATE, "2026-10-19 10:46", "late.json")
        self.log(DESK_B, "2026-10-20 10:02", "b.json")


class Helpers(unittest.TestCase):
    def test_entry_time_to_bar(self):
        self.assertEqual(scorecard.k_from_entry_time("10:15 AM"), 8)
        self.assertEqual(scorecard.k_from_entry_time("9:35 AM"), 0)
        self.assertEqual(scorecard.k_from_entry_time("12:00 PM"), 29)
        self.assertEqual(scorecard.k_from_entry_time("3:55 PM"), 76)
        for bad in ("10:17 AM", "9:30 AM", "10:15", "", None, "25:00 AM"):
            with self.assertRaises(scorecard.ScorecardError):
                scorecard.k_from_entry_time(bad)

    def test_normalize_signal(self):
        s = scorecard.normalize_signal({"entry": 364.18, "stop": 358.32, "target": 370.04, "entry_time": "10:15 AM"})
        self.assertEqual((s["k"], s["entry_time"], s["risk_per_share"], s["target_r"]), (8, "10:15 AM", 5.86, 1.0))
        s = scorecard.normalize_signal({"entry": "364.18", "stop": 358.32, "target": 370.04, "k": 8})
        self.assertEqual((s["k"], s["entry_time"], s["entry"]), (8, "10:15 AM", 364.18))
        for bad in ({"entry": 364.18, "stop": 364.18, "target": 370.04, "k": 8},        # stop at the entry
                    {"entry": 364.18, "stop": 358.32, "target": 364.0, "k": 8},         # target under the entry
                    {"entry": 364.18, "stop": 358.32, "target": 370.04},                # no bar, no time
                    {"entry": 364.18, "stop": 358.32, "target": 370.04, "k": 8, "entry_time": "10:20 AM"},
                    {"entry": 364.18, "stop": 358.32, "target": 370.04, "k": 77},
                    {"entry": None, "stop": 358.32, "target": 370.04, "k": 8}, None, "x"):
            with self.assertRaises(scorecard.ScorecardError):
                scorecard.normalize_signal(bad)

    def test_late_is_more_than_one_bar_after_the_entry_bar_closed(self):
        call = {"date": "2026-10-19", "signal": {"k": 8}}
        # bar 8 closes 10:15 ET = 14:15Z; the grace is one bar
        for logged, late, mins in (("2026-10-19T14:16:00Z", False, 1.0), ("2026-10-19T14:20:00Z", False, 5.0),
                                   ("2026-10-19T14:20:01Z", True, 5.0), ("2026-10-19T14:21:00Z", True, 6.0),
                                   ("2026-10-19T14:10:00Z", False, -5.0), ("2026-10-19T15:00:00Z", True, 45.0)):
            self.assertEqual(scorecard.lateness(dict(call, logged_at=logged)), (late, mins), logged)
        # in winter the same clock is an hour later in UTC
        self.assertEqual(scorecard.lateness({"date": "2026-12-01", "signal": {"k": 8}, "logged_at": "2026-12-01T15:20:00Z"}),
                         (False, 5.0))
        with self.assertRaises(scorecard.ScorecardError):
            scorecard.lateness(dict(call, logged_at="yesterday"))

    def test_ids_and_dates(self):
        self.assertEqual(scorecard.call_id("2026-10-19", "TSLA", "orb15|r=1", "10:15 AM"), TSLA_ID)
        self.assertEqual(scorecard.desk_date({"as_of": "2026-10-19 10:15 ET"}), "2026-10-19")
        self.assertEqual(scorecard.desk_date({"regime": {"date": "2026-10-20"}, "as_of": "x"}), "2026-10-20")
        with self.assertRaises(scorecard.ScorecardError):
            scorecard.desk_date({"as_of": "soon"})
        self.assertEqual(scorecard.week_of("2026-10-21"), "2026-10-19")
        self.assertEqual(scorecard.week_of("2026-10-16"), "2026-10-12")


class Log(Temp):
    def test_log_twice_gives_three_lines(self):
        code, text, err = self.log(DESK_A, "2026-10-19 10:17")
        self.assertEqual(code, 0, err)
        self.assertIn("Logged 3 new call(s), 0 already", text)
        lines = self.read_lines("calls.jsonl")
        self.assertEqual([c["id"] for c in lines], [TSLA_ID, "2026-10-19|MSFT|orb15|r=1|10:15 AM",
                                                    "2026-10-19|JPM|vwap_reclaim|r=1|10:15 AM"])
        c = lines[0]
        self.assertEqual(c["logged_at"], "2026-10-19T14:17:00Z")
        self.assertEqual((c["date"], c["as_of"], c["symbol"], c["setup"], c["config_id"], c["rank"]),
                         ("2026-10-19", "2026-10-19 10:15 ET", "TSLA", "orb15", "orb15|r=1", 1))
        self.assertEqual((c["lab_oos_exp_R"], c["tm_p_target"]), (0.12, 0.294))
        self.assertIsNone(lines[1]["tm_p_target"])
        self.assertEqual((c["signal"]["k"], c["signal"]["entry"], c["signal"]["stop"], c["signal"]["target"]),
                         (8, 364.18, 358.32, 370.04))
        self.assertNotIn("late:", text)
        code, text, _ = self.log(DESK_A, "2026-10-19 10:18")
        self.assertEqual(code, 0)
        self.assertIn("Logged 0 new call(s), 3 already", text)
        self.assertEqual(len(self.read_lines("calls.jsonl")), 3)

    def test_the_desk_may_have_appended_the_same_ids_first(self):
        mine = {"id": TSLA_ID, "logged_at": "2026-10-19T14:16:00Z", "date": "2026-10-19", "symbol": "TSLA",
                "config_id": "orb15|r=1", "signal": TSLA["signal"]}
        os.makedirs(os.path.join("trader", "scorecard"))
        with open(os.path.join("trader", "scorecard", "calls.jsonl"), "w") as fh:
            fh.write(json.dumps(mine) + "\n")
        code, text, _ = self.log(DESK_A, "2026-10-19 10:17")
        self.assertEqual(code, 0)
        self.assertIn("Logged 2 new call(s), 1 already", text)
        lines = self.read_lines("calls.jsonl")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["logged_at"], "2026-10-19T14:16:00Z")      # the first line stands

    def test_a_late_board_says_so_when_logging(self):
        code, text, _ = self.log(DESK_LATE, "2026-10-19 10:46")
        self.assertEqual(code, 0)
        self.assertIn("[late: logged 36 min after the entry bar closed]", text)

    def test_a_signal_without_a_bar_takes_it_from_the_time(self):
        c = copy.deepcopy(TSLA)
        del c["signal"]["k"]
        self.log(board("2026-10-19 10:15 ET", [c]), "2026-10-19 10:17")
        self.assertEqual(self.read_lines("calls.jsonl")[0]["signal"]["k"], 8)

    def test_log_refusals_and_skips(self):
        code, _, err = run(["log", "--now", "2026-10-19 10:17"])
        self.assertEqual(code, 1)
        self.assertIn("--from-desk", err)
        code, _, err = run(["log", "--from-desk", "--now", "2026-10-19 10:17"])
        self.assertEqual(code, 1)
        self.assertIn("no desk board at trader/desk.json", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        broken = copy.deepcopy(DESK_A)
        broken["candidates"][1]["signal"] = {"entry": 1, "stop": 2, "target": 3, "k": 8}
        broken["candidates"].append({"symbol": "", "signal": {}})
        broken["candidates"].append("junk")
        code, text, _ = self.log(broken, "2026-10-19 10:17")
        self.assertEqual(code, 0)
        self.assertIn("Logged 2 new call(s)", text)
        self.assertIn("skipped MSFT: a long needs stop < entry < target", text)
        self.assertEqual(len(self.read_lines("calls.jsonl")), 2)
        code, text, _ = self.log(board("2026-10-19 08:30 ET", [], "morning"), "2026-10-19 08:31")
        self.assertEqual(code, 0)
        self.assertIn("0 candidate(s)", text)
        self.write(os.path.join("trader", "desk.json"), DESK_B)
        code, text, _ = run(["log", "--from-desk", "--now", "2026-10-20 10:02", "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["logged"], ["2026-10-20|TSLA|orb15|r=1|10:00 AM"])
        self.assertEqual((doc["calls_before"], doc["calls_after"]), (2, 3))


class Grade(Temp):
    def test_grade_waits_for_the_session_to_close(self):
        self.no_network()
        self.log(DESK_A, "2026-10-19 10:17")
        code, text, err = self.grade("2026-10-19 12:00")
        self.assertEqual(code, 0, err)
        self.assertIn("Graded 0 call(s), 3 pending", text)
        self.assertIn("the 2026-10-19 session closes at 4:00 PM ET; it is 2026-10-19 12:00", text)
        self.assertEqual(self.read_lines("outcomes.jsonl"), [])
        code, text, _ = self.grade("2026-10-19 15:59")
        self.assertIn("3 pending", text)

    def test_grade_plays_both_fills_from_the_store(self):
        self.no_network()
        self.log(DESK_A, "2026-10-19 10:17")
        code, text, err = self.grade("2026-10-19 16:30")
        self.assertEqual(code, 0, err)
        self.assertIn("Graded 3 call(s), 0 pending", text)
        self.assertIn("Appended to trader/scorecard/outcomes.jsonl", text)
        self.assertIn("next-open fill 364.21: close at 3:55 PM, 367.23, +0.49R (plan fill 364.18: +0.50R)", text)
        self.assertIn("[store]", text)
        self.assertNotIn("LATE", text)
        outs = self.read_lines("outcomes.jsonl")
        self.assertEqual(len(outs), 3)
        for o in outs:
            want = EXPECT[o["id"]]
            self.assertEqual(o["fill"], "next_open")
            self.assertEqual((o["outcome"], o["exit_slot"], o["R_net"]), want[:3], o["id"])
            self.assertAlmostEqual(o["exit_price"], want[4], places=2)
            self.assertIs(o["late"], False)
            self.assertEqual(o["graded_at"], "2026-10-19T20:30:00Z")
            no, cl = o["fills"]["next_open"], o["fills"]["close"]
            self.assertEqual((no["fill"], cl["fill"]), ("next_open", "close"))
            for k in ("outcome", "R_net", "exit_slot", "exit_price", "entry_fill", "R_gross", "mae_R", "mfe_R", "bars_held"):
                self.assertEqual(o[k], no[k], k)
            self.assertAlmostEqual(no["entry_fill"], want[3], places=3)
            self.assertEqual(cl["R_net"], want[5])
            self.assertEqual((o["until_slot"], o["cost_bps"], o["source"]), (76, 4.0, "store"))
        self.assertEqual(outs[0]["exit_time"], "3:55 PM")
        self.assertEqual(outs[2]["exit_time"], "11:40 AM")
        # grading again appends nothing
        code, text, _ = self.grade("2026-10-19 16:31")
        self.assertIn("Graded 0 call(s), 0 pending, 3 already graded", text)
        self.assertEqual(len(self.read_lines("outcomes.jsonl")), 3)
        # the same numbers as dt.sim.play on the fixture session
        b = bars.bars_of(bars.load_store("TSLA", "bars5m", CACHE)["sessions"]["2026-10-19"])
        both = scorecard.sim.play_both(b, TSLA["signal"], until=76)
        self.assertEqual(outs[0]["fills"], both)

    def test_costs_and_flat_by_from_the_flags_and_the_config(self):
        self.no_network()
        self.log(DESK_A, "2026-10-19 10:17")
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:30"})
        code, text, _ = self.grade("2026-10-19 16:30", "--cost-bps", "0", "--json")
        doc = json.loads(text)
        tsla = [o for o in doc["graded"] if o["id"] == TSLA_ID][0]
        self.assertEqual((tsla["until_slot"], tsla["cost_bps"]), (71, 0.0))
        self.assertEqual(tsla["R_net"], tsla["R_gross"])
        self.assertEqual(tsla["exit_slot"], 71)
        self.assertEqual(doc["requests"], 0)
        code, _, err = self.grade("2026-10-19 16:30", "--cost-bps", "-1")
        self.assertEqual(code, 1)
        self.assertIn("--cost-bps", err)

    def test_a_late_call_is_graded_marked_and_kept(self):
        self.no_network()
        self.log(DESK_A, "2026-10-19 10:17")
        self.log(DESK_LATE, "2026-10-19 10:46", "late.json")
        code, text, _ = self.grade("2026-10-19 16:30")
        self.assertIn("Graded 4 call(s) (1 late)", text)
        self.assertIn("LATE: logged 36 min after the entry bar closed, out of the headline", text)
        outs = {o["id"]: o for o in self.read_lines("outcomes.jsonl")}
        late = outs["2026-10-19|AMZN|orb15|r=1|10:10 AM"]
        self.assertIs(late["late"], True)
        self.assertEqual(late["late_min"], 36.0)
        self.assertEqual((late["outcome"], late["R_net"]), ("target", 0.9461))
        self.assertIs(outs[TSLA_ID]["late"], False)

    def test_grade_one_date_only(self):
        self.no_network()
        self.log_everything()
        code, text, _ = self.grade("2026-10-20 16:30", "--date", "2026-10-16")
        self.assertIn("Graded 1 call(s), 0 pending, 5 already graded or not on 2026-10-16", text)
        outs = self.read_lines("outcomes.jsonl")
        self.assertEqual([o["id"] for o in outs], ["2026-10-16|NVDA|orb15|r=1|10:20 AM"])
        self.assertEqual((outs[0]["outcome"], outs[0]["R_net"]), ("target", 1.0032))
        code, text, _ = self.grade("2026-10-19 20:00")
        self.assertIn("Graded 4 call(s) (1 late), 1 pending, 1 already graded", text)
        self.assertIn("pending 2026-10-20|TSLA|orb15|r=1|10:00 AM: the 2026-10-20 session closes", text)
        code, _, err = self.grade("2026-10-20 16:30", "--date", "soon")
        self.assertEqual(code, 1)
        self.assertIn("--date wants a date", err)

    def test_grade_fetches_five_days_when_the_store_lacks_the_date(self):
        calls = []

        def fake_chart(sym, rng="60d", interval="5m"):
            calls.append((sym, rng, interval))
            return fixture_series("TSLA", "2026-10-19", as_symbol=sym)
        net.chart = fake_chart
        c = copy.deepcopy(TSLA)
        c["symbol"] = "ZZZZ"
        self.log(board("2026-10-19 10:15 ET", [c, copy.deepcopy(c)]), "2026-10-19 10:17")
        code, text, err = self.grade("2026-10-19 16:30", "--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(calls, [("ZZZZ", "5d", "5m")])                 # one request a symbol
        self.assertEqual(len(doc["graded"]), 1)
        o = doc["graded"][0]
        self.assertEqual((o["source"], o["outcome"], o["R_net"]), ("yahoo", "close", 0.4896))
        # the date is not in the answer: pending with the reason, no outcome written
        net.chart = lambda sym, rng="60d", interval="5m": fixture_series("TSLA", "2026-10-20", as_symbol=sym)
        c2 = copy.deepcopy(c)
        c2["symbol"] = "YYYY"
        self.log(board("2026-10-19 10:15 ET", [c2]), "2026-10-19 10:17", "y.json")
        code, text, _ = self.grade("2026-10-19 16:30")
        self.assertIn("pending 2026-10-19|YYYY|orb15|r=1|10:15 AM: no bars for YYYY on 2026-10-19 in Yahoo's 5-day window", text)
        self.assertEqual(len(self.read_lines("outcomes.jsonl")), 1)

        def broken(sym, rng="60d", interval="5m"):
            raise net.NetError("Yahoo answered HTTP 429 for /v8/finance/chart/%s" % sym)
        net.chart = broken
        code, text, _ = self.grade("2026-10-19 16:30")
        self.assertEqual(code, 0)
        self.assertIn("pending 2026-10-19|YYYY|orb15|r=1|10:15 AM: Yahoo: Yahoo answered HTTP 429", text)

    def test_grade_reads_a_saved_chart_under_offline(self):
        self.no_network()
        os.mkdir("charts")
        self.write(os.path.join("charts", "ZZZZ.json"), chart_doc(fixture_series("TSLA", "2026-10-19", "ZZZZ")))
        c = copy.deepcopy(TSLA)
        c["symbol"] = "ZZZZ"
        self.log(board("2026-10-19 10:15 ET", [c]), "2026-10-19 10:17")
        code, text, err = self.grade("2026-10-19 16:30", "--offline", "charts")
        self.assertEqual(code, 0, err)
        self.assertIn("[offline]", text)
        outs = self.read_lines("outcomes.jsonl")
        self.assertEqual((outs[0]["source"], outs[0]["R_net"]), ("offline", 0.4896))
        c["symbol"] = "WWWW"
        self.log(board("2026-10-19 10:15 ET", [c]), "2026-10-19 10:17", "w.json")
        code, text, _ = self.grade("2026-10-19 16:30", "--offline", "charts")
        self.assertIn("pending 2026-10-19|WWWW|orb15|r=1|10:15 AM: no saved chart for WWWW in charts", text)
        # a partial session (the data stops before the flat-by bar) is not graded
        short = fixture_series("TSLA", "2026-10-19", "WWWW")
        for k in ("t", "o", "h", "l", "c", "v"):
            short[k] = short[k][:40]
        self.write(os.path.join("charts", "WWWW.json"), chart_doc(short))
        code, text, _ = self.grade("2026-10-19 16:30", "--offline", "charts")
        self.assertIn("bars for WWWW on 2026-10-19 stop at slot 39; the flat-by slot 76 has not closed", text)
        self.assertEqual(len(self.read_lines("outcomes.jsonl")), 1)

    def test_grade_with_nothing_logged(self):
        code, text, _ = self.grade("2026-10-19 16:30")
        self.assertEqual(code, 0)
        self.assertIn("No calls in trader/scorecard/calls.jsonl yet", text)

    def test_a_holiday_or_a_bad_signal_stays_pending_with_the_reason(self):
        self.no_network()
        self.log(board("2026-11-26 10:15 ET", [copy.deepcopy(TSLA)]), "2026-11-26 10:17")   # Thanksgiving
        code, text, _ = self.grade("2026-11-27 16:30")
        self.assertIn("2026-11-26 is not a trading day (NYSE holiday: Thanksgiving Day)", text)
        os.makedirs(os.path.join("trader", "scorecard"), exist_ok=True)
        with open(os.path.join("trader", "scorecard", "calls.jsonl"), "a") as fh:
            fh.write(json.dumps({"id": "2026-10-19|TSLA|x|3:55 PM", "logged_at": "2026-10-19T14:00:00Z",
                                 "date": "2026-10-19", "symbol": "TSLA", "config_id": "x",
                                 "signal": {"k": 76, "entry": 364.18, "stop": 358.32, "target": 370.04}}) + "\n")
        code, text, _ = self.grade("2026-10-19 16:30")
        self.assertIn("the entry bar 76 (3:55 PM) leaves no bar before the flat-by slot 76", text)
        self.assertEqual(self.read_lines("outcomes.jsonl"), [])


class Report(Temp):
    def setUp(self):
        Temp.setUp(self)
        self.no_network()
        self.log_everything()
        code, _, err = self.grade("2026-10-20 16:30")
        self.assertEqual(code, 0, err)

    def headline_trades(self, fill="next_open"):
        outs = {o["id"]: o for o in self.read_lines("outcomes.jsonl")}
        calls = {c["id"]: c for c in self.read_lines("calls.jsonl")}
        rows = []
        for cid, o in outs.items():
            if o["late"]:
                continue
            f = o["fills"][fill]
            rows.append({"outcome": f["outcome"], "R_net": f["R_net"], "symbol": o["symbol"], "date": o["date"],
                         "setup": calls[cid]["setup"], "lab": calls[cid]["lab_oos_exp_R"],
                         "key": (o["date"], calls[cid]["signal"]["k"], o["symbol"])})
        rows.sort(key=lambda r: r["key"])
        return rows

    def test_the_headline_equals_stats_block_on_the_non_late_outcomes(self):
        code, text, err = run(["report", "--json", "--now", "2026-10-20 16:40"])
        self.assertEqual(code, 0, err)
        rep = json.loads(text)
        self.assertEqual((rep["calls"], rep["graded"], rep["pending"], rep["late"], rep["headline_n"]), (6, 6, 0, 1, 5))
        trades = self.headline_trades()
        self.assertEqual(len(trades), 5)
        self.assertEqual(rep["stats"], stats.stats_block(trades))
        self.assertEqual(rep["stats"]["n"], 5)
        self.assertEqual((rep["stats"]["target"], rep["stats"]["stop"], rep["stats"]["close"]), (2, 1, 2))
        self.assertEqual(rep["stats"]["win_rate"], 0.4)
        lo, hi = stats.wilson(2, 5)
        self.assertEqual(rep["stats"]["win_ci95"], [round(lo, 4), round(hi, 4)])
        self.assertEqual(rep["stats_plan_fill"], stats.stats_block(self.headline_trades("close")))
        self.assertEqual(rep["stats_late"]["n"], 1)
        self.assertEqual(rep["stats_late"]["exp_R"], 0.9461)
        self.assertTrue(rep["small_sample"])
        self.assertEqual(rep["fill"], "next_open")
        # date, then the entry bar, then the symbol
        self.assertEqual([t["id"] for t in rep["trades"]], ["2026-10-16|NVDA|orb15|r=1|10:20 AM",
                                                            "2026-10-19|JPM|vwap_reclaim|r=1|10:15 AM",
                                                            "2026-10-19|MSFT|orb15|r=1|10:15 AM", TSLA_ID,
                                                            "2026-10-20|TSLA|orb15|r=1|10:00 AM"])

    def test_by_setup_by_week_and_the_equity_curves(self):
        code, text, _ = run(["report", "--json", "--now", "2026-10-20 16:40"])
        rep = json.loads(text)
        trades = self.headline_trades()
        self.assertEqual(sorted(rep["by_setup"]), ["orb15", "vwap_reclaim"])
        for name, st in rep["by_setup"].items():
            self.assertEqual(st, stats.stats_block([t for t in trades if t["setup"] == name]), name)
        self.assertEqual([w["week"] for w in rep["by_week"]], ["2026-10-12", "2026-10-19"])
        self.assertEqual([w["n"] for w in rep["by_week"]], [1, 4])
        self.assertEqual(rep["by_week"][0]["R"], 1.0032)
        self.assertEqual(rep["by_week"][1]["R"], round(sum(t["R_net"] for t in trades[1:]), 4))
        self.assertEqual(rep["by_week"][1]["cum_R"], round(sum(t["R_net"] for t in trades), 4))
        self.assertEqual(rep["by_week"][1]["win_rate"], 0.25)
        cum, want = 0.0, []
        for t in trades:
            cum += t["R_net"]
            want.append(round(cum, 4))
        self.assertEqual(rep["equity_R"], want)
        self.assertEqual(rep["equity_usd"], [round(x * 50, 2) for x in want])
        self.assertEqual(rep["r_usd"], 50.0)
        code, text, _ = run(["report", "--json", "--r-usd", "25", "--now", "2026-10-20 16:40"])
        self.assertEqual(json.loads(text)["equity_usd"], [round(x * 25, 2) for x in want])
        self.write(os.path.join("trader", "config.json"), {"max_loss_per_trade_usd": 100})
        code, text, _ = run(["report", "--json", "--now", "2026-10-20 16:40"])
        rep = json.loads(text)
        self.assertEqual(rep["r_usd"], 100.0)
        self.assertEqual(rep["equity_usd"][-1], round(want[-1] * 100, 2))

    def test_the_gap_between_the_lab_and_the_record(self):
        code, text, _ = run(["report", "--json", "--now", "2026-10-20 16:40"])
        rep = json.loads(text)
        trades = self.headline_trades()
        lab = sum(t["lab"] for t in trades) / len(trades)
        self.assertEqual(rep["lab_expectation_R"], round(lab, 4))
        self.assertEqual(rep["lab_expectation_n"], 5)
        self.assertEqual(rep["realised_R"], rep["stats"]["exp_R"])
        self.assertEqual(rep["gap_R"], round(rep["stats"]["exp_R"] - lab, 4))

    def test_the_text_report(self):
        code, text, err = run(["report", "--now", "2026-10-20 16:40"])
        self.assertEqual(code, 0, err)
        for must in ("6 call(s), 6 graded, 0 pending, 1 late", "Headline: 5 call(s), next-open fill", "Win rate       40.0 %",
                     "95 % CI", "Expectancy", "bootstrap CI", "seed 19", "Profit factor", "Plan fill", "Equity",
                     "max_loss_per_trade_usd", "simulated fills", "Lab said", "gap", "With 5 graded call(s) the record "
                     "says almost nothing yet", "30 is the least worth reading", "By setup:", "orb15", "vwap_reclaim",
                     "By week (Monday):", "2026-10-12", "2026-10-19", "Late (1, graded but out of the headline",
                     "2026-10-19|AMZN|orb15|r=1|10:10 AM: target +0.95R", "most people who day trade lose money"):
            self.assertIn(must, text, must)
        self.assertNotIn("Pending (", text)
        self.assertNotIn("\u2014", text)
        for never in ("projection", "will make", "guaranteed", "sure thing"):
            self.assertNotIn(never, text.lower())

    def test_since_and_an_empty_record(self):
        code, text, _ = run(["report", "--json", "--since", "2026-10-19", "--now", "2026-10-20 16:40"])
        rep = json.loads(text)
        self.assertEqual((rep["calls"], rep["graded"], rep["headline_n"], rep["since"]), (5, 5, 4, "2026-10-19"))
        self.assertEqual([w["week"] for w in rep["by_week"]], ["2026-10-19"])
        code, _, err = run(["report", "--since", "monday"])
        self.assertEqual(code, 1)
        self.assertIn("--since wants a date", err)
        code, text, _ = run(["report", "--dir", "elsewhere", "--now", "2026-10-20 16:40"])
        self.assertEqual(code, 0)
        self.assertIn("0 call(s), 0 graded, 0 pending, 0 late", text)
        self.assertIn("No graded call in the headline yet", text)
        code, text, _ = run(["report", "--dir", "elsewhere", "--json"])
        self.assertEqual(json.loads(text)["stats"], stats.stats_block([]))

    def test_publish_writes_the_contract_keys(self):
        code, text, _ = run(["report", "--publish", "--now", "2026-10-20 16:40"])
        self.assertEqual(code, 0)
        self.assertIn("Wrote trader/publish/scorecard.json.", text)
        with open(os.path.join("trader", "publish", "scorecard.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        for k in ("as_of", "calls", "graded", "late", "stats", "by_setup", "by_week", "equity_R", "equity_usd",
                  "lab_expectation_R", "note"):
            self.assertIn(k, doc, k)
        self.assertEqual(doc["as_of"], "2026-10-20T20:40:00Z")
        self.assertEqual(doc["stats"], stats.stats_block(self.headline_trades()))
        self.assertEqual(doc["created_by"], "trader-scorecard")
        self.assertIn("most people who day trade lose money", doc["note"])
        code, text, _ = run(["report", "--publish", "out/sc.json", "--json", "--now", "2026-10-20 16:40"])
        self.assertTrue(os.path.exists(os.path.join("out", "sc.json")))
        self.assertEqual(json.loads(text)["published"], "out/sc.json")


class Show(Temp):
    def test_show_a_call_before_and_after_grading(self):
        self.no_network()
        self.log(DESK_A, "2026-10-19 10:17")
        code, text, err = run(["show", TSLA_ID])
        self.assertEqual(code, 0, err)
        self.assertIn("Call " + TSLA_ID, text)
        self.assertIn("entry 364.18 at 10:15 AM (bar 8), stop 358.32 (or_mid), target 370.04 (1R), risk 5.86 a share", text)
        self.assertIn("opening range 354.47 to 362.18", text)
        self.assertIn("On time: logged 2 min after the entry bar closed", text)
        self.assertIn("No outcome yet", text)
        self.grade("2026-10-19 16:30")
        code, text, _ = run(["show", TSLA_ID])
        self.assertIn("Outcome graded 2026-10-19T20:30:00Z from the bar store, held to slot 76 (3:55 PM), costs 4 bps", text)
        self.assertIn("next-open fill 364.21: close at 3:55 PM, 367.23, +0.49R net (+0.51R gross)", text)
        self.assertIn("plan fill      364.18: close at 3:55 PM, 367.23, +0.50R net (+0.52R gross)", text)
        self.assertIn("In the headline", text)
        code, text, _ = run(["show", TSLA_ID, "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["call"]["id"], TSLA_ID)
        self.assertEqual(doc["outcome"]["fills"]["close"]["R_net"], 0.4956)

    def test_an_unknown_id_refuses_in_one_line(self):
        self.log(DESK_A, "2026-10-19 10:17")
        code, text, err = run(["show", "2026-10-19|TSLA|orb15|r=1|10:20 AM"])
        self.assertEqual(code, 1)
        self.assertEqual(text, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("no call with id '2026-10-19|TSLA|orb15|r=1|10:20 AM' in trader/scorecard/calls.jsonl (3 calls)", err)
        self.assertNotIn("Traceback", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-scorecard", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            upstream = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-scorecard\n"))
        self.assertGreater(len(text.splitlines()), 60)
        fm = text.split("---\n")[1]
        self.assertGreater(len(fm[fm.index("description:"):fm.index("argument-hint:")]), 200)
        self.assertEqual(text[text.index("## Safety"):], upstream[upstream.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("${claude_skill_dir}", "what it reads and writes", "## steps", "## rules", "what it never does",
                     "calls.jsonl", "outcomes.jsonl", "trader/publish/scorecard.json", "late", "wilson", "bootstrap",
                     "next_open", "364.18", "358.32", "370.04", "+0.49r", "most people who day trade lose money",
                     "not a prediction", "not financial advice"):
            self.assertIn(must, low, must)
        for never in ("\u2014", "a buy", "real " + "time", "tick " + "by tick", "guarantee" + "d", "projection"):
            self.assertNotIn(never, low, never)

    def test_python_3_8_and_no_em_dashes_in_the_tool(self):
        folder = os.path.join(ROOT, "trader-scorecard")
        for f in os.listdir(folder):
            if not f.endswith((".py", ".md")):
                continue
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("\u2014", src, f)
            if f.endswith(".py"):
                for bad in ("import " + "zoneinfo", "from " + "zoneinfo", ".remove" + "prefix(", ".remove" + "suffix("):
                    self.assertNotIn(bad, src, f)
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", f)
                for bad in ("list" + "[", "dict" + "[", "tuple" + "["):
                    self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), f)


if __name__ == "__main__":
    unittest.main()
