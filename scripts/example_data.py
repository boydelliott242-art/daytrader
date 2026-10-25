#!/usr/bin/env python3
"""Example dashboard data, computed from a bar store with the dt library (CONTRACTS.md shapes).

    python3 scripts/example_data.py                                  # tests/fixtures/dt-cache -> docs/data/*.json
    python3 scripts/example_data.py --cache-root trader/cache --out docs/data
    python3 scripts/example_data.py --no-timemachine                 # skip the upstream replay on the board

Writes docs/data/{edge,desk,regime,scorecard,largecap}.json so the dashboard renders before the
real tools have run. Every number comes from playing the setup library over the sessions in the
store (next-open fill, 4 bps round trip, stop before target inside a bar, Wilson and bootstrap
intervals, seed 19). The files are marked "example": true and the tools overwrite them with
--publish. The market caps and sectors in largecap.json are a hand-written table for the fixture's
symbols, not a screener pull. Standard library only. Nothing here touches the network or an order.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if os.path.isdir(os.path.join(HERE, "dt")):
    sys.path.insert(0, HERE)
else:
    sys.path.insert(0, ROOT)

from dt import bars, clock, io, regime, setups, sim, stats, universe  # noqa: E402

CONTEXT = ("SPY", "QQQ", "IWM", "DIA", "^VIX", "^VIX3M", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU",
           "XLB", "XLRE", "XLC")
SECTOR_ETFS = {"XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
               "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLI": "Industrials",
               "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real Estate", "XLC": "Communication Services"}
# A hand-written table for the fixture's stocks: approximate caps (example data, not a screener pull).
EXAMPLE_SYMBOLS = {
    "NVDA": ("NVIDIA Corporation", 4.4e12, "Technology", "Semiconductors", "United States", "1999"),
    "MSFT": ("Microsoft Corporation", 3.8e12, "Technology", "Computer Software: Prepackaged Software", "United States", "1986"),
    "AAPL": ("Apple Inc.", 3.7e12, "Technology", "Computer Manufacturing", "United States", "1980"),
    "GOOGL": ("Alphabet Inc.", 3.0e12, "Communication Services", "Internet Content and Information", "United States", "2004"),
    "AMZN": ("Amazon.com, Inc.", 2.3e12, "Consumer Discretionary", "Catalog and Specialty Distribution", "United States", "1997"),
    "META": ("Meta Platforms, Inc.", 1.8e12, "Communication Services", "Internet Content and Information", "United States", "2012"),
    "AVGO": ("Broadcom Inc.", 1.6e12, "Technology", "Semiconductors", "United States", "2009"),
    "TSLA": ("Tesla, Inc.", 1.4e12, "Consumer Discretionary", "Auto Manufacturing", "United States", "2010"),
    "TSM": ("Taiwan Semiconductor Manufacturing Company Ltd.", 1.3e12, "Technology", "Semiconductors", "Taiwan", "1997"),
    "BRK.B": ("Berkshire Hathaway Inc.", 1.0e12, "Financials", "Property and Casualty Insurers", "United States", None),
    "JPM": ("JP Morgan Chase & Co.", 8.5e11, "Financials", "Major Banks", "United States", None),
    "ORCL": ("Oracle Corporation", 8.0e11, "Technology", "Computer Software: Prepackaged Software", "United States", "1986"),
    "LLY": ("Eli Lilly and Company", 7.9e11, "Health Care", "Biotechnology: Pharmaceutical Preparations", "United States", None),
    "WMT": ("Walmart Inc.", 7.8e11, "Consumer Staples", "Department and Specialty Retail Stores", "United States", "1970"),
    "V": ("Visa Inc.", 6.8e11, "Financials", "Business Services", "United States", "2008"),
    "XOM": ("Exxon Mobil Corporation", 5.0e11, "Energy", "Integrated Oil Companies", "United States", None),
    "JNJ": ("Johnson & Johnson", 4.5e11, "Health Care", "Biotechnology: Pharmaceutical Preparations", "United States", None),
    "COST": ("Costco Wholesale Corporation", 4.0e11, "Consumer Staples", "Department and Specialty Retail Stores", "United States", "1985"),
}
DOW = ("Mon", "Tue", "Wed", "Thu", "Fri")
ROLL_WINDOW = 30
ROLL_TEST = 20
MIN_SLICE = 30
DESK_RUNS = (10 * 60, 10 * 60 + 15, 10 * 60 + 30, 11 * 60)    # the live schedule, minutes after midnight ET
NOTE_WORDS = "Candidates matched a rule with a measured record. None is a prediction. Most people who day trade lose money."


def example_note(what):
    return ("EXAMPLE: %s, computed by scripts/example_data.py over the offline fixture "
            "(tests/fixtures/dt-cache) so the dashboard renders before the tools have run. "
            "The tools overwrite this file with --publish." % what)


def log(msg):
    print(msg, file=sys.stderr)
    sys.stderr.flush()


# ---------------------------------------------------------------- the store

def read_store(root, symbols):
    """{sym: {"sessions": [(date, bars)], "daily": rows}} for the symbols the store has."""
    out = {}
    for s in symbols:
        st = bars.load_store(s, "bars5m", root)
        if not st:
            continue
        out[s] = {"sessions": bars.sessions_of(st), "daily": bars.daily_rows(s, root)}
    return out


def stock_symbols(root):
    folder = os.path.join(root, "bars5m")
    if not os.path.isdir(folder):
        raise SystemExit("no bar store at %s" % root)
    names = [f[:-8].replace("_", "^") for f in sorted(os.listdir(folder)) if f.endswith(".json.gz") and not f.startswith("_dropped")]
    return [s for s in names if s not in CONTEXT and not s.startswith("^")]


def meta_of(sym):
    row = EXAMPLE_SYMBOLS.get(sym)
    if not row:
        return {"symbol": sym, "name": sym, "market_cap": None, "tier": "unknown", "sector": None, "industry": None,
                "country": None, "ipo_year": None}
    name, cap, sector, industry, country, ipo = row
    return {"symbol": sym, "name": name, "market_cap": cap, "tier": universe.tier(cap), "sector": sector,
            "industry": industry, "country": country, "ipo_year": ipo}


# ---------------------------------------------------------------- the lab

def play_store(store, spy_sessions, spy_daily, cfg, cost_bps, cfgs):
    """Every config over every symbol and session -> trades (both fills kept on each line)."""
    trades = []
    for sym in sorted(store):
        daily = store[sym]["daily"]
        meta = meta_of(sym)
        for date, sess in store[sym]["sessions"]:
            if not bars.is_full(sess, date):
                continue
            ctx = {"prev_close": bars.prev_close(daily, date), "daily_atr": bars.daily_atr(daily, date),
                   "avg_vol20": bars.avg_volume(daily, date), "spy": spy_sessions.get(date),
                   "spy_prev_close": bars.prev_close(spy_daily, date), "window": clock.entry_window(date, cfg)}
            until = clock.flat_slot(date, cfg)
            dow = DOW[datetime.date.fromisoformat(date).weekday()]
            for cid, sig in setups.run_all(sess, ctx, cfgs).items():
                both = sim.play_both(sess, sig, until, cost_bps)
                if not both["next_open"] or not both["close"]:
                    continue
                hour = clock.slot_close_minutes(sig["k"]) // 60
                trades.append({"symbol": sym, "date": date, "config_id": cid, "setup": sig["setup"], "k": sig["k"],
                               "entry_time": sig["entry_time"], "hour": str(hour), "dow": dow,
                               "tier": meta["tier"], "sector": meta["sector"],
                               "signal": {k: sig[k] for k in ("entry", "stop", "target", "target_r", "stop_rule")},
                               "next_open": both["next_open"], "close": both["close"]})
    return trades


def block(trades, fill):
    rows = [dict(t, outcome=t[fill]["outcome"], R_net=t[fill]["R_net"]) for t in trades]
    return stats.stats_block(rows)


def slices(trades, key, fill, tags=None):
    out = {}
    groups = {}
    for t in trades:
        vals = tags.get(t["date"], []) if tags is not None else [t.get(key)]
        for v in vals:
            if v is None:
                continue
            groups.setdefault(str(v), []).append(t)
    for v in sorted(groups):
        if len(groups[v]) >= MIN_SLICE:
            out[v] = block(groups[v], fill)
    return out


def config_blocks(cfg, trades, dates, split_at, tags, fill):
    mine = [t for t in trades if t["config_id"] == cfg["id"]]
    is_t = [t for t in mine if t["date"] < split_at]
    oos_t = [t for t in mine if t["date"] >= split_at]
    oos = block(oos_t, fill)
    other = "close" if fill == "next_open" else "next_open"
    return {"id": cfg["id"], "setup": cfg["setup"], "params": cfg["params"], "label": cfg["label"],
            "all": block(mine, fill), "is": block(is_t, fill), "oos": oos,
            "by_regime": slices(mine, None, fill, tags), "by_hour": slices(mine, "hour", fill),
            "by_tier": slices(mine, "tier", fill), "by_sector": slices(mine, "sector", fill),
            "by_dow": slices(mine, "dow", fill),
            "by_fill": {other: {"all": block(mine, other), "is": block(is_t, other), "oos": block(oos_t, other)}},
            "status": stats.status_of(oos)}


def seen_at(k):
    """The first scheduled desk run (CONTRACTS: 10:00, 10:15, 10:30, 11:00 ET) at or after bar k's close,
    in minutes after midnight, or None when no run would have seen it."""
    close = clock.slot_close_minutes(k)
    for run in DESK_RUNS:
        if run >= close:
            return run
    return None


def rolling(trades, dates, cfgs, fill, window=ROLL_WINDOW, test_days=ROLL_TEST):
    """Select on the previous `window` sessions, trade the approved set on the next one, roll.
    Also returns `shown`: what the scheduled desk would have put on the board (approved and watch,
    one per symbol), with the late flag the scorecard applies."""
    by_date = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t)
    test = dates[-test_days:] if len(dates) > test_days else dates[1:]
    equity, daily, picked, per_day, curve, shown = 0.0, [], [], [], [], []
    for d in test:
        i = dates.index(d)
        train_dates = set(dates[max(0, i - window):i])
        train = [t for t in trades if t["date"] in train_dates]
        approved, live = {}, {}
        for c in cfgs:
            b = block([t for t in train if t["config_id"] == c["id"]], fill)
            status = stats.status_of(b)
            if status == "approved":
                approved[c["id"]] = b["exp_ci95"][0]
            if status in ("approved", "watch"):
                live[c["id"]] = (b["exp_ci95"][0], status)
        todays, board = {}, {}
        for t in by_date.get(d, []):
            if t["config_id"] in live and seen_at(t["k"]) is not None:
                best = board.get(t["symbol"])
                if best is None or live[t["config_id"]][0] > live[best["config_id"]][0]:
                    board[t["symbol"]] = t
            if t["config_id"] not in approved:
                continue
            best = todays.get(t["symbol"])
            if best is None or approved[t["config_id"]] > approved[best["config_id"]]:
                todays[t["symbol"]] = t
        day_trades = [todays[s] for s in sorted(todays)]
        r = sum(t[fill]["R_net"] for t in day_trades)
        equity += r
        daily.append(round(r, 4))
        curve.append(round(equity, 4))
        picked.extend(day_trades)
        for s in sorted(board):
            t = board[s]
            run = seen_at(t["k"])
            shown.append(dict(t, status=live[t["config_id"]][1], seen_at=clock.clock12(run),
                              late=(run - clock.slot_close_minutes(t["k"])) > clock.BAR_MIN))
        per_day.append({"date": d, "approved": sorted(approved), "watch": sorted(k for k in live if k not in approved),
                        "trades": len(day_trades), "shown": len(board), "R": round(r, 4)})
    return {"window": window, "test_days": len(test), "selected_per_day": per_day, "days": test,
            "daily_R": daily, "equity_R": curve, "stats": block(picked, fill), "fill": fill}, picked, shown


# ---------------------------------------------------------------- the desk and the time machine

def chart_doc(sym, sessions):
    """A bar store -> the Yahoo v8 chart shape the upstream time machine reads offline."""
    t, o, h, l, c, v = [], [], [], [], [], []
    for _, sess in sessions:
        for b in sess:
            t.append(b["t"]); o.append(b["o"]); h.append(b["h"]); l.append(b["l"]); c.append(b["c"]); v.append(b["v"])
    meta = {"symbol": sym.replace(".", "-"), "dataGranularity": "5m", "range": "60d", "currency": "USD",
            "exchangeTimezoneName": "America/New_York", "regularMarketTime": (t[-1] + 300) if t else 0}
    return {"chart": {"result": [{"meta": meta, "timestamp": t,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]}}],
                      "error": None}}


def run_timemachine(sym, sessions, date, sig, until_hhmm):
    tm = os.path.join(ROOT, "trader-timemachine", "timemachine.py")
    if not os.path.exists(tm):
        return None
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    try:
        json.dump(chart_doc(sym, sessions), tmp)
        tmp.close()
        argv = [sys.executable, tm, "run", "--symbol", sym, "--date", date, "--entry", str(sig["entry"]),
                "--stop", str(sig["stop"]), "--target", str(sig["target"]), "--entry-time", sig["entry_time"],
                "--until", until_hhmm, "--offline", tmp.name, "--json", "--now", "%s %s" % (date, until_hhmm)]
        orr = sig.get("opening_range")
        if orr and sig["setup"].startswith("orb"):
            argv += ["--setup", "orb", "--or-minutes", str(orr.get("minutes") or 15),
                     "--stop-rule", sig.get("stop_rule") if sig.get("stop_rule") in ("or_mid", "or_low") else "auto"]
        else:
            argv += ["--setup", sig["setup"]]
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=600)
        if p.returncode != 0:
            log("  time machine refused %s: %s" % (sym, p.stderr.strip()))
            return None
        doc = json.loads(p.stdout)
    finally:
        os.unlink(tmp.name)
    mc, hist = doc["mc"], doc.get("history") or {}
    return {"p_target": mc["p_target"], "p_stop": mc["p_stop"], "p_neither": mc["p_neither"], "p_green": mc["p_green"],
            "avg_R": mc["avg_R"], "median_close": mc["median_close"], "p5_close": mc["p5_close"],
            "p95_close": mc["p95_close"], "replays": mc["replays"], "until": mc.get("until"),
            "history": {k: hist.get(k) for k in ("sessions", "triggered", "target", "stop", "close", "green", "avg_R", "note")},
            "verdict": doc["verdict"], "verdict_notes": doc.get("verdict_notes") or [], "seed": mc["seed"]}


def desk_board(store, configs, tags_of, date, at_hhmm, cfg, top, timemachine, spy_daily):
    at = clock.parse_hhmm(at_hhmm)
    last_k = (clock.minutes_of(at) - clock.OPEN_MIN) // clock.BAR_MIN - 1    # the last bar CLOSED by `at`
    today_tags = tags_of.get(date, [])
    live = {c["id"]: c for c in configs if c["status"] in ("approved", "watch")}
    cfgs = [c for c in setups.configs() if c["id"] in live]
    spy = store.get("SPY", {}).get("sessions", [])
    spy_bars = dict(spy).get(date)
    rows = []
    for sym in sorted(store):
        if sym in CONTEXT or sym.startswith("^"):
            continue
        sess = dict(store[sym]["sessions"]).get(date)
        if not sess:
            continue
        partial = sess[:last_k + 1]
        daily = store[sym]["daily"]
        ctx = {"prev_close": bars.prev_close(daily, date), "daily_atr": bars.daily_atr(daily, date),
               "avg_vol20": bars.avg_volume(daily, date), "spy": spy_bars[:last_k + 1] if spy_bars else None,
               "spy_prev_close": bars.prev_close(spy_daily, date), "window": clock.entry_window(date, cfg)}
        best = None
        for cid, sig in setups.run_all(partial, ctx, cfgs).items():
            c = live[cid]
            slice_ = None
            for tag in today_tags:
                b = (c.get("by_regime") or {}).get(tag)
                if b and b["n"] >= 50 and (slice_ is None or b["n"] > slice_[1]["n"]):
                    slice_ = (tag, b)
            use = slice_[1] if slice_ else c["oos"]
            score = (use.get("exp_ci95") or [None])[0]
            if score is None:
                continue
            flags = []
            if sig["k"] < last_k - 1:
                flags.append("late")
            if c["status"] == "watch":
                flags.append("watch_only")
            for tag in today_tags:
                b = (c.get("by_regime") or {}).get(tag)
                if b and b["n"] >= 50 and (b.get("exp_R") or 0) < 0:
                    flags.append("regime_against")
                    break
            risk = sig["entry"] - sig["stop"]
            if risk > 0 and partial[-1]["c"] >= sig["entry"] + 0.5 * risk:
                flags.append("moving")
            cand = {"symbol": sym, "setup": sig["setup"], "config_id": cid, "label": c["label"], "signal": sig,
                    "lab": {"status": c["status"], "oos": c["oos"],
                            "regime_slice": {"tag": slice_[0], "stats": slice_[1]} if slice_ else None,
                            "fill_note": "ranked on the next-open fill after 4 bps; the plan price is the idealised number"},
                    "flags": flags, "score": round(score, 4)}
            if best is None or cand["score"] > best["score"]:
                best = cand
        if best:
            rows.append(best)
    rows.sort(key=lambda r: (-r["score"], r["symbol"]))
    out = []
    for i, r in enumerate(rows[:top]):
        m = meta_of(r["symbol"])
        r["timemachine"] = None
        if timemachine and i < 5:
            log("  time machine: %s %s" % (r["symbol"], r["config_id"]))
            r["timemachine"] = run_timemachine(r["symbol"], store[r["symbol"]]["sessions"], date, r["signal"],
                                               cfg.get("flat_by") or "15:55")
        out.append(dict({"rank": i + 1, "symbol": r["symbol"], "name": m["name"], "sector": m["sector"], "tier": m["tier"]},
                        **{k: r[k] for k in ("setup", "config_id", "label", "signal", "lab", "timemachine", "flags", "score")}))
    return out, len(rows)


def live_regime(store, date, at_hhmm, spy_daily):
    at = clock.parse_hhmm(at_hhmm)
    last_k = (clock.minutes_of(at) - clock.OPEN_MIN) // clock.BAR_MIN - 1
    spy = dict(store.get("SPY", {}).get("sessions", [])).get(date)
    if not spy or last_k < 2:
        return None
    sp = spy[:last_k + 1]
    pc = bars.prev_close(spy_daily, date)
    oh, ol = bars.opening_range(sp, 3)
    last = sp[-1]["c"]
    vw = bars.vwap_series(sp)[-1]
    above, up, n = 0, 0, 0
    for sym in store:
        if sym in CONTEXT or sym.startswith("^"):
            continue
        sess = dict(store[sym]["sessions"]).get(date)
        if not sess:
            continue
        c = sess[last_k]["c"]
        n += 1
        above += 1 if c > sess[0]["o"] else 0
        prev = bars.prev_close(store[sym]["daily"], date)
        up += 1 if prev and c > prev else 0
    sectors = []
    for etf in SECTOR_ETFS:
        sess = dict(store.get(etf, {}).get("sessions", [])).get(date)
        if sess:
            sectors.append({"etf": etf, "name": SECTOR_ETFS[etf],
                            "move_pct": round((sess[last_k]["c"] / sess[0]["o"] - 1) * 100, 3)})
    sectors.sort(key=lambda s: -s["move_pct"])
    return {"at": at_hhmm, "spy_gap_pct": round((sp[0]["o"] / pc - 1) * 100, 3) if pc else None,
            "spy_or": "above" if last > oh else "below" if last < ol else "inside",
            "spy_vs_vwap_pct": round((last / vw - 1) * 100, 3) if vw else None,
            "breadth_above_open_pct": round(100.0 * above / n, 1) if n else None,
            "breadth_up_pct": round(100.0 * up / n, 1) if n else None, "breadth_n": n, "sectors": sectors}


# ---------------------------------------------------------------- the scorecard

def scorecard_doc(shown, fill, edge, max_loss):
    """The scorecard's report: every call the scheduled desk would have shown, graded on the real bars.
    A late call (seen more than one bar after its entry bar closed) is graded but never counted."""
    rows = sorted(shown, key=lambda t: (t["date"], t["k"], t["symbol"]))
    graded = [dict(t, outcome=t[fill]["outcome"], R_net=t[fill]["R_net"]) for t in rows]
    counted = [t for t in graded if not t.get("late")]
    head = stats.stats_block(counted)
    eq, eq_r, eq_usd = 0.0, [], []
    for t in counted:
        eq += t["R_net"]
        eq_r.append(round(eq, 4))
        eq_usd.append(round(eq * max_loss, 2))
    by_setup = {}
    for t in counted:
        by_setup.setdefault(t["setup"], []).append(t)
    by_setup = {k: stats.stats_block(v) for k, v in sorted(by_setup.items())}
    weeks = {}
    for t in counted:
        y, w, _ = datetime.date.fromisoformat(t["date"]).isocalendar()
        weeks.setdefault("%d-W%02d" % (y, w), []).append(t)
    by_week = [{"week": k, "n": len(v), "green": sum(1 for t in v if t["R_net"] > 0),
                "R": round(sum(t["R_net"] for t in v), 4)} for k, v in sorted(weeks.items())]
    live_ids = {c["id"] for c in edge["configs"] if c["status"] in ("approved", "watch")}
    pooled = [t for t in edge["_trades"] if t["config_id"] in live_ids and t["date"] >= edge["split"]["oos"]["first"]]
    lab = stats.stats_block([dict(t, outcome=t[fill]["outcome"], R_net=t[fill]["R_net"]) for t in pooled])
    gap = round(head["exp_R"] - lab["exp_R"], 4) if head["exp_R"] is not None and lab["exp_R"] is not None else None
    first, last = (graded[0]["date"], graded[-1]["date"]) if graded else (None, None)
    return {"created_by": "trader-scorecard", "as_of": io.now_iso(), "example": True, "fill": fill,
            "calls": len(rows), "graded": len(graded), "late": len(graded) - len(counted), "pending": 0,
            "first_date": first, "last_date": last, "headline": head, "by_setup": by_setup, "by_week": by_week,
            "equity_R": eq_r, "equity_usd": eq_usd, "max_loss_per_trade_usd": max_loss,
            "lab_oos_exp_R": lab["exp_R"], "lab_oos_exp_ci95": lab["exp_ci95"], "lab_oos_n": lab["n"], "gap_R": gap,
            "recent": [{"date": t["date"], "symbol": t["symbol"], "config_id": t["config_id"], "status": t.get("status"),
                        "entry_time": t["entry_time"], "seen_at": t.get("seen_at"), "late": bool(t.get("late")),
                        "outcome": t["outcome"], "R_net": t["R_net"]} for t in graded[-12:]],
            "note": example_note("every call the scheduled desk (10:00, 10:15, 10:30, 11:00 ET) would have shown over "
                                 "the walk-forward's test sessions, approved and watch alike, graded on the real bars; "
                                 "late calls are graded but not counted") + " " + NOTE_WORDS}


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Example dashboard data from a bar store (standard library only).")
    ap.add_argument("--cache-root", default=os.path.join("tests", "fixtures", "dt-cache"))
    ap.add_argument("--out", default=os.path.join("docs", "data"))
    ap.add_argument("--config", default=os.path.join("trader-budget", "config.example.json"))
    ap.add_argument("--fill", default="next_open", choices=["next_open", "close"])
    ap.add_argument("--cost-bps", type=float, default=sim.DEFAULT_COST_BPS)
    ap.add_argument("--at", default="10:15", help="the board's cutoff on the last session (New York)")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--no-timemachine", action="store_true")
    a = ap.parse_args(argv)
    root = a.cache_root if os.path.isabs(a.cache_root) else os.path.join(os.getcwd(), a.cache_root)
    if not os.path.isdir(root):
        root = os.path.join(ROOT, a.cache_root)
    cfg = io.read_config(a.config if os.path.exists(a.config) else os.path.join(ROOT, a.config))
    syms = stock_symbols(root)
    store = read_store(root, syms + list(CONTEXT))
    if "SPY" not in store:
        raise SystemExit("the store has no SPY: the lab needs it for context")
    spy_sessions = dict(store["SPY"]["sessions"])
    spy_daily = store["SPY"]["daily"]
    dates = sorted(spy_sessions)
    tags_of = {d: regime.session_tags(d, {"SPY": spy_daily, "^VIX": store.get("^VIX", {}).get("daily"),
                                           "^VIX3M": store.get("^VIX3M", {}).get("daily")}) for d in dates}
    stocks = {s: store[s] for s in syms if s in store}
    cfgs = setups.configs()
    log("playing %d configs over %d symbols and %d sessions" % (len(cfgs), len(stocks), len(dates)))
    trades = play_store(stocks, spy_sessions, spy_daily, cfg, a.cost_bps, cfgs)
    log("  %d trades" % len(trades))
    split_i = (len(dates) * 2) // 3
    split_at = dates[split_i]
    configs = []
    for c in cfgs:
        log("  stats %s" % c["id"])
        configs.append(config_blocks(c, trades, dates, split_at, tags_of, a.fill))
    log("rolling walk-forward")
    roll, picked, shown = rolling(trades, dates, cfgs, a.fill)
    counts = {}
    for c in configs:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    edge = {"created_by": "trader-lab", "as_of": io.now_iso(), "example": True, "fill": a.fill, "fills": ["next_open", "close"],
            "cost_bps": a.cost_bps, "symbols": sorted(stocks), "sessions": {"first": dates[0], "last": dates[-1], "n": len(dates)},
            "configs_tested": len(configs), "status_counts": counts,
            "split": {"is": {"first": dates[0], "last": dates[split_i - 1], "n": split_i},
                      "oos": {"first": split_at, "last": dates[-1], "n": len(dates) - split_i}},
            "configs": configs, "rolling": roll, "trades": len(trades),
            "note": example_note("the setup library over the fixture's %d stocks and %d sessions" % (len(stocks), len(dates)))
            + " configs_tested is printed next to every table: with dozens of configs, something looks good by luck. "
              "Approved means the OOS record cleared the gate, not that the trade wins. " + NOTE_WORDS}
    os.makedirs(a.out, exist_ok=True)
    io.write_json(os.path.join(a.out, "edge.json"), edge)
    log("wrote edge.json (%s)" % counts)

    date = dates[-1]
    daily = regime.daily_context(date, spy_daily, store.get("^VIX", {}).get("daily"), store.get("^VIX3M", {}).get("daily"))
    tags = daily.pop("tags")
    reg = {"created_by": "trader-regime", "as_of": "%s %s ET" % (date, a.at), "example": True, "date": date,
           "daily": daily, "live": live_regime(store, date, a.at, spy_daily), "tags": tags,
           "note": example_note("the regime read for the fixture's last session")}
    io.write_json(os.path.join(a.out, "regime.json"), reg)
    log("wrote regime.json (%s)" % ", ".join(tags))

    log("the board for %s at %s" % (date, a.at))
    board, scanned_hits = desk_board(store, configs, tags_of, date, a.at, cfg, a.top, not a.no_timemachine, spy_daily)
    desk = {"created_by": "trader-desk", "mode": "live", "as_of": "%s %s ET" % (date, a.at), "example": True,
            "regime": dict(reg["daily"], live=reg["live"], tags=tags),
            "universe": len(stocks), "scanned": len(stocks), "matched": scanned_hits,
            "excluded": {"earnings": [], "blocked": [], "note": "offline example: the earnings calendar was not read"},
            "candidates": board,
            "note": NOTE_WORDS + " " + example_note("a replay of the desk's board on the fixture's last session")}
    io.write_json(os.path.join(a.out, "desk.json"), desk)
    log("wrote desk.json (%d candidates)" % len(board))

    edge["_trades"] = trades
    sc = scorecard_doc(shown, a.fill, edge, float(cfg.get("max_loss_per_trade_usd") or 50))
    io.write_json(os.path.join(a.out, "scorecard.json"), sc)
    log("wrote scorecard.json (%d graded)" % sc["graded"])

    rows = sorted((meta_of(s) for s in stocks), key=lambda m: -(m["market_cap"] or 0))
    lc = {"created_by": "trader-largecap", "as_of": io.now_iso(), "example": True, "min_cap_usd": 10e9,
          "universe_rule": "hand-written table for the fixture's symbols (example)", "count": len(rows), "symbols": rows,
          "note": example_note("a hand-written market-cap table for the fixture's stocks, not a Nasdaq screener pull")}
    io.write_json(os.path.join(a.out, "largecap.json"), lc)
    log("wrote largecap.json (%d symbols)" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
