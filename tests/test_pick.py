"""trader-pick: the spec and buckets, the features by hand, the market table, earnings, the three scorers and rules, the
walk-forward on the fixture cache, the look-ahead check, the card's claim, and the CLI offline."""

import io as _io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _dt_fixtures import daily_rows, ROOT, CACHE
from _load import load

sys.path.insert(0, ROOT)
from dt import pick, io as dtio  # noqa: E402

cli = load("trader-pick", "pick")
SYMS = sorted(f.split(".")[0] for f in os.listdir(os.path.join(CACHE, "daily"))
              if not f.startswith("_") and not f.startswith("^") and f.split(".")[0] not in pick.CONTEXT)


def run(argv):
    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def records(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


class Spec(unittest.TestCase):
    def test_codes_labels_and_the_extra_module(self):
        s = pick.base_spec()
        self.assertEqual(pick.code_of(s, "atr_pct", 1.0), 0)
        self.assertEqual(pick.code_of(s, "atr_pct", 1.5), 1)
        self.assertEqual(pick.code_of(s, "atr_pct", 9.0), 3)
        self.assertEqual(pick.code_of(s, "atr_pct", None), 4)
        self.assertEqual(pick.code_of(s, "atr_pct", float("nan")), 4)
        self.assertEqual(pick.label_of(s, "atr_pct", 4), "na")
        self.assertEqual(pick.nbuckets(s, "atr_pct"), 5)
        self.assertEqual(pick.bucket("rsi_14", 70.0), "b5")
        self.assertEqual(pick.bucket("spy_trend", "up"), "up")
        self.assertEqual(pick.bucket("spy_trend", "sideways"), "na")
        self.assertEqual(pick.features_of(s), pick.FEATURES)
        codes = pick.encode(s, {"atr_pct": 3.0, "ret_1d": 0.5}, {"spy_trend": "down", "dow": "fri", "vix": 22.0})
        lab = pick.labels(s, codes)
        self.assertEqual((lab["atr_pct"], lab["ret_1d"], lab["spy_trend"], lab["dow"], lab["vix"], lab["rsi_14"]), ("b2", "b3", "down", "fri", "b2", "na"))
        d = tempfile.mkdtemp()
        mod = os.path.join(d, "x.py")
        write(mod, "CUTS = {'my_feat': (0.0, 1.0)}\nCATEGORICAL = {'my_cat': ('a', 'b')}\ndef compute(sym, rows, ctx):\n    return {}\n")
        s2 = pick.base_spec(mod)
        self.assertEqual(pick.features_of(s2)[-1], "my_cat")
        self.assertIn("my_feat", s2["cuts"])
        self.assertEqual(s2["extra"], os.path.abspath(mod))
        bad = os.path.join(d, "bad.py")
        write(bad, "CUTS = {}\n")
        with self.assertRaises(pick.PickError):
            pick.base_spec(bad)
        shutil.rmtree(d)


class Features(unittest.TestCase):
    def test_symbol_table_by_hand(self):
        rows = daily_rows(100.0, 40, 1.0, 2.0)                    # +1 a day, a 2-wide range
        rows[-1][1], rows[-1][2], rows[-1][3], rows[-1][4] = 100.0, 104.0, 99.0, 103.0   # opens 100, closes 103: an UP session
        table = pick.symbol_table("T", rows)
        self.assertEqual(len(table), 40 - pick.MIN_HISTORY)
        date, raw, oc, cc, hi, lo, adv = table[-1]
        self.assertEqual(date, rows[-1][0])
        self.assertAlmostEqual(oc, 3.0, 6)
        self.assertAlmostEqual(hi, 4.0, 6)
        self.assertAlmostEqual(lo, -1.0, 6)
        j = len(rows) - 2
        self.assertAlmostEqual(cc, (103.0 / rows[j][4] - 1) * 100, 6)
        self.assertAlmostEqual(raw["ret_1d"], (rows[j][4] / rows[j - 1][4] - 1) * 100, 6)
        self.assertAlmostEqual(raw["ret_5d"], (rows[j][4] / rows[j - 5][4] - 1) * 100, 6)
        self.assertAlmostEqual(raw["atr_pct"], 2.0 / rows[j][4] * 100, 6)
        self.assertAlmostEqual(raw["relvol_1d"], 1.0, 6)
        self.assertAlmostEqual(raw["close_pos_1d"], 0.5, 6)
        self.assertEqual(raw["rsi_14"], 100.0)
        self.assertEqual((raw["sym_hit_60"], raw["sym_dn_60"]), (0.0, 0.0))        # the UP is the session scored, not before it
        self.assertAlmostEqual(adv, sum(r[4] * r[5] for r in rows[j - 19:j + 1]) / 20.0, 3)
        self.assertEqual(pick.symbol_table("T", rows[:10]), [])
        raw2, adv2 = pick.today_raw("T", rows[:-1], rows[-1][0])
        self.assertEqual({k: raw2[k] for k in ("ret_1d", "atr_pct", "rsi_14")}, {k: raw[k] for k in ("ret_1d", "atr_pct", "rsi_14")})
        self.assertAlmostEqual(adv2, adv, 3)
        self.assertEqual(pick.today_features(rows[:5], rows[-1][0]), None)

    def test_the_extra_module_merges_by_date(self):
        rows = daily_rows(100.0, 40, 1.0, 2.0)
        extra = lambda sym, rs, ctx: {r[0]: {"my_feat": float(k)} for k, r in enumerate(rs)}
        table = pick.symbol_table("T", rows, extra, {})
        self.assertEqual(table[-1][1]["my_feat"], 39.0)

    def test_market_table_and_today(self):
        spy = daily_rows(400.0, 80, 1.0, 3.0)
        vix = daily_rows(18.0, 80, 0.0, 1.0)
        tags = {"sessions": {spy[-1][0]: {"nq_gap_pct": 0.8}}}
        m = pick.market_table(spy, vix, tags)
        d = spy[-1][0]
        self.assertEqual(m[d]["spy_trend"], "up")
        self.assertAlmostEqual(m[d]["spy_ret_1d"], (spy[-2][4] / spy[-3][4] - 1) * 100, 6)
        self.assertEqual((m[d]["vix"], m[d]["nq_gap"]), (18.0, 0.8))
        self.assertNotIn(spy[10][0], m)
        today = pick.market_for_today(spy, "2027-01-04", vix, -0.4)
        self.assertEqual((today["spy_trend"], today["nq_gap"], today["dow"]), ("up", -0.4, "mon"))
        self.assertIsNone(pick.market_for_today(spy[:20], "2027-01-04"))


class Earnings(unittest.TestCase):
    def test_the_calendar_drops_the_report_and_the_reaction(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "earnings"))
        os.symlink(os.path.join(CACHE, "daily"), os.path.join(d, "daily"))
        # no time given (most of Nasdaq's calendar): the move may land on the next session, so both are left out
        write(os.path.join(d, "earnings", "2026-10-22.json"), json.dumps({"date": "2026-10-22", "rows": [{"symbol": "TSLA", "surprise": "5"}]}))
        e = pick.load_earnings(d)
        self.assertEqual(list(e), ["TSLA"])
        raws = pick.raw_rows(d, ["TSLA", "SPY"])
        self.assertTrue(all(r[1] == "TSLA" for r in raws))                   # SPY is context, never a candidate
        self.assertEqual([(r[0], r[8]) for r in raws if r[8]], [("2026-10-22", "report"), ("2026-10-23", "reaction")])
        from dt import bars
        market = pick.market_table(bars.daily_rows("SPY", d), bars.daily_rows("^VIX", d))
        info = {}
        rows, dates, dropped = pick.encode_rows(raws, market, pick.base_spec(), info=info)
        special = [r[0] for r in raws if r[0] in market and r[3] is not None and not r[8] and pick.special_of(r[2])]
        self.assertEqual((dropped, info["reaction_dropped"], info["special_dropped"]), (2, 1, len(special)))   # special: left out by default
        self.assertFalse(set(["2026-10-22", "2026-10-23"] + special) & set(r[pick.DATE] for r in rows))
        rows3, _, _ = pick.encode_rows(raws, market, pick.base_spec(), exclude_special=False)
        self.assertEqual(len(rows3), len(rows) + len(special))
        rows2, _, dropped2 = pick.encode_rows(raws, market, pick.base_spec(), exclude_earnings=False)
        self.assertEqual((dropped2, len(rows2)), (0, len(rows) + 2))
        # a report marked before the open moves the stock on its own session only
        write(os.path.join(d, "earnings", "2026-10-22.json"), json.dumps({"date": "2026-10-22", "rows": [{"symbol": "TSLA", "time": "time-pre-market"}]}))
        raws = pick.raw_rows(d, ["TSLA"])
        self.assertEqual([(r[0], r[8]) for r in raws if r[8]], [("2026-10-22", "report")])
        shutil.rmtree(d)

    def test_earnings_session_by_hand(self):
        rep = {"2026-10-22": {"time": "time-after-hours"}, "2026-10-26": {"time": "time-pre-market"}, "2026-10-28": {"time": "time-not-supplied"}}
        self.assertEqual(pick.earnings_session(rep, "2026-10-22", "2026-10-21"), "report")
        self.assertEqual(pick.earnings_session(rep, "2026-10-23", "2026-10-22"), "reaction")     # after the close: the next session
        self.assertEqual(pick.earnings_session(rep, "2026-10-26", "2026-10-23"), "report")
        self.assertIsNone(pick.earnings_session(rep, "2026-10-27", "2026-10-26"))              # before the open: that session only
        self.assertEqual(pick.earnings_session(rep, "2026-10-29", "2026-10-28"), "reaction")     # no time: the safe reading
        self.assertIsNone(pick.earnings_session(rep, "2026-10-21", "2026-10-20"))
        self.assertIsNone(pick.earnings_session({}, "2026-10-21", None))
        self.assertIsNone(pick.earnings_session(None, "2026-10-21", "2026-10-20"))


class Special(unittest.TestCase):
    def test_special_of(self):
        self.assertIsNone(pick.special_of({"ret_1d": 19.99}))
        self.assertEqual(pick.special_of({"ret_1d": -48.4}), "moved -48.4 % the session before")
        self.assertEqual(pick.special_of({"ret_1d": 20.0}), "moved +20.0 % the session before")
        self.assertEqual(pick.special_of({"ret_1d": 6.0}, 5.0), "moved +6.0 % the session before")
        for raw in ({"ret_1d": None}, {"ret_1d": float("nan")}, {}, None):
            self.assertIsNone(pick.special_of(raw))

    def test_special_situations_leave_training_and_scoring(self):
        market = {"2026-10-22": {"spy_trend": "flat", "dow": "thu"}}
        raws = [("2026-10-22", "A", {"ret_1d": -48.4, "atr_pct": 15.9}, 1.0, -47.0, 2.0, -1.0, 5e7, None),
                ("2026-10-22", "B", {"ret_1d": 1.0, "atr_pct": 2.0}, 0.5, 0.6, 1.0, -0.5, 5e7, None),
                ("2026-10-22", "C", {"ret_1d": 25.0}, 0.1, 0.1, 0.5, -0.5, 5e7, "report")]
        info = {}
        rows, dates, dropped = pick.encode_rows(raws, market, pick.base_spec(), True, True, info)
        self.assertEqual([r[pick.SYM] for r in rows], ["B"])
        self.assertEqual((dropped, info["special_dropped"], info["reaction_dropped"]), (1, 1, 0))   # C counts once, as earnings
        rows, _, _ = pick.encode_rows(raws, market, pick.base_spec(), True, False)
        self.assertEqual([r[pick.SYM] for r in rows], ["A", "B"])


def synth_rows(n_days=200, names=30):
    """Encoded rows where a hot ATR bucket closes UP often and a cold one closes DOWN often (the rest are quiet)."""
    s = pick.base_spec()
    out = []
    for i in range(n_days):
        d = "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)
        for k in range(names):
            hot, cold = k % 6 == 0, k % 6 == 1
            raw = {"atr_pct": 5.0 if hot else 0.5 if cold else 2.0}
            codes = pick.encode(s, raw, {"spy_trend": "flat", "dow": "mon"})
            up = 1 if hot and (i + k) % 3 == 0 else 0
            dn = 1 if cold and (i + k) % 3 == 0 else 0
            oc = 2.5 if up else -2.5 if dn else 0.1
            out.append((d, "S%02d" % k, codes, up, dn, oc, oc, max(oc, 0.0) + 0.5, min(oc, 0.0) - 0.5, 5e7, raw["atr_pct"]))
    return s, out


class Scorers(unittest.TestCase):
    def test_fit_lifts_calibration_and_the_three_rules(self):
        s, rows = synth_rows()
        m = pick.train(rows, None, s)
        self.assertEqual(m["n"], len(rows))
        self.assertAlmostEqual(m["base_up"], sum(r[pick.UP] for r in rows) / float(len(rows)), 6)
        fi = m["features"].index("atr_pct")
        self.assertGreater(m["lift_up"][fi][3], m["lift_up"][fi][1])       # the hot bucket lifts the up odds
        self.assertGreater(m["lift_dn"][fi][0], m["lift_dn"][fi][1])       # the cold bucket lifts the down odds
        self.assertGreater(m["dev_oc"][fi][3], m["dev_oc"][fi][0])
        self.assertEqual(len(m["cal_up"]), pick.CAL_BINS)
        hot = next(r for r in rows if r[pick.ATR] == 5.0)
        cold = next(r for r in rows if r[pick.ATR] == 0.5)
        su, sd, e = pick.score_codes(m, hot[pick.CODES])
        su2, sd2, e2 = pick.score_codes(m, cold[pick.CODES])
        self.assertGreater(su, su2)
        self.assertGreater(sd2, sd)
        self.assertGreater(e, e2)
        self.assertGreaterEqual(pick.probability(m["cal_up"], su), pick.probability(m["cal_up"], su2))
        self.assertIsNone(pick.probability([], 1.0))
        cands = [("HOT", hot[pick.CODES], 5e7, None), ("COLD", cold[pick.CODES], 5e7, None), ("THIN", hot[pick.CODES], 1e6, None),
                 ("SPY", hot[pick.CODES], 1e9, None)]
        for rule in pick.RULES:
            p, ranked = pick.rank(m, cands, rule, floor_x=0.0)
            self.assertEqual([r["symbol"] for r in ranked][0], "HOT", rule)
            self.assertEqual(set(r["symbol"] for r in ranked), {"HOT", "COLD"})
        p, ranked = pick.rank(m, cands, "up", exclude={"HOT"}, floor_x=0.0)
        self.assertEqual(p["symbol"], "COLD")
        p, _ = pick.rank(m, cands, "up", floor_x=99.0)
        self.assertIsNone(p)
        self.assertIn("base rate", pick.floor_words("up", m))
        self.assertIn("points", pick.floor_words("net", m))
        with self.assertRaises(pick.PickError):
            pick.rule_key("guess")
        with self.assertRaises(pick.PickError):
            pick.fit(pick.new_counts(s), [], s)


class WalkForward(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.dates, cls.market, cls.info = pick.build_rows(CACHE, SYMS, workers=1)
        cls.res = pick.walk_forward(cls.rows, cls.dates, min_train=120, refit_every=20)

    def test_the_fixture_rows(self):
        self.assertEqual(len(self.dates), 450)
        self.assertGreater(len(self.rows), 7000)
        for r in self.rows[:50]:
            self.assertEqual(len(r[pick.CODES]), len(pick.FEATURES))
            self.assertEqual(r[pick.UP], 1 if r[pick.OC] >= pick.TARGET_PCT else 0)
            self.assertEqual(r[pick.DN], 1 if r[pick.OC] <= -pick.TARGET_PCT else 0)
            self.assertNotIn(r[pick.SYM], pick.CONTEXT)

    def test_every_pick_was_ranked_before_it_was_known(self):
        res = self.res
        self.assertEqual((res["oos_sessions"], res["refits"], res["oos_first"]), (330, 17, self.dates[120]))
        self.assertEqual(res["h2_first"], self.dates[120 + 165])
        for rule in pick.RULES:
            r = res["rules"][rule]
            for seg in ("all", "h1", "h2"):
                self.assertIn(seg, r["pick"])
            p = r["pick"]["all"]
            self.assertEqual(p["n"] + r["no_pick_days"], 330)
            self.assertTrue(p["up_ci"][0] <= p["up_rate"] <= p["up_ci"][1])
            self.assertEqual(r["pick"]["h1"]["n"] + r["pick"]["h2"]["n"], p["n"])
            self.assertEqual(r["top5"]["all"]["n"], 330 * 5)
            for x in r["recent"]:
                self.assertGreaterEqual(x["date"], res["oos_first"])
        self.assertTrue(res["base"]["all"]["n"] > 1000)
        self.assertEqual(res["baseline_highest_atr"]["all"]["n"], 330)
        w = pick.words(res, "up")
        self.assertIn("base rates", w)
        self.assertIn("below", w)
        self.assertIn("no out-of-sample picks", pick.words({}, "up"))


class LeakCheck(unittest.TestCase):
    def test_a_module_that_reads_the_session_is_caught(self):
        d = tempfile.mkdtemp()
        clean = os.path.join(d, "clean.py")
        leaky = os.path.join(d, "leaky.py")
        write(clean, "CUTS = {'prev_close': (100.0,)}\ndef compute(sym, rows, ctx):\n    return {rows[i][0]: {'prev_close': rows[i - 1][4]} for i in range(1, len(rows))}\n")
        write(leaky, "CUTS = {'same_close': (100.0,)}\ndef compute(sym, rows, ctx):\n    return {r[0]: {'same_close': r[4]} for r in rows}\n")
        self.assertFalse(pick.leak_check(clean, CACHE, SYMS, samples=10)["leak"])
        res = pick.leak_check(leaky, CACHE, SYMS, samples=10)
        self.assertTrue(res["leak"])
        self.assertEqual(res["mismatches"][0]["feature"], "same_close")
        shutil.rmtree(d)


class Claim(unittest.TestCase):
    def wf(self, up1, dn1, up2, dn2, n=165):
        def half(u, d):
            from dt import stats
            return {"n": n, "up_rate": u, "dn_rate": d, "up_ci": list(stats.wilson(int(u * n), n)), "dn_ci": list(stats.wilson(int(d * n), n))}
        return {"rules": {"up": {"pick": {"h1": half(up1, dn1), "h2": half(up2, dn2), "all": half((up1 + up2) / 2, (dn1 + dn2) / 2)}}}}

    def test_up_only_when_both_halves_separate(self):
        self.assertEqual(cli.claim_of(self.wf(0.40, 0.10, 0.38, 0.12), "up"), "up")
        self.assertEqual(cli.claim_of(self.wf(0.40, 0.10, 0.20, 0.22), "up"), "move")
        self.assertEqual(cli.claim_of(self.wf(0.30, 0.36, 0.30, 0.36), "up"), "move")
        self.assertEqual(cli.claim_of({}, "up"), "move")
        self.assertIn("no measured edge", cli.direction_words(self.wf(0.30, 0.33, 0.30, 0.33), "up"))
        self.assertIn("MORE often", cli.direction_words(self.wf(0.10, 0.40, 0.10, 0.40), "up"))
        self.assertIn("measured edge in both halves", cli.direction_words(self.wf(0.40, 0.10, 0.38, 0.12), "up"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs(os.path.join("trader", "cache", "earnings"))
        for sub in ("daily", "bars5m"):
            os.symlink(os.path.join(CACHE, sub), os.path.join("trader", "cache", sub))
        write(os.path.join("trader", "largecap.json"), json.dumps({"symbols": [{"symbol": s} for s in SYMS]}))

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d, ignore_errors=True)

    def test_train_morning_grade_report(self):
        code, out, err = run(["train", "--workers", "1", "--publish"])
        self.assertEqual(code, 0, err)
        for must in ("Out of sample", "any large cap", "highest-ATR", "The card's claim"):
            self.assertIn(must, out, must)
        model = dtio.read_json(os.path.join("trader", "pick", "model.json"))
        self.assertEqual((model["symbols"], model["rule"], model["walk_forward"]["oos_sessions"]), (len(SYMS), "up", 330))
        self.assertIn(model["claim"], ("up", "move"))
        self.assertTrue(os.path.exists(os.path.join("trader", "publish", "pick.json")))
        self.assertEqual(model["special_move_pct"], pick.SPECIAL_MOVE_PCT)
        self.assertIn("special situations", out)
        # who reports today comes from the Nasdaq calendar file, the backtest's own source; yesterday's file names who
        # reacts today (a report after the close or with no time given), and a report marked before the open does not
        write(os.path.join("trader", "cache", "earnings", "2026-10-23.json"), json.dumps({"date": "2026-10-23", "rows": [{"symbol": SYMS[0]}]}))
        write(os.path.join("trader", "cache", "earnings", "2026-10-22.json"), json.dumps({"date": "2026-10-22", "rows": [
            {"symbol": SYMS[1], "time": "time-not-supplied"}, {"symbol": SYMS[2], "time": "time-pre-market"}]}))
        code, out, err = run(["morning", "--date", "2026-10-23", "--at", "08:05", "--no-premarket", "--write", "--publish", "--now", "2026-10-23 08:06"])
        self.assertEqual(code, 0, err)
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        self.assertEqual((doc["date"], doc["cutoff"], doc["store_through"], doc["store_stale"], doc["rule"]), ("2026-10-23", "08:05", "2026-10-22", False, "up"))
        self.assertEqual((doc["reporting_today"], doc["reacting_today"]), (1, 1))
        self.assertIn("Nasdaq calendar", doc["reporting_source"])
        self.assertIn("2026-10-22", doc["reacting_source"])
        self.assertNotIn(SYMS[0], [r["symbol"] for r in doc["ranked"]])
        self.assertNotIn(SYMS[1], [r["symbol"] for r in doc["ranked"]])
        self.assertEqual(doc["special_move_pct"], pick.SPECIAL_MOVE_PCT)
        self.assertIn("react today", out)
        self.assertIsNone(doc["nq_gap_pct"])
        self.assertIn("2 %", doc["direction"])
        self.assertIn("The 8:25 pick for 2026-10-23", out)
        self.assertIn("Most people who day trade lose money", out)
        self.assertNotIn(chr(0x2014), out)
        for r in doc["ranked"]:
            self.assertTrue(0 <= r["p_up"] <= 1 and 0 <= r["p_dn"] <= 1)
        if doc["pick"]:
            self.assertIn("MOVE" if doc["claim"] == "move" else "ABOVE", out)
            rec = records(os.path.join("trader", "pick", "record.jsonl"))
            self.assertEqual((rec[0]["symbol"], rec[0]["status"]), (doc["pick"]["symbol"], "pending"))
            code, out, err = run(["grade", "--write"])                                      # 2026-10-23 is in the fixture store
            self.assertEqual(code, 0, err)
            rec = records(os.path.join("trader", "pick", "record.jsonl"))
            self.assertEqual(rec[0]["status"], "graded")
            self.assertIn(rec[0]["up"], (0, 1))
            self.assertIn("1 graded", out)
            # the same morning again logs nothing twice
            run(["morning", "--date", "2026-10-23", "--no-premarket", "--write", "--now", "2026-10-23 08:06"])
            self.assertEqual(len(records(os.path.join("trader", "pick", "record.jsonl"))), 1)
        else:
            self.assertIn("NO PICK", out)
        code, out, err = run(["report"])
        self.assertEqual(code, 0, err)
        self.assertIn("Biggest lifts", out)
        code, out, err = run(["morning", "--date", "2026-10-27", "--no-premarket", "--now", "2026-10-27 08:06"])
        self.assertEqual(code, 0, err)
        self.assertIn("STALE", out)
        # a special situation is left out of the morning and listed with its reason; the threshold rides in the model
        from dt import bars
        model = dtio.read_json(os.path.join("trader", "pick", "model.json"))
        moves = {}
        for s in SYMS[2:]:                                     # SYMS[0] reports today and SYMS[1] reacts: already left out
            raw, adv = pick.today_raw(s, bars.daily_rows(s, os.path.join("trader", "cache")), "2026-10-23")
            if raw and adv and adv >= model["min_adv_usd"] and raw.get("ret_1d") is not None:
                moves[s] = abs(raw["ret_1d"])
        top = max(sorted(moves), key=lambda s: moves[s])
        model["special_move_pct"] = moves[top] - 1e-9
        dtio.write_json(os.path.join("trader", "pick", "model.json"), model)
        code, out, err = run(["morning", "--date", "2026-10-23", "--no-premarket", "--json", "--now", "2026-10-23 08:06"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual([x["symbol"] for x in doc["special_today"]], [top])
        self.assertIn("the session before", doc["special_today"][0]["reason"])
        self.assertNotIn(top, [r["symbol"] for r in doc["ranked"]])
        model["special_move_pct"] = None                                                     # trained with --keep-special
        dtio.write_json(os.path.join("trader", "pick", "model.json"), model)
        code, out, err = run(["morning", "--date", "2026-10-23", "--no-premarket", "--json", "--now", "2026-10-23 08:06"])
        self.assertEqual(json.loads(out)["special_today"], [])

    def test_no_model_is_a_plain_refusal(self):
        code, out, err = run(["morning", "--date", "2026-10-23", "--no-premarket"])
        self.assertEqual(code, 1)
        self.assertIn("train first", err)


if __name__ == "__main__":
    unittest.main()
