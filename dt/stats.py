"""Honest numbers: Wilson intervals, bootstrap CIs, profit factor, drawdown, the STATS block."""

import math
import random
import statistics

SEED = 19
DRAWS = 2000


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k successes in n trials -> (lo, hi)."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / float(n)
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_mean_ci(values, draws=DRAWS, seed=SEED, alpha=0.05):
    """Percentile bootstrap CI of the mean; deterministic with the seed."""
    n = len(values)
    if n == 0:
        return (None, None)
    if n == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    means = []
    choice = rng.choice
    for _ in range(draws):
        means.append(sum(choice(values) for _ in range(n)) / float(n))
    means.sort()
    lo = means[int(alpha / 2 * draws)]
    hi = means[min(draws - 1, int((1 - alpha / 2) * draws))]
    return (lo, hi)


def profit_factor(rs):
    wins = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    if losses == 0:
        return None if wins == 0 else float("inf")
    return wins / losses


def max_drawdown(rs):
    """Largest peak-to-trough fall of the cumulative R curve, in R (>= 0)."""
    peak = cum = 0.0
    dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def stats_block(trades, r_key="R_net"):
    """The STATS block of CONTRACTS.md from a list of trade dicts (outcome, R_net, symbol, date)."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "target": 0, "stop": 0, "close": 0, "green": 0, "win_rate": None, "win_ci95": [None, None],
                "exp_R": None, "exp_ci95": [None, None], "pf": None, "avg_win_R": None, "avg_loss_R": None,
                "max_dd_R": None, "median_R": None, "n_symbols": 0, "n_sessions": 0}
    rs = [float(t[r_key]) for t in trades]
    target = sum(1 for t in trades if t.get("outcome") == "target")
    stop = sum(1 for t in trades if t.get("outcome") == "stop")
    close = n - target - stop
    green = sum(1 for r in rs if r > 0)
    lo, hi = wilson(target, n)
    elo, ehi = bootstrap_mean_ci(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    pf = profit_factor(rs)
    return {
        "n": n, "target": target, "stop": stop, "close": close, "green": green,
        "win_rate": round(target / float(n), 4), "win_ci95": [round(lo, 4), round(hi, 4)],
        "exp_R": round(statistics.mean(rs), 4), "exp_ci95": [round(elo, 4), round(ehi, 4)],
        "pf": (None if pf is None else (999.0 if pf == float("inf") else round(pf, 3))),
        "avg_win_R": round(statistics.mean(wins), 4) if wins else None,
        "avg_loss_R": round(statistics.mean(losses), 4) if losses else None,
        "max_dd_R": round(max_drawdown(rs), 3), "median_R": round(statistics.median(rs), 4),
        "n_symbols": len(set(t.get("symbol") for t in trades)),
        "n_sessions": len(set(t.get("date") for t in trades)),
    }


def status_of(oos):
    """The desk's gate: approved / watch / rejected from an OOS STATS block."""
    if not oos or not oos.get("n"):
        return "rejected"
    lo = (oos.get("exp_ci95") or [None, None])[0]
    if oos["n"] >= 100 and lo is not None and lo > 0:
        return "approved"
    if oos["n"] >= 30 and (oos.get("exp_R") or 0) > 0:
        return "watch"
    return "rejected"
