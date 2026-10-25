#!/usr/bin/env python3
"""The desk for trader-desk: the advisor that drives the whole chain. Standard library only.
Long side only. It never places, sizes or approves an order.

    python3 desk.py morning --write                       # before the open: universe, regime, earnings, daily scan, news
    python3 desk.py live --write                          # after 9:50 AM ET: the candidate board at the current bar
    python3 desk.py live --at 10:15 --top 8 --write       # cut the clock at 10:15 AM ET
    python3 desk.py replay --date 2026-10-19 --at 10:15   # the same board from the bar store, no network
    python3 desk.py pick TSLA --target 1R --write         # trader/entry.json + the upstream exit plan
    python3 desk.py grade --date 2026-10-19               # after the close: grade the logged calls

morning   Runs the sibling tools as subprocesses: trader-largecap (the universe, when the
          file is over 20 hours old), trader-regime daily, Nasdaq's earnings calendar for
          today and the next session (a large cap reporting today, any time, or tomorrow
          pre-market is excluded with the reason; a name reporting within five sessions is
          flagged earnings_week), the upstream trader-scanner daily scan of the large-cap
          list, and trader-news for up to 20 daily hits. Writes trader/desk.json (mode morning).
          A sibling that is missing is said so and skipped.

live      The day and the cutoff work like the upstream scanner's orb window: it refuses in one
          plain line before the first bar after the 15-minute opening range closes (9:50 AM),
          on a closed day, or after no_entries_after (15:30). Bars: Yahoo spark closes for
          every symbol, then one v8 chart per symbol for the --max-charts most traded
          (PARTIAL when it stops short); --offline reads the bar store instead. Every config
          the lab approved (plus watch, flagged, with --include-watch) runs on each chart at
          the cutoff; the exclusions are dropped; flags: late (the entry bar closed more than
          one bar before the cutoff: a late entry is a different trade), earnings_week,
          regime_against (the config's record in today's regime tag is negative with n >= 50),
          watch_only, moving (3 % or more from the prior close), untested (no edge.json).
          Score = the lab's OOS expectancy CI lower bound (the regime slice when n >= 50, else
          pooled). The upstream time machine runs on the top N fresh candidates (seed 19).
          Writes trader/desk.json (mode live) and logs every candidate shown to
          trader/scorecard/calls.jsonl, once.

replay    live with --offline and --now "<date> <HH:MM>": the board from the cache, no network.

pick      Takes one candidate from trader/desk.json, writes trader/entry.json in the
          trader-entry shape and runs the upstream exit.py plan --write, so trader/plan.json is
          the upstream's. Then prints the next commands. It never sizes, checks or places.

grade     trader-scorecard/scorecard.py grade --date D, then scorecard.py report. Without that
          sibling (or with --local) it grades the day's logged calls on the bar store itself.

Every file path defaults to the upstream layout under the current directory (trader/...,
docs/data/...). --cache-root (trader/cache) is the bar store. Times are New York; --now
"2026-10-19 10:15" pins the clock. Exit code 0 = ran, 1 = refused or an error (one plain line).

Candidates matched a rule with a measured record. None is a prediction. Most people who day
trade lose money.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE if os.path.isdir(os.path.join(HERE, "dt")) else ROOT)

from dt import bars, clock, io, net, setups, sim, stats  # noqa: E402

NOTE = ("Candidates matched a rule with a measured record. None is a prediction. "
        "Most people who day trade lose money.")
OR_MINUTES = 15
NEWS_TAGS = ("earnings", "halt", "legal", "regulatory", "risk", "deal")
MOVE_PCT = 3.0
REGIME_MIN_N = 50
LARGECAP_MAX_AGE_H = 20.0
EARNINGS_WEEK_DAYS = 5
NEWS_MAX_TICKERS = 20
TM_SEED = 19
TM_TIMEOUT = 600
CONTEXT = ("SPY", "QQQ", "IWM", "DIA", "^VIX", "^VIX3M", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU",
           "XLB", "XLRE", "XLC")
SIBLINGS = {
    "largecap": ("trader-largecap", "largecap.py"),
    "regime": ("trader-regime", "regime.py"),
    "scanner": ("trader-scanner", "scan.py"),
    "news": ("trader-news", "news.py"),
    "timemachine": ("trader-timemachine", "timemachine.py"),
    "exit": ("trader-exit", "exit.py"),
    "scorecard": ("trader-scorecard", "scorecard.py"),
    "budget": ("trader-budget", "size.py"),
    "risk": ("trader-risk", "check.py"),
    "broker": ("trader-broker", "ticket.py"),
}
STATUS_WORDS = {"approved": "approved (the OOS record cleared the gate; not a prediction)",
                "watch": "watch (OOS positive but the CI crosses zero)", "untested": "untested (the lab has not run)"}


class DeskError(Exception):
    pass


# ---------------------------------------------------------------- siblings and subprocesses

def sibling(key):
    """The path of a sibling skill's tool (../<folder>/<script> next to this skill), or None."""
    folder, script = SIBLINGS[key]
    for base in (ROOT, HERE):
        p = os.path.join(base, folder, script)
        if os.path.isfile(p):
            return p
    return None


def sibling_cmd(key):
    """How to print the sibling's command: the resolved path, or the skill-dir form."""
    p = sibling(key)
    if p:
        return p
    folder, script = SIBLINGS[key]
    return "${CLAUDE_SKILL_DIR}/../%s/%s" % (folder, script)


def run_tool(path, args, timeout=None):
    """Run a sibling tool with this interpreter. -> {code, out, err, cmd}; code -1 when it could not run."""
    cmd = [sys.executable, path] + [str(x) for x in args]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"code": -1, "out": "", "err": "%s timed out after %s s" % (os.path.basename(path), timeout), "cmd": cmd}
    except OSError as e:
        return {"code": -1, "out": "", "err": str(e), "cmd": cmd}
    return {"code": r.returncode, "out": r.stdout, "err": r.stderr, "cmd": cmd}


def last_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def parse_json_out(text):
    """The JSON document a sibling printed with --json (progress lines before it are skipped)."""
    t = text or ""
    for opener in ("{", "["):
        i = t.find(opener)
        if i >= 0:
            try:
                return json.loads(t[i:])
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------- inputs

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr, flush=True)


def read_config(path):
    return io.read_config(path) if path else {}


def store_symbols(cache_root):
    d = os.path.join(cache_root or io.CACHE, "bars5m")
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".json.gz") and not f.startswith("_"):
                s = f[:-len(".json.gz")]
                if s not in CONTEXT:
                    out.append(s)
    return out


def read_symbols(path, cache_root=None, offline=False):
    """The large-cap list (SYMBOL|Name lines, or a JSON list); offline, the bar store's symbols when the file is missing."""
    if path and os.path.exists(path):
        syms = io.symbols_from_file(path)
        if syms:
            return syms
        raise DeskError("%s holds no symbols" % path)
    if offline:
        syms = store_symbols(cache_root)
        if syms:
            return syms
    raise DeskError("no symbol list at %s: run largecap.py build --write (or scan.py universe) first, "
                    "or pass --symbols-file" % path)


def names_from_file(path):
    out = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "|" in ln:
                    s, n = ln.split("|", 1)
                    out[s.strip().upper()] = n.strip()
    return out


def read_largecap(path):
    doc = io.read_json(path, None) if path else None
    out = {}
    if isinstance(doc, dict):
        for s in doc.get("symbols") or []:
            if isinstance(s, dict) and s.get("symbol"):
                out[str(s["symbol"]).upper()] = s
    return out, (doc if isinstance(doc, dict) else None)


def read_edge(path):
    doc = io.read_json(path, None) if path else None
    if not isinstance(doc, dict) or not isinstance(doc.get("configs"), list):
        return None
    return doc


def read_regime(path):
    doc = io.read_json(path, None) if path else None
    return doc if isinstance(doc, dict) else None


def regime_tags(doc):
    tags = (doc or {}).get("tags")
    return [str(t) for t in tags] if isinstance(tags, list) else []


def select_configs(edge, include_watch):
    """The configs to run: the lab's approved ones (plus watch), or every config when there is no edge.json.
    -> (cfgs for dt.setups.run_all, {config_id: edge entry})."""
    library = {c["id"]: c for c in setups.configs()}
    if edge is None:
        return list(library.values()), {}
    chosen, info = [], {}
    for ec in edge["configs"]:
        if not isinstance(ec, dict):
            continue
        st = ec.get("status")
        if st != "approved" and not (include_watch and st == "watch"):
            continue
        cid = ec.get("id")
        lib = library.get(cid)
        if lib is None:
            if ec.get("setup") in setups.SETUPS and isinstance(ec.get("params"), dict):
                lib = {"id": cid, "setup": ec["setup"], "params": ec["params"], "label": ec.get("label") or cid}
            else:
                continue
        chosen.append(lib)
        info[cid] = ec
    return chosen, info


def next_trading_day(d):
    nd = d + datetime.timedelta(days=1)
    for _ in range(20):
        if clock.trading_day(nd)[0]:
            return nd
        nd += datetime.timedelta(days=1)
    return nd


def trading_days_after(d, n):
    out = []
    cur = d
    for _ in range(n):
        cur = next_trading_day(cur)
        out.append(cur)
    return out


def norm_symbol(s):
    return str(s or "").strip().upper().replace("/", ".")


# ---------------------------------------------------------------- exclusions

def read_exclusions(path, day, note):
    """Earnings exclusions, blocked names and the earnings-week list from the morning desk.json
    (or a live desk.json of the same day, or a hand-made file: {earnings: [...], blocked: [...]} or a list).
    -> {"earnings": [...], "blocked": [...], "earnings_week": [...], "daily_hits": [...], "news": {...}, "source": path}"""
    out = {"earnings": [], "blocked": [], "earnings_week": [], "daily_hits": [], "news": {}, "source": None}
    if not path or not os.path.exists(path):
        return out
    try:
        doc = io.read_json(path, None)
    except io.IOError_ as e:
        note("exclusions: %s" % e)
        return out
    if isinstance(doc, list):
        out["earnings"] = [{"symbol": norm_symbol(s), "when": None, "reason": "listed in %s" % path} for s in doc if s]
        out["source"] = path
        return out
    if not isinstance(doc, dict):
        return out
    if doc.get("mode") in ("morning", "live") and doc.get("date") and doc["date"] != day:
        note("%s is for %s, not %s: its exclusions are not used" % (path, doc["date"], day))
        return out
    ex = doc.get("exclusions") or doc.get("excluded") or doc
    if isinstance(ex, dict):
        for e in ex.get("earnings") or []:
            if isinstance(e, dict) and e.get("symbol"):
                out["earnings"].append({"symbol": norm_symbol(e["symbol"]), "when": e.get("when"),
                                        "reason": e.get("reason") or "reports soon"})
            elif isinstance(e, str):
                out["earnings"].append({"symbol": norm_symbol(e), "when": None, "reason": "listed in %s" % path})
        out["blocked"] = [norm_symbol(b) for b in (ex.get("blocked") or []) if b]
    for w in doc.get("earnings_week") or []:
        if isinstance(w, dict) and w.get("symbol"):
            out["earnings_week"].append(dict(w, symbol=norm_symbol(w["symbol"])))
    out["daily_hits"] = [h for h in (doc.get("daily_hits") or []) if isinstance(h, dict) and h.get("symbol")]
    out["news"] = doc.get("news") if isinstance(doc.get("news"), dict) else {}
    out["source"] = path
    return out


# ---------------------------------------------------------------- the window

def live_window(now_utc, at, explicit_day, cfg, or_minutes=OR_MINUTES):
    """-> (day, cutoff naive ET). Mirrors the upstream scanner's orb_window, plus no_entries_after moved up
    on an early close like the upstream entry timer."""
    now = clock.to_eastern(now_utc)
    day = explicit_day or now.date()
    ok, why = clock.trading_day(day)
    if not ok:
        raise DeskError("No session on %s (%s): the market is closed. Run live on a trading day after 9:50 AM, "
                        "or replay a past day with replay --date D --at HH:MM." % (clock.day_label(day), why))
    s = clock.session(day)
    close_dt = datetime.datetime.combine(day, s[1])
    range_end = clock.slot_time(day, or_minutes // clock.BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=clock.BAR_MIN)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise DeskError("--at %s is later than now (%s ET)." % (at.strftime("%H:%M"), clock.clock_label(now)))
        if cutoff > close_dt:
            raise DeskError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock.clock_label(close_dt),
                                                                       clock.day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise DeskError("The market closed at %s ET on %s. Replay the session with --at 10:15."
                            % (clock.clock_label(close_dt), clock.day_label(day)))
    if cutoff < first_close:
        raise DeskError("It is %s ET: the %d-minute opening range runs 9:30 to %s and the first bar after it closes "
                        "at %s. Run live again after %s." % (clock.clock_label(cutoff), or_minutes,
                                                             clock.clock_label(range_end), clock.clock_label(first_close),
                                                             clock.clock_label(first_close)))
    until = clock.parse_hhmm(cfg.get("no_entries_after") or "15:30", "no_entries_after")
    until_dt = datetime.datetime.combine(day, until)
    if until_dt >= close_dt:  # an early close moves the cutoff up with it
        until_dt = until_dt - (datetime.datetime.combine(day, clock.CLOSE) - close_dt)
    if cutoff > until_dt:
        raise DeskError("It is %s ET, after %s, your last time for new entries (no_entries_after): a signal found "
                        "now is expired. Run live tomorrow after %s, or replay today with --at 10:15."
                        % (clock.clock_label(cutoff), until_dt.strftime("%H:%M"), clock.clock_label(first_close)))
    return day, cutoff


def last_slot(day, cutoff):
    """The index of the last 5-minute bar complete at the cutoff."""
    open_dt = clock.slot_time(day, 0)
    return int((cutoff - open_dt).total_seconds() // (clock.BAR_MIN * 60)) - 1


# ---------------------------------------------------------------- bars

class Stores(object):
    """Lazy bar-store reads, one load per symbol and kind."""

    def __init__(self, root):
        self.root = root
        self._c = {}

    def get(self, sym, kind="bars5m"):
        key = (sym, kind)
        if key not in self._c:
            try:
                self._c[key] = bars.load_store(sym, kind, self.root)
            except io.IOError_:
                self._c[key] = None
        return self._c[key]

    def daily(self, sym):
        return ((self.get(sym, "daily") or {}).get("rows")) or []

    def session(self, sym, day, cutoff_ts):
        """The store's bars of `day` complete at the cutoff (start + 5 min <= cutoff), or None when the day is not there."""
        st = self.get(sym)
        rows = ((st or {}).get("sessions") or {}).get(day)
        if not rows:
            return None
        out = []
        for r in rows:
            if r[0] + clock.BAR_MIN * 60 <= cutoff_ts:
                out.append(r)
            else:
                break
        return bars.bars_of(out)


def chart_session(series, day, cutoff_utc):
    """A live Yahoo series -> the day's partial session of complete bars (a contiguous prefix), or []."""
    try:
        sess = bars.series_to_sessions(series, cutoff_utc, include_partial=True)
    except (KeyError, TypeError, ValueError):
        return []
    return sess.get(day) or []


def fetch_online(symbols, day, today, cutoff_utc, max_charts, pause, say):
    """Stage 1: spark closes for every symbol, ranked by last close x regularMarketVolume.
    Stage 2: a v8 chart for the top --max-charts. -> (charts {sym: series}, failed {sym: why}, skipped, ranked)"""
    rng = "1d" if day == today else "5d"
    n_req = (len(symbols) + net.SPARK_MAX - 1) // net.SPARK_MAX
    say("Stage 1: 5-minute closes for %s symbols (%d spark requests)..." % ("{:,}".format(len(symbols)), n_req))
    data, failed = net.spark(symbols, rng, "5m", pause=pause, progress=say)
    ranked = []
    for s in symbols:
        ser = data.get(s)
        if not ser:
            continue
        sess = chart_session(ser, day.isoformat(), cutoff_utc)
        if not sess:
            failed[s] = "no complete bars for %s" % day.isoformat()
            continue
        vol = (ser.get("meta") or {}).get("regularMarketVolume") or 0
        try:
            vol = float(vol)
        except (TypeError, ValueError):
            vol = 0.0
        ranked.append((s, sess[-1]["c"] * vol))
    ranked.sort(key=lambda x: (-x[1], x[0]))
    skipped = 0
    if max_charts and len(ranked) > max_charts:
        skipped = len(ranked) - max_charts
        ranked = ranked[:max_charts]
    say("Stage 2: %d v8 charts, most traded first..." % len(ranked))
    charts = {}
    for i, (s, _) in enumerate(ranked):
        try:
            charts[s] = net.chart(s, rng, "5m")
        except net.NetError as e:
            failed[s] = str(e)
        if (i + 1) % 50 == 0:
            say("  %d charts so far" % (i + 1))
        if pause and i + 1 < len(ranked):
            net.SLEEP(pause)
    return charts, failed, skipped, len(ranked)


def yahoo_doc_from_store(sym, store, day, cutoff_ts, prev_close=None):
    """A Yahoo-shaped v8 chart document built from the bar store: every stored session up to `day`,
    the day itself cut at the cutoff. The upstream time machine reads it with --offline FILE."""
    ts, o, h, l, c, v = [], [], [], [], [], []
    for d in sorted((store or {}).get("sessions") or {}):
        if d > day:
            continue
        for r in store["sessions"][d]:
            if d == day and r[0] + clock.BAR_MIN * 60 > cutoff_ts:
                break
            ts.append(int(r[0]))
            o.append(r[1])
            h.append(r[2])
            l.append(r[3])
            c.append(r[4])
            v.append(int(r[5] or 0))
    off = clock.eastern_offset(clock.utc_of(cutoff_ts))
    meta = {"currency": "USD", "symbol": net.to_yahoo(sym), "exchangeName": "NMS", "instrumentType": "EQUITY",
            "regularMarketTime": int(cutoff_ts), "gmtoffset": off * 3600, "timezone": "EDT" if off == -4 else "EST",
            "exchangeTimezoneName": "America/New_York", "dataGranularity": "5m", "range": "60d",
            "source": "trader-desk from the bar store (%s)" % ((store or {}).get("source") or "cache")}
    if prev_close:
        meta["chartPreviousClose"] = prev_close
    return {"chart": {"result": [{"meta": meta, "timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]}}],
                      "error": None}}


# ---------------------------------------------------------------- the lab's numbers

def stats_of(block):
    """A STATS block, or the oos / all one inside a nested block."""
    if not isinstance(block, dict):
        return None
    if "exp_ci95" in block or "n" in block:
        return block
    for k in ("oos", "all"):
        if isinstance(block.get(k), dict):
            return block[k]
    return None


def ci_low(block):
    ci = (block or {}).get("exp_ci95") if isinstance(block, dict) else None
    if isinstance(ci, (list, tuple)) and ci and ci[0] is not None:
        try:
            return float(ci[0])
        except (TypeError, ValueError):
            return None
    return None


def lab_block(ec, tags, fill_note):
    """The candidate's lab block and its score from one edge.json config entry."""
    oos = stats_of(ec.get("oos")) or stats_of(ec.get("all"))
    slices = []
    for t in tags:
        s = stats_of((ec.get("by_regime") or {}).get(t))
        if s and (s.get("n") or 0) >= REGIME_MIN_N:
            slices.append((t, s))
    against = sorted(t for t, s in slices if s.get("exp_R") is not None and s["exp_R"] < 0)
    chosen = None
    if slices:
        slices.sort(key=lambda ts: (-(ts[1].get("n") or 0), ts[0]))
        chosen = {"tag": slices[0][0], "stats": slices[0][1]}
    score = ci_low(chosen["stats"]) if chosen else None
    if score is None:
        score = ci_low(oos)
    return {"status": ec.get("status"), "oos": oos, "regime_slice": chosen, "fill_note": fill_note,
            "regime_against": against}, score


def pct(x, digits=1):
    return ("%%.%df %%%%" % digits) % (100.0 * x) if x is not None else "n/a"


def ci_text(ci, as_pct=False):
    if not isinstance(ci, (list, tuple)) or len(ci) < 2 or ci[0] is None or ci[1] is None:
        return "[n/a]"
    if as_pct:
        return "[%.1f, %.1f]" % (100.0 * ci[0], 100.0 * ci[1])
    return "[%+.3f, %+.3f]" % (ci[0], ci[1])


def stats_line(s):
    if not s or not s.get("n"):
        return "no trades"
    return "n %d, target first %s %s, exp %+.3fR %s after costs" % (
        s["n"], pct(s.get("win_rate")), ci_text(s.get("win_ci95"), True), s.get("exp_R") or 0.0, ci_text(s.get("exp_ci95")))


# ---------------------------------------------------------------- the time machine

def tm_args(cand, day, cutoff_txt, until=None, seed=TM_SEED, offline_file=None):
    sig = cand["signal"]
    args = ["run", "--symbol", cand["symbol"], "--date", day, "--entry", "%.2f" % sig["entry"],
            "--stop", "%.2f" % sig["stop"], "--target", "%.2f" % sig["target"], "--entry-time", sig["entry_time"],
            "--json", "--now", "%s %s" % (day, cutoff_txt), "--seed", str(seed)]
    orr = sig.get("opening_range")
    if isinstance(orr, dict) and orr.get("minutes"):
        args += ["--setup", "orb", "--or-minutes", str(int(orr["minutes"]))]
    else:
        args += ["--setup", str(cand.get("setup") or "level")]
    if until:
        args += ["--until", until]
    if offline_file:
        args += ["--offline", offline_file]
    return args


def tm_block(doc):
    mc, h = doc.get("mc") or {}, doc.get("history") or {}
    out = {k: mc.get(k) for k in ("p_target", "p_stop", "p_neither", "p_green", "avg_R", "median_close", "p5_close",
                                  "p95_close", "r_at_target", "replays", "seed", "window", "until", "pool_sessions",
                                  "pool_from", "pool_to", "bars_left")}
    out["history"] = {k: h.get(k) for k in ("sessions", "triggered", "target", "stop", "close", "green", "avg_R",
                                            "rule", "note")}
    out["verdict"] = doc.get("verdict")
    out["verdict_notes"] = list(doc.get("verdict_notes") or [])
    out["warnings"] = list(doc.get("warnings") or [])
    out["source"] = doc.get("source")
    return out


def run_timemachine(tm_path, cand, day, cutoff_txt, until=None, seed=TM_SEED, offline_file=None):
    """The upstream time machine as a subprocess -> the candidate's timemachine block, or {"error": one line}."""
    r = run_tool(tm_path, tm_args(cand, day, cutoff_txt, until, seed, offline_file), timeout=TM_TIMEOUT)
    if r["code"] != 0:
        why = last_line(r["err"]) or last_line(r["out"]) or "timemachine.py exited %d" % r["code"]
        return {"error": why}
    doc = parse_json_out(r["out"])
    if not isinstance(doc, dict) or "mc" not in doc:
        return {"error": "timemachine.py printed no result"}
    return tm_block(doc)


# ---------------------------------------------------------------- the board

def intraday_read(spy_bars, spy_pc, sessions):
    """The desk's own cheap intraday read from the bars already in hand (no extra request)."""
    out = {}
    if spy_bars:
        last = spy_bars[-1]["c"]
        if spy_pc:
            out["spy_gap_pct"] = round((spy_bars[0]["o"] / spy_pc - 1) * 100, 2)
            out["spy_vs_prev_pct"] = round((last / spy_pc - 1) * 100, 2)
        out["spy_move_pct"] = round((last / spy_bars[0]["o"] - 1) * 100, 2) if spy_bars[0]["o"] else None
        if len(spy_bars) >= OR_MINUTES // clock.BAR_MIN:
            oh, ol = bars.opening_range(spy_bars, OR_MINUTES // clock.BAR_MIN)
            out["spy_or"] = "above" if last > oh else "below" if last < ol else "inside"
        vw = bars.vwap_series(spy_bars)[-1]
        out["spy_vs_vwap_pct"] = round((last / vw - 1) * 100, 2) if vw else None
    above = up = n = 0
    for s in sessions.values():
        b = s.get("bars")
        if not b:
            continue
        n += 1
        if b[-1]["c"] > b[0]["o"]:
            above += 1
        pc = s.get("prev_close")
        if pc and b[-1]["c"] > pc:
            up += 1
    if n:
        out["breadth_above_open_pct"] = round(100.0 * above / n, 1)
        out["breadth_up_pct"] = round(100.0 * up / n, 1)
        out["breadth_n"] = n
    return out


def rank_candidates(cands):
    def key(c):
        late = 1 if "late" in c["flags"] else 0
        s = c.get("score")
        return (late, 0 if s is not None else 1, -(s or 0.0), c["symbol"], c["config_id"])
    cands.sort(key=key)
    for i, c in enumerate(cands, 1):
        c["rank"] = i


def build_board(a, now_utc, day, cutoff, cfg, symbols, sessions, spy, stores, edge, cfg_info, cfgs, regime_doc,
                excl, largecap, names, notes, counts):
    """sessions: {sym: {"bars": [...], "meta": {...}, "prev_close": x}} (only usable charts). -> (candidates, extra)"""
    day_s = day.isoformat()
    tags = regime_tags(regime_doc)
    lk = last_slot(day, cutoff)
    window = clock.entry_window(day, cfg)
    fill_note = None
    if edge:
        fill_note = "lab record: %s fill, %s bps round trip, OOS %s to %s" % (
            edge.get("fill") or "next_open", edge.get("cost_bps", 4),
            ((edge.get("split") or {}).get("oos") or {}).get("first"), ((edge.get("split") or {}).get("oos") or {}).get("last"))
    ex_sym = {}
    for e in excl["earnings"]:
        ex_sym[e["symbol"]] = e
    blocked = set(excl["blocked"]) | set(norm_symbol(b) for b in (cfg.get("blocked") or []) if b)
    week = {}
    for w in excl["earnings_week"]:
        week.setdefault(w["symbol"], w)
    daily_hits = {h["symbol"].upper(): h for h in excl["daily_hits"]}
    news = excl["news"] or {}
    dropped = {"earnings": [], "blocked": []}
    cands = []
    for sym in symbols:
        s = sessions.get(sym)
        if not s or not s.get("bars"):
            continue
        if sym in blocked:
            dropped["blocked"].append(sym)
            continue
        if sym in ex_sym and not a.allow_earnings:
            dropped["earnings"].append(dict(ex_sym[sym]))
            continue
        b = s["bars"]
        pc = s.get("prev_close")
        rows = stores.daily(sym)
        ctx = {"window": window, "prev_close": pc, "daily_atr": bars.daily_atr(rows, day_s) if rows else None,
               "avg_vol20": bars.avg_volume(rows, day_s) if rows else None, "spy": spy.get("bars"),
               "spy_prev_close": spy.get("prev_close")}
        try:
            sigs = setups.run_all(b, ctx, cfgs)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            notes.append("%s: the setups could not run (%s: %s)" % (sym, type(e).__name__, e))
            continue
        if not sigs:
            continue
        counts["matched_symbols"] += 1
        info = largecap.get(sym) or {}
        last = b[-1]["c"]
        move = round((last / pc - 1) * 100, 2) if pc else None
        for cid, sig in sigs.items():
            ec = cfg_info.get(cid)
            if ec:
                lab, score = lab_block(ec, tags, fill_note)
            else:
                lab, score = {"status": "untested", "oos": None, "regime_slice": None,
                              "fill_note": "no edge.json: the lab has not run, this config has no measured record",
                              "regime_against": []}, None
            flags = []
            if lk - sig["k"] > 1:
                flags.append("late")
            if sym in ex_sym:
                flags.append("earnings")
            if sym in week or sym in ex_sym:
                flags.append("earnings_week")
            if lab["regime_against"]:
                flags.append("regime_against")
            if ec and ec.get("status") == "watch":
                flags.append("watch_only")
            if not ec:
                flags.append("untested")
            if move is not None and abs(move) >= MOVE_PCT:
                flags.append("moving")
            if sym in daily_hits:
                flags.append("daily_hit")
            nw = news.get(sym) or {}
            if isinstance(nw, dict) and (nw.get("important") or any(nw.get("tags", {}).get(t) for t in NEWS_TAGS)):
                flags.append("news")
            cfg_entry = next((c for c in cfgs if c["id"] == cid), None)
            signal = {"entry": sig["entry"], "stop": sig["stop"], "target": sig["target"], "target_r": sig["target_r"],
                      "entry_time": sig["entry_time"], "k": sig["k"], "opening_range": sig.get("opening_range"),
                      "stop_rule": sig.get("stop_rule"), "risk_per_share": sig.get("risk_per_share"),
                      "level": sig.get("level"), "bars_since_entry": lk - sig["k"]}
            for extra in ("vol_x_actual", "gap_pct", "sigma_pct"):
                if extra in sig:
                    signal[extra] = sig[extra]
            cands.append({"rank": None, "symbol": sym, "name": info.get("name") or names.get(sym) or "",
                          "sector": info.get("sector"), "tier": info.get("tier"), "setup": sig["setup"],
                          "config_id": cid, "label": (cfg_entry or {}).get("label") or cid, "signal": signal,
                          "lab": {k: lab[k] for k in ("status", "oos", "regime_slice", "fill_note")},
                          "regime_against": lab["regime_against"], "timemachine": None, "flags": flags,
                          "score": score, "last_close": round(last, 2), "prev_close": round(pc, 2) if pc else None,
                          "move_pct": move, "earnings": ex_sym.get(sym) or week.get(sym),
                          "daily_hit": daily_hits.get(sym), "news": nw or None})
    rank_candidates(cands)
    return cands, dropped


def shown_slice(cands, top):
    fresh = [c for c in cands if "late" not in c["flags"]]
    late = [c for c in cands if "late" in c["flags"]]
    shown = fresh[:top] + late[:top]
    more = {"fresh": max(0, len(fresh) - top), "late": max(0, len(late) - top)}
    for i, c in enumerate(shown, 1):
        c["rank"] = i
    return shown, more, len(fresh), len(late)


def log_calls(path, cands, day, as_of, now_utc):
    """Append each shown candidate to the scorecard's calls.jsonl, once (existing ids are read first)."""
    existing = set()
    try:
        for row in io.read_jsonl(path):
            if isinstance(row, dict) and row.get("id"):
                existing.add(row["id"])
    except ValueError:
        pass
    n = 0
    for c in cands:
        cid = "%s|%s|%s|%s" % (day, c["symbol"], c["config_id"], c["signal"]["entry_time"])
        c["call_id"] = cid
        if cid in existing:
            continue
        tm = c.get("timemachine") or {}
        io.append_jsonl(path, {"id": cid, "logged_at": clock.iso(now_utc), "date": day, "as_of": as_of,
                               "symbol": c["symbol"], "setup": c["setup"], "config_id": c["config_id"], "rank": c["rank"],
                               "signal": c["signal"], "lab_oos_exp_R": (c["lab"].get("oos") or {}).get("exp_R"),
                               "tm_p_target": tm.get("p_target"), "late": "late" in c["flags"], "flags": c["flags"],
                               "score": c["score"], "created_by": "trader-desk"})
        existing.add(cid)
        n += 1
    return n


def regime_strip(regime_doc, desk_live=None):
    if not regime_doc:
        line = "Regime: no trader/regime.json (run regime.py daily --write); no regime gate applied."
    else:
        d = regime_doc.get("daily") or {}
        bits = []
        if d.get("ret5d_pct") is not None:
            bits.append("SPY 5d %+.2f %%" % d["ret5d_pct"])
        if d.get("vix") is not None:
            bits.append("VIX %.1f%s" % (d["vix"], (" vs VIX3M %.1f" % d["vix3m"]) if d.get("vix3m") is not None else ""))
        if d.get("prev_range_atr") is not None:
            bits.append("prior SPY range %.2fx its ATR" % d["prev_range_atr"])
        tags = ", ".join(regime_tags(regime_doc)) or "no tags"
        line = "Regime (%s): %s%s" % (regime_doc.get("date") or regime_doc.get("as_of") or "?", tags,
                                      ("; " + "; ".join(bits)) if bits else "")
        lv = regime_doc.get("live") or {}
        if lv:
            more = []
            if lv.get("spy_gap_pct") is not None:
                more.append("SPY gap %+.2f %%" % lv["spy_gap_pct"])
            if lv.get("spy_or"):
                more.append("SPY %s its opening range" % lv["spy_or"])
            if lv.get("breadth_above_open_pct") is not None:
                more.append("breadth above open %.0f %%" % lv["breadth_above_open_pct"])
            if more:
                line += "\n  regime.py live at %s: %s" % (lv.get("at") or "?", ", ".join(more))
    if desk_live:
        more = []
        if desk_live.get("spy_gap_pct") is not None:
            more.append("SPY gap %+.2f %%" % desk_live["spy_gap_pct"])
        if desk_live.get("spy_move_pct") is not None:
            more.append("SPY %+.2f %% since the open" % desk_live["spy_move_pct"])
        if desk_live.get("spy_or"):
            more.append("%s its 15-minute range" % desk_live["spy_or"])
        if desk_live.get("spy_vs_vwap_pct") is not None:
            more.append("%+.2f %% vs VWAP" % desk_live["spy_vs_vwap_pct"])
        if desk_live.get("breadth_above_open_pct") is not None:
            more.append("breadth: %.0f %% of %d charts above their open, %.0f %% up on the day"
                        % (desk_live["breadth_above_open_pct"], desk_live.get("breadth_n") or 0,
                           desk_live.get("breadth_up_pct") or 0))
        if more:
            line += "\n  at the cutoff: " + "; ".join(more)
    return line


def candidate_lines(c, until12="4:00 PM"):
    sig = c["signal"]
    who = c["symbol"] + (("  %s" % c["name"]) if c.get("name") else "")
    meta = ", ".join(x for x in (c.get("sector"), c.get("tier")) if x)
    out = ["%d. %s%s" % (c["rank"], who, ("  (%s)" % meta) if meta else "")]
    out.append("   %s  [%s]" % (c["label"], c["config_id"]))
    out.append("   entry %.2f at %s (bar %d), stop %.2f, target %.2f (%gR, risk %.2f a share); last close %.2f%s"
               % (sig["entry"], sig["entry_time"], sig["k"], sig["stop"], sig["target"], sig["target_r"],
                  sig.get("risk_per_share") or (sig["entry"] - sig["stop"]), c.get("last_close") or 0.0,
                  (", %+.2f %% vs the prior close" % c["move_pct"]) if c.get("move_pct") is not None else ""))
    lab = c["lab"]
    st = lab.get("status") or "untested"
    out.append("   lab OOS: %s; %s" % (STATUS_WORDS.get(st, st), stats_line(lab.get("oos"))))
    rs = lab.get("regime_slice")
    if rs:
        out.append("   regime slice %s: %s" % (rs["tag"], stats_line(rs["stats"])))
    if c.get("regime_against"):
        out.append("   REGIME AGAINST: this config lost money in %s with n >= %d" % (", ".join(c["regime_against"]), REGIME_MIN_N))
    if lab.get("fill_note"):
        out.append("   %s" % lab["fill_note"])
    tm = c.get("timemachine")
    if tm and tm.get("error"):
        out.append("   time machine: %s" % tm["error"])
    elif tm:
        out.append("   time machine (seed %s, %s replays): target first %s, stop first %s, neither %s, green %s, average R %+.3f"
                   % (tm.get("seed"), "{:,}".format(tm.get("replays") or 0), pct(tm.get("p_target")), pct(tm.get("p_stop")),
                      pct(tm.get("p_neither")), pct(tm.get("p_green")), tm.get("avg_R") or 0.0))
        h = tm.get("history") or {}
        if h.get("triggered"):
            out.append("     history: the same rule fired %d times in %d sessions: %d target, %d stop, %d neither, average R %+.3f"
                       % (h["triggered"], h.get("sessions") or 0, h.get("target") or 0, h.get("stop") or 0,
                          h.get("close") or 0, h.get("avg_R") or 0.0))
        elif h.get("note"):
            out.append("     history: %s" % h["note"])
        elif h.get("sessions"):
            out.append("     history: the same rule never fired in those %d sessions" % h["sessions"])
        if tm.get("verdict"):
            out.append("     " + tm["verdict"])
        for n in tm.get("verdict_notes") or []:
            out.append("     " + n)
        for w in tm.get("warnings") or []:
            out.append("     Note: %s." % w)
    flags = list(c["flags"])
    if flags:
        words = []
        for f in flags:
            if f == "late":
                words.append("late (the entry bar closed %d bars before the cutoff: a late entry is a different trade)"
                             % sig.get("bars_since_entry", 0))
            elif f == "earnings" and c.get("earnings"):
                words.append("earnings (%s)" % c["earnings"].get("reason"))
            elif f == "earnings_week" and c.get("earnings"):
                words.append("earnings_week (%s %s)" % (c["earnings"].get("date") or "", c["earnings"].get("when") or ""))
            elif f == "moving":
                words.append("moving (%+.2f %% vs the prior close)" % (c.get("move_pct") or 0.0))
            elif f == "daily_hit" and c.get("daily_hit"):
                words.append("daily_hit (%s)" % ", ".join(c["daily_hit"].get("setups") or []))
            else:
                words.append(f)
        out.append("   flags: " + "; ".join(words))
    return out


def print_board(doc, a):
    cands = doc["candidates"]
    print("Desk board, %s %s ET: %s symbols, %s charts read, %s matched a rule%s."
          % (clock.day_label(datetime.date.fromisoformat(doc["date"])), doc["cutoff"], "{:,}".format(doc["universe"]),
             "{:,}".format(doc["scanned"]), doc["counts"].get("matched_symbols", 0),
             (" (%d candidate%s)" % (doc["counts"]["candidates"], "" if doc["counts"]["candidates"] == 1 else "s"))
             if doc["counts"].get("candidates") else ""))
    if doc.get("partial"):
        print("PARTIAL: %s" % doc["partial"])
    e = doc.get("edge") or {}
    if e:
        print("Lab: %d config%s run (%d approved%s) from %s as of %s; %s configs tested in the lab, so some look good by luck."
              % (e["run"], "" if e["run"] == 1 else "s", e["approved"], (", %d watch" % e["watch"]) if e["watch"] else "",
                 e["path"], e.get("as_of") or "?", e.get("configs_tested") if e.get("configs_tested") is not None else "?"))
    else:
        print("NO EDGE FILE: the lab has not run (%s is missing). Every config ran and every candidate is UNTESTED: "
              "no measured record, no score. Run lab.py run first." % a.edge)
    print(regime_strip(doc.get("regime"), doc.get("regime_desk")))
    ex = doc.get("excluded") or {}
    if ex.get("earnings") or ex.get("blocked"):
        bits = []
        if ex.get("earnings"):
            bits.append("earnings: " + ", ".join("%s (%s)" % (x["symbol"], x.get("reason") or "") for x in ex["earnings"]))
        if ex.get("blocked"):
            bits.append("blocked: " + ", ".join(ex["blocked"]))
        print("Excluded: " + "; ".join(bits) + ".")
    fresh = [c for c in cands if "late" not in c["flags"]]
    late = [c for c in cands if "late" in c["flags"]]
    if not cands:
        print("No candidate matched a rule with a record by %s." % doc["cutoff12"])
    if fresh:
        print("Candidates (fresh: the entry bar closed at the cutoff or one bar before):")
        for c in fresh:
            for ln in candidate_lines(c):
                print(ln)
    if late:
        print("Late (the entry bar closed more than one bar before the cutoff; a late entry is a different trade: "
              "the price, the stop distance and the odds have moved):")
        for c in late:
            for ln in candidate_lines(c):
                print(ln)
    more = doc.get("more") or {}
    if more.get("fresh") or more.get("late"):
        print("... and %d more fresh, %d more late candidates not shown (--top N for more)."
              % (more.get("fresh", 0), more.get("late", 0)))
    for n in doc.get("notes") or []:
        print("Note: %s" % n)
    print(NOTE)
    if doc.get("wrote"):
        print("Wrote %s; logged %d new call%s to %s." % (doc["wrote"], doc["logged"], "" if doc["logged"] == 1 else "s",
                                                           doc["calls_path"]))
    else:
        print("Dry run: nothing written (add --write for trader/desk.json and the scorecard log).")
    print("Next: desk.py pick SYMBOL --write (entry.json and the upstream exit plan), then the time machine, "
          "size.py, check.py and ticket.py show. Nothing was placed.")


# ---------------------------------------------------------------- live / replay

def cmd_live(a, offline=False, explicit_date=None):
    t0 = time.time()
    now_utc = clock.parse_now(a.now)
    cfg = read_config(a.config)
    at = clock.parse_hhmm(a.at, "--at") if a.at else None
    day, cutoff = live_window(now_utc, at, explicit_date, cfg)
    day_s = day.isoformat()
    cutoff_utc = clock.eastern_to_utc(cutoff)
    cutoff_ts = clock.ts_of(cutoff_utc)
    cutoff_txt = cutoff.strftime("%H:%M")
    as_of = "%s %s ET" % (day_s, cutoff_txt)
    notes = []
    note = notes.append
    offline = offline or a.offline
    today = clock.to_eastern(clock.utc_now()).date()
    if not offline and (today - day).days > 6:
        raise DeskError("Yahoo's free 5-minute history is short: replay %s with replay --date %s --at %s"
                        % (day_s, day_s, cutoff_txt))
    symbols = read_symbols(a.symbols_file, a.cache_root, offline)
    symbols = [s for s in symbols if s not in CONTEXT]
    largecap, lc_doc = read_largecap(a.largecap)
    names = names_from_file(a.symbols_file)
    edge = read_edge(a.edge)
    if edge is None and a.edge and os.path.exists(a.edge):
        note("%s is not an edge.json (no configs list): treated as missing" % a.edge)
    cfgs, cfg_info = select_configs(edge, a.include_watch)
    regime_doc = read_regime(a.regime)
    if regime_doc and regime_doc.get("date") and regime_doc["date"] != day_s:
        note("%s is for %s, not %s: its tags still gate the board; run regime.py daily --write" % (a.regime, regime_doc["date"], day_s))
    excl_path = a.exclusions or a.out
    excl = read_exclusions(excl_path, day_s, note)
    stores = Stores(a.cache_root)
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "no_data": 0, "scanned": 0,
              "matched_symbols": 0, "candidates": 0}
    lk = last_slot(day, cutoff)
    want = lk + 1
    sessions = {}
    partial = None
    failed = {}

    def prev_close_of(sym, meta):
        rows = stores.daily(sym)
        pc = bars.prev_close(rows, day_s) if rows else None
        if pc is None and meta:
            pc = meta.get("chartPreviousClose") or meta.get("previousClose")
        return float(pc) if pc else None

    if offline:
        progress(a, "Reading the bar store at %s for %s symbols, %s cut at %s..." % (a.cache_root, len(symbols), day_s, cutoff_txt))
        for sym in symbols:
            b = stores.session(sym, day_s, cutoff_ts)
            if b is None:
                counts["no_data"] += 1
                failed[sym] = "no stored session for %s" % day_s
                continue
            counts["fetched"] += 1
            if len(b) != want:
                counts["unusable"] += 1
                failed[sym] = "the stored session has %d of the %d bars complete at %s" % (len(b), want, cutoff_txt)
                continue
            counts["scanned"] += 1
            sessions[sym] = {"bars": b, "meta": {}, "prev_close": prev_close_of(sym, None)}
        spy_bars = stores.session("SPY", day_s, cutoff_ts)
        if spy_bars is not None and len(spy_bars) != want:
            spy_bars = None
        spy = {"bars": spy_bars, "prev_close": prev_close_of("SPY", None)}
        if not spy_bars:
            note("no SPY bars for %s in the store: the relative-strength rules cannot fire" % day_s)
    else:
        charts, failed, skipped, ranked = fetch_online(symbols, day, today, cutoff_utc, a.max_charts, a.pause,
                                                       lambda m: progress(a, m))
        if skipped:
            partial = ("%s more symbols were not charted (only the %s most traded are; --max-charts 0 charts all)."
                       % ("{:,}".format(skipped), "{:,}".format(a.max_charts)))
        counts["fetched"] = len(charts)
        for sym in symbols:
            ser = charts.get(sym)
            if not ser:
                if sym not in failed:
                    counts["no_data"] += 1
                continue
            b = chart_session(ser, day_s, cutoff_utc)
            if len(b) != want:
                counts["unusable"] += 1
                failed[sym] = "the chart has %d of the %d bars complete at %s (a missing bar is a halt or thin data)" % (len(b), want, cutoff_txt)
                continue
            counts["scanned"] += 1
            sessions[sym] = {"bars": b, "meta": ser.get("meta") or {}, "prev_close": prev_close_of(sym, ser.get("meta"))}
        spy_bars = None
        spy_meta = {}
        try:
            spy_ser = net.chart("SPY", "1d" if day == today else "5d", "5m")
            spy_meta = spy_ser.get("meta") or {}
            spy_bars = chart_session(spy_ser, day_s, cutoff_utc)
            if len(spy_bars) != want:
                spy_bars = None
        except net.NetError as e:
            note("SPY chart: %s" % e)
        spy = {"bars": spy_bars, "prev_close": prev_close_of("SPY", spy_meta)}
        if not spy_bars:
            note("no complete SPY session at %s: the relative-strength rules cannot fire" % cutoff_txt)
    cands, dropped = build_board(a, now_utc, day, cutoff, cfg, symbols, sessions, spy, stores, edge, cfg_info, cfgs,
                                 regime_doc, excl, largecap, names, notes, counts)
    counts["candidates"] = len(cands)
    shown, more, n_fresh, n_late = shown_slice(cands, max(1, a.top))
    # the time machine on the fresh candidates kept
    tm_path = sibling("timemachine")
    if not a.no_timemachine:
        if not tm_path:
            note("trader-timemachine/timemachine.py is not next to this skill: no replays on this board")
        else:
            tmpdir = tempfile.mkdtemp(prefix="desk-tm-") if offline else None
            cache = {}
            try:
                fresh_kept = [c for c in shown if "late" not in c["flags"]]
                progress(a, "Time machine: %d candidate%s (seed %d)..." % (len(fresh_kept), "" if len(fresh_kept) == 1 else "s", a.seed))
                for c in fresh_kept:
                    sig = c["signal"]
                    key = (c["symbol"], sig["entry"], sig["stop"], sig["target"], sig["entry_time"], bool(sig.get("opening_range")))
                    if key in cache:
                        c["timemachine"] = dict(cache[key])
                        continue
                    off_file = None
                    if offline:
                        off_file = os.path.join(tmpdir, "%s-60d5m.json" % c["symbol"].replace("/", "."))
                        if not os.path.exists(off_file):
                            doc = yahoo_doc_from_store(c["symbol"], stores.get(c["symbol"]), day_s, cutoff_ts,
                                                       sessions.get(c["symbol"], {}).get("prev_close"))
                            with open(off_file, "w", encoding="utf-8") as fh:
                                json.dump(doc, fh, separators=(",", ":"))
                    c["timemachine"] = run_timemachine(tm_path, c, day_s, cutoff_txt, a.tm_until, a.seed, off_file)
                    cache[key] = c["timemachine"]
                    if not offline and a.pause:
                        net.SLEEP(a.pause)
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
    desk_live = intraday_read(spy.get("bars"), spy.get("prev_close"), sessions)
    doc = {"created_by": "trader-desk", "mode": "live", "as_of": as_of, "created_at": clock.iso(now_utc),
           "date": day_s, "cutoff": cutoff_txt, "cutoff12": clock.clock_label(cutoff), "last_bar": lk,
           "source": "cache" if offline else "yahoo", "cache_root": a.cache_root if offline else None,
           "regime": regime_doc, "regime_tags": regime_tags(regime_doc), "regime_desk": desk_live,
           "universe": len(symbols), "scanned": counts["scanned"], "counts": counts, "partial": partial,
           "failed": len(failed), "failed_symbols": failed if len(failed) <= 50 else dict(list(failed.items())[:50]),
           "excluded": {"earnings": dropped["earnings"], "blocked": dropped["blocked"]},
           "exclusions_from": excl.get("source"), "earnings_week": excl["earnings_week"],
           "daily_hits": excl["daily_hits"], "news": excl["news"],
           "edge": ({"path": a.edge, "as_of": edge.get("as_of"), "configs_tested": edge.get("configs_tested"),
                     "fill": edge.get("fill"), "cost_bps": edge.get("cost_bps"), "run": len(cfgs),
                     "approved": sum(1 for e in cfg_info.values() if e.get("status") == "approved"),
                     "watch": sum(1 for e in cfg_info.values() if e.get("status") == "watch")} if edge else None),
           "configs_run": [c["id"] for c in cfgs], "top": a.top, "fresh": n_fresh, "late": n_late, "more": more,
           "candidates": shown, "timemachine": {"seed": a.seed, "until": a.tm_until, "ran": not a.no_timemachine and bool(tm_path)},
           "notes": notes, "seconds": round(time.time() - t0, 1), "note": NOTE}
    doc["wrote"] = None
    doc["logged"] = 0
    doc["calls_path"] = os.path.join(a.scorecard_dir, "calls.jsonl")
    if a.write:
        io.write_json(a.out, doc)
        doc["wrote"] = a.out
        doc["logged"] = log_calls(doc["calls_path"], shown, day_s, as_of, now_utc)
        io.write_json(a.out, doc)  # with the call ids
        if a.publish:
            io.write_json(os.path.join(a.docs_data, "desk.json"), doc)
    elif a.publish:
        note("--publish needs --write")
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print_board(doc, a)
    return 0 if shown else 1


def cmd_replay(a):
    try:
        day = datetime.date.fromisoformat(a.date)
    except (TypeError, ValueError):
        raise DeskError("--date wants YYYY-MM-DD, got %r" % a.date)
    if not a.at:
        raise DeskError("replay wants --at HH:MM (New York), the clock to cut the day at")
    clock.parse_hhmm(a.at, "--at")
    a.now = "%s %s" % (day.isoformat(), a.at)
    a.offline = True
    return cmd_live(a, offline=True, explicit_date=day)


# ---------------------------------------------------------------- morning

def earnings_when(row):
    t = str(row.get("time") or "").lower()
    if "pre" in t:
        return "pre-market"
    if "after" in t:
        return "after-close"
    return "unknown"


def earnings_rows(doc):
    """Nasdaq's calendar answer, or a plain list of rows, -> rows."""
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if isinstance(doc, dict):
        data = doc.get("data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            return [r for r in data["rows"] if isinstance(r, dict)]
        if isinstance(doc.get("rows"), list):
            return [r for r in doc["rows"] if isinstance(r, dict)]
    return []


def largecap_fresh(path, now_utc):
    doc = io.read_json(path, None) if path else None
    if not isinstance(doc, dict) or not doc.get("as_of"):
        return False, doc
    try:
        t = datetime.datetime.strptime(str(doc["as_of"])[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False, doc
    return (now_utc - t).total_seconds() < LARGECAP_MAX_AGE_H * 3600, doc


def summarize_news(doc, tickers):
    """news.py --json -> per ticker: item counts, the tags that matter, MOVING."""
    out = {t: {"items": 0, "important": 0, "tags": {}, "moving": False, "move_pct": None, "titles": []} for t in tickers}
    if not isinstance(doc, dict):
        return out
    for it in doc.get("items") or []:
        if not isinstance(it, dict):
            continue
        touches = [str(t).upper() for t in (it.get("touches") or [])] or [str(it.get("ticker") or "").upper()]
        for t in touches:
            if t not in out:
                continue
            o = out[t]
            o["items"] += 1
            if it.get("important"):
                o["important"] += 1
            for tag in it.get("tags") or []:
                if tag in NEWS_TAGS:
                    o["tags"][tag] = o["tags"].get(tag, 0) + 1
            if len(o["titles"]) < 3 and it.get("title"):
                o["titles"].append(str(it["title"])[:120])
    for m in doc.get("moves") or []:
        if not isinstance(m, dict):
            continue
        t = str(m.get("ticker") or "").upper()
        if t in out:
            out[t]["move_pct"] = m.get("pct")
            out[t]["moving"] = bool(m.get("moving")) or (m.get("pct") is not None and abs(m["pct"]) >= MOVE_PCT)
    return out


def cmd_morning(a):
    t0 = time.time()
    now_utc = clock.parse_now(a.now)
    local = clock.to_eastern(now_utc)
    today = local.date()
    cfg = read_config(a.config)
    notes = []
    note = notes.append
    steps = {}
    offline = a.offline
    if offline and not os.path.isdir(offline):
        raise DeskError("--offline wants a folder holding earnings-today.json, earnings-next.json, watchlist.json, "
                        "news.json and regime.json; %s is not one" % offline)
    session_day = today if clock.trading_day(today)[0] else next_trading_day(today)
    if session_day != today:
        note("%s is not a trading day (%s): planning for %s" % (today.isoformat(), clock.trading_day(today)[1], session_day.isoformat()))
    next_day = next_trading_day(session_day)
    day_s = session_day.isoformat()

    def off_doc(name):
        p = os.path.join(offline, name)
        if not os.path.exists(p):
            note("offline: no %s" % p)
            return None
        return io.read_json(p, None)

    # 1. the universe
    fresh, lc_doc = largecap_fresh(a.largecap, now_utc)
    if offline or a.no_universe:
        steps["largecap"] = {"ran": False, "note": "skipped (%s)" % ("offline" if offline else "--no-universe")}
    elif fresh:
        steps["largecap"] = {"ran": False, "note": "%s is under %.0f h old (as of %s)" % (a.largecap, LARGECAP_MAX_AGE_H, lc_doc.get("as_of"))}
    else:
        p = sibling("largecap")
        if not p:
            steps["largecap"] = {"ran": False, "note": "trader-largecap/largecap.py is not next to this skill"}
            note("the large-cap universe was not rebuilt: trader-largecap is missing; using %s as it is" % a.symbols_file)
        else:
            progress(a, "Building the large-cap universe (largecap.py build --write)...")
            r = run_tool(p, ["build", "--write"], timeout=600)
            steps["largecap"] = {"ran": True, "code": r["code"], "note": last_line(r["err"] if r["code"] else r["out"])}
            if r["code"]:
                note("largecap.py build failed: %s" % (last_line(r["err"]) or "exit %d" % r["code"]))
    try:
        symbols = [s for s in read_symbols(a.symbols_file, a.cache_root, False) if s not in CONTEXT]
    except DeskError as e:
        if offline:
            symbols = store_symbols(a.cache_root)
            note(str(e))
        else:
            raise
    largecap, _ = read_largecap(a.largecap)
    names = names_from_file(a.symbols_file)
    symset = set(symbols)
    # 2. the regime
    regime_doc = None
    if offline:
        regime_doc = off_doc("regime.json")
        steps["regime"] = {"ran": False, "note": "offline: read regime.json from %s" % offline}
    else:
        p = sibling("regime")
        if not p:
            steps["regime"] = {"ran": False, "note": "trader-regime/regime.py is not next to this skill"}
            note("no regime read: trader-regime is missing")
        else:
            args = ["daily", "--write", "--cache-root", a.cache_root]
            if a.now:
                args += ["--date", day_s]
            progress(a, "Regime (regime.py daily --write)...")
            r = run_tool(p, args, timeout=300)
            steps["regime"] = {"ran": True, "code": r["code"], "note": last_line(r["err"] if r["code"] else r["out"])}
            if r["code"]:
                note("regime.py daily failed: %s" % (last_line(r["err"]) or "exit %d" % r["code"]))
        regime_doc = read_regime(a.regime)
    # 3. earnings
    calendar = {}
    if offline:
        calendar[day_s] = earnings_rows(off_doc("earnings-today.json"))
        calendar[next_day.isoformat()] = earnings_rows(off_doc("earnings-next.json"))
        week = io.read_json(os.path.join(offline, "earnings-week.json"), None)
        if isinstance(week, dict):
            for d, rows in week.items():
                calendar.setdefault(d, earnings_rows(rows))
        steps["earnings"] = {"ran": False, "note": "offline: read earnings-*.json from %s" % offline}
    else:
        days = [session_day] + trading_days_after(session_day, EARNINGS_WEEK_DAYS)
        got, bad = 0, []
        progress(a, "Earnings calendar for %s and the next %d sessions (Nasdaq)..." % (day_s, EARNINGS_WEEK_DAYS))
        for i, d in enumerate(days):
            try:
                calendar[d.isoformat()] = net.nasdaq_earnings(d.isoformat())
                got += 1
            except net.NetError as e:
                bad.append("%s: %s" % (d.isoformat(), e))
            if i + 1 < len(days):
                net.SLEEP(net.PAUSE)
        steps["earnings"] = {"ran": True, "code": 0 if got else 1, "note": "%d of %d days read" % (got, len(days))}
        for b in bad:
            note("earnings calendar %s" % b)
        if not got:
            note("NO EARNINGS CALENDAR: nothing is excluded for earnings today; check the names by hand")
    earnings_ex, week_rows = [], []
    for d in sorted(calendar):
        for row in calendar[d]:
            sym = norm_symbol(row.get("symbol"))
            if not sym or sym not in symset:
                continue
            when = earnings_when(row)
            entry = {"symbol": sym, "date": d, "when": when, "name": row.get("name") or largecap.get(sym, {}).get("name") or names.get(sym)}
            if d == day_s:
                entry["reason"] = "reports today (%s)" % {"pre-market": "pre-market", "after-close": "after the close"}.get(when, "time not given")
                earnings_ex.append(entry)
            elif d == next_day.isoformat() and when == "pre-market":
                entry["reason"] = "reports tomorrow pre-market (%s)" % d
                earnings_ex.append(entry)
            else:
                entry["reason"] = "reports %s (%s)" % (d, when)
                week_rows.append(entry)
    blocked = [norm_symbol(b) for b in (cfg.get("blocked") or []) if b]
    # 4. the daily scan
    hits, scan_doc = [], None
    if a.no_scan:
        steps["scan"] = {"ran": False, "note": "--no-scan"}
    elif offline:
        scan_doc = off_doc("watchlist.json")
        steps["scan"] = {"ran": False, "note": "offline: read watchlist.json from %s" % offline}
    else:
        p = sibling("scanner")
        if not p:
            steps["scan"] = {"ran": False, "note": "trader-scanner/scan.py is not next to this skill"}
            note("no daily scan: trader-scanner is missing")
        elif not os.path.exists(a.symbols_file):
            steps["scan"] = {"ran": False, "note": "no %s to scan" % a.symbols_file}
        else:
            args = ["daily", "--watchlist", a.symbols_file, "--min-volume", "0", "--json"]
            if a.now:
                args += ["--now", a.now]
            progress(a, "Daily scan of %d large caps (scan.py daily, writes trader/watchlist.json)..." % len(symbols))
            r = run_tool(p, args, timeout=1800)
            scan_doc = parse_json_out(r["out"])
            if scan_doc is None:
                scan_doc = io.read_json("trader/watchlist.json", None)
            steps["scan"] = {"ran": True, "code": r["code"], "note": last_line(r["err"]) if r["code"] not in (0, 1) else ""}
            if r["code"] not in (0, 1) or scan_doc is None:
                note("scan.py daily: %s" % (last_line(r["err"]) or "no watchlist came back"))
    if isinstance(scan_doc, dict):
        for h in scan_doc.get("hits") or []:
            if isinstance(h, dict) and h.get("symbol"):
                hits.append({"symbol": str(h["symbol"]).upper(), "setups": h.get("setups") or [], "price": h.get("price"),
                             "rsi": h.get("rsi"), "volume": h.get("volume"), "close_date": h.get("close_date")})
    # 5. the news
    tickers = [h["symbol"] for h in hits][:NEWS_MAX_TICKERS]
    news = {}
    if a.no_news:
        steps["news"] = {"ran": False, "note": "--no-news"}
    elif offline:
        news = summarize_news(off_doc("news.json"), tickers or [str(t).upper() for t in (off_doc("news.json") or {}).get("tickers", [])])
        steps["news"] = {"ran": False, "note": "offline: read news.json from %s" % offline}
    elif not tickers:
        steps["news"] = {"ran": False, "note": "no daily hits to check"}
    else:
        p = sibling("news")
        if not p:
            steps["news"] = {"ran": False, "note": "trader-news/news.py is not next to this skill"}
            note("no news check: trader-news is missing")
        else:
            args = ["--tickers"] + tickers + ["--hours", str(a.hours), "--json", "--dry-run", "--config", a.config]
            if a.now:
                args += ["--now", clock.iso(now_utc)]
            progress(a, "News for %d daily hits (news.py, last %s h)..." % (len(tickers), a.hours))
            r = run_tool(p, args, timeout=900)
            news = summarize_news(parse_json_out(r["out"]), tickers)
            steps["news"] = {"ran": True, "code": r["code"], "note": last_line(r["err"]) if r["code"] not in (0, 1) else ""}
            if r["code"] not in (0, 1):
                note("news.py: %s" % (last_line(r["err"]) or "exit %d" % r["code"]))
    doc = {"created_by": "trader-desk", "mode": "morning", "as_of": "%s %s ET" % (local.date().isoformat(), local.strftime("%H:%M")),
           "created_at": clock.iso(now_utc), "date": day_s, "next_session": next_day.isoformat(),
           "regime": regime_doc, "regime_tags": regime_tags(regime_doc), "universe": len(symbols),
           "universe_file": a.symbols_file, "exclusions": {"earnings": earnings_ex, "blocked": blocked},
           "earnings_week": week_rows, "earnings_days": sorted(calendar), "daily_hits": hits,
           "news": news, "news_hours": a.hours, "steps": steps, "notes": notes, "seconds": round(time.time() - t0, 1),
           "note": NOTE}
    if a.write:
        io.write_json(a.out, doc)
        if a.publish:
            io.write_json(os.path.join(a.docs_data, "desk.json"), doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("Morning desk, %s %s ET, planning %s (next session %s)." % (clock.day_label(local.date()), local.strftime("%H:%M"),
                                                                        clock.day_label(session_day), next_day.isoformat()))
    print("Universe: %s large caps from %s%s." % ("{:,}".format(len(symbols)), a.symbols_file,
                                                  (" (built %s)" % lc_doc.get("as_of")) if isinstance(lc_doc, dict) and lc_doc.get("as_of") else ""))
    print(regime_strip(regime_doc))
    if earnings_ex:
        print("Earnings exclusions (%d): %s." % (len(earnings_ex), "; ".join("%s %s" % (e["symbol"], e["reason"]) for e in earnings_ex)))
    else:
        print("Earnings exclusions: none of the large caps reports today or tomorrow pre-market%s."
              % ("" if calendar else " (no calendar was read)"))
    if week_rows:
        print("Reporting within %d sessions (flag earnings_week): %s." % (EARNINGS_WEEK_DAYS, ", ".join("%s %s" % (w["symbol"], w["date"]) for w in week_rows)))
    print("Blocked (config): %s." % (", ".join(blocked) if blocked else "none"))
    if hits:
        print("Daily scan: %d chart%s matched a daily setup (the scanner wrote trader/watchlist.json):" % (len(hits), "" if len(hits) == 1 else "s"))
        for h in hits[:a.show]:
            print("  %-6s %10s  %s" % (h["symbol"], ("$%.2f" % h["price"]) if h.get("price") is not None else "", ", ".join(h["setups"])))
        if len(hits) > a.show:
            print("  ... and %d more (--show N)." % (len(hits) - a.show))
    else:
        print("Daily scan: no hits%s." % ("" if steps.get("scan", {}).get("ran") or offline else " (not run)"))
    if news:
        flagged = [(t, n) for t, n in news.items() if n["items"] or n["moving"]]
        if flagged:
            print("News (last %s h):" % a.hours)
            for t, n in flagged:
                tags = ", ".join("%s x%d" % (k, v) for k, v in sorted(n["tags"].items()))
                print("  %-6s %d item%s%s%s%s" % (t, n["items"], "" if n["items"] == 1 else "s",
                                                  (", %d important" % n["important"]) if n["important"] else "",
                                                  ("; tags: " + tags) if tags else "",
                                                  ("; MOVING %+.2f %%" % n["move_pct"]) if n["moving"] and n.get("move_pct") is not None else (" MOVING" if n["moving"] else "")))
        else:
            print("News (last %s h): nothing new on the daily hits." % a.hours)
    for k, s in steps.items():
        if s.get("note") and (not s.get("ran") or s.get("code")):
            print("Step %s: %s" % (k, s["note"]))
    for n in notes:
        print("Note: %s" % n)
    if a.write:
        print("Wrote %s (mode morning)." % a.out)
    else:
        print("Dry run: nothing written (add --write).")
    print("Next: after 9:50 AM ET, desk.py live --write. " + NOTE)
    return 0


# ---------------------------------------------------------------- pick

def cmd_pick(a):
    now_utc = clock.parse_now(a.now)
    doc = io.read_json(a.desk, None)
    if not isinstance(doc, dict) or doc.get("mode") != "live":
        raise DeskError("no live board at %s: run desk.py live --write (or replay) first" % a.desk)
    sym = norm_symbol(a.symbol)
    cands = [c for c in doc.get("candidates") or [] if isinstance(c, dict) and c.get("symbol") == sym]
    if a.config:
        cands = [c for c in cands if c.get("config_id") == a.config]
    if not cands:
        raise DeskError("%s is not on the board in %s%s: the candidates are %s" % (
            sym, a.desk, (" for config %s" % a.config) if a.config else "",
            ", ".join(sorted(set(c.get("symbol", "?") for c in doc.get("candidates") or []))) or "none"))
    cands.sort(key=lambda c: c.get("rank") or 0)
    c = cands[0]
    sig = c["signal"]
    orr = sig.get("opening_range")
    orb_family = isinstance(orr, dict) and orr.get("high") is not None and orr.get("low") is not None
    day = doc["date"]
    bar_start = clock.slot_time(day, int(sig["k"])).strftime("%H:%M")
    level = round(float(orr["high"]), 2) if orb_family else (sig.get("level") if sig.get("level") is not None else sig["entry"])
    entry = {"created_by": "trader-desk", "symbol": sym, "setup": "orb" if orb_family else "level", "side": "buy",
             "date": day, "status": "triggered", "time": sig["entry_time"], "bar_start": bar_start,
             "bar_index": int(sig["k"]), "price": float(sig["entry"]), "level": level, "interval": "5m",
             "opening_range": {"high": float(orr["high"]), "low": float(orr["low"]), "minutes": int(orr.get("minutes") or 15)} if orb_family else None,
             "source": doc.get("source") or "desk", "checked_at": clock.iso(now_utc),
             "reason": "the %s bar closed at %.2f and matched %s (%s)" % (bar_start, sig["entry"], c["config_id"], c.get("label") or ""),
             "config_id": c["config_id"], "desk_rank": c.get("rank"), "desk_as_of": doc.get("as_of"),
             "flags": list(c.get("flags") or [])}
    stop = a.stop
    if not stop:
        if orb_family and sig.get("stop_rule") in ("or_mid", "or_low"):
            stop = sig["stop_rule"].replace("_", "-")
        else:
            stop = "price:%.2f" % sig["stop"]
    target = a.target or ("%gR" % float(sig["target_r"]))
    print("Pick: %s, rank %s on the %s board, %s [%s]." % (sym, c.get("rank"), doc.get("as_of"), c.get("label"), c["config_id"]))
    print("  entry %.2f at %s (bar %d), the desk's stop %.2f (%s), target %.2f (%gR)."
          % (sig["entry"], sig["entry_time"], sig["k"], sig["stop"], sig.get("stop_rule") or "", sig["target"], sig["target_r"]))
    if "late" in (c.get("flags") or []):
        print("  WARNING: this candidate is late (the entry bar closed %s bars before the board's cutoff). A late entry is "
              "a different trade: the price, the stop distance and the odds have moved." % sig.get("bars_since_entry", "?"))
    for f in c.get("flags") or []:
        if f in ("earnings", "earnings_week", "regime_against", "watch_only", "untested", "moving"):
            print("  flag: %s" % f)
    exit_path = sibling("exit")
    if a.write:
        entry_path = a.entry_out
        io.write_json(entry_path, entry)
        print("Wrote %s (created_by trader-desk, status triggered, setup %s)." % (entry_path, entry["setup"]))
    else:
        tmpd = tempfile.mkdtemp(prefix="desk-pick-")
        entry_path = os.path.join(tmpd, "entry.json")
        io.write_json(entry_path, entry)
    try:
        if not exit_path:
            raise DeskError("trader-exit/exit.py is not next to this skill: the plan was not made (entry.json %s)" %
                            ("written" if a.write else "not written"))
        args = ["plan", "--from", entry_path, "--target", target, "--stop", stop, "--config", a.config_path]
        if a.write:
            args += ["--write", a.plan_out]
        if a.now:
            args += ["--now", a.now]
        print("Running the upstream exit planner: exit.py %s" % " ".join(args[:7]))
        r = run_tool(exit_path, args, timeout=120)
        sys.stdout.write(r["out"])
        if r["code"] != 0:
            raise DeskError("exit.py refused the plan: %s" % (last_line(r["err"]) or "exit %d" % r["code"]))
    finally:
        if not a.write:
            shutil.rmtree(os.path.dirname(entry_path), ignore_errors=True)
    flat = read_config(a.config_path).get("flat_by") or "15:55"
    if not a.write:
        print("Dry run: nothing written (add --write for trader/entry.json and trader/plan.json).")
    print("Next, in order (each one is the upstream tool, each one needs your yes; nothing was placed):")
    print("  1. python3 %s run --write --until %s" % (sibling_cmd("timemachine"), flat))
    print("  2. python3 %s --write" % sibling_cmd("budget"))
    print("  3. python3 %s" % sibling_cmd("risk"))
    print("  4. python3 %s show" % sibling_cmd("broker"))
    print("The desk never sizes, checks or places. " + NOTE)
    return 0


# ---------------------------------------------------------------- grade

def grade_local(a, day):
    """Without trader-scorecard: play the day's logged calls on the bar store, append outcomes.jsonl, print the record."""
    calls_path = os.path.join(a.scorecard_dir, "calls.jsonl")
    out_path = os.path.join(a.scorecard_dir, "outcomes.jsonl")
    calls = [c for c in io.read_jsonl(calls_path) if isinstance(c, dict) and c.get("date") == day]
    if not calls:
        raise DeskError("no calls logged for %s in %s" % (day, calls_path))
    done = set()
    for o in io.read_jsonl(out_path):
        if isinstance(o, dict):
            done.add((o.get("id"), o.get("fill")))
    stores = Stores(a.cache_root)
    cfg = read_config(a.config)
    until = clock.flat_slot(day, cfg)
    now_utc = clock.parse_now(a.now)
    graded, missing, rows = 0, [], []
    for c in calls:
        sig = c.get("signal") or {}
        st = stores.get(c["symbol"])
        full = ((st or {}).get("sessions") or {}).get(day)
        if not full:
            missing.append(c["symbol"])
            continue
        b = bars.bars_of(full)
        for fill in ("next_open", "close"):
            if (c["id"], fill) in done:
                continue
            res = sim.play(b, {"k": sig["k"], "entry": sig["entry"], "stop": sig["stop"], "target": sig["target"]}, fill, until, a.cost_bps)
            if not res:
                continue
            row = {"id": c["id"], "graded_at": clock.iso(now_utc), "fill": fill, "outcome": res["outcome"], "R_net": res["R_net"],
                   "R_gross": res["R_gross"], "exit_slot": res["exit_slot"], "exit_price": res["exit_price"],
                   "entry_fill": res["entry_fill"], "late": bool(c.get("late")), "symbol": c["symbol"], "date": day,
                   "config_id": c.get("config_id"), "created_by": "trader-desk"}
            io.append_jsonl(out_path, row)
            done.add((c["id"], fill))
            rows.append(row)
            graded += 1
    print("Graded %d outcome%s for %s on the bar store (%s), %d call%s%s." % (
        graded, "" if graded == 1 else "s", day, a.cache_root, len(calls), "" if len(calls) == 1 else "s",
        ("; no stored session for %s (run scripts/fetch_cache.py after the close)" % ", ".join(sorted(set(missing)))) if missing else ""))
    allrows = [o for o in io.read_jsonl(out_path) if isinstance(o, dict) and o.get("date") == day]
    for fill in ("next_open", "close"):
        rs = [o for o in allrows if o.get("fill") == fill and not o.get("late")]
        if not rs:
            continue
        s = stats.stats_block(rs)
        print("  %s fill, late calls excluded: %s" % (fill, stats_line(s)))
    late_n = sum(1 for o in allrows if o.get("late") and o.get("fill") == "next_open")
    if late_n:
        print("  %d late call%s graded but kept out of the headline numbers (not knowable in time)." % (late_n, "" if late_n == 1 else "s"))
    print("Wrote %s. Every number is from the bars; a real fill can be worse. %s" % (out_path, NOTE))
    return 0


def cmd_grade(a):
    now_utc = clock.parse_now(a.now)
    day = a.date or clock.to_eastern(now_utc).date().isoformat()
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        raise DeskError("--date wants YYYY-MM-DD, got %r" % a.date)
    p = sibling("scorecard")
    if a.local or not p:
        if not p:
            print("trader-scorecard/scorecard.py is not next to this skill: grading locally on the bar store.")
        return grade_local(a, day)
    r = run_tool(p, ["grade", "--date", day], timeout=900)
    sys.stdout.write(r["out"])
    if r["err"].strip():
        sys.stderr.write(r["err"])
    if r["code"] not in (0, 1):
        raise DeskError("scorecard.py grade failed: %s" % (last_line(r["err"]) or "exit %d" % r["code"]))
    r2 = run_tool(p, ["report"], timeout=300)
    sys.stdout.write(r2["out"])
    if r2["err"].strip():
        sys.stderr.write(r2["err"])
    if r2["code"] not in (0, 1):
        raise DeskError("scorecard.py report failed: %s" % (last_line(r2["err"]) or "exit %d" % r2["code"]))
    print(NOTE)
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="The desk (trader-desk): morning plan, live candidate board, pick, grade. "
                                             "It never places, sizes or approves an order.")
    sub = ap.add_subparsers(dest="cmd")

    def paths(p):
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--cache-root", default="trader/cache", help="the bar store (default trader/cache)")
        p.add_argument("--symbols-file", default="trader/largecap.txt", help="SYMBOL|Name lines (default trader/largecap.txt)")
        p.add_argument("--largecap", default="trader/largecap.json", help="names, sectors and tiers")
        p.add_argument("--regime", default="trader/regime.json")
        p.add_argument("--out", default="trader/desk.json")
        p.add_argument("--docs-data", default=os.path.join("docs", "data"))
        p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:15"')
        p.add_argument("--write", action="store_true", help="write trader/desk.json (and the scorecard log)")
        p.add_argument("--publish", action="store_true", help="also copy desk.json to docs/data/")
        p.add_argument("--json", action="store_true")
        p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("morning", help="before the open: universe, regime, earnings exclusions, daily scan, news")
    paths(p)
    p.add_argument("--no-universe", action="store_true", help="do not rebuild trader/largecap.*")
    p.add_argument("--no-scan", action="store_true")
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--hours", type=float, default=18, help="news window in hours (18)")
    p.add_argument("--show", type=int, default=20)
    p.add_argument("--offline", metavar="DIR", help="read earnings-today.json, earnings-next.json, watchlist.json, news.json, regime.json from DIR")

    def live_flags(p):
        paths(p)
        p.add_argument("--at", help="cut the clock at this New York time, HH:MM")
        p.add_argument("--edge", default=os.path.join("trader", "lab", "edge.json"), help="the lab's edge.json")
        p.add_argument("--exclusions", help="the morning desk.json (default: --out when it is today's), or {earnings, blocked}")
        p.add_argument("--max-charts", type=int, default=300, help="v8 charts to fetch, most traded first (300; 0 = all)")
        p.add_argument("--top", type=int, default=8, help="candidates kept for the time machine and the board (8)")
        p.add_argument("--allow-earnings", action="store_true", help="keep names reporting today or tomorrow pre-market, flagged")
        p.add_argument("--include-watch", action="store_true", help="also run the lab's watch configs, flagged watch_only")
        p.add_argument("--no-timemachine", action="store_true")
        p.add_argument("--tm-until", help="end each replay at HH:MM (the time machine's --until; default 16:00)")
        p.add_argument("--seed", type=int, default=TM_SEED)
        p.add_argument("--scorecard-dir", default=os.path.join("trader", "scorecard"))
        p.add_argument("--pause", type=float, default=net.PAUSE, help="seconds between requests (%.1f)" % net.PAUSE)

    p = sub.add_parser("live", help="after 9:50 AM ET: the candidate board at the current bar")
    live_flags(p)
    p.add_argument("--offline", action="store_true", help="bars from the store, no network")
    p = sub.add_parser("replay", help="live on a past day from the bar store (no network)")
    live_flags(p)
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.set_defaults(offline=True)

    p = sub.add_parser("pick", help="one candidate -> trader/entry.json and the upstream exit plan")
    p.add_argument("symbol")
    p.add_argument("--desk", default="trader/desk.json")
    p.add_argument("--config", dest="config", help="the config id on the board (default: the symbol's best-ranked)")
    p.add_argument("--target", help="2R, 1R, 1.5R or price:X (default: the signal's R)")
    p.add_argument("--stop", help="or-mid, or-low, price:X, pct:X, atr:K (default: the signal's rule)")
    p.add_argument("--entry-out", default="trader/entry.json")
    p.add_argument("--plan-out", default="trader/plan.json")
    p.add_argument("--config-path", default="trader/config.json", help="trader/config.json for exit.py")
    p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:15"')
    p.add_argument("--write", action="store_true", help="write trader/entry.json and trader/plan.json")

    p = sub.add_parser("grade", help="after the close: scorecard.py grade --date D, then scorecard.py report")
    p.add_argument("--date", help="YYYY-MM-DD (default today, New York)")
    p.add_argument("--now")
    p.add_argument("--local", action="store_true", help="grade on the bar store here instead of trader-scorecard")
    p.add_argument("--scorecard-dir", default=os.path.join("trader", "scorecard"))
    p.add_argument("--cache-root", default="trader/cache")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--cost-bps", type=float, default=sim.DEFAULT_COST_BPS)

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "morning":
            return cmd_morning(a)
        if a.cmd == "live":
            return cmd_live(a)
        if a.cmd == "replay":
            return cmd_replay(a)
        if a.cmd == "pick":
            return cmd_pick(a)
        return cmd_grade(a)
    except (DeskError, clock.ClockError, io.IOError_, net.NetError) as e:
        print("desk.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("desk.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
