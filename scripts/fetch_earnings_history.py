#!/usr/bin/env python3
"""Nasdaq's earnings calendar for every calendar day the daily store covers, one file a day, resumable.

    python3 scripts/fetch_earnings_history.py [--cache-root trader/cache] [--threads 3] [--since 2024-10-01] [--refresh-last 3]

Writes trader/cache/earnings/YYYY-MM-DD.json: {"date", "fetched_at", "rows": [{symbol, time, eps, epsForecast, surprise,
marketCap, fiscalQuarterEnding, noOfEsts}]}. Weekends and holidays are fetched too: a company that reports on a Saturday
(Berkshire) moves on the next session. A day already on disk is skipped unless it is among the last --refresh-last days
(the calendar fills in EPS after the reports).

A refresh never makes a file worse: an empty answer does not replace a file that has rows (the run counts it as a miss),
and a report time already on disk is kept when Nasdaq's newer answer says "time-not-supplied" (it drops the times of past
dates). The pick leaves out a name on its report date and on the session after any report (dt.pick.earnings_session), in
the backtest and live alike, and the research reads the surprise of a report made after the prior close. Stdlib.
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
NO_TIME = "time-not-supplied"


def merge(new_rows, old_rows):
    """The new answer, with any report time the file on disk knew where the new answer has none."""
    times = {r.get("symbol"): r.get("time") for r in old_rows or [] if isinstance(r, dict) and r.get("time") and r.get("time") != NO_TIME}
    out = []
    for r in new_rows:
        r = dict(r)
        if (not r.get("time") or r.get("time") == NO_TIME) and times.get(r.get("symbol")):
            r["time"] = times[r["symbol"]]
        out.append(r)
    return out


def fetch_one(date, out_dir, tries=3):
    path = os.path.join(out_dir, date + ".json")
    err = None
    for k in range(tries):
        try:
            rows = net.nasdaq_earnings(date)
            new = [{key: r.get(key) for key in KEEP} for r in rows if isinstance(r, dict) and r.get("symbol")]
            old = dtio.read_json(path, None) if os.path.exists(path) else None
            old_rows = (old.get("rows") or []) if isinstance(old, dict) else []
            if not new and old_rows:
                return date, None, "an empty answer; kept the %d rows already on disk" % len(old_rows)
            doc = {"date": date, "fetched_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "rows": merge(new, old_rows)}
            dtio.write_json(path, doc)
            return date, len(doc["rows"]), None
        except Exception as e:  # the next try, then a plain miss
            err = str(e)[:120]
            time.sleep(2.0 * (k + 1))
    return date, None, err


def all_days(first, last):
    out, d = [], first
    while d <= last:
        out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


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
        first = datetime.date.fromisoformat(spy[0][0]) if spy else datetime.date.today()
        last = datetime.date.fromisoformat(spy[-1][0]) if spy else datetime.date.today()
        # every calendar day from the store's first session to a week past its last: the calendar is known ahead
        dates = all_days(first, last + datetime.timedelta(days=7))
    if a.since:
        dates = [d for d in dates if d >= a.since]
    fresh = set(dates[-a.refresh_last:]) if a.refresh_last else set()
    todo = [d for d in dates if d in fresh or not os.path.exists(os.path.join(out_dir, d + ".json"))]
    if not a.quiet:
        print("earnings calendar: %d days, %d to fetch, %d threads -> %s" % (len(dates), len(todo), a.threads, out_dir))
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
