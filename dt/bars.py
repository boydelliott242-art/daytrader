"""Bars: Yahoo series -> sessions on the 5-minute grid, the bar store, VWAP, ATR."""

import datetime
import os
import statistics

from . import clock
from .io import CACHE, read_json, write_json

STORE_KINDS = ("bars5m", "daily")


def series_to_sessions(series, now_utc=None, include_partial=False):
    """{date: [bar...]} from a Yahoo 5-minute series: regular-session bars on the grid,
    with a close, complete by now_utc (a bar is complete when its start + 5 min <= now).
    Returns only FULL sessions unless include_partial (then today's partial day too)."""
    now_et = clock.to_eastern(now_utc or clock.utc_now())
    by_day = {}
    hs, ls, os_, vs, cs = series.get("h"), series.get("l"), series.get("o"), series.get("v"), series["c"]
    for i, ts in enumerate(series["t"]):
        c = cs[i]
        if c is None:
            continue
        et = clock.to_eastern(clock.utc_of(ts))
        k = clock.slot_of(et)
        if k is None:
            continue
        if et + datetime.timedelta(minutes=clock.BAR_MIN) > now_et:
            continue
        o = os_[i] if os_ else None
        h = hs[i] if hs else None
        l = ls[i] if ls else None
        if o is None or h is None or l is None:
            continue
        v = (vs[i] if vs else 0) or 0
        day = et.date().isoformat()
        by_day.setdefault(day, {})[k] = {"t": int(ts), "o": float(o), "h": float(h), "l": float(l),
                                          "c": float(c), "v": int(v)}
    out = {}
    for day, slots in by_day.items():
        n = clock.slots_of_day(day)
        bars = [slots[k] for k in range(n) if k in slots]
        full = len(bars) == n
        if full or include_partial:
            # partial: only a contiguous prefix counts (a hole means a halt or thin data)
            if not full:
                prefix = []
                for k in range(n):
                    if k in slots:
                        prefix.append(slots[k])
                    else:
                        break
                bars = prefix
            if bars:
                out[day] = bars
    return out


def is_full(bars, day):
    return len(bars) == clock.slots_of_day(day)


def rows_of(bars):
    return [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars]


def bars_of(rows):
    return [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in rows]


def store_path(sym, kind="bars5m", root=None):
    if kind not in STORE_KINDS:
        raise ValueError("unknown store kind %r" % kind)
    return os.path.join(root or CACHE, kind, "%s.json.gz" % sym.upper().replace("^", "_"))


def load_store(sym, kind="bars5m", root=None):
    return read_json(store_path(sym, kind, root))


def save_store(sym, doc, kind="bars5m", root=None):
    write_json(store_path(sym, kind, root), doc)


def sessions_of(store, before=None, last_n=None):
    """Sorted [(date, bars)] from a bars5m store; before = exclusive date cutoff."""
    if not store:
        return []
    items = []
    for day, rows in (store.get("sessions") or {}).items():
        if before and day >= before:
            continue
        items.append((day, bars_of(rows)))
    items.sort()
    if last_n:
        items = items[-last_n:]
    return items


def load_sessions(sym, before=None, last_n=None, root=None):
    return sessions_of(load_store(sym, "bars5m", root), before, last_n)


def daily_rows(sym, root=None):
    doc = load_store(sym, "daily", root)
    return (doc or {}).get("rows") or []


def prev_close(rows, date_str):
    """The last daily close strictly before date_str, or None."""
    best = None
    for r in rows:
        if r[0] < date_str and r[4] is not None:
            best = r[4]
        elif r[0] >= date_str:
            break
    return best


def daily_before(rows, date_str, n):
    out = [r for r in rows if r[0] < date_str and r[4] is not None]
    return out[-n:] if n else out


def sma(values, n):
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / float(n)


def daily_atr(rows, date_str, n=14):
    """Wilder-free simple ATR of the n daily bars before date_str (true range with the prior close)."""
    rs = daily_before(rows, date_str, n + 1)
    if len(rs) < 2:
        return None
    trs = []
    for i in range(1, len(rs)):
        pc = rs[i - 1][4]
        h, l = rs[i][2], rs[i][3]
        if h is None or l is None or pc is None:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def avg_volume(rows, date_str, n=20):
    rs = daily_before(rows, date_str, n)
    vs = [r[5] for r in rs if r[5]]
    return sum(vs) / float(len(vs)) if vs else None


def opening_range(bars, n):
    rows = bars[:n]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def vwap_series(bars):
    """Cumulative VWAP by bar on typical price (h+l+c)/3; vwap[k] uses bars[:k+1] only."""
    out = []
    pv = vol = 0.0
    for b in bars:
        v = b["v"] or 0
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        pv += tp * v
        vol += v
        out.append(pv / vol if vol > 0 else b["c"])
    return out


def atr(bars, n=14, upto=None):
    """ATR over the last n bars up to index `upto` (inclusive); fewer bars -> average of what is there."""
    rows = bars if upto is None else bars[:upto + 1]
    if not rows:
        return None
    trs = []
    for i, b in enumerate(rows):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = rows[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    last = trs[-n:]
    return sum(last) / float(len(last))


def returns_std(bars, upto):
    """Population stdev of close-to-close returns over bars[:upto+1]; None with fewer than 8 returns."""
    cs = [b["c"] for b in bars[:upto + 1]]
    rets = [cs[i] / cs[i - 1] - 1.0 for i in range(1, len(cs)) if cs[i - 1]]
    if len(rets) < 8:
        return None
    return statistics.pstdev(rets)


def cents_down(x):
    import math
    return math.floor(round(x * 100, 6)) / 100.0


def cents_up(x):
    import math
    return math.ceil(round(x * 100, 6)) / 100.0


def half_up(x):
    import decimal
    return float(decimal.Decimal(repr(x)).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP))


def session_day_type(bars):
    """Hindsight label from a full session: trend_day when |close - open| >= 0.6 x range."""
    if not bars:
        return None
    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    rng = hi - lo
    if rng <= 0:
        return "range_day"
    return "trend_day" if abs(bars[-1]["c"] - bars[0]["o"]) >= 0.6 * rng else "range_day"
