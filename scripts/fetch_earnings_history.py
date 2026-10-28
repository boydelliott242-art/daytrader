#!/usr/bin/env python3
"""Nasdaq's earnings calendar for every trading date in the daily store, one file a date, resumable.

    python3 scripts/fetch_earnings_history.py [--cache-root trader/cache] [--threads 3] [--since 2024-10-01] [--refresh-last 3]

Writes trader/cache/earnings/YYYY-MM-DD.json: {"date", "fetched_at", "rows": [{symbol, time, eps, epsForecast, surprise,
marketCap, fiscalQuarterEnding, noOfEsts}]}. A date already on disk is skipped unless it is among the last --refresh-last
dates (the calendar fills in EPS after the reports). The pick backtest leaves out each name on its report date, as the live
pick does, and the research reads the surprise of a report made after the prior close (known before the open). Stdlib.
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dt import net, bars as dtbars, io as dtio  # noqa: E402

KEEP = ("symbol", "time", "eps", "epsForecast", "surprise", "marketCap", "fiscalQuarterEnding", "noOfEsts")


def fetch_one(date, out_dir, tries=3):
    for k in range(tries):
        try:
            rows = net.nasdaq_earnings(date)
            doc = {"date": date, "fetched_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "rows": [{key: r.get(key) for key in KEEP} for r in rows if isinstance(r, dict) and r.get("symbol")]}
            dtio.write_json(os.path.join(out_dir, date + ".json"), doc)
            return date, len(doc["rows"]), None
        except Exception as e:  # the next try, then a plain miss
            err = str(e)[:120]
            time.sleep(2.0 * (k + 1))
    return date, None, err


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-root", default=os.path.join("trader", "cache"))
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--since")
    ap.add_argument("--refresh-last", type=int, default=3)
    ap.add_argument("--dates", help="comma separated: only these")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    out_dir = os.path.join(a.cache_root, "earnings")
    os.makedirs(out_dir, exist_ok=True)
    if a.dates:
        dates = [d.strip() for d in a.dates.split(",") if d.strip()]
    else:
        spy = dtbars.daily_rows("SPY", a.cache_root)
        dates = [r[0] for r in spy]
        # the next trading days too: the calendar is known ahead
        last = datetime.date.fromisoformat(dates[-1]) if dates else datetime.date.today()
        d = last
        while len(dates) and d < last + datetime.timedelta(days=7):
            d += datetime.timedelta(days=1)
            if d.weekday() < 5:
                dates.append(d.isoformat())
    if a.since:
        dates = [d for d in dates if d >= a.since]
    fresh = set(dates[-a.refresh_last:]) if a.refresh_last else set()
    todo = [d for d in dates if d in fresh or not os.path.exists(os.path.join(out_dir, d + ".json"))]
    if not a.quiet:
        print("earnings calendar: %d dates, %d to fetch, %d threads -> %s" % (len(dates), len(todo), a.threads, out_dir))
    done = failed = 0
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.threads)) as pool:
        for date, n, err in pool.map(lambda d: fetch_one(d, out_dir), todo):
            if err:
                failed += 1
                if not a.quiet:
                    print("  %s: %s" % (date, err))
            else:
                done += 1
            if not a.quiet and (done + failed) % 25 == 0:
                print("  %d/%d, %d s" % (done + failed, len(todo), int(time.time() - t0)))
    if not a.quiet:
        print("fetched %d, failed %d, in %d s" % (done, failed, int(time.time() - t0)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
