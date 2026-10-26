"""The bearish setup library: the short side of the desk (CONTRACTS.md, "The research desk", Sides).

Same contract as dt/setups.py: a setup is a pure function over one session plus context that returns at most
ONE signal per session, the first bar whose CLOSE confirms the rule, and never reads bars[k+1:]. A short signal
carries "side": "short", a stop ABOVE the entry (rounded up to the cent), a target BELOW it, and
risk_per_share = stop - entry. Config ids end in "|short" so none collides with a long id. The long library
is untouched: nothing here is read unless a caller asks for the short side.

Shorting is research here. Nothing places a short order: the upstream risk manager blocks short sales unless the
user's config allows them, and the paper broker refuses one without allow_short. A short can lose more than the
stake; every card says so.
"""

from .bars import opening_range, atr, cents_up, half_up
from . import clock
from .setups import _scan, _vwap, MIN_RISK_FRAC, _r


def _short_signal(name, params, k, entry, stop, target_r=None, target=None, stop_rule="", opening_range_=None,
                  level=None, extra=None):
    entry = round(float(entry), 2)
    stop = cents_up(stop)
    risk = round(stop - entry, 4)
    if risk <= 0 or risk < entry * MIN_RISK_FRAC:
        return None
    if target is None:
        target = half_up(entry - target_r * risk)
    else:
        target = half_up(target)
        if target >= entry:
            return None
        target_r = round((entry - target) / risk, 4)
    if target <= 0:
        return None
    sig = {"setup": name, "side": "short", "params": dict(params), "k": k, "entry": entry, "stop": stop,
           "target": target, "target_r": round(float(target_r), 4), "stop_rule": stop_rule,
           "entry_time": clock.clock12(clock.slot_close_minutes(k)), "risk_per_share": round(risk, 2),
           "opening_range": opening_range_, "level": level}
    if extra:
        sig.update(extra)
    return sig


# ---------------------------------------------------------------- the opening-range breakdown

def orb_down(bars, ctx, params, name="orb15_down"):
    n = int(params.get("or_minutes", 15)) // clock.BAR_MIN
    if len(bars) <= n:
        return None
    oh, ol = opening_range(bars, n)
    stop_rule = params.get("stop_rule", "or_mid")
    orr = {"high": round(oh, 2), "low": round(ol, 2), "minutes": n * clock.BAR_MIN}

    def cond(k):
        b = bars[k]
        if not b["c"] < ol:
            return None
        stop = (oh + ol) / 2.0 if stop_rule == "or_mid" else oh
        return _short_signal(name, params, k, b["c"], stop, params["target_r"], stop_rule=stop_rule,
                             opening_range_=orr)
    return _scan(bars, ctx, n, cond)


# ---------------------------------------------------------------- VWAP rejection

def vwap_reject(bars, ctx, params, name="vwap_reject"):
    """3+ closes under VWAP, one close above it (the reclaim attempt), then a close back below: short at that
    close, stop above the attempt's high."""
    vw = _vwap(bars, ctx)
    below = int(params.get("below_bars", 3))

    def cond(k):
        if k < below + 2:
            return None
        if not all(bars[j]["c"] < vw[j] for j in range(k - below - 1, k - 1)):
            return None
        if not bars[k - 1]["c"] > vw[k - 1]:
            return None
        if not bars[k]["c"] < vw[k]:
            return None
        stop = max(bars[k - 1]["h"], bars[k]["h"])
        return _short_signal(name, params, k, bars[k]["c"], stop, params["target_r"], stop_rule="reject_high",
                             level=round(vw[k], 2))
    return _scan(bars, ctx, 7, cond)


# ---------------------------------------------------------------- the failed breakout

def failed_breakout(bars, ctx, params, name="failed_breakout"):
    """A close above the opening-range high, then the first close back below it (the bar before it still
    above): short, stop above the day high so far."""
    n = int(params.get("or_minutes", 15)) // clock.BAR_MIN
    if len(bars) <= n + 1:
        return None
    oh, ol = opening_range(bars, n)
    orr = {"high": round(oh, 2), "low": round(ol, 2), "minutes": n * clock.BAR_MIN}

    def cond(k):
        if k < n + 1:
            return None
        if not bars[k - 1]["c"] > oh:
            return None
        if not bars[k]["c"] < oh:
            return None
        hod = max(b["h"] for b in bars[:k + 1])
        return _short_signal(name, params, k, bars[k]["c"], hod, params["target_r"], stop_rule="day_high",
                             opening_range_=orr, level=round(oh, 2))
    return _scan(bars, ctx, n + 1, cond)


# ---------------------------------------------------------------- the gap fade

def gap_fade(bars, ctx, params, name="gap_fade"):
    pc = ctx.get("prev_close")
    if not pc or len(bars) < 2:
        return None
    gap = bars[0]["o"] / pc - 1
    if gap < params.get("gap_min", 0.01):
        return None
    b0 = bars[0]
    if not b0["c"] < b0["o"]:
        return None
    last = int(params.get("last_k", 11))

    def cond(k):
        if k > last:
            return None
        if bars[k]["c"] < b0["l"]:
            return _short_signal(name, params, k, bars[k]["c"], b0["h"], params["target_r"],
                                 stop_rule="first_bar_high", level=round(b0["l"], 2),
                                 extra={"gap_pct": round(gap * 100, 2)})
        return None
    return _scan(bars, ctx, 1, cond)


# ---------------------------------------------------------------- breakdowns later in the day

def lod_break(bars, ctx, params, name="lod_break"):
    cons_n = int(params.get("cons_bars", 6))
    cons_atr = float(params.get("cons_atr", 1.5))
    near_atr = float(params.get("near_atr", 1.0))

    def cond(k):
        if k < cons_n + 1:
            return None
        cons = bars[k - cons_n:k]
        lod = min(b["l"] for b in bars[:k])
        a = atr(bars, 14, k)
        ch = max(b["h"] for b in cons)
        cl = min(b["l"] for b in cons)
        if ch - cl > cons_atr * a or cl - lod > near_atr * a:
            return None
        if not bars[k]["c"] < lod:
            return None
        return _short_signal(name, params, k, bars[k]["c"], ch, params["target_r"], stop_rule="consolidation_high",
                             level=round(lod, 2))
    return _scan(bars, ctx, 17, cond)


def pm_breakdown(bars, ctx, params, name="pm_breakdown"):
    first_k = int(params.get("first_k", 54))
    last_k = int(params.get("last_k", 68))
    stop_bars = int(params.get("stop_bars", 6))

    def cond(k):
        if k > last_k:
            return None
        lod = min(b["l"] for b in bars[:k])
        if not bars[k]["c"] < lod:
            return None
        stop = max(b["h"] for b in bars[k - stop_bars:k])
        return _short_signal(name, params, k, bars[k]["c"], stop, params["target_r"],
                             stop_rule="high_of_last_%d" % stop_bars, level=round(lod, 2))
    return _scan(bars, ctx, first_k, cond)


# ---------------------------------------------------------------- the registry

SETUPS = {
    "orb15_down": {"fn": orb_down, "label": "15-minute opening-range breakdown", "base": {"or_minutes": 15},
                   "grid": [{"target_r": r, "stop_rule": s} for r in (1.0, 1.5, 2.0) for s in ("or_mid", "or_high")]},
    "orb30_down": {"fn": orb_down, "label": "30-minute opening-range breakdown", "base": {"or_minutes": 30},
                   "grid": _r((1.0, 1.5, 2.0), stop_rule="or_mid")},
    "vwap_reject": {"fn": vwap_reject, "label": "VWAP rejection: 3+ bars under VWAP, one close above, a close back below",
                    "base": {"below_bars": 3}, "grid": _r((1.0, 2.0))},
    "failed_breakout": {"fn": failed_breakout, "label": "failed breakout: a close above the 15-minute range high, then a close back below it",
                        "base": {"or_minutes": 15}, "grid": _r((1.0, 2.0))},
    "gap_fade": {"fn": gap_fade, "label": "gap fade: gap up 1%+, red first bar, a close below its low by 10:30",
                 "base": {"gap_min": 0.01, "last_k": 11}, "grid": _r((1.0, 2.0))},
    "lod_break": {"fn": lod_break, "label": "low-of-day breakdown after a tight consolidation (after 11:00)",
                  "base": {"cons_bars": 6, "cons_atr": 1.5, "near_atr": 1.0}, "grid": _r((1.0, 2.0))},
    "pm_breakdown": {"fn": pm_breakdown, "label": "afternoon breakdown below the day low (2:00 to 3:15)",
                     "base": {"first_k": 54, "last_k": 68, "stop_bars": 6}, "grid": _r((1.0, 2.0))},
}

STOP_WORDS = {"or_mid": "stop at the range midpoint", "or_high": "stop at the range high",
              "reject_high": "stop above the rejection high", "day_high": "stop above the day high",
              "first_bar_high": "stop above the first bar's high", "consolidation_high": "stop above the consolidation high",
              "high_of_last_6": "stop above the high of the last 6 bars"}


def config_id(setup, params):
    cid = setup
    if "target_r" in params:
        cid += "|r=%g" % params["target_r"]
    if params.get("stop_rule") and params["stop_rule"] != "or_mid" and setup.startswith("orb"):
        cid += "|stop=%s" % params["stop_rule"]
    return cid + "|short"


def configs(names=None):
    """Every short config (or of the named setups): [{"id", "side", "setup", "params", "label"}]."""
    out = []
    for name, spec in SETUPS.items():
        if names and name not in names:
            continue
        for g in spec["grid"]:
            params = dict(spec["base"])
            params.update(g)
            label = spec["label"]
            sr = params.get("stop_rule")
            if sr and sr in STOP_WORDS:
                label += ", " + STOP_WORDS[sr]
            if "target_r" in params:
                label += ", target %gR" % params["target_r"]
            out.append({"id": config_id(name, params), "side": "short", "setup": name, "params": params,
                        "label": label + " (short)"})
    return out


def run_config(cfg, bars, ctx):
    spec = SETUPS[cfg["setup"]]
    return spec["fn"](bars, ctx, cfg["params"], name=cfg["setup"])
