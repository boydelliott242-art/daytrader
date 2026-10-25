"""The setup library. A setup is a pure function over one session (plus context known at
the bar) that returns at most ONE signal per session: the first bar whose CLOSE confirms
the rule, or None. A setup never reads bars[k+1:] when deciding at bar k."""

from .bars import opening_range, vwap_series, atr, returns_std, cents_down, half_up
from . import clock

MIN_RISK_FRAC = 0.0005   # a stop under 0.05 % of the price is too tight to survive the spread (upstream rule)


def _vwap(bars, ctx):
    cache = ctx.setdefault("_cache", {})
    key = ("vwap", len(bars))
    if key not in cache:
        cache[key] = vwap_series(bars)
    return cache[key]


def _signal(name, params, k, entry, stop, target_r=None, target=None, stop_rule="", opening_range_=None,
            level=None, extra=None):
    entry = round(float(entry), 2)
    stop = cents_down(stop)
    risk = round(entry - stop, 4)
    if risk <= 0 or risk < entry * MIN_RISK_FRAC:
        return None
    if target is None:
        target = half_up(entry + target_r * risk)
    else:
        target = half_up(target)
        if target <= entry:
            return None
        target_r = round((target - entry) / risk, 4)
    sig = {"setup": name, "params": dict(params), "k": k, "entry": entry, "stop": stop, "target": target,
           "target_r": round(float(target_r), 4), "stop_rule": stop_rule,
           "entry_time": clock.clock12(clock.slot_close_minutes(k)), "risk_per_share": round(risk, 2),
           "opening_range": opening_range_, "level": level}
    if extra:
        sig.update(extra)
    return sig


def _scan(bars, ctx, start_k, cond):
    first_k, last_k = ctx.get("window") or (2, 71)
    last_k = min(last_k, len(bars) - 1)
    for k in range(max(start_k, first_k), last_k + 1):
        sig = cond(k)
        if sig:
            return sig
    return None


# ---------------------------------------------------------------- the opening-range family

def orb(bars, ctx, params, name="orb15"):
    n = int(params.get("or_minutes", 15)) // clock.BAR_MIN
    if len(bars) <= n:
        return None
    oh, ol = opening_range(bars, n)
    vol_x = params.get("vol_x")
    rs_min = params.get("rs_min")
    gap_min = params.get("gap_min")
    if gap_min is not None:
        pc = ctx.get("prev_close")
        if not pc or bars[0]["o"] / pc - 1 < gap_min - 1e-9:
            return None
    range_vol = sum(b["v"] for b in bars[:n]) / float(n)
    spy = ctx.get("spy")
    stop_rule = params.get("stop_rule", "or_mid")
    orr = {"high": round(oh, 2), "low": round(ol, 2), "minutes": n * clock.BAR_MIN}

    def cond(k):
        b = bars[k]
        if not b["c"] > oh:
            return None
        if vol_x is not None and (range_vol <= 0 or b["v"] < vol_x * range_vol):
            return None
        if rs_min is not None:
            if not spy or len(spy) <= k or not spy[0]["o"] or not bars[0]["o"]:
                return None
            if (b["c"] / bars[0]["o"] - 1) - (spy[k]["c"] / spy[0]["o"] - 1) < rs_min - 1e-9:
                return None
        stop = (oh + ol) / 2.0 if stop_rule == "or_mid" else ol
        extra = {}
        if vol_x is not None:
            extra["vol_x_actual"] = round(b["v"] / range_vol, 2) if range_vol else None
        if gap_min is not None:
            extra["gap_pct"] = round((bars[0]["o"] / ctx["prev_close"] - 1) * 100, 2)
        return _signal(name, params, k, b["c"], stop, params["target_r"], stop_rule=stop_rule,
                       opening_range_=orr, extra=extra)
    return _scan(bars, ctx, n, cond)


# ---------------------------------------------------------------- gap and go

def gap_go(bars, ctx, params, name="gap_go"):
    pc = ctx.get("prev_close")
    if not pc or len(bars) < 2:
        return None
    gap = bars[0]["o"] / pc - 1
    if gap < params.get("gap_min", 0.01):
        return None
    b0 = bars[0]
    if not b0["c"] > b0["o"]:
        return None
    last = int(params.get("last_k", 11))

    def cond(k):
        if k > last:
            return None
        if bars[k]["c"] > b0["h"]:
            return _signal(name, params, k, bars[k]["c"], b0["l"], params["target_r"], stop_rule="first_bar_low",
                           level=round(b0["h"], 2), extra={"gap_pct": round(gap * 100, 2)})
        return None
    return _scan(bars, ctx, 1, cond)


# ---------------------------------------------------------------- VWAP rules

def vwap_reclaim(bars, ctx, params, name="vwap_reclaim"):
    vw = _vwap(bars, ctx)
    below = int(params.get("below_bars", 3))

    def cond(k):
        if k < below + 1:
            return None
        if not all(bars[j]["c"] < vw[j] for j in range(k - below, k)):
            return None
        if not bars[k]["c"] > vw[k]:
            return None
        stop = min(bars[j]["l"] for j in range(k - below, k + 1))
        return _signal(name, params, k, bars[k]["c"], stop, params["target_r"],
                       stop_rule="low_of_last_%d" % (below + 1), level=round(vw[k], 2))
    return _scan(bars, ctx, 6, cond)


def vwap_pullback(bars, ctx, params, name="vwap_pullback"):
    vw = _vwap(bars, ctx)
    touch = float(params.get("touch_atr", 0.1))

    def cond(k):
        if k < 4:
            return None
        if not (bars[k]["c"] > vw[k] and vw[k] > vw[k - 3] and bars[k - 1]["c"] > vw[k - 1]):
            return None
        a = atr(bars, 14, k)
        if not bars[k]["l"] <= vw[k] + touch * a:
            return None
        return _signal(name, params, k, bars[k]["c"], bars[k]["l"], params["target_r"], stop_rule="bar_low",
                       level=round(vw[k], 2))
    return _scan(bars, ctx, 6, cond)


def vwap_revert(bars, ctx, params, name="vwap_revert"):
    vw = _vwap(bars, ctx)
    sigma = float(params.get("sigma", 2.0))
    stop_atr = float(params.get("stop_atr", 0.5))
    min_r = float(params.get("min_r", 0.5))

    def cond(k):
        sd = returns_std(bars, k)
        if sd is None:
            return None
        b = bars[k]
        thr = vw[k] * (1 - sigma * sd)
        if not (b["c"] <= thr and b["c"] > b["o"]):
            return None
        a = atr(bars, 14, k)
        sig = _signal(name, params, k, b["c"], b["l"] - stop_atr * a, target=vw[k], stop_rule="bar_low_minus_atr",
                      level=round(vw[k], 2), extra={"sigma_pct": round(sigma * sd * 100, 3)})
        if sig and sig["target_r"] < min_r:
            return None
        return sig
    return _scan(bars, ctx, 11, cond)


# ---------------------------------------------------------------- breakouts later in the day

def hod_break(bars, ctx, params, name="hod_break"):
    cons_n = int(params.get("cons_bars", 6))
    cons_atr = float(params.get("cons_atr", 0.6))
    near_atr = float(params.get("near_atr", 0.5))

    def cond(k):
        if k < cons_n + 1:
            return None
        cons = bars[k - cons_n:k]
        hod = max(b["h"] for b in bars[:k])
        a = atr(bars, 14, k)
        ch = max(b["h"] for b in cons)
        cl = min(b["l"] for b in cons)
        if ch - cl > cons_atr * a or hod - ch > near_atr * a:
            return None
        if not bars[k]["c"] > hod:
            return None
        return _signal(name, params, k, bars[k]["c"], cl, params["target_r"], stop_rule="consolidation_low",
                       level=round(hod, 2))
    return _scan(bars, ctx, 17, cond)


def pm_break(bars, ctx, params, name="pm_break"):
    first_k = int(params.get("first_k", 54))
    last_k = int(params.get("last_k", 68))
    stop_bars = int(params.get("stop_bars", 6))

    def cond(k):
        if k > last_k:
            return None
        hod = max(b["h"] for b in bars[:k])
        if not bars[k]["c"] > hod:
            return None
        stop = min(b["l"] for b in bars[k - stop_bars:k])
        return _signal(name, params, k, bars[k]["c"], stop, params["target_r"],
                       stop_rule="low_of_last_%d" % stop_bars, level=round(hod, 2))
    return _scan(bars, ctx, first_k, cond)


def inside_break(bars, ctx, params, name="inside_break"):
    def cond(k):
        p, pp = bars[k - 1], bars[k - 2]
        if not (p["h"] <= pp["h"] and p["l"] >= pp["l"]):
            return None
        if not bars[k]["c"] > p["h"]:
            return None
        return _signal(name, params, k, bars[k]["c"], p["l"], params["target_r"], stop_rule="inside_bar_low",
                       level=round(p["h"], 2))
    return _scan(bars, ctx, 6, cond)


# ---------------------------------------------------------------- the registry

def _r(rs, **base):
    return [dict(base, target_r=r) for r in rs]


SETUPS = {
    "orb15": {"fn": orb, "label": "15-minute opening-range breakout", "base": {"or_minutes": 15},
              "grid": [{"target_r": r, "stop_rule": s} for r in (1.0, 1.5, 2.0) for s in ("or_mid", "or_low")]},
    "orb30": {"fn": orb, "label": "30-minute opening-range breakout", "base": {"or_minutes": 30},
              "grid": _r((1.0, 1.5, 2.0), stop_rule="or_mid")},
    "orb15_vol": {"fn": orb, "label": "15-minute opening-range breakout with volume at least 1x the range's",
                  "base": {"or_minutes": 15, "vol_x": 1.0}, "grid": _r((1.0, 1.5, 2.0), stop_rule="or_mid")},
    "orb15_rs": {"fn": orb, "label": "15-minute opening-range breakout with relative strength over SPY",
                 "base": {"or_minutes": 15, "rs_min": 0.003}, "grid": _r((1.0, 1.5, 2.0), stop_rule="or_mid")},
    "orb15_gap": {"fn": orb, "label": "15-minute opening-range breakout after a gap up of 0.5% or more",
                  "base": {"or_minutes": 15, "gap_min": 0.005}, "grid": _r((1.0, 1.5, 2.0), stop_rule="or_mid")},
    "gap_go": {"fn": gap_go, "label": "gap and go: gap up 1%+, green first bar, close above its high by 10:30",
               "base": {"gap_min": 0.01, "last_k": 11}, "grid": _r((1.0, 2.0))},
    "vwap_reclaim": {"fn": vwap_reclaim, "label": "VWAP reclaim after 3+ bars below", "base": {"below_bars": 3},
                     "grid": _r((1.0, 2.0))},
    "vwap_pullback": {"fn": vwap_pullback, "label": "pullback to a rising VWAP that closes back above it",
                      "base": {"touch_atr": 0.1}, "grid": _r((1.0, 2.0))},
    "hod_break": {"fn": hod_break, "label": "high-of-day breakout after a tight consolidation (after 11:00)",
                  "base": {"cons_bars": 6, "cons_atr": 0.6, "near_atr": 0.5}, "grid": _r((1.0, 2.0))},
    "pm_break": {"fn": pm_break, "label": "afternoon breakout above the day high (2:00 to 3:15)",
                 "base": {"first_k": 54, "last_k": 68, "stop_bars": 6}, "grid": _r((1.0, 2.0))},
    "vwap_revert": {"fn": vwap_revert, "label": "mean reversion: green bar 2 sigma under VWAP, target VWAP",
                    "base": {"sigma": 2.0, "stop_atr": 0.5, "min_r": 0.5}, "grid": [{}]},
    "inside_break": {"fn": inside_break, "label": "inside-bar breakout (after 10:00)", "base": {},
                     "grid": _r((1.0, 2.0))},
}

STOP_WORDS = {"or_mid": "stop at the range midpoint", "or_low": "stop at the range low",
              "first_bar_low": "stop at the first bar's low", "bar_low": "stop at the bar's low",
              "consolidation_low": "stop at the consolidation low", "inside_bar_low": "stop at the inside bar's low",
              "bar_low_minus_atr": "stop half an ATR under the bar's low"}


def config_id(setup, params):
    cid = setup
    if "target_r" in params:
        cid += "|r=%g" % params["target_r"]
    if params.get("stop_rule") and params["stop_rule"] != "or_mid" and setup.startswith("orb"):
        cid += "|stop=%s" % params["stop_rule"]
    return cid


def configs(names=None):
    """Every config of every setup (or of the named setups): [{"id", "setup", "params", "label"}]."""
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
            out.append({"id": config_id(name, params), "setup": name, "params": params, "label": label})
    return out


def run_config(cfg, bars, ctx):
    spec = SETUPS[cfg["setup"]]
    return spec["fn"](bars, ctx, cfg["params"], name=cfg["setup"])


def run_all(bars, ctx, cfgs=None):
    """{config_id: signal} for every config that fires on this (partial or full) session."""
    out = {}
    for cfg in cfgs or configs():
        sig = run_config(cfg, bars, ctx)
        if sig:
            out[cfg["id"]] = sig
    return out
