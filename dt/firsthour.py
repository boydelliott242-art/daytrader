"""The first hour of the pick's session: what the path so far already says about the day, counted over past picks.

A path, not a forecast. The first hour (09:30 to 10:30) holds about three quarters of the day's range, and the day's high
or low is set in it on about nine pick mornings in ten, so a first hour that has already run +1 percent is most of the
way to a +2 percent close; the REST of the day from 10:30 was a coin flip in both halves of the record (no first-hour
bucket carried a direction edge from 10:30 to the close: the research of 2026-10-28, 54 tests, none confirmed). The card
may print the table's row for today's first hour beside that sentence, never as a call on the rest of the day.

    bucket_of(first_hour_pct) -> "down" | "flat" | "up"
    first_hour_of(bars, open_) -> (first_hour_pct, close_1030) from regular-session bars (hourly or 5-minute)
    table(picks, hourly_session, h2_first) -> {"buckets": {name: {"all"|"h1"|"h2": stats}}, "n", "missing", "note"}

picks are the walk-forward's records ({"date", "symbol", "oc", "up", "dn"}); hourly_session(sym, date) returns that
day's regular-session hourly bars [[HH:MM, o, h, l, c, v], ...] (trader/cache_long/bars60m) or None. Pure stdlib.
"""

from . import stats as dtstats

EDGES = (-1.0, 1.0)            # fixed before the research looked at outcomes (PREREG of 2026-10-28)
NAMES = ("down", "flat", "up")
NOTE = ("past picks grouped by their first hour (09:30 to 10:30) against the open: how the day ended from the open; the rest of "
        "the day from 10:30 was a coin flip in every group")


def bucket_of(pct):
    if pct is None:
        return None
    if pct < EDGES[0]:
        return "down"
    if pct > EDGES[1]:
        return "up"
    return "flat"


def first_hour_of(bars, open_=None):
    """(first-hour move in percent of the open, the price at 10:30) from regular-session bars [[HH:MM, o, h, l, c, v], ...]
    or {"t"/"hm", "o", "h", "l", "c"} dicts; None when the bars do not reach 10:30."""
    rows = []
    for b in bars or []:
        if isinstance(b, dict):
            rows.append((b.get("hm"), b.get("o"), b.get("c")))
        else:
            rows.append((b[0], b[1], b[4]))
    rows = [r for r in rows if r[0] and r[1] is not None and r[2] is not None]
    if not rows:
        return None
    o = float(open_ if open_ is not None else rows[0][1])
    first = [r for r in rows if r[0] < "10:30"]
    hourly = len(first) == 1 and first[0][0] == "09:30"           # one hourly bar is the whole first hour
    if not first or not (hourly or first[-1][0] >= "10:25"):        # five-minute bars: complete once the 10:25 bar is in
        return None
    c = float(first[-1][2])
    if o <= 0:
        return None
    return (c / o - 1) * 100.0, c


def _stats(recs):
    n = len(recs)
    if not n:
        return {"n": 0}
    out = {"n": n}
    for k, test in (("up", lambda r: r["oc"] >= 2.0), ("dn", lambda r: r["oc"] <= -2.0),
                    ("band", lambda r: 2.0 <= r["oc"] <= 5.0), ("rest_up", lambda r: r["rest"] > 0)):
        c = sum(1 for r in recs if test(r))
        lo, hi = dtstats.wilson(c, n)
        out.update({k: c, k + "_rate": round(c / float(n), 4), k + "_ci": [round(lo, 4), round(hi, 4)]})
    rest = [r["rest"] for r in recs]
    lo, hi = dtstats.bootstrap_mean_ci(rest) if n >= 2 else (None, None)
    out.update({"rest_mean": round(sum(rest) / n, 4), "rest_ci": [round(lo, 4) if lo is not None else None, round(hi, 4) if hi is not None else None]})
    return out


def table(picks, hourly_session, h2_first):
    """The first-hour table over past picks: for each bucket, by half and in all, how the day ended from the open (2 % up,
    2 % down, 2 to 5 % up) and the rest of the day from 10:30."""
    rows, missing = [], 0
    for p in picks:
        bars = hourly_session(p["symbol"], p["date"])
        if not bars or len(bars) < 2:
            missing += 1
            continue
        fh = first_hour_of(bars)
        if fh is None:
            missing += 1
            continue
        pct, c1030 = fh
        close = float(bars[-1][4])
        rows.append({"date": p["date"], "bucket": bucket_of(pct), "oc": float(p["oc"]), "rest": (close / c1030 - 1) * 100.0})
    out = {"buckets": {}, "n": len(rows), "missing": missing, "edges": list(EDGES), "h2_first": h2_first, "note": NOTE}
    for name in NAMES:
        sel = [r for r in rows if r["bucket"] == name]
        out["buckets"][name] = {"all": _stats(sel), "h1": _stats([r for r in sel if r["date"] < h2_first]),
                                "h2": _stats([r for r in sel if r["date"] >= h2_first])}
    return out


def words(entry, pct, table_):
    """The card's sentence for today's first hour, from the table; '' when there is no row for it."""
    name = bucket_of(pct)
    row = ((table_ or {}).get("buckets") or {}).get(name) or {}
    a = row.get("all") or {}
    if not a.get("n"):
        return ""

    def r(k):
        lo, hi = a[k + "_ci"]
        return "%.0f %% [%.0f, %.0f]" % (100 * a[k + "_rate"], 100 * lo, 100 * hi)
    return ("first hour %+.2f %% from the open; of %d past picks whose first hour was %s, %s closed 2 %% or more above the open and %s "
            "2 %% or more below; from 10:30 the rest of the day rose on %s of them, a coin flip" % (
                pct, a["n"], {"down": "down more than 1 %", "flat": "within 1 %", "up": "up more than 1 %"}[name], r("up"), r("dn"), r("rest_up")))


def hours_of(bars5):
    """Regular-session hourly bars [(HH:MM, o, h, l, c)] built from five-minute bars ({"t": UTC seconds, "o", "h", "l",
    "c"}): 09:30, 10:30, ..., the last one cut at the close."""
    from . import clock
    out = {}
    for b in bars5 or []:
        if not isinstance(b.get("t"), (int, float)) or b.get("c") is None:
            continue
        et = clock.to_eastern(clock.utc_of(b["t"]))
        m = et.hour * 60 + et.minute - 570
        if m < 0 or m >= 390:
            continue
        k = m // 60
        o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
        if k not in out:
            out[k] = [o, h, l, c, b["t"]]
        else:
            x = out[k]
            x[1], x[2], x[3] = max(x[1], h), min(x[2], l), c
    return [("%02d:%02d" % divmod(570 + 60 * k, 60), v[0], v[1], v[2], v[3]) for k, v in sorted(out.items())]


def close_confirmed_breakout(bars5, cost_bp=4.0):
    """The shadow entry rule the first-hour research left to watch (2026-10-28, rule d): after an hourly bar CLOSES above
    the first hour's high, buy at the next hour's open, stop at the first hour's low, sell at the close. From five-minute
    bars, so the stop is checked in order. -> {"entered": False} or {"entered": True, "entry_time", "ret_pct" (after
    cost_bp a round trip), "r", "stopped"}; None without a full first hour and one more."""
    hours = hours_of(bars5)
    if len(hours) < 3:
        return None
    hi1, lo1 = hours[0][2], hours[0][3]
    from . import clock
    for k in range(1, len(hours) - 1):
        if hours[k][4] > hi1:
            entry = hours[k + 1][1]
            start = 570 + 60 * (k + 1)
            exit_, stopped = None, False
            for b in bars5:
                et = clock.to_eastern(clock.utc_of(b["t"]))
                m = et.hour * 60 + et.minute
                if m < start or m >= 960 or b.get("l") is None:
                    continue
                if float(b["l"]) <= lo1:
                    exit_, stopped = min(lo1, float(b["o"])), True     # a gap through the stop fills at the open
                    break
                exit_ = float(b["c"])
            if exit_ is None or entry <= 0:
                return None
            ret = (exit_ / entry - 1) * 100.0 - cost_bp / 100.0
            risk = (entry - lo1) / entry * 100.0
            return {"entered": True, "entry_time": hours[k + 1][0], "ret_pct": round(ret, 3), "r": round(ret / risk, 3) if risk > 0 else None,
                    "stopped": stopped}
    return {"entered": False}
