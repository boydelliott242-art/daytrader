"""The research desk's roles, prompts, validator, decision and memory (CONTRACTS.md, "The research desk").

The roles and the debate order are adapted from Tauric Research's TradingAgents v0.7.0 (vendor/TradingAgents,
Apache-2.0): the four analysts, the bull and bear researchers, the research manager, the trader, the three risk
debaters and the portfolio manager; plus the momentum and regime analysts, the contrarian and the code-fed risk
analyst that the desk adds. Claude plays each role inside Claude Code: this module writes the prompt (the role file
plus the evidence pack plus the prior outputs), reads the JSON the role wrote, validates it against the pack, and
when every role of the plan validates, computes the scores and the card. Nothing here calls a model.
"""

import datetime
import json
import os
import re

from . import io as dtio, clock, scoring, evidence as dtevidence, regimes

ROLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-agents", "roles")
KINDS = ("DATA", "SIGNAL", "STAT", "INTERPRETATION", "ASSUMPTION", "UNCERTAINTY")
CITED_KINDS = ("DATA", "SIGNAL", "STAT")
CONFIDENCE = ("low", "medium", "high")
STANCES = ("bullish", "mildly_bullish", "neutral", "mixed", "mildly_bearish", "bearish")
RATINGS = ("buy", "overweight", "hold", "underweight", "sell")
ACTIONS = ("long", "short", "hold")
RISK_STANCES = ("take", "reduce", "avoid")
REVIEW = "REVIEW"

# role: (team, stance enum, core sources in the pack, extra required keys)
ROLES = {
    "market":            ("analysts", STANCES, ("bars", "session"), ()),
    "momentum":          ("analysts", STANCES, ("bars",), ()),
    "fundamentals":      ("analysts", STANCES, ("fundamentals",), ()),
    "news":              ("analysts", STANCES, ("news", "intel"), ()),
    "sentiment":         ("analysts", STANCES, ("social",), ()),
    "regime":            ("analysts", STANCES, ("regime",), ()),
    "bull":              ("research", STANCES, (), ("strength",)),
    "bear":              ("research", STANCES, (), ("strength",)),
    "contrarian":        ("research", STANCES, (), ("attacks",)),
    "research_manager":  ("manager", RATINGS, (), ("recommendation", "side_preference", "plan")),
    "trader":            ("trader", ACTIONS, (), ("action", "entry", "stop", "target", "size_note")),
    "risk_aggressive":   ("risk", RISK_STANCES, (), ("adjustments",)),
    "risk_conservative": ("risk", RISK_STANCES, (), ("adjustments",)),
    "risk_neutral":      ("risk", RISK_STANCES, (), ("adjustments",)),
    "risk_analyst":      ("risk", RISK_STANCES, ("short_interest",), ("verdict_long", "verdict_short")),
    "portfolio_manager": ("decision", RATINGS, (), ("rating", "side", "horizon", "thesis", "invalidation", "key_risks", "no_trade")),
}
STANCE_KEY = {"research_manager": "recommendation", "trader": "action", "portfolio_manager": "rating"}

DEPTH = {
    "fast":     {"analysts": ["market", "momentum", "regime"], "debate_rounds": 1, "contrarian": False, "risk_rounds": 0},
    "standard": {"analysts": ["market", "momentum", "fundamentals", "news", "sentiment", "regime"], "debate_rounds": 1, "contrarian": False, "risk_rounds": 1},
    "deep":     {"analysts": ["market", "momentum", "fundamentals", "news", "sentiment", "regime"], "debate_rounds": 2, "contrarian": True, "risk_rounds": 2},
    "maximum":  {"analysts": ["market", "momentum", "fundamentals", "news", "sentiment", "regime"], "debate_rounds": 3, "contrarian": True, "risk_rounds": 3},
}
NUMBER_RE = re.compile(r"(?<![\w.:-])(-?\d+(?:,\d{3})*(?:\.\d+)?)(\s*%)?(?![\w:-])")


class AgentsError(Exception):
    pass


# ------------------------------------------------------------------------------------------- the plan
def plan_for(depth):
    """The ordered steps of a run: [{"step", "role", "round", "needs": [steps], "team"}]."""
    d = DEPTH[depth]
    steps = []

    def add(role, rnd, needs):
        step = "%s_%d" % (role, rnd) if role in ("bull", "bear", "risk_aggressive", "risk_conservative", "risk_neutral") else role
        steps.append({"step": step, "role": role, "round": rnd, "needs": list(needs), "team": ROLES[role][0]})
        return step
    analysts = [add(r, 1, []) for r in d["analysts"]]
    last = None
    debate = []
    for rnd in range(1, d["debate_rounds"] + 1):
        last = add("bull", rnd, analysts + ([last] if last else []))
        debate.append(last)
        last = add("bear", rnd, analysts + [last])
        debate.append(last)
    if d["contrarian"]:
        last = add("contrarian", 1, analysts + debate)
    mgr = add("research_manager", 1, analysts + debate + ([last] if d["contrarian"] else []))
    trd = add("trader", 1, [mgr, "market"] if "market" in d["analysts"] else [mgr])
    risk_prev = [trd]
    for rnd in range(1, d["risk_rounds"] + 1):
        a = add("risk_aggressive", rnd, risk_prev)
        c = add("risk_conservative", rnd, risk_prev + [a])
        n = add("risk_neutral", rnd, risk_prev + [a, c])
        risk_prev = [trd, a, c, n]
    ra = add("risk_analyst", 1, [trd])
    add("portfolio_manager", 1, risk_prev + [ra, mgr])
    return steps


def step_role(step):
    m = re.match(r"^(.*?)(?:_(\d+))?$", step)
    role = m.group(1) if m else step
    if role not in ROLES and "_" in step:
        role = step.rsplit("_", 1)[0]
    return role


# ------------------------------------------------------------------------------------------- the files of a run
def run_dir(root, date, sym):
    return os.path.join(root, "trader", "research", date, sym.upper())


def output_path(rd, step):
    return os.path.join(rd, "outputs", step + ".json")


def prompt_path(rd, step):
    return os.path.join(rd, "prompts", step + ".md")


def read_output(rd, step):
    path = output_path(rd, step)
    if not os.path.exists(path):
        return None
    try:
        return dtio.read_json(path, None)
    except dtio.IOError_ as e:
        return {"_error": str(e)}


def status(rd, depth):
    """Which steps are done (validated), ready (needs met), waiting or rejected."""
    steps = plan_for(depth)
    pack = dtevidence.load(os.path.join(rd, "evidence.json"))
    done, state = {}, []
    for s in steps:
        out = read_output(rd, s["step"])
        problems = validate_output(s["role"], s["round"], out, pack, rd, s) if out is not None else None
        ok = out is not None and not problems
        if ok:
            done[s["step"]] = out
        state.append({"step": s["step"], "role": s["role"], "round": s["round"], "team": s["team"],
                      "state": "done" if ok else "rejected" if out is not None else "ready" if all(n in done for n in s["needs"]) else "waiting",
                      "problems": problems or [], "needs": s["needs"]})
    return state, done, pack


# ------------------------------------------------------------------------------------------- prompts
def role_text(role):
    path = os.path.join(ROLES_DIR, role + ".md")
    if not os.path.exists(path):
        raise AgentsError("no role file %s" % path)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def shared_text():
    with open(os.path.join(ROLES_DIR, "_shared.md"), encoding="utf-8") as fh:
        return fh.read()


def pack_lines(pack, kinds=None, limit=None):
    lines = []
    for it in pack.get("items") or []:
        if kinds and it["kind"] not in kinds:
            continue
        lines.append("- [%s] %s (%s; %s)" % (it["id"], it["text"], it["kind"], it.get("source") or "?"))
    if limit and len(lines) > limit:
        lines = lines[:limit] + ["- ... %d more items in evidence.json" % (len(lines) - limit)]
    return lines


def prior_lines(rd, steps_done, needs):
    lines = []
    for step in needs:
        out = steps_done.get(step)
        if not out:
            continue
        lines.append("### %s (round %s): stance %s, confidence %s" % (out.get("role"), out.get("round"), out.get(STANCE_KEY.get(out.get("role"), "stance")), out.get("confidence")))
        lines.append("summary: %s" % out.get("summary"))
        for c in (out.get("claims") or [])[:12]:
            lines.append("- %s [%s; %s]" % (c.get("text"), c.get("kind"), ", ".join(c.get("evidence") or []) or "no evidence"))
        for r in (out.get("rebuttals") or [])[:6]:
            lines.append("- rebuttal to %s: %s [%s]" % (r.get("to"), r.get("text"), ", ".join(r.get("evidence") or []) or "no evidence"))
        if out.get("scores"):
            lines.append("- scores: %s" % json.dumps(out["scores"]))
        for key in ("recommendation", "side_preference", "plan", "action", "entry", "stop", "target", "size_note", "adjustments",
                    "verdict_long", "verdict_short", "rating", "side", "horizon", "attacks", "strength"):
            if key in out and out[key] not in (None, "", []):
                lines.append("- %s: %s" % (key, json.dumps(out[key]) if not isinstance(out[key], str) else out[key]))
        lines.append("")
    return lines


def write_prompt(rd, step_spec, pack, steps_done, depth):
    role, rnd, step = step_spec["role"], step_spec["round"], step_spec["step"]
    core = ROLES[role][2]
    missing_core = [m for m in (pack.get("missing") or []) if m in core]
    lines = ["# %s, round %d, for %s on %s (cutoff %s, depth %s)" % (role, rnd, pack["symbol"], pack["date"], pack.get("cutoff"), depth), "",
             "Write your answer as JSON to `%s`. Then run `python3 trader-agents/agents.py next %s --date %s`." % (
                 output_path(rd, step), pack["symbol"], pack["date"]), ""]
    lines += [shared_text(), ""]
    lines += [role_text(role), ""]
    lines += ["## Your output contract", "",
              "```json", json.dumps(output_skeleton(role, rnd, pack), indent=2), "```", ""]
    if missing_core:
        lines += ["## Sources missing for this role", "",
                  "The pack has no %s. Say so in `missing`, keep confidence at `low` or `medium`, and do not fill the gap from memory." % ", ".join(missing_core), ""]
    if step_spec["needs"]:
        lines += ["## What the earlier roles said (cite their steps by name; cite evidence by id)", ""]
        lines += prior_lines(rd, steps_done, step_spec["needs"])
    lines += ["## The evidence pack (%d items; the full JSON is %s)" % (len(pack.get("items") or []), os.path.join(rd, "evidence.json")), ""]
    lines += pack_lines(pack)
    lines += ["", "Missing from the pack: %s" % (", ".join(pack.get("missing") or []) or "nothing")]
    path = prompt_path(rd, step)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def output_skeleton(role, rnd, pack):
    team, enum, core, extra = ROLES[role]
    sk = {"role": role, "round": rnd, "symbol": pack["symbol"], "date": pack["date"], STANCE_KEY.get(role, "stance"): "<one of: %s>" % "|".join(enum),
          "confidence": "<low|medium|high>",
          "claims": [{"text": "<one sentence, with the exact numbers of the cited items>", "kind": "<%s>" % "|".join(KINDS), "evidence": ["E1"]}],
          "missing": ["<a source this role needed and the pack lacks, or nothing>"], "summary": "<two or three sentences>"}
    if team in ("research", "risk") and role not in ("risk_analyst",):
        sk["rebuttals"] = [{"to": "<a prior step name, e.g. bear_1>", "text": "<...>", "evidence": ["E1"]}]
    if role in ("bull", "bear"):
        sk["strength"] = "<1 to 5: how decisively your side's case stands after reading the other>"
    if role == "contrarian":
        sk["attacks"] = [{"target": "<bull_1|bear_1|consensus>", "text": "<the weakest assumption and why>", "evidence": ["E1"]}]
    if role in ("market", "momentum", "fundamentals", "news", "sentiment", "regime"):
        sk["scores"] = {"score": "<0 to 10, 5 neutral, with the band>", "data_quality": "<0 to 10>"}
    if role == "research_manager":
        sk.update({"side_preference": "<long|short|none>", "plan": "<the plan for the trader: which setup, which side, sizing against a standard unit, what would change the call>"})
    if role == "trader":
        sk.update({"action": "<long|short|hold>", "entry": "<absolute price from a cited SIGNAL item, or null>", "stop": "<absolute price, or null>",
                   "target": "<absolute price, or null>", "setup_config": "<the config id of the cited SIGNAL, or null>",
                   "size_note": "<sizing against a standard unit, in words; never a dollar figure the pack does not have>"})
    if role in ("risk_aggressive", "risk_conservative", "risk_neutral"):
        sk["adjustments"] = "<what to change in the trader's proposal, in words>"
    if role == "risk_analyst":
        sk.update({"verdict_long": "<ok|caution|do_not_trade>", "verdict_short": "<ok|caution|do_not_trade>", "adjustments": "<...>"})
    if role == "portfolio_manager":
        sk.update({"side": "<long|short|none>", "horizon": "<intraday|swing>", "thesis": "<the evidence that decided it>",
                   "invalidation": "<the price or event that proves it wrong>", "key_risks": ["<...>"], "no_trade": False,
                   "overrides_trader": False, "override_reason": "<why the rating disagrees with the trader's direction, when it does>"})
    return sk


# ------------------------------------------------------------------------------------------- the validator
def _checkable_numbers(text):
    out = []
    for m in NUMBER_RE.finditer(text or ""):
        raw, pct = m.group(1), m.group(2)
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if pct or "." in raw or abs(v) >= 1000:
            out.append(round(v, 2))
    return out


def _number_backed(n, pool):
    for m in pool:
        if abs(n - m) <= 0.011 or (m and abs(n - m) / abs(m) <= 0.01):
            return True
    return False


def validate_output(role, rnd, out, pack, rd=None, step_spec=None):
    """-> [] when the output is accepted, else the list of problems (each a plain sentence)."""
    p = []
    if out is None:
        return ["no output file"]
    if "_error" in out:
        return ["the output is not valid JSON: %s" % out["_error"]]
    if not isinstance(out, dict):
        return ["the output is not a JSON object"]
    team, enum, core, extra = ROLES[role]
    stance_key = STANCE_KEY.get(role, "stance")
    for key in ("role", "round", "symbol", "date", stance_key, "confidence", "claims", "summary", "missing") + tuple(extra):
        if key not in out:
            p.append("missing key %r" % key)
    if p:
        return p
    if out.get("role") != role:
        p.append("role is %r, expected %r" % (out.get("role"), role))
    if str(out.get("round")) != str(rnd):
        p.append("round is %r, expected %r" % (out.get("round"), rnd))
    if str(out.get("symbol") or "").upper() != pack["symbol"] or str(out.get("date")) != pack["date"]:
        p.append("symbol/date %s/%s do not match the pack %s/%s" % (out.get("symbol"), out.get("date"), pack["symbol"], pack["date"]))
    stance = out.get(stance_key)
    if role == "risk_analyst":
        for k in ("verdict_long", "verdict_short"):
            if out.get(k) not in ("ok", "caution", "do_not_trade"):
                p.append("%s must be ok, caution or do_not_trade" % k)
    if stance not in enum:
        p.append("%s %r is not one of %s" % (stance_key, stance, ", ".join(enum)))
    if out.get("confidence") not in CONFIDENCE:
        p.append("confidence must be low, medium or high")
    claims = out.get("claims")
    if not isinstance(claims, list) or not claims:
        p.append("claims must be a non-empty list")
        claims = []
    index = dtevidence.item_index(pack)
    for i, c in enumerate(claims, 1):
        if not isinstance(c, dict) or not c.get("text"):
            p.append("claim %d has no text" % i)
            continue
        kind = c.get("kind")
        if kind not in KINDS:
            p.append("claim %d: kind %r is not one of %s" % (i, kind, ", ".join(KINDS)))
            continue
        ev = c.get("evidence") or []
        bad = [e for e in ev if e not in index]
        if bad:
            p.append("claim %d cites %s, not in the pack" % (i, ", ".join(bad)))
        if kind in CITED_KINDS and not ev:
            p.append("claim %d is %s but cites no evidence id" % (i, kind))
        if kind in CITED_KINDS and ev and not bad:
            pool = set()
            for e in ev:
                pool |= dtevidence.numbers_in(index[e])
            for n in _checkable_numbers(c["text"]):
                if not _number_backed(n, pool):
                    p.append("claim %d quotes %s, which appears in none of its cited items (%s)" % (i, ("%g" % n), ", ".join(ev)))
    own_missing = [m for m in (out.get("missing") or []) if m and not str(m).startswith("<")]
    pack_missing_core = [m for m in (pack.get("missing") or []) if m in core]
    if out.get("confidence") == "high" and (pack_missing_core or own_missing):
        p.append("confidence high while sources are missing (%s)" % ", ".join(pack_missing_core + own_missing))
    if role in ("bull", "bear", "risk_aggressive", "risk_conservative", "risk_neutral") and rnd >= 2:
        if not out.get("rebuttals"):
            p.append("round %d needs rebuttals to the other side's last round" % rnd)
    for r in out.get("rebuttals") or []:
        if not isinstance(r, dict) or not r.get("text") or not r.get("to"):
            p.append("a rebuttal needs 'to' and 'text'")
            break
        bad = [e for e in (r.get("evidence") or []) if e not in index]
        if bad:
            p.append("a rebuttal cites %s, not in the pack" % ", ".join(bad))
    if role in ("bull", "bear"):
        try:
            if not 1 <= int(out.get("strength")) <= 5:
                p.append("strength must be 1 to 5")
        except (TypeError, ValueError):
            p.append("strength must be 1 to 5")
    if role == "contrarian" and not out.get("attacks"):
        p.append("the contrarian must name at least one attack")
    if role == "research_manager" and out.get("side_preference") not in ("long", "short", "none"):
        p.append("side_preference must be long, short or none")
    if role == "trader":
        p += _validate_trader(out, pack, index)
    if role == "portfolio_manager":
        p += _validate_pm(out, pack, rd, step_spec)
    return p


def _signal_items(pack):
    return [it for it in pack.get("items") or [] if it["kind"] == "SIGNAL" and isinstance(it.get("value"), dict) and it["value"].get("signal")]


def _validate_trader(out, pack, index):
    p = []
    action = out.get("action")
    if action == "hold":
        return p
    levels = {k: out.get(k) for k in ("entry", "stop", "target")}
    for k, v in levels.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            p.append("%s must be an absolute positive price (got %r), never a percentage or a range" % (k, v))
    if p:
        return p
    e, s, t = levels["entry"], levels["stop"], levels["target"]
    if action == "long" and not (s < e < t):
        p.append("a long needs stop < entry < target")
    if action == "short" and not (t < e < s):
        p.append("a short needs target < entry < stop")
    cited = set()
    for c in out.get("claims") or []:
        for ev in c.get("evidence") or []:
            cited.add(ev)
    sig_items = [it for it in _signal_items(pack) if it["id"] in cited]
    if not sig_items:
        p.append("the trader must cite the SIGNAL item its levels come from")
        return p
    ok = False
    for it in sig_items:
        sig = it["value"]["signal"]
        side = sig.get("side") or "long"
        if side != action:
            continue
        if all(abs(levels[k] - float(sig[k])) / float(sig[k]) <= 0.02 for k in ("entry", "stop", "target")):
            ok = True
            break
    if not ok:
        p.append("the levels are not within 2 %% of any cited %s SIGNAL's entry, stop and target" % action)
    return p


def _validate_pm(out, pack, rd, step_spec):
    p = []
    if out.get("side") not in ("long", "short", "none"):
        p.append("side must be long, short or none")
    if out.get("horizon") not in ("intraday", "swing"):
        p.append("horizon must be intraday or swing")
    if not isinstance(out.get("no_trade"), bool):
        p.append("no_trade must be true or false")
    if not out.get("thesis") or not out.get("invalidation"):
        p.append("thesis and invalidation are required")
    if not isinstance(out.get("key_risks"), list) or not out["key_risks"]:
        p.append("key_risks must be a non-empty list")
    rating = out.get("rating")
    direction = {"buy": "long", "overweight": "long", "hold": "none", "underweight": "short", "sell": "short"}.get(rating)
    if direction and out.get("side") not in (direction, "none") and not out.get("no_trade"):
        p.append("rating %s points %s but side is %s" % (rating, direction, out.get("side")))
    if rd:
        trader = read_output(rd, "trader")
        if isinstance(trader, dict) and trader.get("action") in ACTIONS:
            t_dir = trader["action"] if trader["action"] != "hold" else "none"
            if direction and direction != t_dir and not out.get("overrides_trader"):
                p.append("the rating's direction (%s) disagrees with the trader's action (%s): set overrides_trader true and give override_reason"
                         % (direction, trader["action"]))
            if out.get("overrides_trader") and not out.get("override_reason"):
                p.append("overrides_trader needs an override_reason")
    return p


# ------------------------------------------------------------------------------------------- the decision and the card
def decide(rd, depth, weights=None, study=None, now_utc=None, weights_test=None):
    state, done, pack = status(rd, depth)
    pending = [s for s in state if s["state"] != "done"]
    scores = scoring.score_pack(pack, done, weights, study, weights_test)
    pm = done.get("portfolio_manager")
    rating = (pm or {}).get("rating") or REVIEW
    side = (pm or {}).get("side") or "none"
    no_trade = bool((pm or {}).get("no_trade")) or rating in ("hold", REVIEW) or side == "none"
    reasons = []
    if pending:
        reasons.append("%d step(s) not validated: %s" % (len(pending), ", ".join(s["step"] for s in pending)))
    risk = (pack.get("risk") or {}).get(side) if side in ("long", "short") else None
    if side in ("long", "short") and risk and risk.get("verdict") == "do_not_trade":
        no_trade = True
        reasons.append("the risk engine says do not trade the %s: %s" % (side, "; ".join(risk.get("reasons") or [])[:300]))
    best = ((pack.get("setups") or {}).get(side) or [None])[0] if side in ("long", "short") else None
    trader = done.get("trader") or {}
    side_scores = (scores.get("sides") or {}).get(side) if side in ("long", "short") else None
    card = {"format": "card/1", "symbol": pack["symbol"], "date": pack["date"], "cutoff": pack.get("cutoff"), "depth": depth,
            "built": clock.iso(now_utc or clock.utc_now()), "rating": rating if pm else REVIEW, "side": side, "no_trade": no_trade,
            "horizon": (pm or {}).get("horizon"), "confidence": (side_scores or {}).get("confidence_label") if side_scores else "low",
            "confidence_value": (side_scores or {}).get("confidence") if side_scores else None,
            "confidence_why": (side_scores or {}).get("confidence_why") if side_scores else [],
            "scores": scores, "best_setup": ({"config_id": best["config_id"], "signal": best["signal"], "lab": best["lab"], "late_by": best["late_by"]} if best else None),
            "trader": {k: trader.get(k) for k in ("action", "entry", "stop", "target", "setup_config", "size_note")} if trader else None,
            "thesis": (pm or {}).get("thesis"), "invalidation": (pm or {}).get("invalidation"), "key_risks": (pm or {}).get("key_risks") or [],
            "reasons": reasons, "regime": {"class": (pack.get("regime") or {}).get("class"), "rule": (pack.get("regime") or {}).get("rule")},
            "catalysts": pack.get("catalysts"), "risk": pack.get("risk"), "missing": pack.get("missing"),
            "roles": {s["step"]: {"state": s["state"], "stance": (done.get(s["step"]) or {}).get(STANCE_KEY.get(s["role"], "stance")),
                                  "confidence": (done.get(s["step"]) or {}).get("confidence"), "summary": (done.get(s["step"]) or {}).get("summary")}
                      for s in state},
            "evidence_cited": sorted(set(e for o in done.values() for c in (o.get("claims") or []) for e in (c.get("evidence") or []))),
            "words": "A card matched rules with a measured record and was read by the desk's roles. A score is a number from named inputs, not a probability. NO TRADE is a result. Most people who day trade lose money" + ("; a short can lose more than the stake." if side == "short" else ".")}
    return card, state, pack


def card_markdown(card, pack):
    index = dtevidence.item_index(pack)
    s = card["scores"]["sides"]
    side = card["side"] if card["side"] in ("long", "short") else None
    lines = ["# %s on %s at %s ET" % (card["symbol"], card["date"], card.get("cutoff")), ""]
    verdict = "NO TRADE" if card["no_trade"] else "%s (%s)" % (card["rating"].upper(), card["side"].upper())
    lines += ["**%s**  rating %s, side %s, horizon %s, confidence %s" % (verdict, card["rating"], card["side"], card.get("horizon") or "n/a", card["confidence"]), ""]
    if card["reasons"]:
        lines += ["- " + r for r in card["reasons"]] + [""]
    snap = pack.get("snapshot") or {}
    lines += ["## PRICE / DATA SNAPSHOT", "- [DATA] last close %s, SMA20 %s, SMA50 %s, SMA200 %s, RSI14 %s, ATR %s %% (E2, E3)" % (
        _n(snap.get("last_close")), _n(snap.get("sma20")), _n(snap.get("sma50")), _n(snap.get("sma200")), _n(snap.get("rsi_14")), _n(snap.get("atr_pct")))]
    intra = snap.get("intraday")
    if intra:
        lines.append("- [DATA] intraday through %s: last %s, %s VWAP, opening range %s, relative volume %s" % (
            intra.get("cutoff"), _n(intra.get("close")), "above" if intra.get("above_vwap") else "below", intra.get("or_position"), _n(intra.get("relvol_so_far"))))
    lines += ["", "## MARKET REGIME", "- [REGIME] %s: %s" % (card["regime"]["class"], card["regime"]["rule"])]
    for role, title in (("momentum", "MOMENTUM"), ("market", "TECHNICAL"), ("fundamentals", "FUNDAMENTAL"), ("news", "NEWS / SENTIMENT"), ("sentiment", None)):
        r = card["roles"].get(role)
        if title:
            lines += ["", "## %s" % title]
        if r and r["state"] == "done":
            lines.append("- [INTERPRETATION] %s: %s (confidence %s). %s" % (role, r["stance"], r["confidence"], r["summary"]))
        elif r:
            lines.append("- [UNCERTAINTY] %s: %s" % (role, r["state"]))
        elif title:
            lines.append("- [UNCERTAINTY] %s: not run at this depth" % role)
    lines += ["", "## CATALYSTS", "- [EVENT] %s" % _cat_words(card["catalysts"])]
    for role, title in (("bull_1", "BULL CASE"), ("bear_1", "BEAR CASE"), ("contrarian", "CONTRARIAN CASE")):
        r = card["roles"].get(role)
        lines += ["", "## %s" % title]
        lines.append("- [INTERPRETATION] %s" % (r["summary"] if r and r["state"] == "done" else "not run at this depth" if not r else r["state"]))
    lines += ["", "## RISK"]
    for sd in ("long", "short"):
        rb = (card["risk"] or {}).get(sd) or {}
        lines.append("- [STAT] %s: verdict %s, liquidity %s, gap95 %s %%, squeeze %s, regime fit %s; %s" % (
            sd, rb.get("verdict"), rb.get("liquidity"), rb.get("gap_risk_pct_95"), rb.get("squeeze_risk"), rb.get("regime_fit"), "; ".join((rb.get("reasons") or [])[:2])))
    gate = ("gated by the feature study" if s["long"].get("momentum_gated") else "weights untested: the feature study has not run") + "; " + (card["scores"].get("weights_verdict") or "")
    lines += ["", "## LONG SCORE", "- [STAT] opportunity %s (momentum %s, raw %s, technical %s, regime %s, catalyst %s, risk %s); lab %s; %s" % (
        _n(s["long"]["opportunity"], 1), _n(s["long"].get("momentum"), 1), _n(s["long"].get("momentum_raw"), 1), _n(s["long"]["technical"], 1), _n(s["long"]["regime"], 0), _n(s["long"]["catalyst"], 0), _n(s["long"]["risk"], 0), s["long"]["lab_status"], gate),
              "", "## SHORT SCORE", "- [STAT] opportunity %s (weakness %s, raw %s, technical %s, regime %s, catalyst %s, risk %s); lab %s" % (
        _n(s["short"]["opportunity"], 1), _n(s["short"].get("weakness"), 1), _n(s["short"].get("momentum_raw"), 1), _n(s["short"]["technical"], 1), _n(s["short"]["regime"], 0), _n(s["short"]["catalyst"], 0), _n(s["short"]["risk"], 0), s["short"]["lab_status"]),
              "", "## CONFIDENCE", "- [STAT] %s (%s)" % (card["confidence"], "; ".join(card.get("confidence_why") or []) or "no side chosen: nothing to grade")]
    lines += ["", "## BEST SETUP"]
    b = card.get("best_setup")
    if card["no_trade"] or not b:
        lines.append("- NO TRADE" + (": " + "; ".join(card["reasons"]) if card["reasons"] else ""))
    else:
        sig = b["signal"]
        lines.append("- [SIGNAL] %s %s at %s: %s %.2f, stop %.2f, target %.2f (%gR); lab %s%s" % (
            side, b["config_id"], sig["entry_time"], "sell short" if side == "short" else "buy above", sig["entry"], sig["stop"], sig["target"], sig["target_r"],
            b["lab"]["status"], (", %d bars late" % b["late_by"]) if b["late_by"] > 1 else ""))
        if card.get("trader") and card["trader"].get("action") not in (None, "hold"):
            t = card["trader"]
            lines.append("- [INTERPRETATION] the trader: %s at %s, stop %s, target %s; %s" % (t["action"], t["entry"], t["stop"], t["target"], t.get("size_note")))
    lines += ["", "## THESIS", "- [INTERPRETATION] %s" % (card.get("thesis") or "none (no portfolio manager output)"),
              "", "## INVALIDATION", "- %s" % (card.get("invalidation") or "n/a"),
              "", "## KEY RISKS"] + ["- %s" % k for k in (card.get("key_risks") or ["n/a"])]
    lines += ["", "## EVIDENCE"]
    for eid in card.get("evidence_cited") or []:
        it = index.get(eid)
        if it:
            lines.append("- %s [%s] %s (%s)" % (eid, it["kind"], it["text"][:160], it.get("source")))
    lines += ["", "Missing: %s" % (", ".join(card.get("missing") or []) or "nothing"), "", card["words"], ""]
    return "\n".join(lines)


def _n(x, places=2):
    """A number for the card, or n/a."""
    if x is None:
        return "n/a"
    try:
        return ("%." + str(places) + "f") % float(x)
    except (TypeError, ValueError):
        return str(x)


def _cat_words(cat):
    from . import catalysts
    return catalysts.words(cat or {"none": True})


# ------------------------------------------------------------------------------------------- memory
def memory_path(root):
    return os.path.join(root, "trader", "research", "memory.jsonl")


def log_card(root, card, now_utc=None):
    """Log the card as pending, one row per symbol, date and side: a card decided again while still pending replaces
    its row; a settled row is never touched. -> "logged" | "replaced" | "settled" (left alone)."""
    path = memory_path(root)
    cid = "%s|%s|%s" % (card["date"], card["symbol"], card["side"])
    b = card.get("best_setup") or {}
    sig = b.get("signal") or {}
    row = {"id": cid, "date": card["date"], "symbol": card["symbol"], "side": card["side"], "rating": card["rating"],
           "no_trade": card["no_trade"], "horizon": card.get("horizon"), "config_id": b.get("config_id"),
           "entry": sig.get("entry"), "stop": sig.get("stop"), "target": sig.get("target"), "entry_time": sig.get("entry_time"),
           "confidence": card.get("confidence"), "thesis": (card.get("thesis") or "")[:600], "status": "pending",
           "logged_at": clock.iso(now_utc or clock.utc_now()), "resolved": None, "outcome": None, "raw_return": None,
           "alpha": None, "reflection": None, "created_by": "trader-agents"}
    rows = [r for r in dtio.read_jsonl(path)] if os.path.exists(path) else []
    for i, r in enumerate(rows):
        if isinstance(r, dict) and r.get("id") == cid:
            if r.get("status") != "pending":
                return "settled"
            rows[i] = row
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for x in rows:
                    fh.write(json.dumps(x, separators=(",", ":")) + "\n")
            os.replace(tmp, path)
            return "replaced"
    dtio.append_jsonl(path, row)
    return "logged"


def settle(root, cache_root, now_utc=None, holding_days=5):
    """Settle pending cards: intraday ones from the scorecard's outcomes, swing ones from the daily closes (the first
    close after the date, holding_days sessions, alpha against SPY). -> (settled, waiting, failed)."""
    from . import bars as dtbars
    path = memory_path(root)
    rows = [r for r in dtio.read_jsonl(path)] if os.path.exists(path) else []
    settled, waiting, failed = [], [], []
    outcomes = {}
    opath = os.path.join(root, "trader", "scorecard", "outcomes.jsonl")
    if os.path.exists(opath):
        for o in dtio.read_jsonl(opath):
            if isinstance(o, dict) and o.get("id"):
                outcomes[o["id"]] = o
    spy = dtbars.daily_rows("SPY", cache_root)
    changed = False
    for r in rows:
        if not isinstance(r, dict) or r.get("status") != "pending":
            continue
        if r.get("no_trade") or r.get("side") not in ("long", "short"):
            r.update({"status": "settled", "outcome": "no_trade", "resolved": r["date"], "raw_return": 0.0, "alpha": 0.0})
            settled.append(r["id"])
            changed = True
            continue
        if r.get("horizon") == "intraday":
            cid = "%s|%s|%s|%s" % (r["date"], r["symbol"], r.get("config_id"), r.get("entry_time"))
            o = outcomes.get(cid)
            if not o:
                waiting.append(r["id"])
                continue
            r.update({"status": "settled", "outcome": o.get("outcome"), "raw_return": o.get("R_net"), "alpha": None, "resolved": r["date"],
                      "settled_by": "scorecard"})
            settled.append(r["id"])
            changed = True
            continue
        daily = dtbars.daily_rows(r["symbol"], cache_root)
        after = [row for row in daily if row[0] > r["date"]]
        if len(after) <= holding_days:
            waiting.append(r["id"])
            continue
        e, x = float(after[0][4]), float(after[holding_days][4])
        raw = (x / e - 1) * (1 if r["side"] == "long" else -1)
        s_after = [row for row in spy if row[0] > r["date"]]
        alpha = None
        if len(s_after) > holding_days and s_after[0][0] == after[0][0]:
            se, sx = float(s_after[0][4]), float(s_after[holding_days][4])
            alpha = raw - (sx / se - 1) * (1 if r["side"] == "long" else -1)
        r.update({"status": "settled", "outcome": "green" if raw > 0 else "red", "raw_return": round(raw, 5), "alpha": None if alpha is None else round(alpha, 5),
                  "resolved": after[holding_days][0], "settled_by": "daily closes, %d sessions from the first close after the date" % holding_days})
        settled.append(r["id"])
        changed = True
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    return settled, waiting, failed


def store_reflection(root, card_id, text):
    path = memory_path(root)
    rows = [r for r in dtio.read_jsonl(path)] if os.path.exists(path) else []
    hit = False
    for r in rows:
        if isinstance(r, dict) and r.get("id") == card_id and r.get("status") == "settled":
            r["reflection"] = text
            hit = True
    if not hit:
        raise AgentsError("no settled card %s to reflect on" % card_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, path)
