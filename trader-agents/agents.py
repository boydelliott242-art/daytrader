#!/usr/bin/env python3
"""The research desk (trader-agents): the TradingAgents roles, run by Claude inside Claude Code, over the desk's evidence.

    python3 agents.py prepare SYM --date D --at 10:15 --depth standard   # the evidence pack + the first prompts
    python3 agents.py next SYM --date D                                  # validate what is written, prompt the next roles
    python3 agents.py validate SYM --date D [--step bull_1]              # the problems, step by step
    python3 agents.py decide SYM --date D [--write]                      # the scores and the card (card.json, card.md), logged
    python3 agents.py card SYM --date D                                  # print the card again
    python3 agents.py status SYM --date D
    python3 agents.py rank [--date D] [--at 10:15] [--top 10] [--write] [--publish]   # the code-only rankings over the universe
    python3 agents.py settle [--write]                                   # settle pending cards (scorecard grades, daily closes)
    python3 agents.py reflect CARD_ID [--apply]                          # the reflection prompt; --apply stores outputs/reflect
    python3 agents.py weights-test [--config ID ...]                     # the momentum score by quintile on the lab's OOS trades
    python3 agents.py doctor                                             # what is present: python, venv, keys by name
    python3 agents.py run SYM --date D --engine vendored                 # the vendored TradingAgents graph (needs python 3.11+ and a key)

Every file lives under trader/research/<date>/<SYM>/. Nothing here calls a model or a network; Claude reads a prompt and writes
a JSON, the validator checks it against the pack, and the card is computed. Nothing places, sizes or approves an order.
"""

import argparse
import datetime
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from dt import agents, evidence, scoring, clock, io as dtio, bars as dtbars, setups, regime as dtregime, risk as dtrisk  # noqa: E402
from dt import catalysts as dtcatalysts, features as dtfeatures, intel_flags, featurestudy  # noqa: E402

NOTE = ("A card matched rules with a measured record and was read by the desk's roles; it is not a prediction and not a buy. "
        "Most people who day trade lose money.")


class Error(Exception):
    pass


def say(msg):
    print(msg)


def _rd(a):
    return agents.run_dir(a.root, a.date, a.symbol)


def _depth_of(rd, a):
    meta = dtio.read_json(os.path.join(rd, "run.json"), None) or {}
    return getattr(a, "depth", None) or meta.get("depth") or "standard"


def cmd_prepare(a):
    sym = a.symbol.upper()
    doc = evidence.build(sym, a.date, a.at, a.root, a.cache_root, a.depth, clock.parse_now(a.now) if a.now else None)
    rd = agents.run_dir(a.root, a.date, sym)
    dtio.write_json(os.path.join(rd, "run.json"), {"symbol": sym, "date": a.date, "cutoff": a.at, "depth": a.depth, "created_by": "trader-agents",
                                                   "prepared_at": clock.iso(clock.utc_now()), "engine": "claude"})
    say("Evidence pack: %s (%d items; missing: %s)" % (doc["path"], len(doc["items"]), ", ".join(doc["missing"]) or "nothing"))
    return cmd_next(a)


def cmd_next(a):
    rd = _rd(a)
    if not os.path.exists(os.path.join(rd, "evidence.json")):
        raise Error("no run for %s on %s: agents.py prepare first" % (a.symbol, a.date))
    depth = _depth_of(rd, a)
    state, done, pack = agents.status(rd, depth)
    plan = agents.plan_for(depth)
    ready, rejected = [], []
    for s in state:
        if s["state"] == "ready":
            spec = next(x for x in plan if x["step"] == s["step"])
            path = agents.write_prompt(rd, spec, pack, done, depth)
            ready.append((s["step"], path))
        elif s["state"] == "rejected":
            rejected.append(s)
    say("Run %s %s, depth %s: %d of %d steps done." % (pack["symbol"], pack["date"], depth, len(done), len(state)))
    for s in rejected:
        say("REJECTED %s:" % s["step"])
        for p in s["problems"]:
            say("  - %s" % p)
        say("  rewrite %s and run next again" % agents.output_path(rd, s["step"]))
    if ready:
        say("Ready now (read each prompt, write its JSON, then run next again):")
        for step, path in ready:
            say("  %s: %s -> %s" % (step, path, agents.output_path(rd, step)))
    waiting = [s["step"] for s in state if s["state"] == "waiting"]
    if waiting:
        say("Waiting on earlier steps: %s" % ", ".join(waiting))
    if len(done) == len(state):
        say("Every step validated: run agents.py decide %s --date %s --write" % (pack["symbol"], pack["date"]))
    return 0 if not rejected else 1


def cmd_validate(a):
    rd = _rd(a)
    depth = _depth_of(rd, a)
    state, done, pack = agents.status(rd, depth)
    bad = 0
    for s in state:
        if a.step and s["step"] != a.step:
            continue
        say("%-20s %s%s" % (s["step"], s["state"], (": " + "; ".join(s["problems"])) if s["problems"] else ""))
        bad += 1 if s["state"] == "rejected" else 0
    return 1 if bad else 0


def cmd_decide(a):
    rd = _rd(a)
    depth = _depth_of(rd, a)
    study = dtio.read_json(os.path.join(a.root, "trader", "lab", "features.json"), None)
    wtest = dtio.read_json(os.path.join(a.root, "trader", "lab", "weights_test.json"), None)
    card, state, pack = agents.decide(rd, depth, scoring.load_weights(a.weights), study, clock.parse_now(a.now) if a.now else None, wtest)
    md = agents.card_markdown(card, pack)
    if a.write:
        dtio.write_json(os.path.join(rd, "card.json"), card)
        with open(os.path.join(rd, "card.md"), "w", encoding="utf-8") as fh:
            fh.write(md)
        logged = agents.log_card(a.root, card)
        say("Wrote %s and card.md; memory: %s." % (os.path.join(rd, "card.json"), {"logged": "logged as pending", "replaced": "the pending row replaced",
                                                                                   "settled": "already settled, left alone"}[logged]))
        if a.publish:
            _publish_latest(a.root, a.publish_dir)
    if a.json:
        print(json.dumps(card, indent=2))
    else:
        print(md)
    return 0 if not card["reasons"] or card["no_trade"] else 1


def cmd_card(a):
    rd = _rd(a)
    path = os.path.join(rd, "card.md")
    if not os.path.exists(path):
        raise Error("no card at %s: agents.py decide --write first" % path)
    with open(path, encoding="utf-8") as fh:
        print(fh.read())
    return 0


def cmd_status(a):
    return cmd_validate(a)


def _publish_latest(root, publish_dir):
    """trader/research/latest.json: the newest card per symbol (the site's panel)."""
    cards = []
    for path in glob.glob(os.path.join(root, "trader", "research", "*", "*", "card.json")):
        c = dtio.read_json(path, None)
        if isinstance(c, dict) and c.get("symbol"):
            cards.append({"symbol": c["symbol"], "date": c["date"], "cutoff": c.get("cutoff"), "depth": c.get("depth"), "rating": c.get("rating"),
                          "side": c.get("side"), "no_trade": c.get("no_trade"), "horizon": c.get("horizon"), "confidence": c.get("confidence"),
                          "thesis": (c.get("thesis") or "")[:400], "invalidation": c.get("invalidation"), "key_risks": (c.get("key_risks") or [])[:4],
                          "long_score": ((c.get("scores") or {}).get("sides") or {}).get("long", {}).get("opportunity"),
                          "short_score": ((c.get("scores") or {}).get("sides") or {}).get("short", {}).get("opportunity"),
                          "risk": {s: ((c.get("risk") or {}).get(s) or {}).get("verdict") for s in ("long", "short")},
                          "regime": (c.get("regime") or {}).get("class"), "missing": c.get("missing"), "built": c.get("built")})
    cards.sort(key=lambda c: (c["date"], c["symbol"]), reverse=True)
    latest = {}
    for c in cards:
        latest.setdefault(c["symbol"], c)
    doc = {"created_by": "trader-agents", "as_of": clock.iso(clock.utc_now()), "cards": sorted(latest.values(), key=lambda c: (c["date"], c["symbol"]), reverse=True),
           "n": len(latest), "note": NOTE}
    dtio.write_json(os.path.join(root, "trader", "research", "latest.json"), doc)
    dtio.write_json(os.path.join(publish_dir, "research.json"), doc)
    say("Published %s (%d cards)." % (os.path.join(publish_dir, "research.json"), len(latest)))


# ---------------------------------------------------------------- rank
def cmd_rank(a):
    root, cache = a.root, a.cache_root or os.path.join(a.root, "trader", "cache")
    weights = scoring.load_weights(a.weights)
    study = dtio.read_json(os.path.join(root, "trader", "lab", "features.json"), None)
    wtest = dtio.read_json(os.path.join(root, "trader", "lab", "weights_test.json"), None)
    date = a.date or clock.to_eastern(clock.utc_now()).date().isoformat()
    largecap = dtio.read_json(os.path.join(root, "trader", "largecap.json"), None) or {}
    names = [r["symbol"] for r in largecap.get("symbols") or [] if isinstance(r, dict) and r.get("symbol")]
    if not names:
        names = sorted(os.path.splitext(os.path.splitext(f)[0])[0] for f in os.listdir(os.path.join(cache, "bars5m")) if f.endswith(".json.gz"))
    if a.symbols:
        names = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    paths = evidence.Paths(root, cache)
    rows = []
    t0 = datetime.datetime.now()
    for i, sym in enumerate(names, 1):
        try:
            pack = evidence.build(sym, date, a.at, root, cache, "fast", None, paths, write=False)
        except Exception as e:  # one broken store must not end the ranking
            continue
        sc = scoring.score_pack(pack, None, weights, study, wtest)
        for side in ("long", "short"):
            s = sc["sides"][side]
            best = (pack["setups"].get(side) or [None])[0]
            rows.append({"symbol": sym, "side": side, "score": s["opportunity"], "momentum": s.get("momentum", s.get("weakness")), "momentum_raw": s.get("momentum_raw"),
                         "technical": s["technical"],
                         "regime": s["regime"], "regime_fit": s["regime_fit"], "catalyst": s["catalyst"], "risk": s["risk"], "verdict": s["risk_verdict"],
                         "confidence": s["confidence_label"], "config_id": s["best_config"], "lab_status": s["lab_status"], "late_by": (best or {}).get("late_by"),
                         "fired": bool(best), "mtf": (pack.get("features") or {}).get("mtf_agreement"), "reasons": ((pack.get("risk") or {}).get(side) or {}).get("reasons", [])[:2],
                         "missing": pack.get("missing")})
        if not a.quiet and (i % 100 == 0 or i == len(names)):
            print("  %d/%d symbols, %ds" % (i, len(names), (datetime.datetime.now() - t0).seconds), file=sys.stderr)
    top = a.top

    def take(side, key, cond=lambda r: True, reverse=True):
        cand = [r for r in rows if r["side"] == side and r.get(key) is not None and cond(r)]
        cand.sort(key=lambda r: (-(r[key] or 0) if reverse else (r[key] or 0), r["symbol"]))
        return cand[:top]
    fired_ok = lambda r: r["fired"] and r["verdict"] != "do_not_trade"
    lists = {
        "top_momentum_longs": take("long", "momentum_raw", lambda r: r["verdict"] != "do_not_trade"),
        "top_short_candidates": take("short", "score", fired_ok),
        "top_high_conviction": take("long", "score", lambda r: fired_ok(r) and r["lab_status"] in ("approved", "watch") and r["confidence"] != "low") +
                               take("short", "score", lambda r: fired_ok(r) and r["lab_status"] in ("approved", "watch") and r["confidence"] != "low"),
        "top_risk_adjusted": sorted([r for r in rows if fired_ok(r) and r["score"] is not None and r["risk"] is not None],
                                    key=lambda r: -(r["score"] * (r["risk"] / 100.0)))[:top],
        "top_contrarian": [r for r in rows if r["fired"] and r["verdict"] != "do_not_trade" and r["mtf"] in ("short_term_up_long_term_down", "short_term_down_long_term_up")
                           and r["regime_fit"] == "for"][:top],
        "most_dangerous_shorts": take("short", "score", lambda r: r["fired"] and r["verdict"] == "do_not_trade"),
        "watchlist": take("long", "momentum_raw", lambda r: not r["fired"]) + take("short", "momentum_raw", lambda r: not r["fired"]),
    }
    gated_note = ("the momentum list ranks by the RAW momentum score (every term); the opportunity scores use only the terms the feature study "
                  "found to add value on that side" if study else "the feature study has not run: every momentum term carries its default weight, untested")
    edge = dtio.read_json(os.path.join(root, "trader", "lab", "edge.json"), None) or {}
    doc = {"created_by": "trader-agents", "as_of": clock.iso(clock.utc_now()), "date": date, "cutoff": a.at, "universe": len(names), "scored": len(rows) // 2,
           "configs_tested": edge.get("configs_tested"), "lab_sessions": edge.get("sessions"), "weights_tested": bool(study), "momentum_note": gated_note,
           "weights_verdict": scoring.weights_verdict(wtest), "lists": lists,
           "no_trade": {k: (not v) for k, v in lists.items()},
           "note": "Rankings from the code-only layers (features, the fired rules, the lab's record, the risk block, the catalysts); the roles run on the "
                   "top names only. An empty list is NO TRADE for that list. " + NOTE}
    if a.write:
        dtio.write_json(os.path.join(root, "trader", "rankings.json"), doc)
        say("Wrote trader/rankings.json")
        if a.publish:
            dtio.write_json(os.path.join(a.publish_dir, "rankings.json"), doc)
            say("Published %s" % os.path.join(a.publish_dir, "rankings.json"))
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("Rankings for %s at %s ET over %d symbols (%s configs tested in the lab; weights %s)." % (
        date, a.at, len(names), edge.get("configs_tested", "?"), "gated by the feature study" if study else "UNTESTED: the feature study has not run"))
    print(gated_note + ".")
    print(scoring.weights_verdict(wtest) + ".")
    for name, lst in lists.items():
        print("\n%s%s" % (name.replace("_", " ").upper(), "" if lst else ": NO TRADE (nothing cleared the bar)"))
        for r in lst:
            print("  %-6s %-5s score %5s  mom %5s (raw %5s) tech %5s regime %5s risk %5s  %-12s %-7s %s%s" % (
                r["symbol"], r["side"], r["score"], r["momentum"], r["momentum_raw"], r["technical"], r["regime"], r["risk"], r["verdict"] or "", r["confidence"] or "",
                r["config_id"] or "no rule", (" late %d" % r["late_by"]) if (r["late_by"] or 0) > 1 else ""))
    print("\n" + doc["note"])
    return 0


# ---------------------------------------------------------------- settle, reflect
def cmd_settle(a):
    settled, waiting, failed = agents.settle(a.root, a.cache_root or os.path.join(a.root, "trader", "cache"), None, a.holding_days)
    say("Settled %d card(s): %s" % (len(settled), ", ".join(settled) or "none"))
    say("Waiting (window not traded or not graded yet): %d" % len(waiting))
    if settled:
        say("Next: agents.py reflect <id> for each settled card, then write outputs/reflect.json and agents.py reflect <id> --apply")
    return 0


def cmd_reflect(a):
    path = agents.memory_path(a.root)
    rows = [r for r in dtio.read_jsonl(path)] if os.path.exists(path) else []
    row = next((r for r in rows if isinstance(r, dict) and r.get("id") == a.card_id), None)
    if not row:
        raise Error("no card %s in %s" % (a.card_id, path))
    if row.get("status") != "settled":
        raise Error("%s is not settled yet (agents.py settle)" % a.card_id)
    date, sym = row["date"], row["symbol"]
    rd = agents.run_dir(a.root, date, sym)
    out_path = os.path.join(rd, "outputs", "reflect.json")
    if a.apply:
        doc = dtio.read_json(out_path, None)
        if not isinstance(doc, dict) or not doc.get("reflection") or doc.get("id") != a.card_id:
            raise Error("%s needs {\"id\": %r, \"reflection\": \"2 to 4 sentences\"}" % (out_path, a.card_id))
        text = str(doc["reflection"]).strip()
        n = len([s for s in text.replace("!", ".").replace("?", ".").split(".") if s.strip()])
        if not 2 <= n <= 5:
            raise Error("the reflection should be 2 to 4 sentences (got %d)" % n)
        agents.store_reflection(a.root, a.card_id, text)
        say("Stored the reflection on %s." % a.card_id)
        return 0
    lines = ["# Reflection on %s" % a.card_id, "", "Write the JSON to `%s`, then run `python3 trader-agents/agents.py reflect %s --apply`." % (out_path, a.card_id), "",
             agents.role_text("reflect"), "", "## The card", "",
             "- rating %s, side %s, horizon %s, confidence %s" % (row.get("rating"), row.get("side"), row.get("horizon"), row.get("confidence")),
             "- setup %s: entry %s, stop %s, target %s at %s" % (row.get("config_id"), row.get("entry"), row.get("stop"), row.get("target"), row.get("entry_time")),
             "- thesis: %s" % row.get("thesis"), "",
             "## The outcome", "",
             "- %s; raw %s; alpha vs SPY %s; resolved %s; settled by %s" % (row.get("outcome"), row.get("raw_return"), row.get("alpha"), row.get("resolved"), row.get("settled_by")), ""]
    cm = os.path.join(rd, "card.md")
    if os.path.exists(cm):
        with open(cm, encoding="utf-8") as fh:
            lines += ["## The full card", "", fh.read()]
    p = os.path.join(rd, "prompts", "reflect.md")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    say("Prompt: %s -> %s" % (p, out_path))
    return 0


# ---------------------------------------------------------------- weights-test, doctor, run
def cmd_weights_test(a):
    edge = dtio.read_json(os.path.join(a.root, "trader", "lab", "edge.json"), None)
    trades = os.path.join(a.root, "trader", "lab", "trades.jsonl.gz")
    if not edge or not os.path.exists(trades):
        raise Error("the lab has not run (trader/lab/edge.json and trades.jsonl.gz): lab.py run first")
    doc = featurestudy.weights_test(trades, a.cache_root or os.path.join(a.root, "trader", "cache"), edge, scoring.load_weights(a.weights), a.config or None)
    if a.write:
        dtio.write_json(os.path.join(a.root, "trader", "lab", "weights_test.json"), doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("Momentum score by quintile over %d OOS trades of %s (configs tested in the lab: %s):" % (doc["n"], ", ".join(doc["configs"]), edge.get("configs_tested")))
    for q in doc.get("by_quintile") or []:
        print("  Q%d score %5.1f to %5.1f  n %5d  exp %+.3fR [%+.3f, %+.3f]  win %s %%" % (
            q["quintile"], q["score_min"], q["score_max"], q["n"], q["exp_R"], q["exp_ci95"][0], q["exp_ci95"][1], round(q["win_rate"] * 100, 1)))
    print(doc.get("verdict") or doc["note"])
    print(doc["note"])
    return 0


def cmd_doctor(a):
    venv = os.path.join(ROOT, "vendor", "TradingAgents", ".venv", "bin", "python")
    print("vendor/TradingAgents: %s" % ("present" if os.path.exists(os.path.join(ROOT, "vendor", "TradingAgents", "pyproject.toml")) else "MISSING"))
    pys = []
    for c in ("python3.13", "python3.12", "python3.11"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if os.path.exists(os.path.join(d, c)):
                pys.append(os.path.join(d, c))
    print("python 3.11+: %s" % (", ".join(pys) or "none on the PATH (scripts/install_tradingagents.sh says how)"))
    print("venv: %s" % (venv if os.path.exists(venv) else "not built (scripts/install_tradingagents.sh)"))
    keys = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
                        "MISTRAL_API_KEY", "FRED_API_KEY", "ALPHA_VANTAGE_API_KEY") if os.environ.get(k)]
    print("provider keys in the environment (names only): %s" % (", ".join(keys) or "none"))
    env = os.path.join(ROOT, "vendor", "TradingAgents", ".env")
    print(".env: %s" % ("present (not read here)" if os.path.exists(env) else "none"))
    home = os.path.expanduser("~/.tradingagents")
    print("~/.tradingagents: %s" % ("present" if os.path.isdir(home) else "none"))
    print("the desk's own roles need none of this: agents.py prepare runs inside Claude Code.")
    return 0


def cmd_run(a):
    if a.engine != "vendored":
        say("engine claude: agents.py prepare %s --date %s --depth %s (then write each role's JSON and run next)" % (a.symbol, a.date, a.depth or "standard"))
        return cmd_prepare(a)
    venv = os.path.join(ROOT, "vendor", "TradingAgents", ".venv", "bin", "python")
    if not os.path.exists(venv):
        say("the vendored engine is not installed: scripts/install_tradingagents.sh (needs Python 3.11+ and a provider key). Nothing ran.")
        return 2
    rd = agents.run_dir(a.root, a.date, a.symbol.upper())
    out = os.path.join(rd, "outputs", "vendored.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd = [venv, os.path.join(HERE, "vendored_driver.py"), a.symbol.upper(), a.date, out, "--reports-dir", os.path.join(rd, "vendored")]
    r = subprocess.run(cmd, cwd=os.path.join(ROOT, "vendor", "TradingAgents"), capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        return r.returncode
    doc = dtio.read_json(out, None) or {}
    pack_path = os.path.join(rd, "evidence.json")
    if os.path.exists(pack_path):
        pack = evidence.load(pack_path)
        items = evidence.Items()
        items.items = pack["items"]
        for name, text in (doc.get("reports") or {}).items():
            if text:
                items.add("INTERPRETATION", "tradingagents v0.7.0", "vendored %s report: %s" % (name, text[:1500]), None, a.date)
        if doc.get("decision"):
            items.add("INTERPRETATION", "tradingagents v0.7.0", "vendored decision (%s): %s" % (doc.get("rating"), doc["decision"][:1500]), None, a.date)
        pack["items"] = items.items
        pack["vendored"] = {"rating": doc.get("rating"), "engine": doc.get("engine"), "settings": doc.get("settings")}
        dtio.write_json(pack_path, pack)
        say("Added the vendored reports to the evidence pack as INTERPRETATION items (the desk's roles may cite them; they are a model's words, not data).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    def common(p, symbol=True):
        if symbol:
            p.add_argument("symbol")
        p.add_argument("--date", default=clock.to_eastern(clock.utc_now()).date().isoformat())
        p.add_argument("--root", default=".")
        p.add_argument("--cache-root")
        p.add_argument("--now")
    p = sub.add_parser("prepare"); common(p)
    p.add_argument("--at", default="10:15"); p.add_argument("--depth", default="standard", choices=agents.DEPTH)
    p = sub.add_parser("next"); common(p); p.add_argument("--depth")
    p = sub.add_parser("validate"); common(p); p.add_argument("--step"); p.add_argument("--depth")
    p = sub.add_parser("status"); common(p); p.add_argument("--step"); p.add_argument("--depth")
    p = sub.add_parser("decide"); common(p); p.add_argument("--depth"); p.add_argument("--write", action="store_true")
    p.add_argument("--publish", action="store_true"); p.add_argument("--publish-dir", default=os.path.join("trader", "publish"))
    p.add_argument("--weights"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("card"); common(p)
    p = sub.add_parser("rank"); common(p, symbol=False); p.add_argument("--at", default="10:15"); p.add_argument("--top", type=int, default=10)
    p.add_argument("--symbols"); p.add_argument("--weights"); p.add_argument("--write", action="store_true"); p.add_argument("--publish", action="store_true")
    p.add_argument("--publish-dir", default=os.path.join("trader", "publish")); p.add_argument("--json", action="store_true"); p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("settle"); p.add_argument("--root", default="."); p.add_argument("--cache-root"); p.add_argument("--holding-days", type=int, default=5)
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("reflect"); p.add_argument("card_id"); p.add_argument("--root", default="."); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("weights-test"); p.add_argument("--root", default="."); p.add_argument("--cache-root"); p.add_argument("--weights")
    p.add_argument("--config", action="append"); p.add_argument("--write", action="store_true"); p.add_argument("--json", action="store_true")
    sub.add_parser("doctor")
    p = sub.add_parser("run"); common(p); p.add_argument("--engine", default="claude", choices=["claude", "vendored"]); p.add_argument("--at", default="10:15")
    p.add_argument("--depth", default="standard", choices=agents.DEPTH)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"prepare": cmd_prepare, "next": cmd_next, "validate": cmd_validate, "status": cmd_status, "decide": cmd_decide, "card": cmd_card,
                "rank": cmd_rank, "settle": cmd_settle, "reflect": cmd_reflect, "weights-test": cmd_weights_test, "doctor": cmd_doctor, "run": cmd_run}[a.cmd](a)
    except (Error, agents.AgentsError, evidence.EvidenceError, dtio.IOError_) as e:
        print("agents.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
