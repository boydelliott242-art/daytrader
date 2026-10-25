"""Play a signal forward on real bars: stop before target inside a bar, next-open or plan fill,
costs in R. Nothing here looks at a bar before the entry bar's close."""

from . import clock
from .bars import cents_down

DEFAULT_COST_BPS = 4.0


def play(bars, signal, fill="next_open", until=None, cost_bps=DEFAULT_COST_BPS):
    """-> outcome dict (CONTRACTS.md) or None when there is no bar left to fill in.
    bars: the session (full or partial); signal: dt.setups signal; until: last slot to hold
    through (default: the flat-by slot for a 78-bar day); fill: next_open | close."""
    k = signal["k"]
    n = len(bars)
    if until is None:
        until = min(n - 1, (clock.SESSION_SLOTS if n > clock.EARLY_SLOTS else clock.EARLY_SLOTS) - 2)
    until = min(until, n - 1)
    stop, target = signal["stop"], signal["target"]
    risk = signal["entry"] - stop
    if risk <= 0:
        return None
    if fill == "next_open":
        if k + 1 > until:
            return None
        entry_fill = float(bars[k + 1]["o"])
        start = k + 1
    else:
        entry_fill = float(signal["entry"])
        start = k + 1
    outcome, exit_slot, exit_price = None, None, None
    worst, best = entry_fill, entry_fill
    for j in range(start, until + 1):
        b = bars[j]
        worst = min(worst, b["l"])
        best = max(best, b["h"])
        if b["l"] <= stop:
            outcome, exit_slot, exit_price = "stop", j, stop
            break
        if b["h"] >= target:
            outcome, exit_slot, exit_price = "target", j, target
            break
    if outcome is None:
        outcome, exit_slot, exit_price = "close", until, float(bars[until]["c"])
    r_gross = (exit_price - entry_fill) / risk
    cost = entry_fill * cost_bps / 10000.0
    r_net = r_gross - cost / risk
    return {"fill": fill, "entry_fill": round(entry_fill, 4), "outcome": outcome, "exit_slot": exit_slot,
            "exit_price": round(exit_price, 4), "R_gross": round(r_gross, 4), "R_net": round(r_net, 4),
            "mae_R": round((worst - entry_fill) / risk, 4), "mfe_R": round((best - entry_fill) / risk, 4),
            "bars_held": exit_slot - start + 1, "cost_bps": cost_bps}


def play_both(bars, signal, until=None, cost_bps=DEFAULT_COST_BPS):
    return {"next_open": play(bars, signal, "next_open", until, cost_bps),
            "close": play(bars, signal, "close", until, cost_bps)}
