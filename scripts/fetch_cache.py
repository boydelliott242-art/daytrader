#!/usr/bin/env python3
"""Build or refresh the bar store (CONTRACTS.md, 'The bar store').

    python3 scripts/fetch_cache.py --build-universe        # Nasdaq screener -> trader/largecap.*, then every symbol
    python3 scripts/fetch_cache.py --only SPY,QQQ,^VIX     # a few symbols
    python3 scripts/fetch_cache.py --max-age-hours 0       # force a refresh
    python3 scripts/fetch_cache.py --behind --threads 6    # only the names not through the last completed session

Resumable: a symbol refreshed within --max-age-hours is skipped. Polite: 0.5 s between
requests. Merges new full sessions into the existing store so history grows over time.
--behind is the catch-up the morning pick and the late-evening job run: a name counts as behind when its daily store
does not reach the last completed session (yesterday's before today's close, today's after it), whatever its age.
"""

import argparse
import concurrent.futures
import datetime
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dt import bars, clock, io, net, universe  # noqa: E402

CONTEXT = ["SPY", "QQQ", "IWM", "DIA", "^VIX", "^VIX3M", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU",
           "XLB", "XLRE", "XLC"]


def fresh(store, max_age_hours):
    if not store or not store.get("updated_at") or max_age_hours <= 0:
        return False
    try:
        t = datetime.datetime.strptime(store["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return (clock.utc_now() - t).total_seconds() < max_age_hours * 3600


def last_completed_session(now_utc):
    """The last session whose close has passed, as an ISO date: today after 4:00 p.m. ET (1:00 p.m. on a half day), else
    the previous trading day."""
    from dt import intel_flags
    now_et = clock.to_eastern(now_utc)
    d = now_et.date()
    s = clock.session(d)
    if s and now_et.time() >= s[1]:
        return d.isoformat()
    return intel_flags.prev_trading_day(d).isoformat()


def behind(sym, ref, root=None):
    """True when the daily store for sym does not reach the session ref (no store at all counts as behind)."""
    rows = bars.daily_rows(sym, root)
    return not rows or rows[-1][0] < ref


def daily_rows_from_series(series, now_utc):
    now_et = clock.to_eastern(now_utc)
    rows = []
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        et = clock.to_eastern(clock.utc_of(ts))
        d = et.date()
        s = clock.session(d)
        # today's bar is partial until the close
        if d == now_et.date() and (not s or now_et.time() < s[1]):
            continue
        rows.append([d.isoformat(), series["o"][i], series["h"][i], series["l"][i], float(c), int(series["v"][i] or 0)])
    rows.sort()
    return rows


def refresh_symbol(sym, now_utc, daily=True, root=None):
    series = net.chart(sym, "60d", "5m")
    sess = bars.series_to_sessions(series, now_utc)
    store = bars.load_store(sym, "bars5m", root) or {"symbol": sym, "interval": "5m", "source": "yahoo", "sessions": {}}
    dropped = []
    seen = bars.series_to_sessions(series, now_utc, include_partial=True)
    for d, b in seen.items():
        if d not in sess:
            dropped.append(d)
    for d, b in sess.items():
        store["sessions"][d] = bars.rows_of(b)
    store["updated_at"] = io.now_iso()
    store["n_sessions"] = len(store["sessions"])
    bars.save_store(sym, store, "bars5m", root)
    out = {"sessions": len(sess), "total": len(store["sessions"]), "dropped": dropped}
    if daily:
        net.SLEEP(net.PAUSE)
        dseries = net.chart(sym, "2y", "1d")
        rows = daily_rows_from_series(dseries, now_utc)
        bars.save_store(sym, {"symbol": sym, "interval": "1d", "source": "yahoo", "updated_at": io.now_iso(),
                              "rows": rows}, "daily", root)
        out["daily"] = len(rows)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    def say(msg):
        if not a.quiet:
            print(msg, flush=True)
    ap.add_argument("--symbols-file", default="trader/largecap.txt")
    ap.add_argument("--build-universe", action="store_true", help="rebuild trader/largecap.* from Nasdaq's screener first")
    ap.add_argument("--min-cap", type=float, default=None, help="market cap floor (default: config largecap_min_cap_usd or 10e9)")
    ap.add_argument("--only", help="comma-separated symbols instead of the file")
    ap.add_argument("--no-context", action="store_true", help="skip SPY/QQQ/VIX/sector ETFs")
    ap.add_argument("--no-daily", action="store_true")
    ap.add_argument("--max-age-hours", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=net.PAUSE)
    ap.add_argument("--root", default=None, help="cache root (default trader/cache)")
    ap.add_argument("--behind", action="store_true", help="only the names whose daily store is not through the last completed session (age ignored)")
    ap.add_argument("--threads", type=int, default=1, help="parallel fetches (the morning catch-up runs 6; the evening job 1)")
    ap.add_argument("--deadline", default=None, help="an ET clock time (HH:MM): no fetch starts after it, so a morning catch-up leaves time to post")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    os.chdir(ROOT)
    cfg = io.read_config()
    now = clock.utc_now()
    t0 = time.time()
    if a.only:
        syms = [s.strip().upper() for s in a.only.split(",") if s.strip()]
    else:
        if a.build_universe or not os.path.exists(a.symbols_file):
            min_cap = a.min_cap or float(cfg.get("largecap_min_cap_usd") or 10e9)
            print("Building the large-cap universe from Nasdaq's screener (cap >= $%.0fB)..." % (min_cap / 1e9), flush=True)
            try:
                doc = universe.build_largecap(net.nasdaq_screener(), min_cap)
            except Exception as e:  # one screener error must not stop the refresh: keep the list on disk and go on
                doc = None
                print("  the screener failed (%s); keeping the list on disk and refreshing it" % str(e)[:120], flush=True)
                if not os.path.exists(a.symbols_file):
                    raise
            old_n = len((io.read_json("trader/largecap.json", None) or {}).get("symbols") or []) if os.path.exists("trader/largecap.json") else 0
            if doc is not None and old_n and doc["count"] < 0.8 * old_n:
                # a screener answer that lost a fifth of the list is a broken answer, not a market: keep the list on disk
                print("  the screener kept %d names against %d on disk; keeping the list on disk" % (doc["count"], old_n), flush=True)
                doc = None
            if doc is not None:
                universe.write_largecap(doc)
                print("  %d symbols; wrote trader/largecap.json and trader/largecap.txt" % doc["count"], flush=True)
                # each evening's list is kept, dated, so the pick's record can admit a name only while it was a large
                # cap and keep the names that later leave the list (dt.pick.universe_caps)
                hist = os.path.join("trader", "largecap_history")
                os.makedirs(hist, exist_ok=True)
                io.write_json(os.path.join(hist, clock.to_eastern(now).date().isoformat() + ".json"), doc)
        syms = [] if a.no_context else list(CONTEXT)
        for s in io.symbols_from_file(a.symbols_file):
            if s not in syms:
                syms.append(s)
    if a.limit:
        syms = syms[:a.limit]
    ref = None
    if a.behind:
        ref = last_completed_session(now)
        syms = [s for s in syms if behind(s, ref, a.root)]
        a.max_age_hours = 0
        say("%d names are not through %s in the daily store; refreshing them with %d thread(s)" % (len(syms), ref, max(1, a.threads)))
    if not a.quiet:
        print("%d symbols to refresh (skipping any fresher than %.0fh)" % (len(syms), a.max_age_hours), flush=True)
    state = {"done": 0, "skipped": 0, "failed": {}, "dropped": {}, "i": 0, "late": 0}
    lock = threading.Lock()
    deadline = clock.parse_hhmm(a.deadline, "--deadline") if a.deadline else None

    def one(sym):
        if deadline and clock.to_eastern(clock.utc_now()).time() >= deadline:
            with lock:
                state["late"] += 1
                state["i"] += 1
            return
        store = bars.load_store(sym, "bars5m", a.root)
        if fresh(store, a.max_age_hours):
            with lock:
                state["skipped"] += 1
                state["i"] += 1
            return
        try:
            r = refresh_symbol(sym, now, daily=not a.no_daily, root=a.root)
            with lock:
                state["done"] += 1
                if r["dropped"]:
                    state["dropped"][sym] = r["dropped"]
        except net.NetError as e:
            with lock:
                state["failed"][sym] = str(e)
        except Exception as e:  # keep going: one bad chart must not stop the run
            with lock:
                state["failed"][sym] = "%s: %s" % (type(e).__name__, e)
        with lock:
            state["i"] += 1
            i = state["i"]
            if not a.quiet and (i % 25 == 0 or i == len(syms)):
                print("  %d/%d  fetched %d  skipped %d  failed %d  %.0fs" % (i, len(syms), state["done"], state["skipped"], len(state["failed"]),
                                                                          time.time() - t0), flush=True)
        if a.pause:
            net.SLEEP(a.pause)

    if max(1, a.threads) == 1:
        for sym in syms:
            one(sym)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.threads) as pool:
            list(pool.map(one, syms))
    done, skipped, failed, dropped = state["done"], state["skipped"], state["failed"], state["dropped"]
    if state["late"]:
        say("%d names were not fetched: the deadline %s ET passed" % (state["late"], a.deadline))
    if ref:
        still = [s for s in syms if behind(s, ref, a.root)]
        say("%d of %d still not through %s after the refresh%s" % (len(still), len(syms), ref, (": " + ", ".join(sorted(still)[:12])) if still else ""))
    # meta
    counts = []
    first = last = None
    root = a.root or io.CACHE
    d5 = os.path.join(root, "bars5m")
    if os.path.isdir(d5):
        for f in os.listdir(d5):
            if not f.endswith(".json.gz"):
                continue
            doc = io.read_json(os.path.join(d5, f))
            days = sorted((doc or {}).get("sessions") or {})
            if days:
                counts.append(len(days))
                first = days[0] if first is None else min(first, days[0])
                last = days[-1] if last is None else max(last, days[-1])
    meta = {"built_at": io.now_iso(), "symbols": len(counts), "sessions_min": min(counts) if counts else 0,
            "sessions_max": max(counts) if counts else 0, "first_date": first, "last_date": last,
            "fetched": done, "skipped": skipped, "failed": len(failed), "seconds": round(time.time() - t0)}
    io.write_json(os.path.join(root, "meta.json"), meta)
    io.write_json(os.path.join(root, "bars5m", "_failed.json"), failed)
    io.write_json(os.path.join(root, "bars5m", "_dropped.json"), dropped)
    say("Done: %d fetched, %d skipped, %d failed in %.0fs. Store: %d symbols, %s..%s, %d-%d sessions each."
        % (done, skipped, len(failed), time.time() - t0, meta["symbols"], first, last, meta["sessions_min"],
           meta["sessions_max"]))
    return 0 if done or skipped or (a.behind and not syms) else 1


if __name__ == "__main__":
    sys.exit(main())
