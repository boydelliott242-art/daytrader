"""The risk block per candidate and side, and the do-not-trade verdicts (CONTRACTS.md, "The research desk", Risk).

Pure: reads the features, the signal, the catalysts, the regime class and whatever the intel ledger holds. Every
figure that is not available is "unknown" and the reasons say so; the verdict never rests on a guess. A short can
lose more than the stake, and the block says so on every short.
"""

from . import regimes
from .bars import daily_before

ADV_THIN_USD = 5e6
ADV_DEEP_USD = 1e8
PARABOLIC_ROC5_PCT = 20.0
PARABOLIC_RELVOL = 3.0
SI_HIGH_PCT = 20.0
SI_LOW_PCT = 5.0
DTC_HIGH = 5.0
EARNINGS_SESSIONS = 5
GAP_PCTILE = 0.95
SHORT_WORDS = "a short can lose more than the stake: the loss is unbounded above the entry"


def gap_risk(daily_rows, date_str, n=250, pctile=GAP_PCTILE):
    """The pctile of the absolute overnight gap (open vs the prior close) over the last n sessions, in percent."""
    rows = daily_before(daily_rows or [], date_str, n + 1)
    gaps = []
    for a, b in zip(rows[:-1], rows[1:]):
        if a[4] and b[1]:
            gaps.append(abs(float(b[1]) / float(a[4]) - 1) * 100.0)
    if len(gaps) < 20:
        return None
    gaps.sort()
    i = min(len(gaps) - 1, int(round(pctile * (len(gaps) - 1))))
    return round(gaps[i], 3)


def liquidity_grade(adv_usd):
    if adv_usd is None:
        return "unknown"
    if adv_usd >= ADV_DEEP_USD:
        return "deep"
    if adv_usd >= ADV_THIN_USD:
        return "ok"
    return "thin"


def _si_from_intel(intel_items):
    """Short interest and days to cover when the ledger carries them (kind short_interest, value {...}); else None."""
    for it in intel_items or []:
        if (it.get("kind") == "short_interest" or "short_interest" in (it.get("tags") or [])) and isinstance(it.get("value"), dict):
            v = it["value"]
            return v.get("short_pct_float"), v.get("days_to_cover"), it.get("source"), it.get("url")
    return None, None, None, None


def risk_block(sym, side, date_str, features=None, signal=None, catalysts=None, regime_class=None, intel_items=None,
               daily_rows=None, lab_oos=None, regime_slice=None):
    f = features or {}
    cat = catalysts or {}
    side = side or "long"
    reasons, unknown = [], []
    adv = f.get("adv_usd_20")
    liq = liquidity_grade(adv)
    si_pct, dtc, si_src, si_url = _si_from_intel(intel_items)
    if si_pct is None:
        squeeze = "unknown"
        unknown.append("short_interest")
    elif si_pct >= SI_HIGH_PCT or (dtc is not None and dtc >= DTC_HIGH):
        squeeze = "high"
    elif si_pct <= SI_LOW_PCT:
        squeeze = "low"
    else:
        squeeze = "medium"
    unknown += ["crowding", "borrow"]
    gap95 = gap_risk(daily_rows, date_str) if daily_rows else None
    if gap95 is None:
        unknown.append("gap_risk")
    ne = cat.get("next_earnings")
    sessions_ahead = ne.get("sessions_ahead") if ne else None
    macro_today = any(e.get("kind") == "macro" for e in cat.get("known") or [])
    event = {"earnings_sessions_ahead": sessions_ahead, "earnings_within_5": (sessions_ahead is not None and sessions_ahead <= EARNINGS_SESSIONS),
             "earnings_next_session": (sessions_ahead is not None and sessions_ahead <= 1), "macro_high_today": macro_today,
             "known_catalysts": len(cat.get("known") or [])}
    stop = (signal or {}).get("stop")
    entry = (signal or {}).get("entry")
    stop_pct = round(abs(float(stop) - float(entry)) / float(entry) * 100.0, 3) if (stop and entry) else None
    inval = {"stop": stop, "level": (signal or {}).get("level"), "rule": (signal or {}).get("stop_rule"), "stop_distance_pct": stop_pct}
    roc5, relvol = f.get("roc_5"), f.get("relvol_20")
    parabolic = roc5 is not None and relvol is not None and roc5 >= PARABOLIC_ROC5_PCT and relvol >= PARABOLIC_RELVOL
    regime_fit = regimes.fit(side, regime_class) if regime_class else "unknown"
    slice_exp = (regime_slice or {}).get("stats", {}).get("exp_R") if regime_slice else None
    asym = bool(event["earnings_next_session"]) or any(
        e.get("kind") in ("earnings", "macro") and e.get("date") == date_str for e in cat.get("known") or [])
    verdict = "ok"
    # the hard stops, per side
    if liq == "thin":
        verdict = "do_not_trade"; reasons.append("average dollar volume under $5 million: too thin for a stop to hold")
    if event["earnings_next_session"]:
        verdict = "do_not_trade"; reasons.append("earnings within the next session: a binary event the stop cannot price")
    if side == "short":
        if parabolic:
            verdict = "do_not_trade"; reasons.append("parabolic: up %.0f %% in 5 sessions on %.1fx volume; squeeze risk" % (roc5, relvol))
        if squeeze == "high":
            verdict = "do_not_trade"; reasons.append("short interest %.0f %% of the float (%s): crowded, squeeze risk" % (si_pct, si_src or "ledger"))
        if regime_class == "strong_bull" and not (slice_exp is not None and slice_exp > 0):
            verdict = "do_not_trade"; reasons.append("a short in a strong bull regime with no positive record in that regime")
    else:
        if regime_class == "crisis":
            verdict = "do_not_trade"; reasons.append("crisis regime: longs stop out on gaps, not on rules")
    # the cautions
    if verdict == "ok":
        if side == "short" and squeeze == "unknown":
            verdict = "caution"; reasons.append("short interest and borrow unknown: squeeze risk cannot be judged")
        if gap95 is not None and stop_pct is not None and gap95 > 2 * stop_pct:
            verdict = "caution"; reasons.append("a typical bad overnight gap (%.1f %%) is more than twice the stop distance (%.1f %%)" % (gap95, stop_pct))
        mtf = f.get("mtf_agreement")
        if mtf in ("short_term_up_long_term_down", "short_term_down_long_term_up"):
            verdict = "caution"; reasons.append("timeframes disagree: %s" % mtf.replace("_", " "))
        if regime_fit == "against":
            verdict = "caution"; reasons.append("the regime (%s) is against a %s" % (regime_class, side))
        if event["earnings_within_5"]:
            verdict = "caution"; reasons.append("earnings within %d sessions" % sessions_ahead)
        if macro_today:
            verdict = "caution"; reasons.append("a HIGH-impact macro print today")
    if side == "short":
        reasons.append(SHORT_WORDS)
    if unknown:
        reasons.append("unknown: " + ", ".join(unknown))
    return {"symbol": sym, "side": side, "date": date_str, "atr_pct": f.get("atr_pct"), "realized_vol_20": f.get("realized_vol_20"),
            "adv_usd": adv, "liquidity": liq, "gap_risk_pct_95": gap95, "event_risk": event, "beta_spy": f.get("beta_spy_63"),
            "regime_class": regime_class, "regime_fit": regime_fit, "squeeze_risk": squeeze, "short_interest_pct": si_pct,
            "days_to_cover": dtc, "crowding": "unknown", "borrow": "unknown", "parabolic": parabolic, "invalidation": inval,
            "asymmetric_event": asym, "verdict": verdict, "reasons": reasons, "unknown": unknown,
            "lab_oos_exp_R": (lab_oos or {}).get("exp_R")}


def score(block):
    """0 to 100, 100 the safest: a plain, documented ladder (no weights to tune)."""
    if not block:
        return None
    s = 100.0
    v = block.get("verdict")
    if v == "do_not_trade":
        s -= 60
    elif v == "caution":
        s -= 25
    liq = block.get("liquidity")
    s -= {"thin": 25, "ok": 5, "unknown": 10}.get(liq, 0)
    atr = block.get("atr_pct")
    if atr is not None:
        s -= min(20.0, max(0.0, (atr - 2.0) * 5.0))
    if block.get("event_risk", {}).get("earnings_within_5"):
        s -= 10
    if block.get("squeeze_risk") == "unknown" and block.get("side") == "short":
        s -= 10
    for _ in block.get("unknown") or []:
        s -= 2
    return round(max(0.0, min(100.0, s)), 1)
