#!/usr/bin/env python3
"""nq_adapter: runs under the NQ backtester's own venv (pandas, numpy, numba). Called by nq.py.

    nq_adapter.py setups   --backtester DIR --repo DIR --out-dir DIR [--start] [--end] [--configs all|a,b] [--fees] [--slippage]
                           [--instrument NQ] [--capital 100000] [--config trader/config.json] [--quiet]
    nq_adapter.py overnight --backtester DIR --repo DIR --out-dir DIR [--start] [--end]
    nq_adapter.py session-tags --backtester DIR --repo DIR --out FILE [--days 60]

Translates the desk's 5-minute setups (dt/setups.py) into the engine's per-minute Signals: the signal bar's
last minute gets target=1 (a market order at the next minute's open, the engine's rule and the desk's
next-open fill), stop_loss and take_profit in points from the plan. Prices are the engine's back-adjusted
continuous series. Fees and slippage are the engine's. Nothing here is a prediction.
"""

import argparse
import gzip
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd


def log(quiet, msg):
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 5-minute RTH sessions from 1-minute bars

RTH_START = 8 * 60 + 30     # Chicago minutes
SLOTS = 78


def sessions_5m(bars):
    """-> {date_str: {"bars": [78 dt bars], "idx": [78 last-minute indexes]}} for sessions with every bucket."""
    rth = bars.rth
    idx = np.nonzero(rth)[0]
    if len(idx) == 0:
        return {}, {"rth_bars": 0, "sessions": 0, "skipped": 0}
    slot = (bars.tod[idx] - RTH_START) // 5
    ok = (slot >= 0) & (slot < SLOTS)
    idx, slot = idx[ok], slot[ok]
    sess = bars.sess[idx]
    tday = bars.tday[idx]
    df = pd.DataFrame({"i": idx, "sess": sess, "slot": slot, "o": bars.open[idx], "h": bars.high[idx], "l": bars.low[idx],
                       "c": bars.close[idx], "v": bars.volume[idx], "t": bars.ts[idx].asi8 // 10 ** 9})
    g = df.groupby(["sess", "slot"], sort=True)
    agg = g.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"), t=("t", "first"),
                last_i=("i", "max"), n=("i", "size")).reset_index()
    out, skipped = {}, 0
    day_of_sess = {}
    for s, d in zip(sess, tday):
        day_of_sess[int(s)] = str(d)[:10]
    for s, grp in agg.groupby("sess", sort=True):
        if len(grp) != SLOTS:
            skipped += 1
            continue
        grp = grp.sort_values("slot")
        rows = [{"t": int(r.t), "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c), "v": int(r.v)}
                for r in grp.itertuples(index=False)]
        out[day_of_sess[int(s)]] = {"bars": rows, "idx": [int(x) for x in grp.last_i.tolist()], "sess": int(s)}
    return out, {"rth_bars": int(len(idx)), "sessions": len(out), "skipped": skipped}


def daily_rows(sessions):
    """RTH daily rows [date, o, h, l, c, v] from the 5-minute sessions (dt.bars shape)."""
    rows = []
    for d in sorted(sessions):
        b = sessions[d]["bars"]
        rows.append([d, b[0]["o"], max(x["h"] for x in b), min(x["l"] for x in b), b[-1]["c"], sum(x["v"] for x in b)])
    return rows


def overnight_table(bars, sessions):
    """Per session: the prior RTH close, the Globex overnight high/low/range to the 08:30 CT open, the gap, and the
    range against the average RTH daily range of the prior 20 sessions."""
    dates = sorted(sessions)
    drows = daily_rows(sessions)
    rng20 = {}
    for i, d in enumerate(dates):
        prev = drows[max(0, i - 20):i]
        rng20[d] = (sum(r[2] - r[3] for r in prev) / len(prev)) if prev else None
    sess_of = {sessions[d]["sess"]: d for d in dates}
    eth = ~bars.rth
    out = {}
    prev_close = None
    prev_date = None
    for d in dates:
        s = sessions[d]["sess"]
        first_rth = sessions[d]["idx"][0] - 4  # the 08:30 minute is 4 bars before the first bucket's last minute
        # overnight = eth bars of this trading session (17:00 CT the evening before .. 08:29 CT), in this sess id
        mask = (bars.sess == s) & eth & (np.arange(len(bars)) < sessions[d]["idx"][0])
        on_idx = np.nonzero(mask)[0]
        rec = {"date": d, "prev_close": prev_close, "prev_date": prev_date, "open": sessions[d]["bars"][0]["o"]}
        if len(on_idx) and prev_close:
            hi = float(bars.high[on_idx].max())
            lo = float(bars.low[on_idx].min())
            rec.update({"overnight_high": round(hi, 2), "overnight_low": round(lo, 2),
                        "overnight_range_pct": round(100 * (hi - lo) / prev_close, 4),
                        "gap_pct": round(100 * (rec["open"] / prev_close - 1), 4),
                        "range_vs_20d": round((hi - lo) / rng20[d], 4) if rng20.get(d) else None, "overnight_bars": int(len(on_idx))})
            g = rec["gap_pct"]
            rec["tags"] = ["nq_gap_up" if g >= 0.5 else "nq_gap_down" if g <= -0.5 else "nq_gap_flat"]
            v = rec["range_vs_20d"]
            if v is not None:
                rec["tags"].append("nq_wide_overnight" if v >= 1.3 else "nq_quiet_overnight" if v <= 0.7 else "nq_normal_overnight")
        else:
            rec.update({"overnight_high": None, "overnight_low": None, "overnight_range_pct": None, "gap_pct": None,
                        "range_vs_20d": None, "overnight_bars": int(len(on_idx)), "tags": []})
        b = sessions[d]["bars"]
        hi_rth, lo_rth = max(x["h"] for x in b), min(x["l"] for x in b)
        rec["rth_range_pct"] = round(100 * (hi_rth - lo_rth) / b[0]["o"], 4)
        rec["rth_day_type"] = "trend_day" if (hi_rth - lo_rth) > 0 and abs(b[-1]["c"] - b[0]["o"]) >= 0.6 * (hi_rth - lo_rth) else "range_day"
        out[d] = rec
        prev_close, prev_date = b[-1]["c"], d
    return out


# ---------------------------------------------------------------- setups -> engine signals

def load_dt(repo):
    sys.path.insert(0, repo)
    from dt import clock, io, setups, sim, stats, bars as dtbars  # noqa: F401
    return clock, io, setups, sim, stats, dtbars


def reason_class(reason):
    r = str(reason).lower()
    if "stop" in r and "take" not in r and "target" not in r:
        return "stop"
    if "target" in r or "take" in r or "profit" in r:
        return "target"
    return "close"


def cmd_setups(a):
    t0 = time.time()
    sys.path.insert(0, a.backtester)
    from nqbt import Backtest, Bars, Signals  # noqa: E402
    from nqbt.instruments import INSTRUMENTS  # noqa: E402
    clock, io, setups, sim, stats, dtbars = load_dt(a.repo)
    cfg = io.read_config(a.config) if a.config and os.path.exists(a.config) else {}
    bars = Bars.load(a.start, a.end, data_dir=os.path.join(a.backtester, "data"))
    log(a.quiet, "%s" % bars)
    sessions, counts = sessions_5m(bars)
    log(a.quiet, "5-minute RTH sessions: %d full (%d skipped: early closes, holidays, gaps)" % (counts["sessions"], counts["skipped"]))
    dates = sorted(sessions)
    drows = daily_rows(sessions)
    over = overnight_table(bars, sessions)
    all_cfgs = setups.configs()
    if a.configs and a.configs != "all":
        want = set(x.strip() for x in a.configs.split(","))
        cfgs = [c for c in all_cfgs if c["id"] in want]
        missing = sorted(want - set(c["id"] for c in cfgs))
        if missing:
            log(a.quiet, "unknown configs skipped: %s" % ", ".join(missing))
    else:
        cfgs = all_cfgs
    inst = INSTRUMENTS[a.instrument]
    point_value = inst.point_value
    # 1. the signals, one pass over the sessions
    log(a.quiet, "running %d configs over %d sessions..." % (len(cfgs), len(dates)))
    per_cfg = {c["id"]: {} for c in cfgs}            # cid -> {fill_bar_index: (date, k, sig)}
    not_applicable = set()
    for n, d in enumerate(dates):
        b = sessions[d]["bars"]
        prev_close = over[d]["prev_close"]
        ctx = {"window": clock.entry_window(d, cfg), "prev_close": prev_close,
               "daily_atr": dtbars.daily_atr(drows, d, 14), "avg_vol20": dtbars.avg_volume(drows, d, 20), "spy": None}
        sigs = setups.run_all(b, ctx, cfgs)
        for cid, sig in sigs.items():
            i = sessions[d]["idx"][sig["k"]]
            per_cfg[cid][i + 1] = (d, sig["k"], sig)      # the engine fills at bar i+1's open
        if n % 500 == 0 and n:
            log(a.quiet, "  %d/%d sessions" % (n, len(dates)))
    for c in cfgs:
        if c["setup"] == "orb15_rs":
            not_applicable.add(c["id"])
    # 2. the engine, one run per config
    N = len(bars)
    trades = []
    engine = {}
    agree = {"same": 0, "n": 0, "dt_better": 0, "engine_better": 0}
    for c in cfgs:
        cid = c["id"]
        fills = per_cfg[cid]
        if not fills:
            engine[cid] = {"n_trades": 0, "note": "no signal" + (" (needs SPY: not applicable on NQ)" if cid in not_applicable else "")}
            continue
        sig_arr = Signals.empty(N)
        for fill_i, (d, k, sig) in fills.items():
            i = fill_i - 1
            sig_arr.target[i] = 1.0
            sig_arr.stop_loss[i] = sig["entry"] - sig["stop"]
            sig_arr.take_profit[i] = sig["target"] - sig["entry"]
        bt = Backtest(bars, instrument=a.instrument, fees=a.fees, slippage=a.slippage, capital=a.capital, flatten_at="14:55",
                      entry_window=("08:45", "14:30"), allow_short=False, max_position=1, intrabar="pessimistic")
        res = bt.run(sig_arr, name=cid)
        tr = res.trades
        st = res.stats
        yearly = res.yearly()
        engine[cid] = {k: (None if (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else (float(v) if isinstance(v, (np.floating, float)) else
                           int(v) if isinstance(v, (np.integer,)) else v)) for k, v in st.items() if not isinstance(v, (pd.Timestamp,))}
        engine[cid]["yearly"] = [{"year": int(y), **{kk: (None if (isinstance(vv, float) and math.isnan(vv)) else round(float(vv), 2)) for kk, vv in row.items()}}
                                 for y, row in yearly.iterrows()] if len(yearly) else []
        engine[cid]["n_trades"] = int(len(tr))
        for row in tr.itertuples(index=False):
            key = int(row.entry_bar)
            hit = fills.get(key)
            if hit is None:
                continue
            d, k, sig = hit
            risk = sig["entry"] - sig["stop"]
            qty = float(row.qty) if row.qty else 1.0
            pts_gross = float(row.gross_pnl) / (qty * point_value)
            pts_net = float(row.net_pnl) / (qty * point_value)
            r_gross = pts_gross / risk
            r_net = pts_net / risk
            outcome = reason_class(row.exit_reason)
            exit_i = int(row.exit_bar)
            b5 = sessions[d]["bars"]
            idxs = sessions[d]["idx"]
            exit_slot = next((j for j, last_i in enumerate(idxs) if last_i >= exit_i), len(idxs) - 1)
            o = over[d]
            cost_usd = float(row.fees) + float(row.slippage)
            # the cross-check: dt.sim on the same 5-minute bars, the engine's dollars as basis points of the fill
            entry_fill = float(row.entry_price)
            cost_bps = cost_usd / (qty * entry_fill * point_value) * 1e4 if entry_fill else 0.0
            check = sim.play(b5, sig, "next_open", clock.flat_slot(d, cfg), cost_bps)
            if check is not None:
                agree["n"] += 1
                if check["outcome"] == outcome:
                    agree["same"] += 1
                elif check["R_net"] > r_net:
                    agree["dt_better"] += 1
                else:
                    agree["engine_better"] += 1
            hour = str(clock.slot_close_minutes(k) // 60)
            dow = pd.Timestamp(d).strftime("%a")
            trades.append({"config_id": cid, "setup": c["setup"], "params": c["params"], "symbol": a.instrument, "date": d, "k": k,
                           "entry": sig["entry"], "stop": sig["stop"], "target": sig["target"], "target_r": sig["target_r"],
                           "stop_rule": sig["stop_rule"], "entry_time": sig["entry_time"], "fill": "next_open",
                           "entry_fill": round(entry_fill, 2), "outcome": outcome, "exit_slot": exit_slot,
                           "exit_price": round(float(row.exit_price), 2), "R_gross": round(r_gross, 4), "R_net": round(r_net, 4),
                           "cost_usd": round(cost_usd, 2), "bars_held": max(1, int(round(float(row.minutes_held) / 5.0))),
                           "mae_R": round(float(row.mae_pts) / risk, 4) if risk else None, "mfe_R": round(float(row.mfe_pts) / risk, 4) if risk else None,
                           "gap_pct": o.get("gap_pct"), "rvol": None, "hour": hour, "dow": dow, "year": d[:4], "tier": "futures",
                           "sector": "index", "nq_overnight_gap_pct": o.get("gap_pct"), "nq_overnight_range_vs_20d": o.get("range_vs_20d"),
                           "regime": list(o.get("tags") or []), "hindsight": {"day_type": o.get("rth_day_type")},
                           "exit_reason": str(row.exit_reason), "dt_check": None if check is None else {"outcome": check["outcome"], "R_net": check["R_net"]}})
        be = next((v for k, v in st.items() if "breakeven" in k.lower() and isinstance(v, (int, float, np.floating)) and v == v), None)
        engine[cid]["breakeven_slip_ticks"] = None if be is None else round(float(be), 2)
        pf = st.get("profit_factor")
        log(a.quiet, "  %-24s %5d trades, net $%s, PF %s, break-even %s ticks  (%.0fs)" % (
            cid, len(tr), "{:,.0f}".format(st.get("net_pnl", 0) or 0),
            ("%.2f" % pf) if isinstance(pf, (int, float, np.floating)) and pf == pf and pf != float("inf") else "n/a",
            ("%.1f" % be) if be is not None else "n/a", time.time() - t0))
    # 3. the edge table (the lab's shapes, by year)
    trades.sort(key=lambda t: (t["date"], t["k"], t["config_id"]))
    os.makedirs(a.out_dir, exist_ok=True)
    with gzip.open(os.path.join(a.out_dir, "trades.jsonl.gz"), "wt", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t, separators=(",", ":")) + "\n")
    n_dates = len(dates)
    k_split = (2 * n_dates) // 3
    is_dates, oos_dates = set(dates[:k_split]), set(dates[k_split:])

    def block(rows):
        return stats.stats_block(rows)

    def slices(rows, key):
        groups = {}
        for t in rows:
            v = t.get(key)
            if isinstance(v, list):
                for x in v:
                    groups.setdefault(x, []).append(t)
            elif v is not None:
                groups.setdefault(str(v), []).append(t)
        return {g: block(r) for g, r in sorted(groups.items()) if len(r) >= 10}

    configs_out = []
    years = sorted(set(d[:4] for d in dates))
    for c in cfgs:
        cid = c["id"]
        rows = [t for t in trades if t["config_id"] == cid]
        is_rows = [t for t in rows if t["date"] in is_dates]
        oos_rows = [t for t in rows if t["date"] in oos_dates]
        oos = block(oos_rows)
        # rolling by year: select when the prior 3 years' record has n >= 100 and a positive lower bound
        roll = {"windows": {}, "trades": []}
        for y in years:
            prior = [t for t in rows if y >= t["year"] >= str(int(y) - 3) and t["year"] < y]
            if len(prior) >= 100:
                pb = block(prior)
                if pb["exp_ci95"][0] is not None and pb["exp_ci95"][0] > 0:
                    roll["windows"][y] = {"n": pb["n"], "exp_R": pb["exp_R"], "lo": pb["exp_ci95"][0]}
                    roll["trades"].extend(t for t in rows if t["year"] == y)
        configs_out.append({"id": cid, "setup": c["setup"], "params": c["params"], "label": c["label"],
                            "all": block(rows), "is": block(is_rows), "oos": oos, "status": ("not_applicable" if cid in not_applicable else stats.status_of(oos)),
                            "by_regime": slices(rows, "regime"), "by_hour": slices(rows, "hour"), "by_dow": slices(rows, "dow"),
                            "by_year": slices(rows, "year"), "hindsight": {"by_day_type": slices([dict(t, day_type=t["hindsight"]["day_type"]) for t in rows], "day_type")},
                            "engine": engine.get(cid), "rolling_by_year": {"selected_years": sorted(roll["windows"]), "windows": roll["windows"],
                                                                          "stats": block(roll["trades"])}})
    configs_out.sort(key=lambda c: -((c["oos"].get("exp_ci95") or [None])[0] if c["oos"].get("exp_ci95") and c["oos"]["exp_ci95"][0] is not None else -99))
    edge = {"created_by": "trader-nq", "as_of": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "fill": "next_open",
            "instrument": a.instrument, "costs": {"fees": a.fees, "slippage": a.slippage, "capital": a.capital},
            "symbols": [a.instrument], "sessions": {"first": dates[0] if dates else None, "last": dates[-1] if dates else None, "n": n_dates,
                                                     "skipped": counts["skipped"]},
            "configs_tested": len(all_cfgs), "configs_run": len(cfgs), "not_applicable": sorted(not_applicable),
            "split": {"is": {"first": dates[0] if dates else None, "last": dates[k_split - 1] if k_split else None, "n": k_split},
                      "oos": {"first": dates[k_split] if k_split < n_dates else None, "last": dates[-1] if dates else None, "n": n_dates - k_split}},
            "n_trades": len(trades), "configs": configs_out,
            "two_engines": {"compared": agree["n"], "same_outcome": agree["same"],
                            "agreement": round(agree["same"] / agree["n"], 4) if agree["n"] else None,
                            "dt_better": agree["dt_better"], "engine_better": agree["engine_better"],
                            "note": "the engine fills at the next 1-minute open with its slippage model; dt.sim fills at the next "
                                    "5-minute open with the same dollars as basis points; inside a bar both put the stop first"},
            "note": ("NQ is one instrument. Fifteen years of 5-minute setups on it are a real test of the rules, not of any "
                     "stock. Prices are the back-adjusted continuous series; fees and slippage are the engine's. Most people "
                     "who day trade lose money."), "seconds": round(time.time() - t0, 1)}
    with open(os.path.join(a.out_dir, "edge.json"), "w", encoding="utf-8") as fh:
        json.dump(edge, fh, indent=1)
    log(a.quiet, "wrote %s/edge.json and trades.jsonl.gz: %d trades, %d configs, %.0fs" % (a.out_dir, len(trades), len(cfgs), time.time() - t0))
    return 0


def cmd_overnight(a):
    t0 = time.time()
    sys.path.insert(0, a.backtester)
    from nqbt import Bars  # noqa: E402
    clock, io, setups, sim, stats, dtbars = load_dt(a.repo)
    cfg = {}
    bars = Bars.load(a.start, a.end, data_dir=os.path.join(a.backtester, "data"))
    sessions, counts = sessions_5m(bars)
    over = overnight_table(bars, sessions)
    dates = sorted(sessions)
    drows = daily_rows(sessions)
    # orb15|r=1 on every session, the dt way (next-open fill at the 5-minute level, 4 bps stand-in for the engine's costs)
    cfgs = [c for c in setups.configs() if c["id"] == "orb15|r=1"]
    rows = []
    for d in dates:
        o = over[d]
        if o.get("gap_pct") is None:
            continue
        b = sessions[d]["bars"]
        ctx = {"window": clock.entry_window(d, cfg), "prev_close": o["prev_close"], "daily_atr": dtbars.daily_atr(drows, d, 14),
               "avg_vol20": dtbars.avg_volume(drows, d, 20), "spy": None}
        sig = setups.run_config(cfgs[0], b, ctx)
        if not sig:
            continue
        # the engine's cost per round turn in dollars (fees both sides plus its default slippage), as basis points of
        # the entry for one NQ contract ($20 a point): a stock-style 4 bps would be ten times too much here
        cost_bps = a.cost_usd / (sig["entry"] * 20.0) * 1e4
        pl = sim.play(b, sig, "next_open", clock.flat_slot(d, cfg), cost_bps)
        if pl is None:
            continue
        g, v = o["gap_pct"], o["range_vs_20d"]
        gb = "lt_-0.5" if g < -0.5 else "-0.5_-0.1" if g < -0.1 else "-0.1_0.1" if g <= 0.1 else "0.1_0.5" if g <= 0.5 else "gt_0.5"
        rb = None if v is None else ("quiet_lt0.7" if v < 0.7 else "normal" if v <= 1.3 else "wide_gt1.3")
        rows.append({"date": d, "symbol": "NQ", "outcome": pl["outcome"], "R_net": pl["R_net"], "gap_bucket": gb, "range_bucket": rb})
    by_gap = {}
    for r in rows:
        by_gap.setdefault(r["gap_bucket"], []).append(r)
    by_range = {}
    for r in rows:
        if r["range_bucket"]:
            by_range.setdefault(r["range_bucket"], []).append(r)
    gaps = sorted(o["gap_pct"] for o in over.values() if o.get("gap_pct") is not None)

    def pctile(q):
        return round(gaps[min(len(gaps) - 1, int(q * (len(gaps) - 1)))], 3) if gaps else None
    bigger = [1 for o in over.values() if o.get("overnight_range_pct") and o["rth_range_pct"] > o["overnight_range_pct"]]
    doc = {"created_by": "trader-nq", "as_of": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "sessions": len(over),
           "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
           "gap_pct_percentiles": {"p5": pctile(0.05), "p25": pctile(0.25), "p50": pctile(0.5), "p75": pctile(0.75), "p95": pctile(0.95)},
           "orb15_r1_trades": len(rows), "by_gap_bucket": {k: stats.stats_block(v) for k, v in sorted(by_gap.items())},
           "by_range_bucket": {k: stats.stats_block(v) for k, v in sorted(by_range.items())},
           "share_rth_range_exceeds_overnight": round(len(bigger) / float(len(over)), 4) if over else None,
           "thresholds": {"gap_up": 0.5, "gap_down": -0.5, "wide": 1.3, "quiet": 0.7},
           "cost_usd_round_turn": a.cost_usd,
           "note": "Overnight = the Globex session from the prior 08:30-15:00 CT close to the 08:30 CT open, back-adjusted prices. "
                   "orb15|r=1 outcomes use dt.sim at the 5-minute level with $%.2f a round turn (the engine's fees and default "
                   "slippage on one contract) taken in R. Context, not a forecast." % a.cost_usd,
           "seconds": round(time.time() - t0, 1)}
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, "overnight.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    log(a.quiet, "wrote %s/overnight.json (%d sessions, %.0fs)" % (a.out_dir, len(over), time.time() - t0))
    return 0


def cmd_session_tags(a):
    sys.path.insert(0, a.backtester)
    from nqbt import Bars  # noqa: E402
    bars = Bars.load(None, None, data_dir=os.path.join(a.backtester, "data"))
    sessions, counts = sessions_5m(bars)
    over = overnight_table(bars, sessions)
    dates = sorted(over)[-a.days:]
    doc = {"created_by": "trader-nq", "as_of": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "days": a.days,
           "sessions": {d: {"nq_gap_pct": over[d].get("gap_pct"), "nq_overnight_range_vs_20d": over[d].get("range_vs_20d"),
                            "tags": over[d].get("tags") or []} for d in dates},
           "note": "the same tag names trader-montecarlo futures writes live; the lab merges them into its regime tags when present"}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    log(a.quiet, "wrote %s (%d sessions)" % (a.out, len(dates)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    for name in ("setups", "overnight", "session-tags"):
        p = sub.add_parser(name)
        p.add_argument("--backtester", required=True)
        p.add_argument("--repo", required=True)
        p.add_argument("--quiet", action="store_true")
        if name == "session-tags":
            p.add_argument("--out", required=True)
            p.add_argument("--days", type=int, default=60)
        else:
            p.add_argument("--out-dir", required=True)
            p.add_argument("--start")
            p.add_argument("--end")
        if name == "overnight":
            p.add_argument("--cost-usd", dest="cost_usd", type=float, default=22.24,
                           help="the engine's cost per round turn on one contract (fees + default slippage), default $22.24")
        if name == "setups":
            p.add_argument("--configs", default="all")
            p.add_argument("--fees", default="ninjatrader_free")
            p.add_argument("--slippage", default="default")
            p.add_argument("--instrument", default="NQ")
            p.add_argument("--capital", type=float, default=100000.0)
            p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    return {"setups": cmd_setups, "overnight": cmd_overnight, "session-tags": cmd_session_tags}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
