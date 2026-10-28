#!/usr/bin/env python3
"""Long daily history for research: every name a large-cap list has held (trader/largecap.json and the dated copies in
trader/largecap_history) plus the market context, as daily rows over --range (default 10y), into a separate store
(--root, default trader/cache_long). The live store under trader/cache, which the desk and the pick read, is never
touched.

    python3 scripts/fetch_daily_history.py [--range 10y] [--root trader/cache_long] [--threads 3] [--max-age-hours 24]
    python3 scripts/fetch_daily_history.py --interval 60m --range 2y       # hourly bars, regular session, root/bars60m

The rows are Yahoo's daily bars (split-adjusted closes, not dividend-adjusted), the shape of trader/cache/daily, and only
full sessions are kept. Research only: scripts/pick_research.py --cache-root trader/cache_long reads them, and
scripts/fetch_earnings_history.py --cache-root trader/cache_long fills the calendar for the same days. A longer history
also carries more survivorship: names that were delisted are not in it. Stdlib; the Yahoo backoff in dt.net.
"""

import argparse
import concurrent.futures
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from dt import bars, clock, io as dtio, net, pick as dtpick  # noqa: E402
from fetch_cache import daily_rows_from_series  # noqa: E402


def symbols_of(largecap):
    """Tonight's names, then every name a kept list held, then the context symbols."""
    docs = []
    hist = os.path.join(os.path.dirname(largecap) or ".", "largecap_history")
    if os.path.isdir(hist):
        for f in sorted(os.listdir(hist)):
            if f.endswith(".json"):
                d = dtio.read_json(os.path.join(hist, f), None)
                if isinstance(d, dict):
                    docs.append(d)
    cur = dtio.read_json(largecap, None)
    if isinstance(cur, dict):
        docs.append(cur)
    out = []
    for d in reversed(docs):
        for r in d.get("symbols") or []:
            s = r.get("symbol") if isinstance(r, dict) else None
            if s and s not in out:
                out.append(s)
    return out + [s for s in sorted(dtpick.CONTEXT) if s not in out]


def intraday_path(root, sym, interval):
    return os.path.join(root, "bars" + interval, sym.upper().replace("^", "_") + ".json.gz")


def intraday_sessions(series, now):
    """{date: [[HH:MM, o, h, l, c, v], ...]} of regular-session bars (a bar is stamped with its start), full sessions only."""
    now_et = clock.to_eastern(now)
    out = {}
    for t, o, h, l, c, v in zip(series["t"], series["o"], series["h"], series["l"], series["c"], series["v"]):
        if c is None or o is None:
            continue
        et = clock.to_eastern(clock.utc_of(t))
        hm = et.strftime("%H:%M")
        if not ("09:30" <= hm < "16:00"):
            continue
        d = et.date()
        s = clock.session(d)
        if d == now_et.date() and (not s or now_et.time() < s[1]):
            continue
        out.setdefault(d.isoformat(), []).append([hm, o, h, l, float(c), int(v or 0)])
    return out


def fetch_one(sym, root, rng, now, interval="1d"):
    try:
        series = net.chart(sym, rng, interval)
        if interval == "1d":
            rows = daily_rows_from_series(series, now)
        else:
            rows = intraday_sessions(series, now)
    except Exception as e:  # one name's failure is a line in the log
        return sym, None, str(e)[:100]
    if not rows:
        return sym, 0, "no rows"
    if interval == "1d":
        bars.save_store(sym, {"symbol": sym, "interval": "1d", "source": "yahoo", "range": rng, "updated_at": dtio.now_iso(), "rows": rows}, "daily", root)
    else:
        dtio.write_json(intraday_path(root, sym, interval), {"symbol": sym, "interval": interval, "source": "yahoo", "range": rng,
                                                              "updated_at": dtio.now_iso(), "sessions": rows})
    return sym, len(rows), None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--largecap", default=os.path.join(ROOT, "trader", "largecap.json"))
    ap.add_argument("--root", default=os.path.join(ROOT, "trader", "cache_long"))
    ap.add_argument("--range", default="10y")
    ap.add_argument("--interval", default="1d", help="1d (the daily store) or an intraday bar such as 60m (root/bars60m: Yahoo keeps 2 years of hourly bars)")
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--max-age-hours", type=float, default=24.0, help="skip a symbol whose file is newer than this")
    ap.add_argument("--symbols", help="comma separated: only these")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if os.path.abspath(a.root) == os.path.abspath(os.path.join(ROOT, "trader", "cache")):
        print("fetch_daily_history.py: refusing to write the live store; pick another --root", file=sys.stderr)
        return 2
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] if a.symbols else symbols_of(a.largecap)
    os.makedirs(os.path.join(a.root, "daily" if a.interval == "1d" else "bars" + a.interval), exist_ok=True)
    now = clock.utc_now()
    fresh_s = time.time()

    def stale(s):
        p = bars.store_path(s, "daily", a.root) if a.interval == "1d" else intraday_path(a.root, s, a.interval)
        return not (os.path.exists(p) and fresh_s - os.path.getmtime(p) < a.max_age_hours * 3600)
    todo = [s for s in syms if stale(s)]
    if not a.quiet:
        print("long daily history: %d symbols, %d to fetch, range %s, %d threads -> %s" % (len(syms), len(todo), a.range, a.threads, a.root))
    ok = bad = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.threads)) as pool:
        for i, (sym, n, err) in enumerate(pool.map(lambda s: fetch_one(s, a.root, a.range, now, a.interval), todo), 1):
            if err:
                bad += 1
                if not a.quiet:
                    print("  %s: %s" % (sym, err))
            else:
                ok += 1
            if not a.quiet and i % 100 == 0:
                print("  %d/%d, %d s" % (i, len(todo), int(time.time() - t0)), flush=True)
    if not a.quiet:
        print("fetched %d, failed %d, in %d s" % (ok, bad, int(time.time() - t0)))
    return 0 if bad < max(5, len(todo) // 20) else 1


if __name__ == "__main__":
    sys.exit(main())
