#!/usr/bin/env python3
"""The pre-market history: each large cap's price at 8:05 ET (and the last pre-market print before 9:30), its regular open
and close, per session, kept in trader/cache/premarket/<SYM>.json.gz and merged run over run.

    python3 scripts/fetch_premarket.py [--range 60d] [--threads 4] [--symbols A,B] [--max-age-hours 12]

Yahoo's chart with includePrePost serves 5-minute bars from 4:00 ET for the last 60 days; the evening cache job runs this
with --range 5d so the file keeps every session from now on, past Yahoo's window. The pick uses it to measure how the
8:05 pre-market move relates to the gap at the open and to the day's open-to-close, which no daily store can answer.
Stdlib only; one request per symbol, the Yahoo backoff in dt.net.
"""

import argparse
import collections
import concurrent.futures
import datetime
import gzip
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dt import net, clock, io as dtio  # noqa: E402

# a Yahoo 5-minute bar is stamped with its START: the bar stamped 08:00 closes at 08:05, so "the price at 08:05" is the close of
# the last bar stamped 08:00 or earlier (and the live job, run at 08:06, sees the same bar closed)
CUTOFFS = (("0805", "08:00"), ("0900", "08:55"), ("0925", "09:20"))


def path_of(cache_root, sym):
    return os.path.join(cache_root, "premarket", sym.upper().replace("^", "_").replace("/", ".") + ".json.gz")


def sessions_of(ser):
    """{date: {"pm_0805", "pm_0900", "pm_0925", "pm_last", "pm_first", "pm_bars", "open", "close", "rth_bars", "post_last"}}"""
    days = collections.OrderedDict()
    for t, o, c in zip(ser["t"], ser["o"], ser["c"]):
        if c is None:
            continue
        et = clock.to_eastern(clock.utc_of(t))
        d = et.date().isoformat()
        hm = et.strftime("%H:%M")
        rec = days.setdefault(d, {"pm_bars": 0, "rth_bars": 0, "pm_first": None, "pm_last": None, "open": None, "close": None, "post_last": None})
        if hm < "09:30":
            rec["pm_bars"] += 1
            if rec["pm_first"] is None:
                rec["pm_first"] = round(float(c), 4)
            rec["pm_last"] = round(float(c), 4)
            for name, last_start in CUTOFFS:
                if hm <= last_start:
                    rec["pm_" + name] = round(float(c), 4)
        elif hm < "16:00":
            rec["rth_bars"] += 1
            if rec["open"] is None and o is not None:
                rec["open"] = round(float(o), 4)
            rec["close"] = round(float(c), 4)
        else:
            rec["post_last"] = round(float(c), 4)
    return days


def fetch_one(sym, cache_root, rng):
    try:
        raw = net.yahoo_get(net.chart_path(sym, rng, "5m") + "&includePrePost=true")
        doc = json.loads(raw) if isinstance(raw, str) else raw
        ser = net.series_from_result(doc["chart"]["result"][0])
    except Exception as e:  # one name's failure is a line in the log
        return sym, None, str(e)[:100]
    new = sessions_of(ser)
    path = path_of(cache_root, sym)
    old = dtio.read_json(path, None) or {}
    sess = dict(old.get("sessions") or {})
    today = clock.to_eastern(clock.utc_now()).date().isoformat()
    for d, rec in new.items():
        if rec["rth_bars"] or d != today or d not in sess:
            sess[d] = rec                  # today's partial session is kept only until the full one replaces it
    dtio.write_json(path, {"symbol": sym, "source": "yahoo 5m includePrePost", "updated_at": clock.iso(clock.utc_now()),
                           "sessions": dict(sorted(sess.items()))})
    return sym, len(new), None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-root", default=os.path.join(ROOT, "trader", "cache"))
    ap.add_argument("--largecap", default=os.path.join(ROOT, "trader", "largecap.json"))
    ap.add_argument("--symbols")
    ap.add_argument("--range", default="60d")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--max-age-hours", type=float, default=12.0, help="skip a symbol whose file is newer than this")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        lc = dtio.read_json(a.largecap, None) or {}
        syms = [r["symbol"] for r in lc.get("symbols") or [] if isinstance(r, dict) and r.get("symbol")]
    os.makedirs(os.path.join(a.cache_root, "premarket"), exist_ok=True)
    now = time.time()
    todo = [s for s in syms if not (os.path.exists(path_of(a.cache_root, s)) and now - os.path.getmtime(path_of(a.cache_root, s)) < a.max_age_hours * 3600)]
    if not a.quiet:
        print("pre-market history: %d symbols, %d to fetch (range %s, %d threads)" % (len(syms), len(todo), a.range, a.threads))
    ok = bad = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.threads)) as pool:
        for i, (sym, n, err) in enumerate(pool.map(lambda s: fetch_one(s, a.cache_root, a.range), todo), 1):
            if err:
                bad += 1
                if not a.quiet:
                    print("  %s: %s" % (sym, err))
            else:
                ok += 1
            if not a.quiet and i % 100 == 0:
                print("  %d/%d, %d s" % (i, len(todo), int(time.time() - t0)))
    if not a.quiet:
        print("fetched %d, failed %d, in %d s" % (ok, bad, int(time.time() - t0)))
    return 0 if bad < max(5, len(todo) // 20) else 1


if __name__ == "__main__":
    sys.exit(main())
