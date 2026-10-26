"""The scores, 0 to 100, from named inputs and the weights file (CONTRACTS.md, "The research desk", Scores).

Code-only terms (momentum, weakness, technical, regime, catalyst, risk, liquidity) come from the evidence pack and can be
scored over the lab's history (weights-test). The agent terms (fundamental, sentiment) come from validated role outputs
and have no history: they are reported beside the code score, never folded into it. A score is a number, not a probability.
"""

import json
import os

from . import regimes

DEFAULT_WEIGHTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-agents", "weights.json")
STATUS_VALUE = {"approved": 1.0, "watch": 0.6, "untested": 0.3, "rejected": 0.0}


def load_weights(path=None):
    path = path or DEFAULT_WEIGHTS
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def clip01(x):
    return max(0.0, min(1.0, x))


def linear(x, lo, hi):
    if x is None or hi == lo:
        return None
    return clip01((float(x) - lo) / (hi - lo))


def structure_value(s, side="long"):
    if s is None:
        return None
    if side == "short":
        return {"down": 1.0, "mixed": 0.5, "up": 0.0}.get(s)
    return {"up": 1.0, "mixed": 0.5, "down": 0.0}.get(s)


def feature_gate(study, feature, side):
    """True when the feature study says the feature adds value on this side, or when there is no study yet."""
    if not study:
        return True
    verdicts = ((study.get("adds_value") or {}).get(side) or {})
    if feature not in verdicts:
        return True
    return bool(verdicts[feature])


def momentum(features, weights, side="long", study=None):
    """0..100 and the terms used: on the short side every term reads weakness (the mirror of the mapping)."""
    terms = weights.get("momentum_terms") or {}
    total, used, detail = 0.0, 0.0, {}
    for name, spec in terms.items():
        if not feature_gate(study, name, side):
            detail[name] = {"value": features.get(name), "scaled": None, "weight": 0.0, "why": "no value in the feature study"}
            continue
        v = features.get(name)
        if spec.get("map") == "structure":
            scaled = structure_value(v, side)
        else:
            scaled = linear(v, spec["lo"], spec["hi"])
            if scaled is not None and side == "short" and name not in ("adx_14", "relvol_20"):
                scaled = 1.0 - scaled
        if scaled is None:
            detail[name] = {"value": v, "scaled": None, "weight": 0.0, "why": "not computable"}
            continue
        w = float(spec["weight"])
        total += scaled * w
        used += w
        detail[name] = {"value": v, "scaled": round(scaled, 3), "weight": w}
    score = round(100.0 * total / used, 1) if used else None
    return score, {"terms": detail, "weight_used": round(used, 3), "side": side}


def technical(best, weights, side="long", features=None):
    """From the best fired config on the side: the lab's status and OOS lower bound, freshness, the timeframes."""
    if not best:
        return None, {"why": "no rule fired on this side"}
    t = weights.get("technical_terms") or {}
    lab = best.get("lab") or {}
    status = lab.get("status") or "untested"
    oos = lab.get("oos") or {}
    lo = (oos.get("exp_ci95") or [None, None])[0]
    mtf = (features or {}).get("mtf_agreement")
    agree = {"agree_up": 1.0 if side == "long" else 0.0, "agree_down": 1.0 if side == "short" else 0.0,
             "short_term_up_long_term_down": 0.5 if side == "long" else 0.4, "short_term_down_long_term_up": 0.4 if side == "long" else 0.5}.get(mtf, 0.5)
    terms = {"lab_status": STATUS_VALUE.get(status, 0.3), "oos_lower_bound": linear(lo, t["oos_lower_bound"]["lo"], t["oos_lower_bound"]["hi"]) if lo is not None else 0.3,
             "fresh": 1.0 if (best.get("late_by") or 0) <= 1 else 0.3, "mtf_agreement": agree}
    total, used = 0.0, 0.0
    for name, v in terms.items():
        w = float(t[name]["weight"])
        total += v * w
        used += w
    return round(100.0 * total / used, 1), {"terms": terms, "config_id": best.get("config_id"), "status": status, "oos_lower_bound": lo}


def regime_score(regime_class, side, weights):
    fit = regimes.fit(side, regime_class) if regime_class else "unknown"
    return float((weights.get("regime_fit") or {}).get(fit, 50)), fit


def catalyst_score(cat, side, weights):
    w = weights.get("catalyst") or {}
    with_side = "up" if side == "long" else "down"
    against = "down" if side == "long" else "up"
    known = cat.get("known") or []
    possible = cat.get("possible") or []
    if any(e.get("direction") == against for e in known):
        return float(w.get("known_against", 0)), "a known catalyst points against the side"
    if any(e.get("direction") == with_side for e in known):
        return float(w.get("known_with", 100)), "a known catalyst points with the side"
    if any(e.get("direction") == against for e in possible):
        return float(w.get("possible_against", 25)), "a possible catalyst points against the side"
    if any(e.get("direction") == with_side for e in possible):
        return float(w.get("possible_with", 60)), "a possible catalyst points with the side"
    if known:
        return float(w.get("possible_with", 60)) * 0.8, "a known catalyst with no direction (an event, not a lean)"
    return float(w.get("none", 40)), "no identified catalyst"


def liquidity_score(features):
    adv = (features or {}).get("adv_usd_20")
    if adv is None:
        return None
    return round(100.0 * clip01((adv - 5e6) / (5e8 - 5e6)), 1)


def opportunity(side, parts, weights):
    key = "long_opportunity" if side == "long" else "short_opportunity"
    vec = weights.get(key) or {}
    total, used, detail = 0.0, 0.0, {}
    for name, w in vec.items():
        if name == "why":
            continue
        v = parts.get("momentum" if name == "weakness" else name)
        if v is None:
            detail[name] = {"score": None, "weight": 0.0}
            continue
        total += float(v) * float(w)
        used += float(w)
        detail[name] = {"score": v, "weight": w}
    return (round(total / used, 1) if used else None), detail


def confidence(pack, outputs, side, lab_status, weights, regime_fit):
    """evidence completeness x agreement x the record grade -> (0..1, grade, why)."""
    core = ("bars", "session", "lab", "regime", "intel", "news", "fundamentals")
    missing = set(pack.get("missing") or [])
    completeness = 1.0 - len([c for c in core if c in missing]) / float(len(core))
    stances = []
    for role, out in (outputs or {}).items():
        st = (out or {}).get("stance")
        if st in ("bullish", "mildly_bullish"):
            stances.append("long")
        elif st in ("bearish", "mildly_bearish"):
            stances.append("short")
        elif st in ("neutral", "mixed"):
            stances.append("none")
    agreement = (stances.count(side) / float(len(stances))) if stances else 1.0   # no roles ran: no agreement term, said below
    grade = float((weights.get("confidence") or {}).get(lab_status or "untested", 0.3))
    value = round(completeness * agreement * grade, 3)
    why = ["completeness %.2f (missing: %s)" % (completeness, ", ".join(sorted(missing & set(core))) or "none"),
           ("agreement %.2f (%d of %d roles lean %s)" % (agreement, stances.count(side), len(stances), side)) if stances else "no roles ran (code only)",
           "record grade %.2f (%s)" % (grade, lab_status or "untested")]
    label = "high" if value >= 0.5 else "medium" if value >= 0.25 else "low"
    capped = False
    if (missing & set(core)) or regime_fit == "against":
        if label == "high":
            label, capped = "medium", True
    if capped:
        why.append("capped at medium: a core source is missing or the regime is against")
    return value, label, why


def weights_verdict(test):
    """One line from trader/lab/weights_test.json, for every card and ranking."""
    if not test:
        return "the weights test has not run: every weight is a choice, untested"
    if test.get("separates"):
        return "weights test: the top momentum quintile's lower bound clears the bottom's upper bound on %s OOS trades" % test.get("n")
    q = test.get("by_quintile") or []
    if q:
        return ("weights test: the momentum score does NOT separate %s OOS trades (bottom quintile %+.3fR, top %+.3fR): the momentum weights "
                "have no measured ground and carry what they carry as a choice" % (test.get("n"), q[0]["exp_R"], q[-1]["exp_R"]))
    return "weights test: " + str(test.get("note") or "no result")


def score_pack(pack, outputs=None, weights=None, study=None, weights_test=None):
    """Every score for both sides from an evidence pack (and validated outputs when there are any)."""
    weights = weights or load_weights()
    f = pack.get("features") or {}
    out = {"sides": {}, "liquidity": liquidity_score(f), "weights_note": weights.get("note"), "weights_verdict": weights_verdict(weights_test)}
    for side in ("long", "short"):
        best = (pack.get("setups") or {}).get(side) or []
        best = best[0] if best else None
        mom, mom_detail = momentum(f, weights, side, study)
        mom_raw, _ = momentum(f, weights, side, None)
        tech, tech_detail = technical(best, weights, side, f)
        reg, fit = regime_score((pack.get("regime") or {}).get("class"), side, weights)
        cat, cat_why = catalyst_score(pack.get("catalysts") or {}, side, weights)
        risk_block = (pack.get("risk") or {}).get(side) or {}
        risk_score = risk_block.get("score")
        parts = {"momentum": mom, "technical": tech, "regime": reg, "catalyst": cat, "risk": risk_score}
        opp, detail = opportunity(side, parts, weights)
        lab_status = (best or {}).get("lab", {}).get("status") if best else "untested"
        conf_value, conf_label, conf_why = confidence(pack, outputs, side, lab_status, weights, fit)
        out["sides"][side] = {"momentum" if side == "long" else "weakness": mom, "momentum_raw": mom_raw, "momentum_gated": study is not None,
                              "momentum_detail": mom_detail, "technical": tech,
                              "technical_detail": tech_detail, "regime": reg, "regime_fit": fit, "catalyst": cat, "catalyst_why": cat_why,
                              "risk": risk_score, "risk_verdict": risk_block.get("verdict"), "opportunity": opp, "opportunity_detail": detail,
                              "confidence": conf_value, "confidence_label": conf_label, "confidence_why": conf_why,
                              "best_config": (best or {}).get("config_id"), "lab_status": lab_status}
    agent_terms = {}
    for role in ("fundamentals", "sentiment", "news", "market", "momentum", "regime"):
        o = (outputs or {}).get(role)
        if o and isinstance(o.get("scores"), dict):
            agent_terms[role] = o["scores"]
    out["agent_terms"] = agent_terms
    out["note"] = "The agent terms are reported beside the code scores and never folded into them: they have no history to test."
    return out
