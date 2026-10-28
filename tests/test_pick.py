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
        # the report time plays no part (Nasdaq drops it for past dates, so the history cannot use it): a report marked
        # before the open leaves out the next session too, the same rule the record was built with
        write(os.path.join(d, "earnings", "2026-10-22.json"), json.dumps({"date": "2026-10-22", "rows": [{"symbol": "TSLA", "time": "time-pre-market"}]}))
        raws = pick.raw_rows(d, ["TSLA"])
        self.assertEqual([(r[0], r[8]) for r in raws if r[8]], [("2026-10-22", "report"), ("2026-10-23", "reaction")])
        # a Saturday report: the next session (Monday 2026-10-19) is its reaction session
        os.remove(os.path.join(d, "earnings", "2026-10-22.json"))
        write(os.path.join(d, "earnings", "2026-10-17.json"), json.dumps({"date": "2026-10-17", "rows": [{"symbol": "TSLA"}]}))
        raws = pick.raw_rows(d, ["TSLA"])
        self.assertEqual([(r[0], r[8]) for r in raws if r[8]], [("2026-10-19", "reaction")])
        shutil.rmtree(d)

    def test_earnings_session_by_hand(self):
        rep = {"2026-10-22": {"time": "time-after-hours"}, "2026-10-26": {"time": "time-pre-market"}, "2026-10-28": {"time": "time-not-supplied"}}
        self.assertEqual(pick.earnings_session(rep, "2026-10-22", "2026-10-21"), "report")
        self.assertEqual(pick.earnings_session(rep, "2026-10-23", "2026-10-22"), "reaction")     # the session after a report
        self.assertEqual(pick.earnings_session(rep, "2026-10-26", "2026-10-23"), "report")
        self.assertEqual(pick.earnings_session(rep, "2026-10-27", "2026-10-26"), "reaction")     # the time plays no part
        self.assertEqual(pick.earnings_session(rep, "2026-10-29", "2026-10-28"), "reaction")
        self.assertIsNone(pick.earnings_session(rep, "2026-10-21", "2026-10-20"))
        sat = {"2026-10-24": {}}                                                                 # a Saturday report: Monday reacts
        self.assertEqual(pick.earnings_session(sat, "2026-10-26", "2026-10-23"), "reaction")
        self.assertIsNone(pick.earnings_session(sat, "2026-10-23", "2026-10-22"))
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
        self.assertEqual(len(self.dates), 447)                     # 450 sessions less three half days (1:00 p.m. closes)
        self.assertFalse(set(self.dates) & pick.HALF_DAYS)
        self.assertEqual(self.info["half_day_dropped"] > 0, True)
        self.assertGreater(len(self.rows), 7000)
        for r in self.rows[:50]:
            self.assertEqual(len(r[pick.CODES]), len(pick.FEATURES))
            self.assertEqual(r[pick.UP], 1 if r[pick.OC] >= pick.TARGET_PCT else 0)
            self.assertEqual(r[pick.DN], 1 if r[pick.OC] <= -pick.TARGET_PCT else 0)
            self.assertNotIn(r[pick.SYM], pick.CONTEXT)

    def test_every_pick_was_ranked_before_it_was_known(self):
        res = self.res
        self.assertEqual((res["oos_sessions"], res["refits"], res["oos_first"]), (327, 17, self.dates[120]))
        self.assertEqual(res["h2_first"], self.dates[120 + 163])
        for rule in pick.RULES:
            r = res["rules"][rule]
            for seg in ("all", "h1", "h2"):
                self.assertIn(seg, r["pick"])
            p = r["pick"]["all"]
            self.assertEqual(p["n"] + r["no_pick_days"], 327)
            for k in ("up", "dn", "band", "touch_up", "touch_dn"):
                lo, hi = p[k + "_ci"]
                self.assertTrue(lo <= p[k + "_rate"] <= hi, k)
            self.assertEqual(p["ci"], "wilson")
            self.assertTrue(r["top5"]["all"]["ci"].startswith("mornings resampled"))
            self.assertTrue(p["up_ci"][0] <= p["up_rate"] <= p["up_ci"][1])
            self.assertEqual(r["pick"]["h1"]["n"] + r["pick"]["h2"]["n"], p["n"])
            self.assertEqual(r["top5"]["all"]["n"], 327 * 5)
            for x in r["recent"]:
                self.assertGreaterEqual(x["date"], res["oos_first"])
        self.assertTrue(res["base"]["all"]["n"] > 1000)
        self.assertEqual(res["baseline_highest_atr"]["all"]["n"], 327)
        self.assertTrue(res["base"]["all"]["ci"].startswith("mornings resampled"))
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
        self.assertEqual((model["symbols"], model["rule"], model["walk_forward"]["oos_sessions"]), (len(SYMS), "up", 327))
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
        self.assertEqual((doc["reporting_today"], doc["reacting_today"]), (1, 2))
        self.assertIn("Nasdaq calendar", doc["reporting_source"])
        self.assertIn("2026-10-22", doc["reacting_source"])
        self.assertNotIn(SYMS[0], [r["symbol"] for r in doc["ranked"]])
        self.assertNotIn(SYMS[1], [r["symbol"] for r in doc["ranked"]])
        self.assertNotIn(SYMS[2], [r["symbol"] for r in doc["ranked"]])
        for k in ("oos", "oos_rules", "direction_kind", "configs_tested", "next_session", "futures_words", "odds_note"):
            self.assertIn(k, doc, k)
        self.assertEqual(doc["configs_tested"], 3)
        self.assertEqual(doc["next_session"], "2026-10-26")
        self.assertNotIn("expected", out)                               # the uncalibrated move is never printed
        self.assertIn("record of past picks", out)
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
        for s in SYMS[3:]:                                     # SYMS[0] reports today, SYMS[1] and SYMS[2] react: already left out
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


class Universe(unittest.TestCase):
    def test_the_newest_list_holding_a_name_sets_its_cap(self):
        old = {"as_of": "2026-10-25T23:00:00Z", "symbols": [{"symbol": "AAA", "market_cap": 20e9, "last_sale": 10.0},
                                                         {"symbol": "GONE", "market_cap": 12e9, "last_sale": 4.0}]}
        new = {"as_of": "2026-10-27T21:00:00Z", "symbols": [{"symbol": "AAA", "market_cap": 30e9, "last_sale": 15.0},
                                                         {"symbol": "BAD", "market_cap": None, "last_sale": 2.0}]}
        caps = pick.universe_caps([new, old])
        self.assertEqual(caps["AAA"][:2], (30e9, 15.0))
        self.assertEqual(caps["GONE"][:2], (12e9, 4.0))          # a name that left keeps its history
        self.assertNotIn("BAD", caps)

    def test_a_session_counts_only_while_the_name_was_a_large_cap(self):
        from dt import bars
        rows = bars.daily_rows("TSLA", CACHE)
        last = float(rows[-1][4])
        caps = {"TSLA": (10e9, last, "2026-10-23")}            # exactly the floor at the last close
        raws = pick.raw_rows(CACHE, ["TSLA"], caps=caps)
        self.assertTrue(all("cap_est" in r[2] for r in raws))
        market = pick.market_table(bars.daily_rows("SPY", CACHE), bars.daily_rows("^VIX", CACHE))
        info = {}
        rows_pit, _, _ = pick.encode_rows(raws, market, pick.base_spec(), info=info, min_cap=10e9)
        self.assertGreater(info["small_dropped"], 0)
        kept = {r[0] for r in rows_pit}
        for d, sym, raw, *_ in raws:
            if d in kept:
                self.assertGreaterEqual(raw["cap_est"], 10e9 - 1)
        rows_all, _, _ = pick.encode_rows(raws, market, pick.base_spec())
        cap_of = {r[0]: r[2]["cap_est"] for r in raws}
        extra = [r for r in rows_all if r[0] not in kept]               # what the rule removed that the other rules kept
        self.assertTrue(extra)
        self.assertTrue(all(cap_of[r[0]] < 10e9 for r in extra))
        self.assertLessEqual(len(extra), info["small_dropped"])         # a small session can also be special or a report day


class Intervals(unittest.TestCase):
    def test_mornings_are_resampled_when_a_morning_holds_several_names(self):
        recs = []
        for k in range(40):                                     # half the mornings every name closes up, half none does
            for j in range(30):
                up = 1 if k % 2 == 0 else 0
                recs.append({"date": "2026-01-%02d" % (k % 28 + 1) + ("a" if k >= 28 else ""), "up": up, "dn": 0, "oc": 2.5 if up else 0.0,
                             "hi": 3.0 if up else 0.5, "lo": -0.5})
        st = pick.seg_stats(recs)
        self.assertTrue(st["ci"].startswith("mornings resampled"))
        from dt import stats
        wlo, whi = stats.wilson(st["up"], st["n"])
        self.assertGreater(st["up_ci"][1] - st["up_ci"][0], 3 * (whi - wlo))   # far wider than the name-by-name interval
        one = pick.seg_stats(recs[::30])
        self.assertEqual(one["ci"], "wilson")
        single = pick.seg_stats([r for r in recs if r["date"] == recs[0]["date"]])
        self.assertEqual((single["ci"], single["up_ci"]), ("one morning: no interval", [None, None]))
        for k in ("band", "touch_up", "touch_dn"):
            self.assertIn(k + "_ci", one)


class CliMore(unittest.TestCase):
    """The morning's failure modes, each with the card it must post."""
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(cls.base)
        try:
            os.makedirs(os.path.join("trader", "cache", "earnings"))
            for sub in ("daily", "bars5m"):
                os.symlink(os.path.join(CACHE, sub), os.path.join("trader", "cache", sub))
            write(os.path.join("trader", "largecap.json"), json.dumps({"symbols": [{"symbol": x} for x in SYMS]}))
            code, out, err = run(["train", "--workers", "1"])
            assert code == 0, err
        finally:
            os.chdir(cwd)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        shutil.copytree(os.path.join(self.base, "trader"), os.path.join(self.d, "trader"), symlinks=True)
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d, ignore_errors=True)

    def morning(self, *extra):
        code, out, err = run(["morning", "--date", "2026-10-23", "--no-premarket", "--now", "2026-10-23 08:06"] + list(extra))
        return code, out, err

    def test_a_futures_file_without_an_nq_read_does_not_crash(self):
        write(os.path.join("trader", "futures.json"), json.dumps({"date": "2026-10-23", "cutoff": "08:05", "nq": None, "es": {"gap_pct": 0.1}}))
        code, out, err = self.morning("--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertIsNone(doc["nq_gap_pct"])
        self.assertIn("NQ fetch failed", doc["futures_words"])

    def test_a_rerun_keeps_the_logged_pick_and_replace_supersedes_it(self):
        code, out, err = self.morning("--write")
        self.assertEqual(code, 0, err)
        first = dtio.read_json(os.path.join("trader", "pick.json"))["pick"]["symbol"]
        code, out, err = self.morning("--write", "--exclude", first)
        self.assertEqual(code, 0, err)
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        self.assertEqual(doc["pick"]["symbol"], first)                  # the card still names the logged pick
        self.assertIn("kept it", doc["kept_note"])
        self.assertEqual(len(records(os.path.join("trader", "pick", "record.jsonl"))), 1)
        code, out, err = self.morning("--write", "--exclude", first, "--replace")
        self.assertEqual(code, 0, err)
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        second = doc["pick"]["symbol"]
        self.assertNotEqual(second, first)
        rec = records(os.path.join("trader", "pick", "record.jsonl"))
        self.assertEqual([(r["symbol"], r["status"]) for r in rec], [(first, "superseded"), (second, "pending")])
        run(["grade", "--write"])
        rec = records(os.path.join("trader", "pick", "record.jsonl"))
        self.assertEqual([r["status"] for r in rec], ["superseded", "graded"])   # only the posted pick is graded
        self.assertEqual(cli.live_record(type("A", (), {"record": os.path.join("trader", "pick", "record.jsonl")})())["graded"], 1)

    def test_a_failed_morning_posts_a_dated_no_pick_card(self):
        orig = cli.morning

        def boom(a):
            raise RuntimeError("boom at 08:08")
        cli.morning = boom
        try:
            code, out, err = self.morning("--write", "--publish")
        finally:
            cli.morning = orig
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        doc = dtio.read_json(os.path.join("trader", "publish", "pick.json"))
        self.assertEqual((doc["date"], doc["no_pick"], doc["failed"]), ("2026-10-23", True, True))
        self.assertIn("boom at 08:08", doc["no_pick_words"])
        self.assertIn("oos", doc)

    def test_an_empty_calendar_is_not_a_day_nobody_reports(self):
        write(os.path.join("trader", "cache", "earnings", "2026-10-23.json"), json.dumps({"date": "2026-10-23", "rows": []}))
        code, out, err = self.morning("--json")
        self.assertEqual(code, 0, err)
        self.assertIn("came back empty", json.loads(out)["reporting_source"])

    def test_a_half_day_posts_no_pick(self):
        code, out, err = run(["morning", "--date", "2025-12-24", "--no-premarket", "--now", "2025-12-24 08:06", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertTrue(doc["half_day"] and doc["no_pick"])
        self.assertIn("1:00 p.m.", doc["no_pick_words"])

    def test_a_late_card_is_posted_marked_and_never_counted(self):
        code, out, err = run(["morning", "--date", "2026-10-23", "--no-premarket", "--now", "2026-10-23 10:05", "--write"])
        self.assertEqual(code, 0, err)
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        self.assertTrue(doc["late"])
        self.assertFalse(os.path.exists(os.path.join("trader", "pick", "record.jsonl")))
        self.assertIn("LATE", out)

    def test_a_name_a_session_behind_is_left_out(self):
        from dt import bars
        daily = os.path.join("trader", "cache", "daily")
        os.remove(daily)                                          # a real copy of the store, one name cut two sessions short
        shutil.copytree(os.path.join(CACHE, "daily"), daily)
        victim = SYMS[4]
        doc = bars.load_store(victim, "daily", os.path.join("trader", "cache"))
        doc["rows"] = [r for r in doc["rows"] if r[0] < "2026-10-22"]
        bars.save_store(victim, doc, "daily", os.path.join("trader", "cache"))
        orig = cli.STALE_SHARE
        cli.STALE_SHARE = 0.5                                     # one name of eighteen: behind, not a stale store
        try:
            code, out, err = self.morning("--json")
        finally:
            cli.STALE_SHARE = orig
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual((doc["behind_today"], doc["store_stale"]), (1, False))
        self.assertNotIn(victim, [r["symbol"] for r in doc["ranked"]])
        code, out, err = self.morning("--json")                   # at the real threshold one in eighteen is a stale store
        doc = json.loads(out)
        self.assertTrue(doc["store_stale"] and doc["no_pick"])
        self.assertIn("session behind", doc["no_pick_words"])

    def test_a_run_before_the_cutoff_is_a_preview_that_writes_nothing(self):
        code, out, err = run(["morning", "--date", "2026-10-23", "--no-premarket", "--now", "2026-10-23 07:30", "--write", "--publish"])
        self.assertEqual(code, 0, err)
        self.assertIn("preview", err)
        self.assertIn("PREVIEW", out)
        for path in (os.path.join("trader", "pick.json"), os.path.join("trader", "publish", "pick.json"), os.path.join("trader", "pick", "record.jsonl")):
            self.assertFalse(os.path.exists(path), path)

    def test_a_rerun_after_925_keeps_the_logged_pick_and_is_not_late(self):
        code, out, err = self.morning("--write")
        first = dtio.read_json(os.path.join("trader", "pick.json"))["pick"]["symbol"]
        code, out, err = run(["morning", "--date", "2026-10-23", "--no-premarket", "--now", "2026-10-23 10:05", "--write", "--exclude", first])
        self.assertEqual(code, 0, err)
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        self.assertEqual((doc["pick"]["symbol"], doc["late"]), (first, False))
        rec = records(os.path.join("trader", "pick", "record.jsonl"))          # the fixture holds the session, so it may be graded already
        self.assertEqual([(r["symbol"], r["status"] in ("pending", "graded")) for r in rec], [(first, True)])

    def test_replace_that_ends_in_no_pick_supersedes_the_days_pick(self):
        code, out, err = self.morning("--write")
        code, out, err = self.morning("--write", "--replace", "--floor-x", "100")
        self.assertEqual(code, 0, err)
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        self.assertTrue(doc["no_pick"])
        self.assertEqual([r["status"] for r in records(os.path.join("trader", "pick", "record.jsonl"))], ["superseded"])

    def test_a_closed_day_posts_no_pick(self):
        code, out, err = run(["morning", "--date", "2026-10-24", "--no-premarket", "--now", "2026-10-24 08:06", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertTrue(doc["no_pick"])
        self.assertIn("closed", doc["no_pick_words"])

    def test_a_bad_record_line_and_a_broken_calendar_do_not_stop_the_morning(self):
        os.makedirs(os.path.join("trader", "pick"), exist_ok=True)
        write(os.path.join("trader", "pick", "record.jsonl"), '{"date": "2026-10-20", "symbol": "X", "status": "graded", "up": 1, "dn": 0, "oc_pct": 2.5}\n{not json\n')
        write(os.path.join("trader", "cache", "earnings", "2026-10-23.json"), "{")
        code, out, err = self.morning("--write")
        self.assertEqual(code, 0, err)
        self.assertIn("skipped line 2", err)
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        self.assertIn("no Nasdaq calendar file", doc["reporting_source"])
        self.assertEqual(doc["live"]["graded"], 1)

    def test_the_failure_card_never_replaces_a_logged_pick(self):
        code, out, err = self.morning("--write", "--publish")
        first = dtio.read_json(os.path.join("trader", "publish", "pick.json"))["pick"]["symbol"]
        orig = cli.morning

        def boom(a):
            raise RuntimeError("boom on the retry")
        cli.morning = boom
        try:
            code, out, err = self.morning("--write", "--publish")
        finally:
            cli.morning = orig
        self.assertEqual(code, 1)
        doc = dtio.read_json(os.path.join("trader", "publish", "pick.json"))
        self.assertEqual(doc["pick"]["symbol"], first)
        self.assertFalse(doc.get("failed"))

    def test_the_next_names_never_repeat_the_pick_and_the_grade_refreshes_the_shadow(self):
        code, out, err = self.morning("--write")
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        self.assertNotIn(doc["pick"]["symbol"], [r["symbol"] for r in doc["ranked"]])
        run(["grade", "--write"])
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        self.assertEqual((doc["live"]["graded"], doc["shadow"]["live"]["graded"]), (1, 1))

    def test_the_shadows_are_logged_and_graded_by_rule_but_never_the_pick(self):
        code, out, err = self.morning("--write")
        self.assertEqual(code, 0, err)
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        sh = doc["shadow"]
        self.assertTrue(sh["symbol"])
        self.assertIn("not the pick", sh["why"])
        self.assertTrue({"atr", "net", "ret"} <= set(doc["shadows"]))           # the live rule is up: the other two are shadows
        self.assertNotIn("up", doc["shadows"])
        self.assertEqual(doc["shadows"]["atr"]["symbol"], sh["symbol"])
        rec = records(os.path.join("trader", "pick", "shadow.jsonl"))
        self.assertEqual(sorted(r["rule"] for r in rec), sorted(doc["shadows"]))   # one row a rule
        self.assertTrue(all(r["status"] == "pending" for r in rec))
        self.morning("--write")                                                   # a re-run logs no shadow twice
        self.assertEqual(len(records(os.path.join("trader", "pick", "shadow.jsonl"))), len(rec))
        run(["grade", "--write"])
        rec = records(os.path.join("trader", "pick", "shadow.jsonl"))
        self.assertTrue(all(r["status"] == "graded" for r in rec))
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        for rn, s_ in doc["shadows"].items():
            self.assertEqual(s_["live"]["graded"], 1, rn)
        pick_rec = records(os.path.join("trader", "pick", "record.jsonl"))[0]
        self.assertIn(pick_rec["first_touch"]["first"], ("up", "down", "neither"))   # the fixture holds 5-minute bars for the day
        self.assertIn("entered", pick_rec["fh_breakout"])
        self.assertEqual(doc["live"]["breakout"]["graded"], 1)

    def test_intraday_follows_the_pick_through_the_session(self):
        from dt import clock
        import datetime as _dt
        code, out, err = self.morning("--write")
        doc = dtio.read_json(os.path.join("trader", "pick.json"))
        sym = doc["pick"]["symbol"]

        def ts(hhmm):
            h, m = map(int, hhmm.split(":"))
            return clock.ts_of(clock.eastern_to_utc(_dt.datetime(2026, 10, 23, h, m)))
        stamps = [ts("%02d:%02d" % divmod(570 + 5 * k, 60)) for k in range(78)]        # 09:30 to 15:55
        closes = [100.0 - (3.0 if k == 0 else 0.0) + 0.05 * k for k in range(78)]
        chart = {"chart": {"result": [{"meta": {}, "timestamp": stamps, "indicators": {"quote": [{
            "open": [100.0] + closes[:-1], "high": [c + 0.2 for c in closes], "low": [97.5] + [c - 0.2 for c in closes[1:]], "close": closes}]}}]}}
        orig_get, orig_sleep = cli.net.yahoo_get, cli.net.SLEEP
        cli.net.yahoo_get = lambda path: chart
        cli.net.SLEEP = lambda s_: None
        try:
            code, out, err = run(["intraday", "--date", "2026-10-23", "--now", "2026-10-23 10:31", "--write"])
            mid = dtio.read_json(os.path.join("trader", "pick.json"))["intraday"]["names"][sym]
            code2, out2, err2 = run(["intraday", "--date", "2026-10-23", "--now", "2026-10-23 16:30", "--write"])
            end = dtio.read_json(os.path.join("trader", "pick.json"))["intraday"]["names"][sym]
        finally:
            cli.net.yahoo_get, cli.net.SLEEP = orig_get, orig_sleep
        self.assertEqual((code, code2), (0, 0), err + err2)
        self.assertEqual((mid["as_of"], mid["closed"], mid["first_touch"], mid["first_touch_time"]), ("10:30", False, "down", "09:30"))
        self.assertAlmostEqual(mid["first_hour_pct"], closes[11] - 100.0, 2)                 # the first hour is complete at 10:30
        json.dumps(dtio.read_json(os.path.join("trader", "pick.json")))                     # and the card stays plain JSON
        self.assertTrue(end["closed"])
        self.assertAlmostEqual(end["from_open_pct"], closes[-1] - 100.0, 2)
        self.assertIn("count of one", dtio.read_json(os.path.join("trader", "pick.json"))["intraday"]["note"])
        # with the daily bar served, the day is measured from the opening auction and ends at the closing auction
        daily = {"chart": {"result": [{"meta": {"dataGranularity": "1d"}, "timestamp": [ts("09:30")],
                                       "indicators": {"quote": [{"open": [98.0], "high": [104.0], "low": [97.0], "close": [103.9]}]}}]}}
        cli.net.yahoo_get = lambda path: daily if "interval=1d" in path else chart
        cli.net.SLEEP = lambda s_: None
        try:
            code3, out3, err3 = run(["intraday", "--date", "2026-10-23", "--now", "2026-10-23 16:30", "--write"])
            off = dtio.read_json(os.path.join("trader", "pick.json"))["intraday"]["names"][sym]
        finally:
            cli.net.yahoo_get, cli.net.SLEEP = orig_get, orig_sleep
        self.assertEqual(code3, 0, err3)
        self.assertEqual(off["open"], 98.0)
        self.assertIn("opening auction", off["open_source"])
        self.assertAlmostEqual(off["from_open_pct"], (103.9 / 98.0 - 1) * 100, 2)
        self.assertEqual(off["first_touch"], "up")                                             # +2 % from 98 came first
        code, out, err = run(["intraday", "--date", "2026-10-26", "--now", "2026-10-26 10:31"])   # no card for that day
        self.assertIn("nothing to follow", out)

    def test_the_published_placeholder_carries_the_record(self):
        code, out, err = run(["grade", "--write", "--publish"])
        cli.publish(type("A", (), {"out": os.path.join("trader", "nothing.json"), "model": os.path.join("trader", "pick", "model.json"),
                                   "record": os.path.join("trader", "pick", "record.jsonl"), "publish_dir": os.path.join("trader", "publish")})(), None)
        doc = dtio.read_json(os.path.join("trader", "publish", "pick.json"))
        self.assertIsNone(doc["date"])
        for k in ("oos", "oos_rules", "claim", "direction_kind", "configs_tested"):
            self.assertIn(k, doc, k)


class Premarket(unittest.TestCase):
    def test_the_print_is_the_last_bar_that_ended_by_the_cutoff_on_the_date(self):
        from dt import clock
        import datetime as _dt

        def ts(day, hhmm):
            h, m = map(int, hhmm.split(":"))
            return clock.ts_of(clock.eastern_to_utc(_dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(h, m))))
        stamps = [ts("2026-10-22", "15:55"), ts("2026-10-23", "07:55"), ts("2026-10-23", "08:00"), ts("2026-10-23", "08:05")]
        doc = {"chart": {"result": [{"meta": {"chartPreviousClose": 100.0}, "timestamp": stamps,
                                     "indicators": {"quote": [{"close": [99.0, 101.0, 102.0, 103.0]}]}}]}}
        orig_get, orig_sleep = cli.net.yahoo_get, cli.net.SLEEP
        cli.net.yahoo_get = lambda path: doc
        cli.net.SLEEP = lambda s: None
        try:
            q = cli.premarket_quotes(["X"], "2026-10-23", _dt.time(8, 8))
            q2 = cli.premarket_quotes(["X"], "2026-10-24", _dt.time(8, 8))
        finally:
            cli.net.yahoo_get, cli.net.SLEEP = orig_get, orig_sleep
        self.assertEqual((q["X"]["last"], q["X"]["time"]), (102.0, "08:05"))   # the 08:05 bar was still forming at 08:08
        self.assertAlmostEqual(q["X"]["vs_prev_close_pct"], 2.0)                 # no store close given: Yahoo's, and it says so
        self.assertIn("Yahoo", q["X"]["prev_close_source"])
        cli.net.yahoo_get = lambda path: doc
        cli.net.SLEEP = lambda s: None
        try:
            q3 = cli.premarket_quotes(["X"], "2026-10-23", _dt.time(8, 8), prev_closes={"X": 85.0})
        finally:
            cli.net.yahoo_get, cli.net.SLEEP = orig_get, orig_sleep
        self.assertAlmostEqual(q3["X"]["vs_prev_close_pct"], 20.0)               # Yahoo's stale close loses to the store's
        self.assertEqual(q3["X"]["prev_close_source"], "the price store")
        self.assertIsNone(q2["X"]["last"])                                   # another day's bars never stand in


class CalendarFetch(unittest.TestCase):
    def test_a_refresh_never_makes_a_calendar_file_worse(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import fetch_earnings_history as feh
        d = tempfile.mkdtemp()
        write(os.path.join(d, "2026-10-26.json"), json.dumps({"date": "2026-10-26", "rows": [
            {"symbol": "AAA", "time": "time-pre-market"}, {"symbol": "BBB", "time": "time-after-hours"}]}))
        orig = feh.net.nasdaq_earnings
        try:
            feh.net.nasdaq_earnings = lambda date: []
            date, n, err = feh.fetch_one("2026-10-26", d, tries=1)
            self.assertIsNone(n)
            self.assertIn("kept the 2 rows", err)
            self.assertEqual(len(dtio.read_json(os.path.join(d, "2026-10-26.json"))["rows"]), 2)
            feh.net.nasdaq_earnings = lambda date: [{"symbol": "AAA", "time": "time-not-supplied", "eps": "$1.00"}, {"symbol": "CCC", "time": "time-not-supplied"}]
            date, n, err = feh.fetch_one("2026-10-26", d, tries=1)
            rows = {r["symbol"]: r for r in dtio.read_json(os.path.join(d, "2026-10-26.json"))["rows"]}
            self.assertEqual((n, err), (2, None))
            self.assertEqual(rows["AAA"]["time"], "time-pre-market")        # the time on disk survives the refresh
            self.assertEqual(rows["AAA"]["eps"], "$1.00")
            self.assertEqual(rows["CCC"]["time"], "time-not-supplied")
            self.assertNotIn("BBB", rows)                                   # the newer answer decides who is listed
        finally:
            feh.net.nasdaq_earnings = orig
            shutil.rmtree(d, ignore_errors=True)


class Wrapper(unittest.TestCase):
    def test_run_sh_breaks_stale_locks_and_publishes_only_the_bundle_from_main(self):
        with open(os.path.join(ROOT, "scripts", "run.sh"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("breaking a stale lock", text)
        self.assertIn('echo $$ > "$lock/pid"', text)
        self.assertIn("not main; nothing published", text)
        self.assertIn("-- docs/data/bundle.enc", text)
        self.assertIn("never pick again", text)
        self.assertIn("gap_pct') is not None", text)
        self.assertLess(text.index("trader-pick/pick.py morning"), text.index("fetch_premarket.py --range 1d"))
        for must in ("another run took the lock a moment ago", "the New York clock could not be read", "'redo' if d.get('failed')",
                     "--refresh-last 10", "the lock build failed"):
            self.assertIn(must, text, must)
        self.assertEqual(text.count("trader-pick/pick.py intraday --write --publish"), 2)      # the live jobs and the 16:20 job

    def test_the_front_page_keeps_one_interval_helper_of_each_kind(self):
        with open(os.path.join(ROOT, "site", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertEqual(html.count("function ci("), 1)                   # the board's R interval
        self.assertEqual(html.count("function pci("), 1)                  # the pick's rate interval
        self.assertNotIn("Most likely", html)
        for must in ("So far today", "Today:</b>", "count of one"):
            self.assertIn(must, html, must)


class LongHistory(unittest.TestCase):
    def test_the_research_fetcher_never_writes_the_live_store_and_keeps_full_sessions(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import fetch_daily_history as fdh
        from dt import clock
        import datetime as _dt
        code = fdh.main(["--root", os.path.join(ROOT, "trader", "cache"), "--symbols", "SPY", "--quiet"])
        self.assertEqual(code, 2)                                         # the live store is refused
        d = tempfile.mkdtemp()
        lc = os.path.join(d, "largecap.json")
        write(lc, json.dumps({"symbols": [{"symbol": "AAA"}]}))
        os.makedirs(os.path.join(d, "largecap_history"))
        write(os.path.join(d, "largecap_history", "2026-10-25.json"), json.dumps({"symbols": [{"symbol": "GONE"}, {"symbol": "AAA"}]}))
        syms = fdh.symbols_of(lc)
        self.assertEqual(syms[:2], ["AAA", "GONE"])                       # a name that left keeps its history
        self.assertIn("SPY", syms)

        def ts(day, hhmm):
            h, m = map(int, hhmm.split(":"))
            return clock.ts_of(clock.eastern_to_utc(_dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(h, m))))
        series = {"t": [ts("2026-10-22", "09:30"), ts("2026-10-22", "15:30"), ts("2026-10-23", "09:30"), ts("2026-10-23", "04:00")],
                  "o": [1.0, 2.0, 3.0, 9.0], "h": [1.5, 2.5, 3.5, 9.5], "l": [0.5, 1.5, 2.5, 8.5], "c": [1.2, 2.2, 3.2, 9.2], "v": [10, 20, 30, 40]}
        now = clock.eastern_to_utc(_dt.datetime(2026, 10, 23, 12, 0))
        sess = fdh.intraday_sessions(series, now)
        self.assertEqual(list(sess), ["2026-10-22"])                       # today's session is partial until the close; pre-market is not kept
        self.assertEqual([b[0] for b in sess["2026-10-22"]], ["09:30", "15:30"])
        shutil.rmtree(d, ignore_errors=True)


class FirstHour(unittest.TestCase):
    def bars5(self, path_pct, open_=100.0, day="2026-10-23"):
        """Five-minute bars from 09:30 whose closes follow path_pct (percent of the open), one a bar."""
        from dt import clock
        import datetime as _dt
        out, prev = [], open_
        for k, pct in enumerate(path_pct):
            h, m = divmod(570 + 5 * k, 60)
            t = clock.ts_of(clock.eastern_to_utc(_dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(h, m))))
            c = open_ * (1 + pct / 100.0)
            out.append({"t": t, "o": prev, "h": max(prev, c) + 0.01, "l": min(prev, c) - 0.01, "c": c, "v": 1})
            prev = c
        return out

    def test_buckets_and_the_first_hour(self):
        from dt import firsthour as fh
        self.assertEqual([fh.bucket_of(x) for x in (-1.5, -1.0, 0.0, 1.0, 1.01, None)], ["down", "flat", "flat", "flat", "up", None])
        hourly = [["09:30", 100.0, 101, 98, 98.5, 1], ["10:30", 98.5, 103, 98, 102.5, 1], ["15:30", 102.5, 103, 102, 102.7, 1]]
        pct, c = fh.first_hour_of(hourly)
        self.assertAlmostEqual(pct, -1.5)
        self.assertEqual(c, 98.5)
        five = [{"hm": "%02d:%02d" % divmod(570 + 5 * k, 60), "o": 100.0, "c": 100.0 + k / 10.0} for k in range(12)]
        self.assertAlmostEqual(fh.first_hour_of(five)[0], 1.1)
        self.assertIsNone(fh.first_hour_of(five[:11]))                     # 10:25 not in yet: the first hour is not over

    def test_the_table_counts_past_picks_by_their_first_hour(self):
        from dt import firsthour as fh
        sessions = {("A", "2025-08-01"): [["09:30", 100, 101, 97, 98, 1], ["10:30", 98, 99, 96, 97, 1]],      # down, closed -3
                    ("A", "2026-05-01"): [["09:30", 100, 103, 99, 102, 1], ["10:30", 102, 104, 101, 103, 1]],   # up, closed +3
                    ("B", "2026-05-02"): [["09:30", 100, 101, 99, 100.5, 1], ["10:30", 100.5, 101, 99, 100, 1]]}  # flat
        picks = [{"date": "2025-08-01", "symbol": "A", "oc": -3.0}, {"date": "2026-05-01", "symbol": "A", "oc": 3.0},
                 {"date": "2026-05-02", "symbol": "B", "oc": 0.0}, {"date": "2026-05-03", "symbol": "C", "oc": 1.0}]
        t = fh.table(picks, lambda s_, d: sessions.get((s_, d)), "2026-03-05")
        self.assertEqual((t["n"], t["missing"]), (3, 1))
        self.assertEqual(t["buckets"]["down"]["all"]["dn"], 1)
        self.assertEqual(t["buckets"]["up"]["h2"]["up"], 1)
        self.assertEqual(t["buckets"]["up"]["h1"]["n"], 0)
        self.assertIn("coin flip", fh.words(None, 2.0, t))
        self.assertEqual(fh.words(None, 2.0, {}), "")

    def test_the_close_confirmed_breakout(self):
        from dt import firsthour as fh
        # first hour 0 to +1 %; the second hour closes above it; the third hour's open is the entry; the day ends +3 %
        path = [0.5] * 12 + [1.5] * 12 + [2.0] * 12 + [3.0] * 42
        r = fh.close_confirmed_breakout(self.bars5(path))
        self.assertTrue(r["entered"])
        self.assertEqual(r["entry_time"], "11:30")
        self.assertGreater(r["ret_pct"], 0)
        self.assertFalse(r["stopped"])
        stop = [0.5] * 12 + [1.5] * 12 + [-2.0] * 54                       # breaks down through the first hour's low: stopped
        self.assertTrue(fh.close_confirmed_breakout(self.bars5(stop))["stopped"])
        self.assertEqual(fh.close_confirmed_breakout(self.bars5([0.2] * 78)), {"entered": False})
        self.assertIsNone(fh.close_confirmed_breakout(self.bars5([0.2] * 20)))

    def test_a_short_refresh_of_the_hourly_store_keeps_its_history(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import fetch_daily_history as fdh
        d = tempfile.mkdtemp()
        path = fdh.intraday_path(d, "AAA", "60m")
        dtio.write_json(path, {"sessions": {"2025-01-02": [["09:30", 1, 1, 1, 1, 1]]}})
        orig_chart = fdh.net.chart
        fdh.net.chart = lambda sym, rng, interval: {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
        orig_sessions = fdh.intraday_sessions
        fdh.intraday_sessions = lambda series, now: {"2026-10-27": [["09:30", 2, 2, 2, 2, 2]]}
        try:
            fdh.fetch_one("AAA", d, "1mo", None, "60m")
        finally:
            fdh.net.chart, fdh.intraday_sessions = orig_chart, orig_sessions
        self.assertEqual(sorted(dtio.read_json(path)["sessions"]), ["2025-01-02", "2026-10-27"])
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
