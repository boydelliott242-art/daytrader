"""The morning's own evidence for the pick: the pre-market move at 8:05 ET and whether the name reported the session before.

An extra feature module in the shape scripts/pick_research.py and dt.pick read (CUTS, CATEGORICAL, compute):

    pm_move   the last pre-market print at 8:05 ET against the prior close, percent (trader/cache/premarket, from
              scripts/fetch_premarket.py; Yahoo keeps 60 days, the nightly job keeps every session from then on)
    rep_prev  "yes" when the name reported earnings on the session before (Nasdaq calendar): its gap is an earnings gap

Both are known at 8:05 on the date they describe; neither reads the session itself, so the leak check passes. Before the
pre-market history begins, pm_move is None (its own na bucket) and the model learns nothing from it there.
Why it is here: over two years of the daily store, large caps that opened 3 percent or more down on a news-free morning
closed +0.40 and +0.33 percent from the open in the two halves (intervals clear of zero) and closed 2 percent up more
often than 2 percent down; earnings gaps showed no reliable direction. The 8:05 print is the morning's estimate of that gap.
"""

import gzip
import json
import os

CUTS = {"pm_move": (-3.0, -1.5, -0.5, 0.5, 1.5, 3.0)}
CATEGORICAL = {"rep_prev": ("yes", "no")}
_CACHE = {}


def premarket_sessions(cache_root, sym):
    key = (cache_root, sym)
    if key not in _CACHE:
        path = os.path.join(cache_root, "premarket", sym.upper().replace("^", "_").replace("/", ".") + ".json.gz")
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                _CACHE[key] = json.load(fh).get("sessions") or {}
        except (OSError, ValueError):
            _CACHE[key] = {}
    return _CACHE[key]


def compute(sym, rows, ctx):
    """{date: {"pm_move", "rep_prev"}} for every session with a prior session in rows."""
    cache_root = ctx.get("cache_root") or ""
    pm = premarket_sessions(cache_root, sym) if cache_root else {}
    reports = set((ctx.get("earnings") or {}).keys())
    out = {}
    for i in range(1, len(rows)):
        d = rows[i][0]
        prev_d, prev_c = rows[i - 1][0], rows[i - 1][4]
        rec = pm.get(d) or {}
        p = rec.get("pm_0805")
        move = (float(p) / float(prev_c) - 1) * 100.0 if (p and prev_c) else None
        out[d] = {"pm_move": move, "rep_prev": "yes" if prev_d in reports else "no"}
    return out
