"""Hand-built sessions and offline helpers for the dt/ tests.

make_session(day, closes) or make_session(day, rows=[(o, h, l, c, v), ...]) builds a FULL
session (78 bars, or 42 on an early close) on the 5-minute grid with the bar start as a UTC
epoch second from dt.clock.eastern_to_utc, exactly the shape dt.bars keeps. Fewer rows than
the grid are padded with flat bars at the last close, so a test can draw the bars it cares
about and leave the rest of the day quiet.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dt import clock  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
CACHE = os.path.join(FIX, "dt-cache")
DAY = "2026-10-19"          # the reel's Monday, a full 78-bar session
HALF_DAY = "2026-11-27"     # an early close: 42 bars
VOL = 100000


def ts_of_slot(day, k):
    """UTC epoch second at which slot k of `day` starts (slot 0 = 9:30 New York)."""
    return clock.ts_of(clock.eastern_to_utc(clock.slot_time(day, k)))


def bar(day, k, o, h, l, c, v=VOL):
    return {"t": ts_of_slot(day, k), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": int(v)}


def flat_row(price, v=VOL):
    return (price, price, price, price, v)


def rows_from_closes(closes, vol=VOL, wick=0.0):
    """(o, h, l, c, v) rows: each bar opens at the close before it, high/low wrap open and close
    (plus an optional wick on each side)."""
    out = []
    prev = closes[0]
    for c in closes:
        o = prev
        out.append((o, max(o, c) + wick, min(o, c) - wick, c, vol))
        prev = c
    return out


def make_session(day=DAY, closes=None, rows=None, vol=VOL, wick=0.0, pad=True):
    """A full session for `day` from closes or (o, h, l, c, v) rows (see the module docstring)."""
    n = clock.slots_of_day(day)
    if rows is None:
        if closes is None:
            closes = [100.0]
        rows = rows_from_closes(list(closes), vol, wick)
    rows = [tuple(r) for r in rows]
    if len(rows) > n:
        raise ValueError("%d rows do not fit a %d-slot session" % (len(rows), n))
    if pad and len(rows) < n:
        last = rows[-1][3] if rows else 100.0
        rows = rows + [flat_row(last, vol)] * (n - len(rows))
    return [bar(day, k, *r) for k, r in enumerate(rows)]


def series_of(sessions):
    """{day: bars} -> a Yahoo-style series {"t", "o", "h", "l", "c", "v"} in time order."""
    allb = []
    for day in sorted(sessions):
        allb.extend(sessions[day])
    allb.sort(key=lambda b: b["t"])
    return {"t": [b["t"] for b in allb], "o": [b["o"] for b in allb], "h": [b["h"] for b in allb],
            "l": [b["l"] for b in allb], "c": [b["c"] for b in allb], "v": [b["v"] for b in allb]}


def chart_doc(series, symbol="TEST", extra_meta=None):
    """Wrap a series as a Yahoo v8 chart document (what dt.net.parse_chart reads)."""
    meta = {"symbol": symbol, "currency": "USD", "dataGranularity": "5m"}
    if extra_meta:
        meta.update(extra_meta)
    return {"chart": {"result": [{"meta": meta, "timestamp": list(series["t"]),
                                  "indicators": {"quote": [{"open": list(series["o"]), "high": list(series["h"]),
                                                            "low": list(series["l"]), "close": list(series["c"]),
                                                            "volume": list(series["v"])}]}}],
                      "error": None}}


def daily_rows(start_price=100.0, n=60, step=0.0, rng=1.0, vol=1000000, first="2026-07-01"):
    """n daily rows [date, o, h, l, c, v] on consecutive weekdays from `first`, closes moving by
    `step` a day with a high-low range of `rng` around the close."""
    import datetime
    d = datetime.date.fromisoformat(first)
    out = []
    c = start_price
    while len(out) < n:
        if d.weekday() < 5:
            out.append([d.isoformat(), c, c + rng / 2.0, c - rng / 2.0, c, vol])
            c += step
        d += datetime.timedelta(days=1)
    return out


def count_assertions(module_text):
    """How many self.assert... calls a test file holds (used by the suite's own sanity test)."""
    return module_text.count("self.assert")
