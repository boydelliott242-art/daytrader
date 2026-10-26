#!/usr/bin/env python3
"""The lab for trader-lab: every setup config x every large cap x every cached session,
played forward on the real 5-minute bars after costs. Standard library only. No network.

    python3 lab.py run                                    # trader/cache -> trader/lab/trades.jsonl.gz + edge.json
    python3 lab.py run --symbols NVDA,AAPL,TSLA --sessions 40 --fill close --cost-bps 2
    python3 lab.py run --symbols-file trader/largecap.txt --workers 8 --publish
    python3 lab.py report                                 # the tables, sorted by the OOS lower bound
    python3 lab.py report --config "orb15|r=1" --by regime
    python3 lab.py why TSLA 2026-10-19                    # every config's signal and outcome on one session

What a run does, symbol by symbol (one task per symbol in a multiprocessing pool):
  1. Every FULL session in the bar store (78 five-minute bars, 42 on an early close).
  2. The context a rule may know at the bar: the entry window from the config
     (no_entries_before 09:45 .. no_entries_after 15:30), the prior close, the
     14-day ATR, the 20-day average volume, SPY's bars that day.
  3. dt.setups.run_all: every config of every setup, at most one signal each per session.
  4. dt.sim.play: the signal walked forward to the flat-by bar, stop before target
     inside a bar, next-open fill (the realistic one) or plan fill, costs in R.
  5. One line per trade in trades.jsonl.gz: the signal, the outcome and the context
     (gap, relative volume, hour, weekday, tier, sector, the regime tags computed
     only from closes before that date, and SPY's day type as a hindsight label).

Then edge.json: per config the STATS block over all sessions, the first two thirds
(in sample) and the last third (out of sample), the status gate on the OOS block,
the breakdowns, and the rolling walk-forward: for each of the last 20 sessions,
the configs that cleared n >= 60 and a positive 95 % lower bound over the 30
sessions BEFORE it, traded on that day, one trade per symbol.

Every number is a record of what a rule did on past bars. None is a prediction.
With 31 configs something looks good by luck. Most people who day trade lose money.
"""

import argparse
import datetime
import gzip
import json
import math
import multiprocessing
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# installed as a skill, dt/ sits next to this file; in the repo it is at the root
sys.path.insert(0, HERE if os.path.isdir(os.path.join(HERE, "dt")) else ROOT)

from dt import bars as dtbars, clock, io as dtio, regime as dtregime, setups, sim, stats as dtstats, universe  # noqa: E402

CONTEXT_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA", "^VIX", "^VIX3M", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI",
                   "XLU", "XLB", "XLRE", "XLC"}
DOW = ("Mon", "Tue", "Wed", "Thu", "Fri")
HOURS = ("9", "10", "11", "12", "13", "14", "15")
SLICE_MIN_N = 10
ROLLING_WINDOW = 30
ROLLING_TEST_DAYS = 20
ROLLING_MIN_N = 60
BEST_DAYS_SHARE = 0.2
GRID_SLOTS = 78
NOTE = ("Every config matched a rule on past 5-minute bars, after costs; none is a prediction. "
        "configs_tested counts every config in the grid: with that many, something looks good by luck. "
        "Approved means the out-of-sample record cleared the gate, not that the next trade wins. "
        "Most people who day trade lose money.")
STATUS_RULE = ("approved = OOS n >= 100 and the 95 % lower bound of the expectancy > 0 after costs; "
               "watch = OOS n >= 30 and exp R > 0 but the CI crosses zero; rejected otherwise.")


class LabError(Exception):
    pass


# ---------------------------------------------------------------- symbols and context

def is_context(sym):
    s = sym.upper()
    return s in CONTEXT_SYMBOLS or s.startswith("^")


def store_symbols(root):
    """Every symbol with a bars5m store under root, in the store's spelling (^VIX, BRK.B)."""
    d = os.path.join(root, "bars5m")
    if not os.path.isdir(d):
        raise LabError("no bar store at %s (run scripts/fetch_cache.py first)" % d)
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json.gz"):
            continue
        name = f[:-len(".json.gz")]
        sym = ("^" + name[1:]) if name.startswith("_") else name
        out.append(sym.upper())
    return out


def pick_symbols(root, symbols=None, symbols_file=None):
    """The tradable symbols of the store, intersected with --symbols / --symbols-file when given.
    Context symbols are never traded. -> (symbols, not_in_store)"""
    have = store_symbols(root)
    wanted = None
    if symbols:
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if symbols_file:
        try:
            from_file = dtio.symbols_from_file(symbols_file)
        except dtio.IOError_ as e:
            raise LabError(str(e))
        wanted = (wanted or []) + from_file
    if wanted is None:
        return [s for s in have if not is_context(s)], []
    have_set = set(have)
    seen, out, missing = set(), [], []
    for s in wanted:
        if s in seen:
            continue
        seen.add(s)
        if is_context(s):
            continue
        if s in have_set:
            out.append(s)
        else:
            missing.append(s)
    return out, missing


def load_largecap(path):
    """{symbol: {"tier", "sector"}} from trader/largecap.json, or {} when absent."""
    doc = dtio.read_json(path, None) if path else None
    out = {}
    for row in (doc or {}).get("symbols") or []:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        tier = row.get("tier") or universe.tier(row.get("market_cap"))
        out[sym] = {"tier": tier or "unknown", "sector": row.get("sector") or "unknown"}
    return out


def load_context(root, nq_tags=None):
    if nq_tags is None:
        nq_tags = os.path.join("trader", "nq", "session_tags.json")   # trader-nq session-tags, when it has run
    """SPY's 5-minute sessions and the daily rows of SPY, ^VIX and ^VIX3M (empty when missing), plus the NQ
    overnight tags per session (trader-nq session-tags) when that file exists."""
    spy = dtbars.load_store("SPY", "bars5m", root) or {}
    tags = {}
    doc = dtio.read_json(nq_tags, None) if nq_tags else None
    if isinstance(doc, dict):
        for d, rec in (doc.get("sessions") or {}).items():
            if isinstance(rec, dict) and rec.get("tags"):
                tags[d] = [str(t) for t in rec["tags"]]
    return {"spy_sessions": spy.get("sessions") or {},
            "spy_daily": dtbars.daily_rows("SPY", root),
            "vix_daily": dtbars.daily_rows("^VIX", root),
            "vix3m_daily": dtbars.daily_rows("^VIX3M", root),
            "nq_tags": tags}


def session_context(date, daily, spy_bars, spy_daily, cfg):
    """What a rule may know at the bar, nothing from later in the day."""
    return {"window": clock.entry_window(date, cfg),
            "prev_close": dtbars.prev_close(daily, date),
            "daily_atr": dtbars.daily_atr(daily, date),
            "avg_vol20": dtbars.avg_volume(daily, date),
            "spy": spy_bars,
            "spy_prev_close": dtbars.prev_close(spy_daily, date) if spy_daily else None}


def regime_tags(date, ctxd, cache=None):
    if cache is not None and date in cache:
        return cache[date]
    tags = dtregime.session_tags(date, {"SPY": ctxd["spy_daily"], "^VIX": ctxd["vix_daily"],
                                        "^VIX3M": ctxd["vix3m_daily"]})
    for t in (ctxd.get("nq_tags") or {}).get(date, []):
        if t not in tags:
            tags.append(t)
    if cache is not None:
        cache[date] = tags
    return tags


def spy_bars_of(date, ctxd, cache=None):
    if cache is not None and date in cache:
        return cache[date]
    rows = ctxd["spy_sessions"].get(date)
    b = dtbars.bars_of(rows) if rows else None
    if cache is not None:
        cache[date] = b
    return b


# ---------------------------------------------------------------- one session

def trade_record(sym, date, cfg, sig, out, bars, ctx, meta=None, tags=None, day_type=None):
    """One line of trades.jsonl.gz: the signal and the outcome flattened (so dt.stats reads a
    line as it is) plus the context."""
    k = sig["k"]
    rec = {"symbol": sym, "date": date, "config_id": cfg["id"], "setup": cfg["setup"], "params": cfg["params"]}
    for key, v in sig.items():
        if key not in ("setup", "params"):
            rec[key] = v
    rec.update(out)
    rec["exit_time"] = clock.clock12(clock.slot_close_minutes(out["exit_slot"]))
    pc = ctx.get("prev_close")
    rec["gap_pct"] = round((bars[0]["o"] / pc - 1) * 100, 3) if pc else None
    av = ctx.get("avg_vol20")
    vol = sum((b["v"] or 0) for b in bars[:k + 1])
    rec["rvol"] = round(vol / (av * (k + 1) / float(GRID_SLOTS)), 3) if av else None
    rec["hour"] = str(clock.slot_close_minutes(k) // 60)
    rec["dow"] = DOW[datetime.date.fromisoformat(date).weekday()]
    meta = meta or {}
    rec["tier"] = meta.get("tier") or "unknown"
    rec["sector"] = meta.get("sector") or "unknown"
    rec["regime"] = list(tags or [])
    rec["hindsight"] = {"day_type": day_type}
    return rec


def signals_on(bars, ctx, cfgs=None):
    """{config_id: signal} on one (full or partial) session: dt.setups.run_all."""
    return setups.run_all(bars, ctx, cfgs)


def session_trades(sym, date, bars, ctx, fill="next_open", until=None, cost_bps=sim.DEFAULT_COST_BPS, cfgs=None,
                   meta=None, tags=None, day_type=None):
    """Every config's trade on one session, in config order."""
    cfgs = cfgs or setups.configs()
    sigs = signals_on(bars, ctx, cfgs)
    out = []
    for cfg in cfgs:
        sig = sigs.get(cfg["id"])
        if not sig:
            continue
        res = sim.play(bars, sig, fill, until, cost_bps)
        if res is None:
            continue
        out.append(trade_record(sym, date, cfg, sig, res, bars, ctx, meta, tags, day_type))
    return out


def study_symbol(sym, root, ctxd, cfg, fill, cost_bps, sessions_n=None, meta=None, cfgs=None, caches=None):
    """All trades of one symbol over its full sessions -> {"symbol", "sessions", "trades", "skipped"}."""
    store = dtbars.load_store(sym, "bars5m", root)
    if not store:
        return {"symbol": sym, "sessions": [], "trades": [], "skipped": "no bars5m store"}
    sessions = [(d, b) for d, b in dtbars.sessions_of(store) if dtbars.is_full(b, d)]
    if sessions_n:
        sessions = sessions[-sessions_n:]
    if not sessions:
        return {"symbol": sym, "sessions": [], "trades": [], "skipped": "no full session"}
    daily = dtbars.daily_rows(sym, root)
    caches = caches if caches is not None else {"tags": {}, "spy": {}, "hind": {}}
    trades, dates = [], []
    for date, b in sessions:
        spy_bars = spy_bars_of(date, ctxd, caches["spy"])
        ctx = session_context(date, daily, spy_bars, ctxd["spy_daily"], cfg)
        tags = regime_tags(date, ctxd, caches["tags"])
        if date not in caches["hind"]:
            caches["hind"][date] = dtregime.hindsight(spy_bars)["day_type"]
        until = clock.flat_slot(date, cfg)
        trades.extend(session_trades(sym, date, b, ctx, fill, until, cost_bps, cfgs, meta, tags,
                                     caches["hind"][date]))
        dates.append(date)
    return {"symbol": sym, "sessions": dates, "trades": trades, "skipped": None}


# ---------------------------------------------------------------- the pool

_PACK = {}


def _init_worker(pack):
    _PACK.clear()
    _PACK.update(pack)
    _PACK["caches"] = {"tags": {}, "spy": {}, "hind": {}}
    _PACK["cfgs"] = setups.configs()


def _worker(sym):
    p = _PACK
    try:
        return study_symbol(sym, p["root"], p["ctxd"], p["cfg"], p["fill"], p["cost_bps"], p["sessions_n"],
                            p["meta"].get(sym), p["cfgs"], p["caches"])
    except Exception as e:  # one bad store must not kill the run
        return {"symbol": sym, "sessions": [], "trades": [], "skipped": "error: %s" % e}


def slim(rec):
    """The row the stats keep in memory: (symbol, date, outcome, R_net, hour, dow, tier, sector, tags, day_type)."""
    return (rec["symbol"], rec["date"], rec["outcome"], float(rec["R_net"]), rec["hour"], rec["dow"], rec["tier"],
            rec["sector"], tuple(rec.get("regime") or ()), (rec.get("hindsight") or {}).get("day_type"))


def stats_of_rows(rows):
    """dt.stats.stats_block over slim rows."""
    return dtstats.stats_block([{"symbol": r[0], "date": r[1], "outcome": r[2], "R_net": r[3]} for r in rows])


def slices(rows, index, keys=None, min_n=SLICE_MIN_N):
    """{value: STATS} for one row field, only values with at least min_n trades."""
    groups = {}
    for r in rows:
        groups.setdefault(r[index], []).append(r)
    out = {}
    for key in (keys or sorted(groups, key=str)):
        g = groups.get(key) or []
        if len(g) >= min_n:
            out[str(key)] = stats_of_rows(g)
    return out


def tag_slices(rows, min_n=SLICE_MIN_N):
    groups = {}
    for r in rows:
        for t in r[8]:
            groups.setdefault(t, []).append(r)
    return {t: stats_of_rows(g) for t, g in sorted(groups.items()) if len(g) >= min_n}


def split_sessions(dates):
    """Chronological 2:1 split by session date: the first two thirds in sample."""
    dates = sorted(set(dates))
    n = len(dates)
    if n < 3:
        return dates, []
    k = (2 * n) // 3
    return dates[:k], dates[k:]


def span(dates):
    dates = sorted(dates)
    return {"first": dates[0] if dates else None, "last": dates[-1] if dates else None, "n": len(dates)}


def rolling_for_config(rows, sessions, window=ROLLING_WINDOW, test_days=ROLLING_TEST_DAYS, min_n=ROLLING_MIN_N):
    """For each test day: this config's STATS over the `window` sessions before it, when it could be
    selected (n >= min_n and the mean is positive: a percentile bootstrap's lower bound sits under the
    mean, so a non-positive mean can never clear lo > 0 and its bootstrap is skipped), and its rows on
    the test day. -> {"windows": {T: {"n", "exp_R", "lo", "first", "last"}}, "day_rows": {T: rows}}"""
    sessions = sorted(sessions)
    tests = test_sessions(sessions, window, test_days)
    by_date = {}
    for r in rows:
        by_date.setdefault(r[1], []).append(r)
    windows, day_rows = {}, {}
    for t in tests:
        i = sessions.index(t)
        win = sessions[i - window:i]
        wrows = []
        for d in win:
            wrows.extend(by_date.get(d, ()))
        n = len(wrows)
        if n >= min_n and sum(r[3] for r in wrows) > 0:
            st = stats_of_rows(wrows)
            lo = st["exp_ci95"][0]
            if lo is not None and lo > 0:
                windows[t] = {"n": st["n"], "exp_R": st["exp_R"], "lo": lo, "hi": st["exp_ci95"][1],
                              "win_rate": st["win_rate"], "first": win[0], "last": win[-1]}
        if t in by_date:
            day_rows[t] = by_date[t]
    return {"windows": windows, "day_rows": day_rows}


def test_sessions(sessions, window=ROLLING_WINDOW, test_days=ROLLING_TEST_DAYS):
    """The last test_days sessions that have a full window of sessions before them."""
    sessions = sorted(sessions)
    n = len(sessions)
    if n <= window:
        return []
    return sessions[max(window, n - test_days):]


def _stats_task(payload):
    job, cid, rows = payload["job"], payload["cid"], payload["rows"]
    out = {"job": job, "cid": cid}
    if job == "core":
        is_set, oos_set = set(payload["is"]), set(payload["oos"])
        out["all"] = stats_of_rows(rows)
        out["is"] = stats_of_rows([r for r in rows if r[1] in is_set])
        out["oos"] = stats_of_rows([r for r in rows if r[1] in oos_set])
        out["by_hour"] = slices(rows, 4, HOURS)
        out["by_dow"] = slices(rows, 5, DOW)
        out["by_tier"] = slices(rows, 6)
    elif job == "slices":
        out["by_regime"] = tag_slices(rows)
        out["by_sector"] = slices(rows, 7)
        out["hindsight"] = {"by_day_type": slices(rows, 9)}
    elif job == "rolling":
        out.update(rolling_for_config(rows, payload["sessions"], payload["window"], payload["test_days"],
                                      payload["min_n"]))
    return out


def rolling_walk_forward(per_config, sessions, window=ROLLING_WINDOW, test_days=ROLLING_TEST_DAYS,
                         min_n=ROLLING_MIN_N):
    """per_config: {cid: rolling_for_config(...)} -> the rolling block. One trade per symbol per test day:
    when several selected configs fire on a symbol, the one with the higher in-window lower bound."""
    sessions = sorted(sessions)
    tests = test_sessions(sessions, window, test_days)
    days, trades, equity, daily_r, selected_per_day = [], [], 0.0, [], []
    for t in tests:
        i = sessions.index(t)
        chosen = {}
        selected = []
        for cid in sorted(per_config):
            info = per_config[cid]["windows"].get(t)
            if not info:
                continue
            selected.append(dict(info, config_id=cid))
            for r in per_config[cid]["day_rows"].get(t, ()):
                cur = chosen.get(r[0])
                if cur is None or info["lo"] > cur[1]["lo"]:
                    chosen[r[0]] = (cid, info, r)
        selected.sort(key=lambda x: (-x["lo"], x["config_id"]))
        day_trades = []
        for sym in sorted(chosen):
            cid, info, r = chosen[sym]
            day_trades.append({"date": t, "symbol": sym, "config_id": cid, "outcome": r[2], "R_net": r[3],
                               "window_lo": info["lo"]})
        day_r = round(sum(x["R_net"] for x in day_trades), 4)
        equity = round(equity + day_r, 4)
        trades.extend(day_trades)
        daily_r.append(day_r)
        selected_per_day.append([x["config_id"] for x in selected])
        days.append({"date": t, "window": {"first": sessions[i - window], "last": sessions[i - 1], "n": window},
                     "selected": [{"config_id": x["config_id"], "n": x["n"], "exp_R": x["exp_R"],
                                   "exp_ci95": [x["lo"], x["hi"]]} for x in selected],
                     "n_trades": len(day_trades), "R": day_r, "equity_R": equity})
    equity_curve = []
    cum = 0.0
    for r in daily_r:
        cum = round(cum + r, 4)
        equity_curve.append(cum)
    best = best_days(daily_r, [d["date"] for d in days])
    block = {"window": window, "test_days": len(tests), "test_days_asked": test_days, "min_n": min_n,
             "rule": ("on each test day, the configs with n >= %d and a 95 %% lower bound of the expectancy "
                      "> 0 over the %d sessions before it; one trade per symbol, the config with the higher "
                      "lower bound when several fire" % (min_n, window)),
             "sessions_first": tests[0] if tests else None, "sessions_last": tests[-1] if tests else None,
             "selected_per_day": selected_per_day, "days": days, "daily_R": daily_r, "equity_R": equity_curve,
             "total_R": round(sum(daily_r), 4), "max_dd_R": round(dtstats.max_drawdown(daily_r), 3),
             "stats": dtstats.stats_block(trades), "trades": trades, "best_days": best}
    if not tests:
        block["note"] = "no test day: the rolling walk-forward needs more than %d sessions" % window
    return block


def best_days(daily_r, dates, share=BEST_DAYS_SHARE):
    """How much of the rolling total came from the best 20 % of days (the dispersion warning)."""
    n = len(daily_r)
    if n == 0:
        return {"share_of_days": share, "n_days": 0, "days": [], "share_of_total_R": None, "warning": None}
    k = max(1, int(math.ceil(share * n)))
    order = sorted(range(n), key=lambda i: -daily_r[i])[:k]
    top = sum(daily_r[i] for i in order)
    total = sum(daily_r)
    frac = round(top / total, 4) if total > 0 and top <= total + 1e-9 else None
    warn = None
    if frac is None:
        warn = ("the rolling total is not positive; there is no profit share to split" if total <= 0 else
                "the best days made more than the whole total: the other days lost money")
    elif frac > 0.5:
        warn = ("%d of %d test days (the best %d %%) made %d %% of the rolling total: a few days carry the "
                "record" % (k, n, int(round(share * 100)), int(round(frac * 100))))
    return {"share_of_days": share, "n_days": k, "days": sorted(dates[i] for i in order), "best_R": round(top, 4),
            "total_R": round(total, 4), "share_of_total_R": frac, "warning": warn}


# ---------------------------------------------------------------- the run

def default_workers():
    return max(1, (os.cpu_count() or 2) - 2)


def _progress(quiet, text):
    if not quiet:
        print(text, file=sys.stderr)
        sys.stderr.flush()


def run_study(root, out_dir, symbols=None, symbols_file=None, largecap="trader/largecap.json", sessions_n=None,
              fill="next_open", cost_bps=sim.DEFAULT_COST_BPS, workers=1, cfg=None, quiet=False,
              window=ROLLING_WINDOW, test_days=ROLLING_TEST_DAYS, min_n=ROLLING_MIN_N, config_path=None):
    """The whole study -> edge.json document (also written, with trades.jsonl.gz, under out_dir)."""
    t0 = time.time()
    cfg = cfg or {}
    syms, missing = pick_symbols(root, symbols, symbols_file)
    if not syms:
        raise LabError("no tradable symbol with a bars5m store under %s" % root)
    ctxd = load_context(root)
    meta = load_largecap(largecap)
    pack = {"root": root, "ctxd": ctxd, "cfg": cfg, "fill": fill, "cost_bps": cost_bps, "sessions_n": sessions_n,
            "meta": meta}
    os.makedirs(out_dir, exist_ok=True)
    trades_path = os.path.join(out_dir, "trades.jsonl.gz")
    tmp_path = trades_path + ".tmp"
    rows_by_config = {c["id"]: [] for c in setups.configs()}
    session_dates, studied, skipped = set(), [], []
    n_trades = 0
    workers = max(1, int(workers or 1))
    pool = None
    if workers > 1 and len(syms) > 1:
        pool = multiprocessing.Pool(min(workers, len(syms)), initializer=_init_worker, initargs=(pack,))
    _progress(quiet, "lab: %d symbols, %d configs, fill %s, %g bp costs, %d worker%s"
              % (len(syms), len(rows_by_config), fill, cost_bps, workers, "" if workers == 1 else "s"))
    try:
        if pool:
            results = pool.imap(_worker, syms, chunksize=4)
        else:
            _init_worker(pack)
            results = (_worker(s) for s in syms)
        with gzip.open(tmp_path, "wt", encoding="utf-8") as fh:
            for i, res in enumerate(results, 1):
                if res["skipped"]:
                    skipped.append({"symbol": res["symbol"], "why": res["skipped"]})
                else:
                    studied.append(res["symbol"])
                    session_dates.update(res["sessions"])
                for rec in res["trades"]:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    rows_by_config[rec["config_id"]].append(slim(rec))
                    n_trades += 1
                if i % 50 == 0 or i == len(syms):
                    _progress(quiet, "  %d/%d symbols, %s trades, %d s" % (i, len(syms), "{:,}".format(n_trades),
                                                                           int(time.time() - t0)))
        os.replace(tmp_path, trades_path)
        if not studied:
            raise LabError("no symbol had a full session in %s" % root)
        sessions = sorted(session_dates)
        is_dates, oos_dates = split_sessions(sessions)
        payloads = []
        for cid, rows in rows_by_config.items():
            payloads.append({"job": "core", "cid": cid, "rows": rows, "is": is_dates, "oos": oos_dates})
            payloads.append({"job": "slices", "cid": cid, "rows": rows})
            payloads.append({"job": "rolling", "cid": cid, "rows": rows, "sessions": sessions, "window": window,
                             "test_days": test_days, "min_n": min_n})
        _progress(quiet, "  stats: %d jobs over %d configs (bootstrap 2000 draws, seed %d)"
                  % (len(payloads), len(rows_by_config), dtstats.SEED))
        parts = pool.imap_unordered(_stats_task, payloads) if pool else (_stats_task(p) for p in payloads)
        per_config, per_rolling = {}, {}
        for j, part in enumerate(parts, 1):
            if part["job"] == "rolling":
                per_rolling[part["cid"]] = {"windows": part["windows"], "day_rows": part["day_rows"]}
            else:
                per_config.setdefault(part["cid"], {}).update(part)
            if j % 30 == 0 or j == len(payloads):
                _progress(quiet, "  stats %d/%d, %d s" % (j, len(payloads), int(time.time() - t0)))
    finally:
        if pool:
            pool.close()
            pool.join()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    configs = []
    for c in setups.configs():
        p = per_config[c["id"]]
        configs.append({"id": c["id"], "setup": c["setup"], "params": c["params"], "label": c["label"],
                        "all": p["all"], "is": p["is"], "oos": p["oos"], "status": dtstats.status_of(p["oos"]),
                        "by_regime": p["by_regime"], "by_hour": p["by_hour"], "by_tier": p["by_tier"],
                        "by_sector": p["by_sector"], "by_dow": p["by_dow"],
                        "hindsight": dict(p["hindsight"], note="SPY's day type is known only after the close: "
                                                              "a breakdown, never a live filter")})
    rolling = rolling_walk_forward(per_rolling, sessions, window, test_days, min_n)
    elapsed = round(time.time() - t0, 1)
    edge = {"created_by": "trader-lab", "as_of": dtio.now_iso(), "fill": fill, "cost_bps": cost_bps,
            "symbols": studied, "n_symbols": len(studied), "sessions": span(sessions),
            "configs_tested": len(setups.configs()),
            "split": {"is": span(is_dates), "oos": span(oos_dates)},
            "status_rule": STATUS_RULE, "configs": configs, "rolling": rolling, "n_trades": n_trades,
            "meta": {"cache_root": root, "out_dir": out_dir, "trades_file": trades_path, "workers": workers,
                     "elapsed_s": elapsed, "sessions_per_symbol": sessions_n, "symbols_file": symbols_file,
                     "largecap": largecap if meta else None, "config": config_path,
                     "entry_window": list(clock.entry_window(sessions[-1], cfg)),
                     "flat_by": cfg.get("flat_by") or "15:55", "skipped": skipped, "not_in_store": missing,
                     "bootstrap": {"draws": dtstats.DRAWS, "seed": dtstats.SEED}},
            "note": NOTE}
    dtio.write_json(os.path.join(out_dir, "edge.json"), edge)
    return edge


def trimmed(edge):
    """edge.json without the per-trade lists: what --publish copies to trader/publish/edge.json."""
    out = dict(edge)
    out["rolling"] = {k: v for k, v in edge["rolling"].items() if k != "trades"}
    return out


def publish(edge, path):
    dtio.write_json(path, trimmed(edge))
    return path


# ---------------------------------------------------------------- printing

def _pct(x):
    return "-" if x is None else "%.1f" % (x * 100)


def _ci_pct(ci):
    if not ci or ci[0] is None:
        return "[-, -]"
    return "[%.1f, %.1f]" % (ci[0] * 100, ci[1] * 100)


def _r(x, sign=True):
    if x is None:
        return "-"
    return ("%+.3f" if sign else "%.3f") % x


def _ci_r(ci):
    if not ci or ci[0] is None:
        return "[-, -]"
    return "[%+.3f, %+.3f]" % (ci[0], ci[1])


def _pf(x):
    if x is None:
        return "-"
    return ">99" if x >= 999 else "%.2f" % x


def _lo(block):
    ci = (block or {}).get("exp_ci95") or [None, None]
    return ci[0]


def sort_key(c):
    lo = _lo(c.get("oos"))
    return (0 if lo is not None else 1, -(lo or 0.0), c["id"])


def stats_row(name, st, width=28):
    if not st or not st.get("n"):
        return "%-*s %-9s %5s   %s" % (width, name, "", "0", "no trade")
    return "%-*s %5d  %5s %-15s %7s %-18s %5s %7s   %s / %s" % (
        width, name, st["n"], _pct(st["win_rate"]), _ci_pct(st["win_ci95"]), _r(st["exp_R"]), _ci_r(st["exp_ci95"]),
        _pf(st["pf"]), ("%.1f R" % st["max_dd_R"]) if st.get("max_dd_R") is not None else "-",
        _r(st["avg_win_R"]), _r(st["avg_loss_R"]))


def table_header(width=28, status=True):
    if status:
        return "%-*s %-9s %5s  %5s %-15s %7s %-18s %5s %7s   %s" % (
            width, "config", "status", "n", "win %", "[95 % CI]", "exp R", "[95 % CI]", "pf", "max DD",
            "avg win / avg loss")
    return "%-*s %5s  %5s %-15s %7s %-18s %5s %7s   %s" % (
        width, "slice", "n", "win %", "[95 % CI]", "exp R", "[95 % CI]", "pf", "max DD", "avg win / avg loss")


def config_row(c, block, width=28):
    st = c.get(block) or {}
    if not st.get("n"):
        return "%-*s %-9s %5s   no trade" % (width, c["id"], c["status"], "0")
    return "%-*s %-9s %5d  %5s %-15s %7s %-18s %5s %7s   %s / %s" % (
        width, c["id"], c["status"], st["n"], _pct(st["win_rate"]), _ci_pct(st["win_ci95"]), _r(st["exp_R"]),
        _ci_r(st["exp_ci95"]), _pf(st["pf"]), "%.1f R" % st["max_dd_R"], _r(st["avg_win_R"]), _r(st["avg_loss_R"]))


def tested_line(edge):
    return "configs tested: %d" % edge["configs_tested"]


def head_lines(edge):
    s, sp = edge["sessions"], edge["split"]
    return ["Lab: %d symbols, %d sessions (%s to %s), %s fill, %g bp costs round trip, run %s."
            % (len(edge["symbols"]), s["n"], s["first"], s["last"], edge["fill"], edge["cost_bps"], edge["as_of"]),
            "In sample %s to %s (%d sessions), out of sample %s to %s (%d sessions)."
            % (sp["is"]["first"], sp["is"]["last"], sp["is"]["n"], sp["oos"]["first"], sp["oos"]["last"],
               sp["oos"]["n"]),
            "Status: " + STATUS_RULE]


def config_table(edge, block, title, top=None):
    cs = sorted(edge["configs"], key=sort_key)
    if top:
        cs = cs[:top]
    lines = ["", tested_line(edge), title, table_header()]
    lines += [config_row(c, block) for c in cs]
    return lines


def rolling_lines(edge):
    r = edge["rolling"]
    st = r["stats"]
    lines = ["", tested_line(edge),
             "Rolling walk-forward: select on the %d sessions before each test day, trade the next day, roll "
             "(%d test days%s)." % (r["window"], r["test_days"],
                                   (", %s to %s" % (r["sessions_first"], r["sessions_last"]))
                                   if r["test_days"] else "")]
    if r.get("note"):
        lines.append("  " + r["note"] + ".")
        return lines
    n_sel = [len(x) for x in r["selected_per_day"]]
    lines.append("  selected configs a day: min %d, median %d, max %d; days with none: %d of %d"
                 % (min(n_sel) if n_sel else 0, int(sorted(n_sel)[len(n_sel) // 2]) if n_sel else 0,
                    max(n_sel) if n_sel else 0, sum(1 for x in n_sel if x == 0), len(n_sel)))
    if st["n"]:
        lines.append("  trades %d (%d symbols): win %s %% %s, exp R %s %s, pf %s, total %+.1f R, max DD %.1f R "
                     "(by day)" % (st["n"], st["n_symbols"], _pct(st["win_rate"]), _ci_pct(st["win_ci95"]),
                                   _r(st["exp_R"]), _ci_r(st["exp_ci95"]), _pf(st["pf"]), r["total_R"],
                                   r["max_dd_R"]))
    else:
        lines.append("  no config cleared the gate on any test day: no rolling trade")
    lines.append("  equity R by day: " + " ".join("%+.1f" % x for x in r["equity_R"]))
    b = r["best_days"]
    if b["share_of_total_R"] is not None:
        lines.append("  the best %d of %d days made %d %% of the rolling total (%+.1f of %+.1f R)"
                     % (b["n_days"], r["test_days"], int(round(b["share_of_total_R"] * 100)), b["best_R"],
                        b["total_R"]))
    if b.get("warning"):
        lines.append("  Warning: " + b["warning"] + ".")
    return lines


def slice_lines(c, family, top_label=None):
    key = {"regime": "by_regime", "hour": "by_hour", "tier": "by_tier", "sector": "by_sector", "dow": "by_dow",
           "day_type": "hindsight"}[family]
    block = c.get(key) or {}
    if family == "day_type":
        block = block.get("by_day_type") or {}
    lines = ["", "%s (%s)  by %s%s" % (c["id"], c["status"], family,
                                       " (hindsight: SPY's day type, never a live filter)" if family == "day_type"
                                       else "")]
    if not block:
        lines.append("  no slice with %d or more trades" % SLICE_MIN_N)
        return lines
    lines.append(table_header(status=False))
    order = list(block)
    if family == "hour":
        order = [h for h in HOURS if h in block]
    elif family == "dow":
        order = [d for d in DOW if d in block]
    for k in order:
        lines.append(stats_row(k, block[k]))
    return lines


def report_lines(edge, config_id=None, by=None, top=None):
    lines = head_lines(edge)
    if config_id:
        cs = [c for c in edge["configs"] if c["id"] == config_id]
        if not cs:
            raise LabError("no config %r in edge.json; the ids: %s" % (config_id, ", ".join(c["id"] for c in edge["configs"])))
        c = cs[0]
        lines += ["", tested_line(edge), "%s: %s" % (c["id"], c["label"]), table_header()]
        for block, name in (("oos", "out of sample"), ("is", "in sample"), ("all", "all sessions")):
            lines.append(config_row(dict(c, id=name), block))
        for fam in ([by] if by else ["regime", "hour", "tier", "sector", "dow", "day_type"]):
            lines += slice_lines(c, fam)
    else:
        lines += config_table(edge, "oos", "Out of sample (the gate), sorted by the OOS 95 %% lower bound; "
                                           "%d test sessions:" % edge["split"]["oos"]["n"], top)
        lines += config_table(edge, "all", "All sessions, same order:", top)
        if by:
            cs = sorted(edge["configs"], key=sort_key)[:(top or 5)]
            for c in cs:
                lines += slice_lines(c, by)
    lines += rolling_lines(edge)
    if edge["meta"].get("skipped"):
        lines.append("Skipped %d symbol(s) without a full session." % len(edge["meta"]["skipped"]))
    lines += ["", NOTE]
    return lines


# ---------------------------------------------------------------- why

def why_session(sym, date, root, fill, cost_bps, cfg, largecap="trader/largecap.json"):
    sym = sym.upper()
    if is_context(sym):
        raise LabError("%s is a context symbol (SPY, QQQ, VIX, sector ETFs); the lab never trades it" % sym)
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise LabError("DATE wants YYYY-MM-DD, got %r" % date)
    store = dtbars.load_store(sym, "bars5m", root)
    if not store:
        raise LabError("no bar store for %s under %s (run scripts/fetch_cache.py)" % (sym, root))
    rows = (store.get("sessions") or {}).get(date)
    if not rows:
        raise LabError("no session for %s on %s in the store (a weekend, a holiday, a dropped day, or not cached)"
                       % (sym, date))
    b = dtbars.bars_of(rows)
    if not dtbars.is_full(b, date):
        raise LabError("%s %s is not a full session in the store (%d bars)" % (sym, date, len(b)))
    ctxd = load_context(root)
    daily = dtbars.daily_rows(sym, root)
    spy_bars = spy_bars_of(date, ctxd)
    ctx = session_context(date, daily, spy_bars, ctxd["spy_daily"], cfg)
    tags = regime_tags(date, ctxd)
    day_type = dtregime.hindsight(spy_bars)["day_type"]
    meta = load_largecap(largecap).get(sym)
    until = clock.flat_slot(date, cfg)
    cfgs = setups.configs()
    trades = session_trades(sym, date, b, ctx, fill, until, cost_bps, cfgs, meta, tags, day_type)
    fired = set(t["config_id"] for t in trades)
    return {"symbol": sym, "date": date, "fill": fill, "cost_bps": cost_bps, "bars": len(b),
            "open": b[0]["o"], "high": max(x["h"] for x in b), "low": min(x["l"] for x in b), "close": b[-1]["c"],
            "prev_close": ctx["prev_close"],
            "gap_pct": round((b[0]["o"] / ctx["prev_close"] - 1) * 100, 2) if ctx["prev_close"] else None,
            "window": list(ctx["window"]), "flat_slot": until, "regime": tags, "hindsight": {"day_type": day_type},
            "tier": (meta or {}).get("tier") or "unknown", "sector": (meta or {}).get("sector") or "unknown",
            "configs_tested": len(cfgs), "trades": trades,
            "did_not_fire": [c["id"] for c in cfgs if c["id"] not in fired]}


def why_lines(doc):
    d = datetime.date.fromisoformat(doc["date"])
    gap = ("gap %+.2f %% vs the prior close %.2f" % (doc["gap_pct"], doc["prev_close"])) if doc["gap_pct"] is not None \
        else "no prior close in the daily store"
    lines = ["%s %s %s: open %.2f, high %.2f, low %.2f, close %.2f, %s, %d bars."
             % (doc["symbol"], DOW[d.weekday()], doc["date"], doc["open"], doc["high"], doc["low"], doc["close"], gap,
                doc["bars"]),
             "Regime before the open: %s. Hindsight: SPY %s (never a live filter). Tier %s, sector %s."
             % (", ".join(doc["regime"]) or "no tags (SPY daily rows missing)",
                doc["hindsight"]["day_type"] or "unknown", doc["tier"], doc["sector"]),
             "%d of %d configs matched a rule (%s fill, %g bp costs, entries from bar %d to bar %d, flat by %s):"
             % (len(doc["trades"]), doc["configs_tested"], doc["fill"], doc["cost_bps"], doc["window"][0],
                doc["window"][1], clock.clock12(clock.slot_close_minutes(doc["flat_slot"])))]
    if doc["trades"]:
        lines.append("%-26s %-9s %8s %8s %8s %4s %8s %-7s %-9s %6s %6s %6s"
                     % ("config", "bar", "entry", "stop", "target", "R", "fill", "outcome", "exit", "R net", "MAE",
                        "MFE"))
        for t in doc["trades"]:
            lines.append("%-26s %-9s %8.2f %8.2f %8.2f %4g %8.2f %-7s %-9s %+6.2f %+6.2f %+6.2f"
                         % (t["config_id"], t["entry_time"], t["entry"], t["stop"], t["target"], t["target_r"],
                            t["entry_fill"], t["outcome"], t["exit_time"], t["R_net"], t["mae_R"], t["mfe_R"]))
        lines.append("bar = the entry bar's close; exit = the close of the bar that hit the stop, the target, "
                     "or the flat-by bar.")
    if doc["did_not_fire"]:
        lines.append("Did not fire: " + ", ".join(doc["did_not_fire"]) + ".")
    lines.append("Each line matched a rule on past bars; none is a prediction. Most people who day trade lose money.")
    return lines


# ---------------------------------------------------------------- commands

def _cfg(path):
    cfg = dtio.read_config(path) if path else {}
    try:
        clock.entry_window("2026-10-19", cfg)
        clock.flat_slot("2026-10-19", cfg)
    except clock.ClockError as e:
        raise LabError("%s: %s" % (path, e))
    return cfg


def cmd_run(a):
    if a.cost_bps < 0:
        raise LabError("--cost-bps wants a number of basis points, 0 or more")
    if a.sessions is not None and a.sessions <= 0:
        raise LabError("--sessions wants a positive count")
    cfg = _cfg(a.config)
    edge = run_study(a.cache_root, a.out_dir, a.symbols, a.symbols_file, a.largecap, a.sessions, a.fill, a.cost_bps,
                     a.workers, cfg, a.quiet, a.window, a.test_days, a.min_n, a.config)
    published = publish(edge, a.publish_path) if a.publish else None
    if a.json:
        print(json.dumps(trimmed(edge), indent=2))
        return 0
    for line in report_lines(edge, top=a.top):
        print(line)
    print("Wrote %s (%s trades) and %s in %s s."
          % (edge["meta"]["trades_file"], "{:,}".format(edge["n_trades"]), os.path.join(a.out_dir, "edge.json"),
             edge["meta"]["elapsed_s"]))
    if published:
        print("Published %s (without the trade lists)." % published)
    return 0


def cmd_report(a):
    path = os.path.join(a.lab_dir, "edge.json")
    if not os.path.exists(path):
        raise LabError("no edge.json in %s yet (run lab.py run first)" % a.lab_dir)
    try:
        edge = dtio.read_json(path)
    except dtio.IOError_ as e:
        raise LabError(str(e))
    for k in ("configs", "rolling", "split", "sessions", "configs_tested"):
        if k not in (edge or {}):
            raise LabError("%s is missing %r: run lab.py run again" % (path, k))
    if a.json:
        if a.config:
            cs = [c for c in edge["configs"] if c["id"] == a.config]
            if not cs:
                raise LabError("no config %r in %s" % (a.config, path))
            print(json.dumps(cs[0], indent=2))
        else:
            print(json.dumps(trimmed(edge), indent=2))
        return 0
    for line in report_lines(edge, a.config, a.by, a.top):
        print(line)
    return 0


def cmd_why(a):
    cfg = _cfg(a.config)
    doc = why_session(a.symbol, a.date, a.cache_root, a.fill, a.cost_bps, cfg, a.largecap)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    for line in why_lines(doc):
        print(line)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The lab (trader-lab): every setup x every large cap x every cached "
                                             "session, after costs. No network.")
    sub = ap.add_subparsers(dest="cmd")

    def cache_flags(p):
        p.add_argument("--cache-root", default="trader/cache", help="the bar store (default trader/cache)")
        p.add_argument("--fill", default="next_open", choices=["next_open", "close"],
                       help="next_open (the realistic fill, default) or close (the plan price)")
        p.add_argument("--cost-bps", type=float, default=sim.DEFAULT_COST_BPS,
                       help="round-trip costs in basis points of the entry (default 4)")
        p.add_argument("--config", default="trader/config.json",
                       help="no_entries_before / no_entries_after / flat_by (default trader/config.json)")
        p.add_argument("--largecap", default="trader/largecap.json",
                       help="tier and sector per symbol (default trader/largecap.json; unknown when absent)")

    p = sub.add_parser("run", help="play every config over every symbol and session, write the edge study")
    cache_flags(p)
    p.add_argument("--symbols", help="only these, comma separated")
    p.add_argument("--symbols-file", help="a SYMBOL|Name list, e.g. trader/largecap.txt")
    p.add_argument("--sessions", type=int, help="the last N full sessions per symbol (default all)")
    p.add_argument("--out-dir", default="trader/lab", help="where trades.jsonl.gz and edge.json go")
    p.add_argument("--workers", type=int, default=default_workers(),
                   help="processes (default cpu count minus 2, at least 1)")
    p.add_argument("--window", type=int, default=ROLLING_WINDOW, help="rolling walk-forward window in sessions")
    p.add_argument("--test-days", type=int, default=ROLLING_TEST_DAYS, help="rolling walk-forward test days")
    p.add_argument("--min-n", type=int, default=ROLLING_MIN_N, help="trades a config needs in the window")
    p.add_argument("--top", type=int, help="print only the top N configs")
    p.add_argument("--publish", action="store_true", help="also copy the trimmed edge.json to trader/publish/")
    p.add_argument("--publish-path", default=os.path.join("trader", "publish", "edge.json"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="no progress on stderr")

    p = sub.add_parser("report", help="print the tables from trader/lab/edge.json")
    p.add_argument("--lab-dir", default="trader/lab")
    p.add_argument("--config", metavar="ID", help='one config in full, e.g. "orb15|r=1"')
    p.add_argument("--by", choices=["regime", "hour", "tier", "sector", "dow", "day_type"],
                   help="the slices for the top configs (or for --config)")
    p.add_argument("--top", type=int, help="how many configs (default all; 5 for --by)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("why", help="every config's signal and outcome on one session")
    p.add_argument("symbol")
    p.add_argument("date", help="YYYY-MM-DD")
    cache_flags(p)
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "report": cmd_report, "why": cmd_why}[a.cmd](a)
    except LabError as e:
        print("lab.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
