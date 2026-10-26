"""The feature study and the weights test over the lab's own trades (CONTRACTS.md, "The research desk", Features, Scores).

Both read trader/lab/trades.jsonl.gz (one line per trade with symbol, date, config_id, side, R_net) and compute the
features for each (symbol, date) from the daily store strictly before the date. The study buckets a config's OOS
trades by each feature's terciles and says whether the top and bottom terciles' expectancy intervals separate; the
weights test scores each trade with the code-only momentum terms and prints expectancy by score quintile. Neither is a
backtest of the desk: both are the ground a weight is allowed to stand on, nothing more.
"""

import gzip
import json
import math
import os

from . import bars as dtbars, features as dtfeatures, scoring, stats as dtstats

MIN_TRADES = 30
STUDY_FEATURES = ("ma_structure", "weekly_structure", "roc_5", "roc_20", "roc_63", "rs_spy_20", "rs_spy_63", "sma20_slope_pct", "sma50_slope_pct", "adx_14", "rsi_14",
                  "macd_hist", "atr_pct", "realized_vol_20", "relvol_20", "vol_accel", "updown_vol_ratio_20", "dist_52w_high_pct",
                  "up_days_20", "trend_r2_63", "weekly_roc_13", "vol_adj_mom_63", "adv_usd_20", "beta_spy_63")
REDUNDANT_RHO = 0.8


def read_trades(path, oos_first=None, configs=None):
    """The slim trade rows (symbol, date, config_id, side, R_net, outcome) from the gz file, OOS only when oos_first is given."""
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except ValueError:
                continue
            if configs and t.get("config_id") not in configs:
                continue
            if oos_first and (t.get("date") or "") < oos_first:
                continue
            out.append({"symbol": t.get("symbol"), "date": t.get("date"), "config_id": t.get("config_id"), "side": t.get("side") or "long",
                        "R_net": t.get("R_net"), "outcome": t.get("outcome")})
    return out


class FeatureCache:
    def __init__(self, cache_root):
        self.root = cache_root
        self.spy = dtbars.daily_rows("SPY", cache_root)
        self.daily = {}
        self.cache = {}

    def get(self, sym, date):
        key = (sym, date)
        if key not in self.cache:
            if sym not in self.daily:
                self.daily[sym] = dtbars.daily_rows(sym, self.root)
            rows = self.daily[sym]
            self.cache[key] = dtfeatures.features_for(sym, date, rows, None, self.spy) if rows else {"missing": list(dtfeatures.NAMES)}
        return self.cache[key]


STRUCTURE_VALUE = {"up": 1.0, "mixed": 0.5, "down": 0.0}


def numeric(name, value):
    """A feature as a number for the study: the structures map up / mixed / down to 1 / 0.5 / 0; booleans to 1 / 0."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, str):
        return STRUCTURE_VALUE.get(value)
    return float(value)


def terciles(values):
    s = sorted(values)
    n = len(s)
    return s[n // 3], s[(2 * n) // 3]


def _rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def spearman(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 10:
        return None
    xs, ys = _rank([p[0] for p in pairs]), _rank([p[1] for p in pairs])
    n = len(pairs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return round(sxy / math.sqrt(sxx * syy), 3)


def study_config(trades, fc, features=STUDY_FEATURES, min_trades=MIN_TRADES):
    """One config's trades -> {feature: {terciles: [stats x3], adds_value, direction}} plus the redundancy pairs."""
    rows = []
    for t in trades:
        f = fc.get(t["symbol"], t["date"])
        rows.append((t, f))
    out = {"n": len(rows), "features": {}, "redundant": []}
    if len(rows) < min_trades:
        out["note"] = "under %d trades: no study" % min_trades
        return out
    cols = {}
    for name in features:
        vals = [(numeric(name, f.get(name)), t) for t, f in rows if numeric(name, f.get(name)) is not None]
        cols[name] = [v for v, _ in vals]
        if len(vals) < min_trades:
            out["features"][name] = {"n": len(vals), "note": "not enough values"}
            continue
        lo, hi = terciles([v for v, _ in vals])
        buckets = {"low": [], "mid": [], "high": []}
        for v, t in vals:
            buckets["low" if v <= lo else "high" if v > hi else "mid"].append(t)
        st = {k: dtstats.stats_block(b) if b else None for k, b in buckets.items()}
        adds, direction = False, None
        if st["low"] and st["high"] and st["low"]["n"] >= 10 and st["high"]["n"] >= 10:
            lo_ci, hi_ci = st["low"]["exp_ci95"], st["high"]["exp_ci95"]
            if hi_ci[0] is not None and lo_ci[1] is not None and hi_ci[0] > lo_ci[1]:
                adds, direction = True, "higher is better"
            elif lo_ci[0] is not None and hi_ci[1] is not None and lo_ci[0] > hi_ci[1]:
                adds, direction = True, "lower is better"
            mid = (st["mid"] or {}).get("exp_R")
            if adds and mid is not None and not (min(st["low"]["exp_R"], st["high"]["exp_R"]) - 0.02 <= mid <= max(st["low"]["exp_R"], st["high"]["exp_R"]) + 0.02):
                adds, direction = False, direction + " but the middle tercile does not sit between (not monotone)"
        out["features"][name] = {"n": len(vals), "cut_low": round(lo, 4), "cut_high": round(hi, 4),
                                 "terciles": {k: (dict(exp_R=v["exp_R"], exp_ci95=v["exp_ci95"], n=v["n"], win_rate=v["win_rate"]) if v else None) for k, v in st.items()},
                                 "adds_value": adds, "direction": direction}
    names = [n for n in features if len(cols.get(n) or []) >= min_trades]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if len(cols[a]) == len(cols[b]):
                rho = spearman(cols[a], cols[b])
                if rho is not None and abs(rho) >= REDUNDANT_RHO:
                    out["redundant"].append({"a": a, "b": b, "spearman": rho})
    return out


def run_study(trades_path, cache_root, edge, configs=None, min_trades=MIN_TRADES):
    """The whole study over the OOS trades of the given configs (default: every approved and watch config)."""
    oos_first = ((edge.get("split") or {}).get("oos") or {}).get("first")
    if not configs:
        configs = [c["id"] for c in edge.get("configs") or [] if c.get("status") in ("approved", "watch")]
    trades = read_trades(trades_path, oos_first, set(configs))
    fc = FeatureCache(cache_root)
    by = {}
    for t in trades:
        by.setdefault(t["config_id"], []).append(t)
    per = {}
    for cid in configs:
        per[cid] = study_config(by.get(cid, []), fc, min_trades=min_trades)
        per[cid]["side"] = (by.get(cid) or [{}])[0].get("side", "long") if by.get(cid) else next(
            (c.get("side", "long") for c in edge.get("configs") or [] if c.get("id") == cid), "long")
    adds = {"long": {}, "short": {}}
    for cid, doc in per.items():
        side = doc.get("side", "long")
        for name, f in (doc.get("features") or {}).items():
            if "adds_value" in f:
                adds[side][name] = adds[side].get(name, False) or bool(f["adds_value"])
    return {"created_by": "trader-lab", "kind": "features", "oos_first": oos_first, "configs": configs, "configs_tested": edge.get("configs_tested"),
            "min_trades": min_trades, "per_config": per, "adds_value": adds, "features": list(STUDY_FEATURES),
            "note": "A feature adds value when the top and bottom terciles' expectancy intervals do not overlap and the middle sits between, on "
                    "that config's OOS trades. With %d features and %d configs something separates by luck; the desk's weights use only features that "
                    "add value on their side, and the card says when the study has not run." % (len(STUDY_FEATURES), len(configs))}


def weights_test(trades_path, cache_root, edge, weights, configs=None, quintiles=5):
    """Score every OOS trade of the configs with the code-only momentum terms (on the trade's side) and bucket by quintile."""
    oos_first = ((edge.get("split") or {}).get("oos") or {}).get("first")
    if not configs:
        configs = [c["id"] for c in edge.get("configs") or [] if c.get("status") in ("approved", "watch")]
    trades = read_trades(trades_path, oos_first, set(configs))
    fc = FeatureCache(cache_root)
    scored = []
    for t in trades:
        f = fc.get(t["symbol"], t["date"])
        s, _ = scoring.momentum(f, weights, t["side"])
        if s is not None:
            scored.append((s, t))
    out = {"created_by": "trader-agents", "kind": "weights-test", "oos_first": oos_first, "configs": configs, "n": len(scored), "by_quintile": [],
           "note": "The momentum score (code-only terms) on each OOS trade of the lab, by quintile: if expectancy does not rise with the score, "
                   "the weights have no ground. The agent terms cannot be tested this way and carry no weight in the code score."}
    if len(scored) < quintiles * 10:
        out["note"] = "only %d scored trades: no quintiles" % len(scored)
        return out
    scored.sort(key=lambda x: x[0])
    n = len(scored)
    for q in range(quintiles):
        chunk = scored[q * n // quintiles:(q + 1) * n // quintiles]
        st = dtstats.stats_block([t for _, t in chunk])
        out["by_quintile"].append({"quintile": q + 1, "score_min": chunk[0][0], "score_max": chunk[-1][0], "n": st["n"], "exp_R": st["exp_R"],
                                   "exp_ci95": st["exp_ci95"], "win_rate": st["win_rate"]})
    first, last = out["by_quintile"][0], out["by_quintile"][-1]
    out["monotone"] = all(out["by_quintile"][i]["exp_R"] <= out["by_quintile"][i + 1]["exp_R"] for i in range(quintiles - 1))
    out["separates"] = (last["exp_ci95"][0] is not None and first["exp_ci95"][1] is not None and last["exp_ci95"][0] > first["exp_ci95"][1])
    out["verdict"] = ("the top quintile's lower bound clears the bottom quintile's upper bound" if out["separates"] else
                      "the quintiles do not separate: the weights have no measured ground on these trades")
    return out
