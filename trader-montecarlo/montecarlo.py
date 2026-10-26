#!/usr/bin/env python3
"""trader-montecarlo: every trade backtested 26,000 times, three ways.

    python3 montecarlo.py plan [--plan trader/plan.json] [--replays 26000] [--seed 19] [--until 15:55] [--write]
    python3 montecarlo.py book --config "orb15|r=1" [--resamples 26000] [--days 20] [--write] [--publish]
    python3 montecarlo.py book --all-approved
    python3 montecarlo.py futures [--at 09:25] [--now "2026-10-26 09:25"] [--offline DIR] [--write]

plan     the upstream time machine on the plan with 26,000 replays (and the reel's 10,000 beside it)
book     the lab's trade book for one config, resampled 26,000 times by session at your sizing rules
futures  the overnight read from Nasdaq-100 and S&P 500 futures (NQ=F, ES=F): gap, range, tags

A replay is a simulation built from the past, never a prediction. Resampling 20 real sessions
26,000 times shows the spread of outcomes those sessions allow; it cannot show what they never
did. Most people who day trade lose money. Standard library only.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(HERE, "dt")):
    sys.path.insert(0, HERE)
else:
    sys.path.insert(0, os.path.dirname(HERE))

from dt import bars as dtbars  # noqa: E402
from dt import clock, io, net, stats  # noqa: E402

REPLAYS = 26000
REEL_REPLAYS = 10000
SEED = 19
DAYS = 20
HORIZON = 60
GAP_TAG_PCT = 0.5
WIDE_X = 1.3
QUIET_X = 0.7
FUTURES = ("NQ=F", "ES=F")
FUT_NAME = {"NQ=F": "Nasdaq-100 futures (NQ)", "ES=F": "S&P 500 futures (ES)"}
CAVEAT = ("Resampling %d real sessions %s times shows the spread of outcomes those sessions allow. It cannot "
          "show what they never did: an earnings gap, a halt, a crash.")
NOTE = "A replay is a simulation built from the past, never a prediction. Most people who day trade lose money."


class MCError(Exception):
    pass


def comma(n):
    return "{:,}".format(int(n))


def pct(x, d=1):
    return "n/a" if x is None else ("%%.%df %%%%" % d) % (100.0 * x)


def signed(x, d=2):
    return "n/a" if x is None else ("%%+.%df" % d) % x


def r3(x):
    return None if x is None else round(float(x), 3)


def money(x):
    return ("-" if x < 0 else "+") + "${:,.2f}".format(abs(x))


# ---------------------------------------------------------------- plan: the upstream time machine, 26,000 times

def timemachine_path(flag):
    if flag:
        if os.path.exists(flag):
            return flag
        raise MCError("no time machine at %s (--timemachine)" % flag)
    for c in (os.path.join(os.path.dirname(HERE), "trader-timemachine", "timemachine.py"),
              os.path.expanduser("~/.claude/skills/trader-timemachine/timemachine.py")):
        if os.path.exists(c):
            return c
    raise MCError("trader-timemachine/timemachine.py is not next to this skill (give --timemachine PATH)")


def run_timemachine(path, plan, replays, seed, until=None, offline=None, now=None, timeout=900):
    args = [sys.executable, path, "run", "--plan", plan, "--replays", str(replays), "--seed", str(seed), "--json"]
    if until:
        args += ["--until", until]
    if offline:
        args += ["--offline", offline]
    if now:
        args += ["--now", now]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MCError("the time machine took longer than %d s" % timeout)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise MCError("the time machine refused: %s" % (msg[-1] if msg else "exit %d" % r.returncode))
    text = r.stdout
    start = text.find("{")
    if start < 0:
        raise MCError("the time machine printed no JSON")
    try:
        return json.loads(text[start:])
    except ValueError as e:
        raise MCError("the time machine's JSON could not be read: %s" % e)


def odds_lines(mc, replays):
    n = replays
    out = []
    for key, label in (("p_target", "target first"), ("p_stop", "stop first"), ("p_neither", "neither"),
                       ("p_green", "green")):
        p = mc.get(key)
        if p is None:
            continue
        lo, hi = stats.wilson(int(round(p * n)), n)
        out.append("  %-13s %5.1f %%   95 %% CI %.1f to %.1f %% (%s replays)" % (label, 100 * p, 100 * lo, 100 * hi, comma(n)))
    return out


def cmd_plan(a):
    now = clock.parse_now(a.now)
    tm = timemachine_path(a.timemachine)
    if not os.path.exists(a.plan):
        raise MCError("no plan at %s: run /trader-exit first (exit.py plan --write)" % a.plan)
    plan = io.read_json(a.plan, {})
    big = run_timemachine(tm, a.plan, a.replays, a.seed, a.until, a.offline, a.now)
    reel = big if a.replays == REEL_REPLAYS else run_timemachine(tm, a.plan, REEL_REPLAYS, SEED, a.until, a.offline, a.now)
    mc, rmc = big.get("mc") or {}, reel.get("mc") or {}
    doc = {"created_by": "trader-montecarlo", "as_of": clock.iso(now), "kind": "plan", "replays": a.replays, "seed": a.seed,
           "plan": {k: plan.get(k) for k in ("symbol", "date", "entry", "stop", "target", "target_r", "entry_time", "flat_by")},
           "tm": big, "reel": {"replays": REEL_REPLAYS, "seed": SEED,
                               "p_target": r3(rmc.get("p_target")), "p_stop": r3(rmc.get("p_stop")),
                               "p_neither": r3(rmc.get("p_neither")), "p_green": r3(rmc.get("p_green")), "avg_R": rmc.get("avg_R")},
           "note": NOTE}
    if a.write:
        io.write_json(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    p = doc["plan"]
    print("%s %s: entry %s at %s, stop %s, target %s (%sR). %s replays of the rest of the session, seed %d."
          % (p.get("symbol"), p.get("date"), p.get("entry"), p.get("entry_time"), p.get("stop"), p.get("target"),
             p.get("target_r"), comma(a.replays), a.seed))
    for ln in odds_lines(mc, a.replays):
        print(ln)
    if mc.get("median_close") is not None:
        print("  Price at the end: median %.2f, 90 %% of replays between %.2f and %.2f. Average R %s."
              % (mc["median_close"], mc.get("p5_close", 0), mc.get("p95_close", 0), signed(mc.get("avg_R"), 3)))
    h = big.get("history") or {}
    if h:
        print("History: the same rule on %s real sessions fired %s times: %s target, %s stop, %s neither. Average R %s."
              % (h.get("sessions"), h.get("triggered"), h.get("target"), h.get("stop"), h.get("close"), signed(h.get("avg_R"), 3)))
    if big.get("verdict"):
        print(big["verdict"])
    for ln in big.get("verdict_notes") or []:
        print(ln)
    print("The reel's own run, %s replays, seed %d: target first %s, stop first %s, neither %s. The difference between "
          "the two runs is dice, under a point; the rest of the error is the model, not the dice."
          % (comma(REEL_REPLAYS), SEED, pct(rmc.get("p_target")), pct(rmc.get("p_stop")), pct(rmc.get("p_neither"))))
    print(NOTE)
    if a.write:
        print("Wrote %s." % a.out)
    return 0


# ---------------------------------------------------------------- book: the trade book, resampled

def read_trades(path):
    if not os.path.exists(path):
        raise MCError("no trade book at %s: run /trader-lab first (lab.py run)" % path)
    opener = gzip.open if path.endswith(".gz") else open
    out = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def sizing_rules(cfg):
    return {"max_loss": float(cfg.get("max_loss_per_trade_usd") or 50), "max_trades": int(cfg.get("max_trades_per_day") or 3),
            "max_daily_loss": float(cfg.get("max_daily_loss_usd") or 150), "budget": float(cfg.get("budget") or 5000)}


def entry_minutes(t):
    try:
        return clock.slot_close_minutes(int(t.get("k")))
    except (TypeError, ValueError):
        return 0


def day_result(trades, rules):
    """One session's trades after the sizing rules, in entry order: at most max_trades, and no new trade once
    the trades already placed could lose max_daily_loss at their stops (every entry counts its full max loss)."""
    rows = sorted(trades, key=lambda t: (entry_minutes(t), t.get("symbol") or ""))
    taken, risked = [], 0.0
    for t in rows:
        if len(taken) >= rules["max_trades"]:
            break
        if risked + rules["max_loss"] > rules["max_daily_loss"] + 1e-9:
            break
        taken.append(float(t.get("R_net", 0.0)))
        risked += rules["max_loss"]
    path, cum = [], 0.0
    for r in taken:
        cum += r
        path.append(cum)
    return {"R": [round(r, 4) for r in taken], "total_R": round(sum(taken), 4), "path": path, "n": len(taken),
            "skipped": len(rows) - len(taken), "hit_daily_limit": risked + rules["max_loss"] > rules["max_daily_loss"] + 1e-9
            and len(rows) > len(taken)}


def percentiles(values, qs=(0.05, 0.25, 0.5, 0.75, 0.95)):
    srt = sorted(values)
    n = len(srt)
    out = {}
    for q in qs:
        i = min(n - 1, max(0, int(round(q * (n - 1)))))
        out["p%d" % int(q * 100)] = round(srt[i], 4)
    return out


def resample(days, n_days, resamples, seed, rules):
    """Block bootstrap: draw n_days sessions with replacement from the per-day results (each day brings its whole
    trade list, so intraday clustering survives). Per resample: total R and dollars, max drawdown, worst day,
    trades, days at the daily limit."""
    if not days:
        raise MCError("no sessions to resample")
    rng = random.Random(seed)
    choice = rng.choice
    totals, dds, worst, ntrades, limit_days, all_r = [], [], [], [], [], []
    for _ in range(resamples):
        cum = peak = dd = 0.0
        tot = 0.0
        w = 0.0
        n = 0
        lim = 0
        for _d in range(n_days):
            d = choice(days)
            for r in d["R"]:
                cum += r
                if cum > peak:
                    peak = cum
                if peak - cum > dd:
                    dd = peak - cum
            tot += d["total_R"]
            if d["total_R"] < w:
                w = d["total_R"]
            n += d["n"]
            lim += 1 if d["hit_daily_limit"] else 0
        totals.append(tot)
        dds.append(dd)
        worst.append(w)
        ntrades.append(n)
        limit_days.append(lim)
    ml = rules["max_loss"]
    per_trade = [t / n for t, n in zip(totals, ntrades) if n]
    return {"days": n_days, "resamples": resamples, "seed": seed,
            "total_R": percentiles(totals), "total_usd": {k: round(v * ml, 2) for k, v in percentiles(totals).items()},
            "p_negative": round(sum(1 for t in totals if t < 0) / float(resamples), 4),
            "max_dd_R": percentiles(dds), "max_dd_usd": {k: round(v * ml, 2) for k, v in percentiles(dds).items()},
            "p_dd_over_10pct": round(sum(1 for d in dds if d * ml > 0.10 * rules["budget"]) / float(resamples), 4),
            "p_dd_over_25pct": round(sum(1 for d in dds if d * ml > 0.25 * rules["budget"]) / float(resamples), 4),
            "p_dd_over_50pct": round(sum(1 for d in dds if d * ml > 0.50 * rules["budget"]) / float(resamples), 4),
            "worst_day_R": percentiles(worst), "trades": percentiles(ntrades, (0.05, 0.5, 0.95)),
            "share_with_a_limit_day": round(sum(1 for l in limit_days if l) / float(resamples), 4),
            "exp_R_per_trade": {"p2_5": round(sorted(per_trade)[int(0.025 * len(per_trade))], 4) if per_trade else None,
                                "p50": round(statistics.median(per_trade), 4) if per_trade else None,
                                "p97_5": round(sorted(per_trade)[min(len(per_trade) - 1, int(0.975 * len(per_trade)))], 4)
                                if per_trade else None}}


def book_for_config(cid, trades, edge, fill, oos_only, rules, resamples, seed, days, horizon):
    rows = [t for t in trades if t.get("config_id") == cid and (t.get("fill") or "next_open") == fill]
    split = (edge or {}).get("split") or {}
    first_oos = ((split.get("oos") or {}).get("first")) if oos_only else None
    if first_oos:
        rows = [t for t in rows if (t.get("date") or "") >= first_oos]
    if not rows:
        raise MCError("no %s trades for %s%s in the book" % (fill, cid, " out of sample" if oos_only else ""))
    by_day = {}
    for t in rows:
        by_day.setdefault(t["date"], []).append(t)
    day_results = [day_result(by_day[d], rules) for d in sorted(by_day)]
    pool = [d for d in day_results if d["n"]] or day_results
    short = resample(pool, days, resamples, seed, rules)
    long = resample(pool, horizon, resamples, seed + 1, rules)
    real = {"sessions": len(by_day), "trades_taken": sum(d["n"] for d in day_results),
            "trades_skipped_by_rules": sum(d["skipped"] for d in day_results),
            "total_R": round(sum(d["total_R"] for d in day_results), 4),
            "limit_days": sum(1 for d in day_results if d["hit_daily_limit"])}
    return {"config_id": cid, "fill": fill, "oos_only": bool(first_oos), "oos_first": first_oos, "rules": rules,
            "real": real, "window": short, "horizon": long,
            "caveat": CAVEAT % (len(by_day), comma(resamples))}


def book_lines(b, configs_tested):
    r, w, h = b["real"], b["window"], b["horizon"]
    ml = b["rules"]["max_loss"]
    out = ["%s, %s fill%s: %d real sessions, %d trades taken at your rules (%d skipped by the day limits), real total %sR."
           % (b["config_id"], b["fill"], " (out of sample)" if b["oos_only"] else "", r["sessions"], r["trades_taken"],
              r["trades_skipped_by_rules"], signed(r["total_R"], 2)),
           "configs tested: %s" % configs_tested,
           b["caveat"],
           "  %d-day windows, %s resamples, seed %d, one R = $%.0f (max_loss_per_trade_usd), at most %d trades a day, "
           "$%.0f daily loss limit:" % (w["days"], comma(w["resamples"]), w["seed"], ml, b["rules"]["max_trades"],
                                        b["rules"]["max_daily_loss"]),
           "    total R      p5 %s  p25 %s  median %s  p75 %s  p95 %s" % tuple(signed(w["total_R"][k], 2) for k in ("p5", "p25", "p50", "p75", "p95")),
           "    total $      p5 %s  median %s  p95 %s" % (money(w["total_usd"]["p5"]), money(w["total_usd"]["p50"]), money(w["total_usd"]["p95"])),
           "    ends below zero %s of windows; max drawdown median %sR, p95 %sR (%s); drawdown over 10 %% of the "
           "budget in %s, over 25 %% in %s, over 50 %% in %s of windows" % (
               pct(w["p_negative"]), signed(w["max_dd_R"]["p50"], 2), signed(w["max_dd_R"]["p95"], 2),
               money(-w["max_dd_usd"]["p95"]), pct(w["p_dd_over_10pct"]), pct(w["p_dd_over_25pct"]), pct(w["p_dd_over_50pct"])),
           "    expectancy a trade: median %sR, 95 %% band %s to %sR; a daily-limit day inside %s of windows"
           % (signed(w["exp_R_per_trade"]["p50"], 3), signed(w["exp_R_per_trade"]["p2_5"], 3), signed(w["exp_R_per_trade"]["p97_5"], 3),
              pct(w["share_with_a_limit_day"])),
           "  %d-day horizon: total R median %s (p5 %s, p95 %s), ends below zero %s, max drawdown p95 %sR."
           % (h["days"], signed(h["total_R"]["p50"], 2), signed(h["total_R"]["p5"], 2), signed(h["total_R"]["p95"], 2),
              pct(h["p_negative"]), signed(h["max_dd_R"]["p95"], 2))]
    return out


def cmd_book(a):
    now = clock.parse_now(a.now)
    cfg = io.read_config(a.config)
    rules = sizing_rules(cfg)
    edge = io.read_json(a.edge, None) if os.path.exists(a.edge) else None
    configs_tested = (edge or {}).get("configs_tested", "n/a")
    trades = read_trades(a.trades)
    if a.all_approved:
        if not edge:
            raise MCError("--all-approved needs the lab's %s" % a.edge)
        want = [c["id"] for c in edge.get("configs", []) if isinstance(c, dict) and (c.get("status") == "approved" or
                (a.include_watch and c.get("status") == "watch"))]
        if not want:
            raise MCError("no approved config in %s" % a.edge)
    elif a.config_id:
        want = [a.config_id]
    else:
        raise MCError("give --config ID or --all-approved")
    books, refused = [], []
    for cid in want:
        try:
            books.append(book_for_config(cid, trades, edge, a.fill, not a.all_sessions, rules, a.resamples, a.seed, a.days,
                                         a.horizon_days))
        except MCError as e:
            refused.append(str(e))
    if not books:
        raise MCError(refused[0] if refused else "nothing to resample")
    books.sort(key=lambda b: -b["window"]["total_R"]["p5"])
    doc = {"created_by": "trader-montecarlo", "as_of": clock.iso(now), "kind": "book", "resamples": a.resamples, "seed": a.seed,
           "days": a.days, "horizon_days": a.horizon_days, "fill": a.fill, "configs_tested": configs_tested, "rules": rules,
           "books": books, "refused": refused, "note": NOTE}
    if a.write:
        io.write_json(a.out, doc)
    if a.publish:
        io.write_json(os.path.join(a.docs_data, "montecarlo.json"), doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    if len(books) > 1:
        print("%d configs resampled %s times over %d-day windows (sorted by the p5 of the window total):"
              % (len(books), comma(a.resamples), a.days))
        for b in books:
            w = b["window"]
            print("  %-26s p5 %sR  median %sR  p95 %sR  below zero %s  dd p95 %sR  (%d real sessions, %d trades)"
                  % (b["config_id"], signed(w["total_R"]["p5"], 2), signed(w["total_R"]["p50"], 2), signed(w["total_R"]["p95"], 2),
                     pct(w["p_negative"]), signed(w["max_dd_R"]["p95"], 2), b["real"]["sessions"], b["real"]["trades_taken"]))
        print("configs tested: %s. %s" % (configs_tested, books[0]["caveat"]))
    else:
        for ln in book_lines(books[0], configs_tested):
            print(ln)
    for r in refused:
        print("  skipped: %s" % r)
    print(NOTE)
    if a.write:
        print("Wrote %s." % a.out)
    return 0


# ---------------------------------------------------------------- futures: the overnight read

def load_chart(sym, offline, rng, interval):
    if offline:
        name = sym + ("-1d" if interval == "1d" else "") + ".json"
        path = offline if os.path.isfile(offline) else os.path.join(offline, name)
        if not os.path.exists(path):
            raise MCError("no offline chart %s" % path)
        return net.parse_chart(json.dumps(io.read_json(path)), sym)
    try:
        return net.chart(sym, rng, interval)
    except net.NetError as e:
        raise MCError("Yahoo has no %s data right now (%s)" % (sym, e))


def et_rows(series):
    """Every 5-minute bar of a futures chart as (New York datetime, o, h, l, c), on the grid, with a close."""
    out = []
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None or ts % 300:
            continue
        et = clock.to_eastern(clock.utc_of(ts))
        out.append((et, series["o"][i], series["h"][i], series["l"][i], float(c)))
    out.sort(key=lambda r: r[0])
    return out


def prev_trading_day(d):
    d = d - datetime.timedelta(days=1)
    while not clock.trading_day(d)[0]:
        d -= datetime.timedelta(days=1)
    return d


def overnight_read(sym, rows, drows, day, cutoff_dt):
    """prior settle = the last bar at or before 16:00 New York of the previous trading day; the overnight window runs
    from that bar to the cutoff (capped at 9:30); range_vs_20d against the average daily range of the 20 daily bars
    before the day."""
    prev = prev_trading_day(day)
    settle_end = datetime.datetime.combine(prev, datetime.time(16, 0))
    settle = [r for r in rows if r[0].date() == prev and r[0] + datetime.timedelta(minutes=5) <= settle_end]
    if not settle:
        raise MCError("%s: no bar before %s 4:00 PM New York in the chart (the settle)" % (sym, prev.isoformat()))
    s = settle[-1]
    cap = min(cutoff_dt, datetime.datetime.combine(day, clock.OPEN))
    on = [r for r in rows if r[0] >= s[0] + datetime.timedelta(minutes=5) and r[0] + datetime.timedelta(minutes=5) <= cap]
    if not on:
        raise MCError("%s: no overnight bar between the %s settle and %s yet" % (sym, prev.isoformat(), cap.strftime("%H:%M")))
    last = on[-1]
    hi = max(r[2] for r in on if r[2] is not None)
    lo = min(r[3] for r in on if r[3] is not None)
    gap = last[4] / s[4] - 1
    dr = [(r[0], r[2], r[3]) for r in drows if r[0] < day.isoformat() and r[1] is not None and r[2] is not None]
    dr = dr[-20:]
    avg = (sum(h - l for _, h, l in dr) / len(dr)) if dr else None
    rng_pct = (hi - lo) / s[4]
    vs = ((hi - lo) / avg) if avg else None
    tags = ["%s_gap_up" % sym[:2].lower() if gap >= GAP_TAG_PCT / 100 else "%s_gap_down" % sym[:2].lower()
            if gap <= -GAP_TAG_PCT / 100 else "%s_gap_flat" % sym[:2].lower()]
    if vs is not None:
        tags.append("%s_wide_overnight" % sym[:2].lower() if vs >= WIDE_X else "%s_quiet_overnight" % sym[:2].lower()
                    if vs <= QUIET_X else "%s_normal_overnight" % sym[:2].lower())
    return {"symbol": sym, "last": round(last[4], 2), "last_bar": last[0].strftime("%Y-%m-%d %H:%M"), "prev_settle": round(s[4], 2),
            "settle_time": s[0].strftime("%Y-%m-%d %H:%M"), "gap_pct": round(100 * gap, 3), "overnight_high": round(hi, 2),
            "overnight_low": round(lo, 2), "overnight_range_pct": round(100 * rng_pct, 3),
            "range_vs_20d": round(vs, 3) if vs is not None else None, "bars": len(on), "daily_bars_used": len(dr), "tags": tags}


def daily_rows_of(series):
    out = []
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        et = clock.to_eastern(clock.utc_of(ts))
        out.append([et.date().isoformat(), series["o"][i], series["h"][i], series["l"][i], float(c), series["v"][i]])
    out.sort()
    return out


def cmd_futures(a):
    now = clock.parse_now(a.now)
    now_et = clock.to_eastern(now)
    day = now_et.date()
    if not clock.trading_day(day)[0]:
        raise MCError("%s is not a trading day (%s): the overnight read is for a session" % (day.isoformat(), clock.trading_day(day)[1]))
    cutoff = datetime.datetime.combine(day, clock.parse_hhmm(a.at, "--at")) if a.at else now_et.replace(second=0, microsecond=0)
    if cutoff > now_et + datetime.timedelta(seconds=59) and not a.offline:
        raise MCError("--at %s is later than now (%s New York)" % (a.at, now_et.strftime("%H:%M")))
    reads, failed = {}, {}
    for sym in FUTURES:
        try:
            series = load_chart(sym, a.offline, "5d", "5m")
            if not a.offline and a.pause:
                net.SLEEP(a.pause)
            dseries = load_chart(sym, a.offline, "60d", "1d")
            if not a.offline and a.pause:
                net.SLEEP(a.pause)
            reads[sym] = overnight_read(sym, et_rows(series), daily_rows_of(dseries), day, cutoff)
        except MCError as e:
            failed[sym] = str(e)
    if not reads:
        raise MCError("; ".join(failed.values()) or "no futures data")
    tags = []
    for sym in FUTURES:
        if sym in reads:
            tags += reads[sym]["tags"]
    doc = {"created_by": "trader-montecarlo", "as_of": clock.iso(now), "date": day.isoformat(), "cutoff": cutoff.strftime("%H:%M"),
           "nq": reads.get("NQ=F"), "es": reads.get("ES=F"), "failed": failed, "tags": tags,
           "thresholds": {"gap_pct": GAP_TAG_PCT, "wide_x": WIDE_X, "quiet_x": QUIET_X},
           "note": "Free Yahoo futures bars, unofficial and delayed; the overnight is the Globex session since the prior 4:00 PM "
                   "New York bar. Context, not a forecast."}
    if a.write:
        io.write_json(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("Overnight read for %s at %s New York (%s):" % (clock.day_label(day), cutoff.strftime("%H:%M"), "offline files" if a.offline else "Yahoo"))
    for sym in FUTURES:
        r = reads.get(sym)
        if not r:
            print("  %s: %s" % (FUT_NAME[sym], failed.get(sym)))
            continue
        vs = "range %.2fx its 20-day average" % r["range_vs_20d"] if r["range_vs_20d"] is not None else "20-day range unknown"
        print("  %s %s: %s %% overnight vs the %s 4:00 PM settle %s; overnight %s to %s (%.2f %%), %s. Tags: %s"
              % (FUT_NAME[sym], "{:,.2f}".format(r["last"]), signed(r["gap_pct"], 2), r["settle_time"][:10], "{:,.2f}".format(r["prev_settle"]),
                 "{:,.2f}".format(r["overnight_low"]), "{:,.2f}".format(r["overnight_high"]), r["overnight_range_pct"], vs, " ".join(r["tags"])))
    print(doc["note"])
    if a.write:
        print("Wrote %s (trader-regime live reads it when it is fresh)." % a.out)
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("plan", help="the upstream time machine on trader/plan.json, 26,000 replays")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--replays", type=int, default=REPLAYS)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--until", help="end each replay at HH:MM New York (the flat-by time)")
    p.add_argument("--offline", metavar="FILE", help="a saved Yahoo 60-day chart for the time machine")
    p.add_argument("--timemachine", help="path to trader-timemachine/timemachine.py")
    p.add_argument("--now")
    p.add_argument("--out", default="trader/montecarlo.json")
    p.add_argument("--write", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("book", help="the lab's trade book for a config, resampled 26,000 times at your sizing rules")
    p.add_argument("--config", dest="config_id", help="a config id from edge.json, e.g. orb15|r=1")
    p.add_argument("--all-approved", action="store_true")
    p.add_argument("--include-watch", action="store_true")
    p.add_argument("--edge", default=os.path.join("trader", "lab", "edge.json"))
    p.add_argument("--trades", default=os.path.join("trader", "lab", "trades.jsonl.gz"))
    p.add_argument("--fill", default="next_open", choices=["next_open", "close"])
    p.add_argument("--all-sessions", action="store_true", help="every session, not just out of sample")
    p.add_argument("--resamples", type=int, default=REPLAYS)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--days", type=int, default=DAYS)
    p.add_argument("--horizon-days", dest="horizon_days", type=int, default=HORIZON)
    p.add_argument("--config-file", dest="config", default="trader/config.json")
    p.add_argument("--now")
    p.add_argument("--out", default="trader/montecarlo.json")
    p.add_argument("--docs-data", default=os.path.join("trader", "publish"))
    p.add_argument("--write", action="store_true")
    p.add_argument("--publish", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("futures", help="the overnight read from NQ and ES futures")
    p.add_argument("--at", help="HH:MM New York (default now, capped at 9:30)")
    p.add_argument("--now")
    p.add_argument("--offline", help="a folder with NQ=F.json, NQ=F-1d.json, ES=F.json, ES=F-1d.json")
    p.add_argument("--pause", type=float, default=net.PAUSE)
    p.add_argument("--out", default="trader/futures.json")
    p.add_argument("--write", action="store_true")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "book": cmd_book, "futures": cmd_futures}[a.cmd](a)
    except (MCError, clock.ClockError, io.IOError_) as e:
        print("montecarlo.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
