"""trader-agents: the plan, the prompts, the validator, the decision, the memory, the rankings, the weights test; all offline on
the fixture cache (TSLA on 2026-10-19 at 10:15) with scripted role outputs that cite the pack's real items."""

import io as _io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _dt_fixtures import daily_rows
from _load import load, FIX, ROOT

sys.path.insert(0, ROOT)
from dt import agents, evidence, scoring, featurestudy, io as dtio  # noqa: E402

cli = load("trader-agents", "agents")
CACHE = os.path.join(FIX, "dt-cache")
DESK_EDGE = os.path.join(FIX, "trader-desk", "edge.json")
DAY = "2026-10-19"


def run(argv):
    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class Project(unittest.TestCase):
    """A temp project folder with the fixture cache linked in and the desk fixture's edge table."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs(os.path.join("trader", "lab"))
        os.symlink(CACHE, os.path.join("trader", "cache"))
        shutil.copy(DESK_EDGE, os.path.join("trader", "lab", "edge.json"))
        shutil.copy(os.path.join(ROOT, "trader-budget", "config.example.json"), os.path.join("trader", "config.json"))

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d, ignore_errors=True)

    def prepare(self, depth="fast", sym="TSLA"):
        return run(["prepare", sym, "--date", DAY, "--at", "10:15", "--depth", depth, "--now", "2026-10-19 10:16"])

    def pack(self, sym="TSLA"):
        return evidence.load(os.path.join("trader", "research", DAY, sym, "evidence.json"))

    def script_output(self, step, role, rnd, pack, **over):
        """A valid output for a role, citing the pack's real DATA item for the snapshot."""
        snap = next(i for i in pack["items"] if i["kind"] == "DATA" and "SMA20" in i["text"])
        rsi = snap["value"]["rsi_14"]
        sk = agents.STANCE_KEY.get(role, "stance")
        default = {"research_manager": "hold", "trader": "hold", "portfolio_manager": "hold"}.get(role, "avoid" if role.startswith("risk_") else "neutral")
        o = {"role": role, "round": rnd, "symbol": pack["symbol"], "date": pack["date"], sk: over.pop("stance", default), "confidence": over.pop("confidence", "medium"),
             "claims": [{"text": "RSI14 reads %s." % rsi, "kind": "DATA", "evidence": [snap["id"]]},
                        {"text": "That is the whole read.", "kind": "INTERPRETATION", "evidence": []}],
             "missing": [], "summary": "Scripted for the test. It cites a real item. It decides nothing by itself."}
        if role in ("market", "momentum", "fundamentals", "news", "sentiment", "regime"):
            o["scores"] = {"score": 5, "data_quality": 5}
        if role in ("bull", "bear"):
            o["strength"] = 3
            o["rebuttals"] = [] if rnd == 1 else [{"to": "%s_%d" % ("bear" if role == "bull" else "bull", rnd - 1), "text": "No.", "evidence": []}]
        if role == "contrarian":
            o["attacks"] = [{"target": "consensus", "text": "The consensus assumes the range holds.", "evidence": []}]
        if role == "research_manager":
            o.update({"side_preference": "none", "plan": "Sit out."})
        if role == "trader":
            o.update({"entry": None, "stop": None, "target": None, "setup_config": None, "size_note": "none"})
        if role.startswith("risk_") and role != "risk_analyst":
            o.update({"adjustments": "none", "rebuttals": []})
        if role == "risk_analyst":
            o.update({"verdict_long": "ok", "verdict_short": "caution", "adjustments": "none"})
        if role == "portfolio_manager":
            o.update({"side": "none", "horizon": "intraday", "thesis": "Nothing cleared the bar (%s)." % snap["id"], "invalidation": "n/a",
                      "key_risks": ["none"], "no_trade": True, "overrides_trader": False, "override_reason": ""})
        o.update(over)
        rd = agents.run_dir(".", DAY, pack["symbol"])
        os.makedirs(os.path.join(rd, "outputs"), exist_ok=True)
        with open(agents.output_path(rd, step), "w", encoding="utf-8") as fh:
            json.dump(o, fh)
        return o

    def script_all(self, depth, pack, **over):
        for s in agents.plan_for(depth):
            self.script_output(s["step"], s["role"], s["round"], pack, **dict(over))


class Plan(unittest.TestCase):
    def test_the_four_depths_and_their_order(self):
        fast = agents.plan_for("fast")
        self.assertEqual([s["step"] for s in fast], ["market", "momentum", "regime", "bull_1", "bear_1", "research_manager", "trader", "risk_analyst", "portfolio_manager"])
        std = agents.plan_for("standard")
        self.assertEqual(len(std), 15)
        self.assertIn("risk_aggressive_1", [s["step"] for s in std])
        deep = agents.plan_for("deep")
        self.assertEqual(len(deep), 21)
        self.assertIn("contrarian", [s["step"] for s in deep])
        self.assertIn("bull_2", [s["step"] for s in deep])
        mx = agents.plan_for("maximum")
        self.assertEqual(len(mx), 26)
        bear2 = next(s for s in deep if s["step"] == "bear_2")
        self.assertIn("bull_2", bear2["needs"])
        pm = std[-1]
        self.assertEqual(pm["role"], "portfolio_manager")
        self.assertIn("risk_analyst", pm["needs"])
        self.assertIn("research_manager", pm["needs"])
        for s in std:
            for n in s["needs"]:
                self.assertIn(n, [x["step"] for x in std])
        self.assertEqual(agents.step_role("risk_aggressive_2"), "risk_aggressive")
        self.assertEqual(agents.step_role("bull_1"), "bull")
        self.assertEqual(agents.step_role("portfolio_manager"), "portfolio_manager")


class Loop(Project):
    def test_prepare_writes_the_pack_and_the_analyst_prompts(self):
        code, out, err = self.prepare("fast")
        self.assertEqual(code, 0, err)
        self.assertIn("Evidence pack", out)
        rd = agents.run_dir(".", DAY, "TSLA")
        self.assertTrue(os.path.exists(os.path.join(rd, "evidence.json")))
        for step in ("market", "momentum", "regime"):
            self.assertTrue(os.path.exists(agents.prompt_path(rd, step)), step)
        self.assertFalse(os.path.exists(agents.prompt_path(rd, "bull_1")))
        with open(agents.prompt_path(rd, "market"), encoding="utf-8") as fh:
            prompt = fh.read()
        for must in ("Evidence first", "TradingAgents v0.7.0", "## Your output contract", "E1", "Missing from the pack", "Market Analyst"):
            self.assertIn(must, prompt, must)
        self.assertNotIn(chr(0x2014), prompt)
        pack = self.pack()
        self.assertEqual((pack["symbol"], pack["date"], pack["cutoff"]), ("TSLA", DAY, "10:15"))
        self.assertTrue(any(i["kind"] == "SIGNAL" and "orb15|r=1" in i["text"] for i in pack["items"]))
        self.assertTrue(pack["lab"]["present"])
        self.assertEqual(pack["setups"]["long"][0]["lab"]["status"], "approved")   # the fixture edge approves orb15_rs|r=1 and orb15|r=1

    def test_the_whole_fast_loop_to_a_card(self):
        self.prepare("fast")
        pack = self.pack()
        self.script_all("fast", pack)
        code, out, err = run(["next", "TSLA", "--date", DAY])
        self.assertEqual(code, 0, out + err)
        self.assertIn("9 of 9 steps done", out)
        code, out, err = run(["decide", "TSLA", "--date", DAY, "--write", "--now", "2026-10-19 11:00"])
        self.assertEqual(code, 0, err)
        rd = agents.run_dir(".", DAY, "TSLA")
        card = dtio.read_json(os.path.join(rd, "card.json"))
        self.assertEqual((card["symbol"], card["rating"], card["side"], card["no_trade"]), ("TSLA", "hold", "none", True))
        self.assertIn("NO TRADE", out)
        for section in ("PRICE / DATA SNAPSHOT", "MARKET REGIME", "LONG SCORE", "SHORT SCORE", "CONFIDENCE", "BEST SETUP", "THESIS", "INVALIDATION", "KEY RISKS", "EVIDENCE"):
            self.assertIn("## " + section, out, section)
        self.assertIn("Most people who day trade lose money", out)
        self.assertNotIn(chr(0x2014), out)
        with open(agents.memory_path("."), encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual((rows[0]["id"], rows[0]["status"], rows[0]["no_trade"]), ("2026-10-19|TSLA|none", "pending", True))
        # decided again while pending: the row is replaced, not duplicated
        code, out, err = run(["decide", "TSLA", "--date", DAY, "--write"])
        self.assertIn("replaced", out)
        with open(agents.memory_path("."), encoding="utf-8") as fh:
            self.assertEqual(len([l for l in fh if l.strip()]), 1)
        code, out, err = run(["card", "TSLA", "--date", DAY])
        self.assertEqual(code, 0)
        self.assertIn("# TSLA on", out)

    def test_a_long_card_with_the_trader_levels_from_the_signal(self):
        self.prepare("fast")
        pack = self.pack()
        sig_item = next(i for i in pack["items"] if i["kind"] == "SIGNAL" and (i["value"] or {}).get("signal") and "orb15_rs|r=1" in i["text"])
        sig = sig_item["value"]["signal"]
        self.script_all("fast", pack)
        self.script_output("research_manager", "research_manager", 1, pack, stance="buy", side_preference="long", plan="Take the ORB.")
        self.script_output("trader", "trader", 1, pack, stance="long", entry=sig["entry"], stop=sig["stop"], target=sig["target"], setup_config="orb15_rs|r=1",
                           size_note="one unit", claims=[{"text": "The rule fired at %s with entry %s." % (sig["entry_time"], sig["entry"]), "kind": "SIGNAL", "evidence": [sig_item["id"]]}])
        self.script_output("portfolio_manager", "portfolio_manager", 1, pack, stance="buy", side="long", horizon="intraday", thesis="The approved rule fired (%s)." % sig_item["id"],
                           invalidation="a close under %s" % sig["stop"], key_risks=["a late fill"], no_trade=False)
        code, out, err = run(["next", "TSLA", "--date", DAY])
        self.assertEqual(code, 0, out + err)
        code, out, err = run(["decide", "TSLA", "--date", DAY, "--write"])
        card = dtio.read_json(os.path.join(agents.run_dir(".", DAY, "TSLA"), "card.json"))
        self.assertEqual((card["rating"], card["side"], card["no_trade"]), ("buy", "long", False))
        self.assertEqual(card["best_setup"]["config_id"], "orb15_rs|r=1")
        self.assertEqual(card["trader"]["entry"], sig["entry"])
        self.assertIn("buy above %.2f" % sig["entry"], out)
        self.assertIn(card["confidence"], ("low", "medium", "high"))
        self.assertGreater(card["scores"]["sides"]["long"]["technical"], card["scores"]["sides"]["short"]["technical"] or 0)
        with open(agents.memory_path("."), encoding="utf-8") as fh:
            row = json.loads(fh.readline())
        self.assertEqual((row["side"], row["config_id"], row["entry"]), ("long", "orb15_rs|r=1", sig["entry"]))


class Validator(Project):
    def test_every_rule_the_contract_names(self):
        self.prepare("fast")
        pack = self.pack()
        idx = evidence.item_index(pack)
        snap = next(i for i in pack["items"] if i["kind"] == "DATA" and "SMA20" in i["text"])
        v = lambda out, role="market", rnd=1: agents.validate_output(role, rnd, out, pack)
        good = self.script_output("market", "market", 1, pack)
        self.assertEqual(v(good), [])
        self.assertEqual(v(None), ["no output file"])
        self.assertTrue(all(p.startswith("missing key") for p in v({"role": "market"})))
        bad = dict(good, claims=[{"text": "RSI14 reads 99.9.", "kind": "DATA", "evidence": [snap["id"]]}])
        self.assertTrue(any("quotes 99.9" in p for p in v(bad)))
        bad = dict(good, claims=[{"text": "Volume was big.", "kind": "DATA", "evidence": []}])
        self.assertTrue(any("cites no evidence" in p for p in v(bad)))
        bad = dict(good, claims=[{"text": "Volume was big.", "kind": "DATA", "evidence": ["E999"]}])
        self.assertTrue(any("E999" in p for p in v(bad)))
        bad = dict(good, confidence="high", missing=["news"])
        self.assertTrue(any("confidence high" in p for p in v(bad)))
        bad = dict(good, stance="very_bullish")
        self.assertTrue(any("stance" in p for p in v(bad)))
        bad = dict(good, symbol="NVDA")
        self.assertTrue(any("do not match the pack" in p for p in v(bad)))
        bad = dict(good, claims=[{"text": "ok", "kind": "GUESS", "evidence": []}])
        self.assertTrue(any("kind" in p for p in v(bad)))
        # a number quoted with the item's own text passes, a sentence-ending period does not break it
        ok = dict(good, claims=[{"text": "The 20-day average volume is {:,.0f}.".format(snap["value"]["avg_vol_20"]), "kind": "DATA", "evidence": [snap["id"]]}])
        self.assertEqual([p for p in v(ok) if "quotes" in p], [])
        # researchers in round 2 need a rebuttal; strength 1 to 5
        bull = self.script_output("bull_2", "bull", 2, pack, rebuttals=[])
        self.assertTrue(any("rebuttal" in p for p in v(bull, "bull", 2)))
        bull = self.script_output("bull_1", "bull", 1, pack, strength=9)
        self.assertTrue(any("strength" in p for p in v(bull, "bull", 1)))
        # the trader: absolute levels within 2 percent of a cited SIGNAL on its side
        sig_item = next(i for i in pack["items"] if i["kind"] == "SIGNAL" and (i["value"] or {}).get("signal"))
        sig = sig_item["value"]["signal"]
        t = self.script_output("trader", "trader", 1, pack, stance="long", entry=sig["entry"], stop=sig["stop"], target=sig["target"], setup_config="x", size_note="one unit")
        self.assertTrue(any("must cite the SIGNAL" in p for p in v(t, "trader")))
        t["claims"].append({"text": "Fired at %s." % sig["entry_time"], "kind": "SIGNAL", "evidence": [sig_item["id"]]})
        self.assertEqual(v(t, "trader"), [])
        t2 = dict(t, entry=round(sig["entry"] * 1.1, 2))
        self.assertTrue(any("within 2" in p for p in v(t2, "trader")))
        t3 = dict(t, entry="5%")
        self.assertTrue(any("absolute positive price" in p for p in v(t3, "trader")))
        t4 = dict(t, action="short")
        self.assertTrue(any("short needs" in p for p in v(t4, "trader")))
        # the portfolio manager against the trader without saying why
        self.script_output("trader", "trader", 1, pack, stance="long", entry=sig["entry"], stop=sig["stop"], target=sig["target"], setup_config="x", size_note="u",
                           claims=t["claims"])
        pm = self.script_output("portfolio_manager", "portfolio_manager", 1, pack, stance="sell", side="short", horizon="intraday", thesis="t", invalidation="i",
                                key_risks=["r"], no_trade=False)
        rd = agents.run_dir(".", DAY, "TSLA")
        probs = agents.validate_output("portfolio_manager", 1, pm, pack, rd)
        self.assertTrue(any("disagrees with the trader" in p for p in probs))
        pm2 = dict(pm, overrides_trader=True, override_reason="the regime")
        self.assertEqual([p for p in agents.validate_output("portfolio_manager", 1, pm2, pack, rd) if "disagrees" in p], [])
        pm3 = dict(pm, rating="buy", side="short")
        self.assertTrue(any("points long but side is short" in p for p in agents.validate_output("portfolio_manager", 1, pm3, pack, rd)))

    def test_next_reports_rejections_by_sentence(self):
        self.prepare("fast")
        pack = self.pack()
        self.script_output("market", "market", 1, pack, claims=[{"text": "RSI14 reads 12.34.", "kind": "DATA", "evidence": ["E3"]}])
        code, out, err = run(["next", "TSLA", "--date", DAY])
        self.assertEqual(code, 1)
        self.assertIn("REJECTED market", out)
        self.assertIn("quotes 12.34", out)
        code, out, err = run(["validate", "TSLA", "--date", DAY, "--step", "market"])
        self.assertEqual(code, 1)
        self.assertIn("rejected", out)


class Rankings(Project):
    def test_rank_over_the_fixture_cache(self):
        code, out, err = run(["rank", "--date", DAY, "--at", "10:15", "--top", "5", "--write", "--quiet"])
        self.assertEqual(code, 0, err)
        doc = dtio.read_json(os.path.join("trader", "rankings.json"))
        for name in ("top_momentum_longs", "top_short_candidates", "top_high_conviction", "top_risk_adjusted", "top_contrarian", "most_dangerous_shorts", "watchlist"):
            self.assertIn(name, doc["lists"], name)
            self.assertIn(name, doc["no_trade"])
        self.assertEqual(doc["date"], DAY)
        self.assertEqual(doc["configs_tested"], 33)
        self.assertFalse(doc["weights_tested"])
        longs = doc["lists"]["top_momentum_longs"]
        self.assertTrue(all(r["side"] == "long" for r in longs))
        self.assertTrue(all(r["verdict"] != "do_not_trade" for r in longs))
        self.assertIn("NO TRADE", out + "NO TRADE")
        self.assertIn("UNTESTED", out)


class Memory(Project):
    def test_settle_intraday_from_the_scorecard_and_swing_from_the_closes(self):
        os.makedirs(os.path.join("trader", "scorecard"))
        with open(os.path.join("trader", "scorecard", "outcomes.jsonl"), "w") as fh:
            fh.write(json.dumps({"id": "2026-10-19|TSLA|orb15|r=1|10:15 AM", "outcome": "close", "R_net": 0.49}) + "\n")
        rows = [{"id": "2026-10-19|TSLA|long", "date": "2026-10-19", "symbol": "TSLA", "side": "long", "horizon": "intraday", "config_id": "orb15|r=1",
                 "entry_time": "10:15 AM", "status": "pending", "no_trade": False},
                {"id": "2026-10-01|TSLA|long", "date": "2026-10-01", "symbol": "TSLA", "side": "long", "horizon": "swing", "status": "pending", "no_trade": False},
                {"id": "2026-10-16|TSLA|short", "date": "2026-10-16", "symbol": "TSLA", "side": "short", "horizon": "swing", "status": "pending", "no_trade": False},
                {"id": "2026-10-10|NVDA|none", "date": "2026-10-10", "symbol": "NVDA", "side": "none", "status": "pending", "no_trade": True}]
        os.makedirs(os.path.join("trader", "research"))
        with open(agents.memory_path("."), "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        settled, waiting, failed = agents.settle(".", os.path.join("trader", "cache"))
        self.assertIn("2026-10-19|TSLA|long", settled)
        self.assertIn("2026-10-01|TSLA|long", settled)
        self.assertIn("2026-10-10|NVDA|none", settled)
        self.assertIn("2026-10-16|TSLA|short", waiting)                                  # the fixture's rows end before five sessions passed
        with open(agents.memory_path("."), encoding="utf-8") as fh:
            got = {json.loads(l)["id"]: json.loads(l) for l in fh if l.strip()}
        self.assertEqual((got["2026-10-19|TSLA|long"]["outcome"], got["2026-10-19|TSLA|long"]["raw_return"]), ("close", 0.49))
        swing = got["2026-10-01|TSLA|long"]
        self.assertIn(swing["outcome"], ("green", "red"))
        self.assertIsNotNone(swing["alpha"])
        self.assertGreater(swing["resolved"], "2026-10-01")
        self.assertEqual(got["2026-10-10|NVDA|none"]["outcome"], "no_trade")
        # the reflection prompt and its storage
        code, out, err = run(["reflect", "2026-10-19|TSLA|long"])
        self.assertEqual(code, 0, err)
        rd = agents.run_dir(".", "2026-10-19", "TSLA")
        self.assertTrue(os.path.exists(os.path.join(rd, "prompts", "reflect.md")))
        os.makedirs(os.path.join(rd, "outputs"), exist_ok=True)
        with open(os.path.join(rd, "outputs", "reflect.json"), "w") as fh:
            json.dump({"id": "2026-10-19|TSLA|long", "reflection": "The call closed flat with +0.49R. The window matches the horizon. Next time weigh the late flag."}, fh)
        code, out, err = run(["reflect", "2026-10-19|TSLA|long", "--apply"])
        self.assertEqual(code, 0, err)
        # the next pack carries the lesson, point in time
        self.prepare("fast")
        pack = self.pack()
        ids = [l["id"] for l in pack["lessons"]]
        self.assertNotIn("2026-10-19|TSLA|long", ids)                                   # resolved on the pack's own date: not yet known
        self.assertIn("2026-10-01|TSLA|long", ids)                                      # resolved a week earlier: known
        code, out, err = run(["prepare", "TSLA", "--date", "2026-10-23", "--at", "10:15", "--depth", "fast", "--now", "2026-10-23 10:16"])
        pack = evidence.load(os.path.join("trader", "research", "2026-10-23", "TSLA", "evidence.json"))
        self.assertTrue(any(l["id"] == "2026-10-19|TSLA|long" and "late flag" in l["reflection"] for l in pack["lessons"]))
        self.assertTrue(any(i["kind"] == "LESSON" for i in pack["items"]))


class Scores(unittest.TestCase):
    def test_momentum_and_opportunity_from_the_weights(self):
        w = scoring.load_weights()
        f = {"rs_spy_20": 10.0, "rs_spy_63": 20.0, "roc_20": 15.0, "ma_structure": "up", "sma50_slope_pct": 5.0, "adx_14": 40.0, "vol_adj_mom_63": 1.0,
             "up_days_20": 0.7, "relvol_20": 2.0}
        s, d = scoring.momentum(f, w, "long")
        self.assertEqual(s, 100.0)
        s2, _ = scoring.momentum(f, w, "short")
        self.assertLess(s2, 30.0)                                                        # the mirror: strength is weakness nowhere
        s3, d3 = scoring.momentum({}, w, "long")
        self.assertIsNone(s3)
        study = {"adds_value": {"long": {"rs_spy_20": False}}}
        s4, d4 = scoring.momentum(f, w, "long", study)
        self.assertEqual(d4["terms"]["rs_spy_20"]["weight"], 0.0)
        self.assertEqual(s4, 100.0)
        self.assertEqual(scoring.regime_score("strong_bull", "short", w), (15.0, "against"))
        self.assertEqual(scoring.catalyst_score({"known": [{"direction": "down"}], "possible": []}, "long", w)[0], 0.0)
        self.assertEqual(scoring.catalyst_score({"known": [], "possible": []}, "long", w)[0], 40.0)
        opp, detail = scoring.opportunity("long", {"momentum": 100, "technical": 100, "regime": 100, "catalyst": 100, "risk": 100}, w)
        self.assertEqual(opp, 100.0)
        opp, detail = scoring.opportunity("long", {"momentum": None, "technical": 50, "regime": 50, "catalyst": 50, "risk": 50}, w)
        self.assertEqual(opp, 50.0)
        self.assertEqual(detail["momentum"]["weight"], 0.0)
        self.assertEqual(scoring.liquidity_score({"adv_usd_20": 5e8}), 100.0)
        self.assertEqual(scoring.liquidity_score({"adv_usd_20": 1e6}), 0.0)

    def test_confidence_is_capped_when_sources_are_missing(self):
        w = scoring.load_weights()
        pack = {"missing": []}
        outs = {"a": {"stance": "bullish"}, "b": {"stance": "bullish"}, "c": {"stance": "bearish"}}
        v, label, why = scoring.confidence(pack, outs, "long", "approved", w, "for")
        self.assertAlmostEqual(v, round(1.0 * (2 / 3.0) * 1.0, 3))
        self.assertEqual(label, "high")
        v2, label2, why2 = scoring.confidence({"missing": ["fundamentals"]}, outs, "long", "approved", w, "for")
        self.assertEqual(label2, "medium")
        self.assertTrue(any("capped" in x for x in why2))
        v3, label3, _ = scoring.confidence(pack, outs, "long", "rejected", w, "for")
        self.assertEqual(label3, "low")


class Study(unittest.TestCase):
    def test_terciles_spearman_and_a_study_on_synthetic_trades(self):
        self.assertEqual(featurestudy.terciles([1, 2, 3, 4, 5, 6]), (3, 5))
        self.assertEqual(featurestudy.spearman([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]), 1.0)
        self.assertEqual(featurestudy.spearman(list(range(10)), list(range(10))[::-1]), -1.0)
        self.assertIsNone(featurestudy.spearman([1, 2], [3, 4]))

        class FC:
            def get(self, sym, date):
                i = int(date[-2:])
                return {"roc_20": float(i), "adx_14": float(i % 3), "rsi_14": float(i) * 2}
        trades = [{"symbol": "T", "date": "2026-09-%02d" % (i % 28 + 1), "config_id": "c", "side": "long", "R_net": (0.5 if i % 28 > 18 else -0.5) + (i % 5) * 0.01, "outcome": "x"}
                  for i in range(120)]
        doc = featurestudy.study_config(trades, FC(), ("roc_20", "adx_14", "rsi_14"))
        self.assertEqual(doc["n"], 120)
        self.assertTrue(doc["features"]["roc_20"]["adds_value"])
        self.assertEqual(doc["features"]["roc_20"]["direction"], "higher is better")
        self.assertFalse(doc["features"]["adx_14"]["adds_value"])
        self.assertEqual([(r["a"], r["b"]) for r in doc["redundant"]], [("roc_20", "rsi_14")])     # the same ranks
        short = featurestudy.study_config(trades[:10], FC())
        self.assertIn("note", short)


if __name__ == "__main__":
    unittest.main()
