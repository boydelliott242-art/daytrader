#!/usr/bin/env python3
"""The scorecard for trader-scorecard: an append-only track record of every
candidate the desk showed, graded on the real bars. Standard library only.

    python3 scorecard.py log --from-desk                  # trader/desk.json -> trader/scorecard/calls.jsonl (never a duplicate)
    python3 scorecard.py grade                            # play every ungraded call on the real bars -> outcomes.jsonl
    python3 scorecard.py grade --date 2026-10-19 --now "2026-10-19 16:30"
    python3 scorecard.py report                           # the live record: calls, graded, win rate with CI, expectancy, by setup, by week
    python3 scorecard.py report --publish                 # also write docs/data/scorecard.json for the dashboard
    python3 scorecard.py show "2026-10-19|TSLA|orb15|r=1|10:15 AM"

Files (CONTRACTS.md, "The scorecard"):
  trader/scorecard/calls.jsonl     one line per candidate shown: id, logged_at, date, as_of, symbol, setup,
                                   config_id, rank, signal, lab_oos_exp_R, tm_p_target
  trader/scorecard/outcomes.jsonl  one line per graded call: id, graded_at, fill, outcome, R_net, exit_slot,
                                   exit_price, late, and both fills under "fills"
  id = "<date>|<symbol>|<config_id>|<entry_time>": a call is logged once, whoever appends it first.

Grading plays the logged signal forward on the session's real 5-minute bars
(dt.sim.play): stop checked before target inside a bar, a bar that touches both
counts as the stop, held until the flat-by slot (15:55, 12:55 on an early
close). Two fills: "next_open" (the bar after the trigger, the realistic fill,
the headline) and "close" (the trigger bar's close, the plan price). Costs:
--cost-bps round trip (default 4) off every trade, in R. A gap through the
stop counts as the stop price here; a real fill can be worse. Bars come from
the bar store (trader/cache/bars5m), else a saved chart under --offline DIR,
else one Yahoo request a symbol (5 days of 5-minute bars).

Late: a call logged more than one bar (5 minutes) after its entry bar closed
was not knowable in time. It is graded and kept, marked late, and left out of
the headline numbers.

Honest numbers only: the win rate with a Wilson 95 % interval, the expectancy
with a bootstrap interval (2,000 draws, seed 19), the profit factor, the gap
between what the lab expected out of sample and what the record shows. Under
30 graded calls the report says so. Nothing here is a prediction, and most
people who day trade lose money.
Exit codes: 0 = done, 1 = refused or an error (show: no such id).
"""

import argparse
import datetime
import gzip
import json
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE if os.path.isdir(os.path.join(HERE, "dt")) else os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dt import bars, clock, net, setups, sim, stats, universe  # noqa: E402
from dt import io as dtio  # noqa: E402

DEFAULT_DIR = "trader/scorecard"
DEFAULT_DESK = "trader/desk.json"
DEFAULT_CACHE = "trader/cache"
DEFAULT_PUBLISH = "docs/data/scorecard.json"
DEFAULT_R_USD = 50.0
LATE_GRACE_MIN = 5          # one 5-minute bar
SMALL_SAMPLE = 30
FILLS = ("next_open", "close")
NOTE = ("A record of the calls the desk showed, graded on real 5-minute bars with simulated fills. It is not a "
        "prediction of the next call, and a good week does not change the base rate: most people who day trade "
        "lose money.")


class ScorecardError(Exception):
    pass


# ---------------------------------------------------------------- files

def paths(d):
    return os.path.join(d, "calls.jsonl"), os.path.join(d, "outcomes.jsonl")


def read_lines(path):
    try:
        return dtio.read_jsonl(path)
    except ValueError as e:
        raise ScorecardError("%s is not valid JSON lines: %s" % (path, e))


def read_json(path, what):
    if not path or not os.path.exists(path):
        raise ScorecardError("no %s at %s" % (what, path))
    try:
        doc = dtio.read_json(path)
    except dtio.IOError_ as e:
        raise ScorecardError(str(e))
    if not isinstance(doc, dict):
        raise ScorecardError("%s is not a %s" % (path, what))
    return doc


def parse_iso(text, what):
    try:
        return datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        raise ScorecardError("%s wants a UTC time like 2026-10-19T14:17:00Z, got %r" % (what, text))


def parse_date(text, what):
    try:
        return datetime.date.fromisoformat(str(text)).isoformat()
    except (TypeError, ValueError):
        raise ScorecardError("%s wants a date like 2026-10-19, got %r" % (what, text))


# ---------------------------------------------------------------- the signal

def k_from_entry_time(text):
    """'10:15 AM' (the entry bar's CLOSE) -> slot 8. Refuses a time off the 5-minute grid."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?\s*$", str(text or ""))
    if not m:
        raise ScorecardError("entry_time wants a clock like 10:15 AM, got %r" % text)
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if not 1 <= h <= 12 or mi > 59:
        raise ScorecardError("entry_time %r is not a clock time" % text)
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    mins = h * 60 + mi
    if (mins - clock.OPEN_MIN) % clock.BAR_MIN or mins <= clock.OPEN_MIN:
        raise ScorecardError("entry_time %s is not a bar close on the 5-minute grid" % text)
    return (mins - clock.OPEN_MIN) // clock.BAR_MIN - 1


def _price(sig, key):
    try:
        x = float(sig.get(key))
    except (TypeError, ValueError):
        raise ScorecardError("the signal has no %s" % key)
    if not x > 0:
        raise ScorecardError("the signal's %s must be a positive price, got %r" % (key, sig.get(key)))
    return x


def normalize_signal(sig):
    """A desk candidate's signal block -> the signal dt.sim.play replays. Raises ScorecardError when it cannot."""
    if not isinstance(sig, dict):
        raise ScorecardError("the candidate has no signal block")
    entry, stop, target = (_price(sig, k) for k in ("entry", "stop", "target"))
    if not stop < entry < target:
        raise ScorecardError("a long needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    k = sig.get("k")
    if k is None:
        if not sig.get("entry_time"):
            raise ScorecardError("the signal needs k (the entry bar) or entry_time (its close)")
        k = k_from_entry_time(sig["entry_time"])
    try:
        k = int(k)
    except (TypeError, ValueError):
        raise ScorecardError("the signal's k must be a bar number, got %r" % sig.get("k"))
    if not 0 <= k < clock.SESSION_SLOTS - 1:
        raise ScorecardError("the signal's entry bar %d is off the session" % k)
    entry_time = clock.clock12(clock.slot_close_minutes(k))
    given = sig.get("entry_time")
    if given and k_from_entry_time(given) != k:
        raise ScorecardError("the signal disagrees with itself: k %d closes at %s, not %s" % (k, entry_time, given))
    out = dict(sig)
    out.update({"k": k, "entry": round(entry, 4), "stop": round(stop, 4), "target": round(target, 4),
                "entry_time": entry_time, "risk_per_share": round(entry - stop, 4)})
    if out.get("target_r") is None:
        out["target_r"] = round((target - entry) / (entry - stop), 4)
    return out


# ---------------------------------------------------------------- log

def call_id(date, symbol, config_id, entry_time):
    return "%s|%s|%s|%s" % (date, symbol, config_id, entry_time)


def desk_date(desk):
    d = desk.get("date") or (desk.get("regime") or {}).get("date") or str(desk.get("as_of") or "")[:10]
    try:
        return datetime.date.fromisoformat(str(d)).isoformat()
    except ValueError:
        raise ScorecardError("the desk board has no usable date (as_of %r)" % desk.get("as_of"))


def candidate_call(desk, cand, now_utc):
    """One desk candidate -> a calls.jsonl line."""
    if not isinstance(cand, dict):
        raise ScorecardError("a candidate is not an object")
    symbol = universe.norm_symbol(cand.get("symbol"))
    if not symbol:
        raise ScorecardError("a candidate has no symbol")
    sig = normalize_signal(cand.get("signal"))
    setup = cand.get("setup") or sig.get("setup")
    config_id = cand.get("config_id")
    if not config_id:
        if setup and isinstance(sig.get("params"), dict):
            config_id = setups.config_id(setup, sig["params"])
        else:
            config_id = setup or "manual"
    date = cand.get("date") or desk_date(desk)
    date = parse_date(date, "the candidate's date")
    lab = cand.get("lab") if isinstance(cand.get("lab"), dict) else {}
    oos = lab.get("oos") if isinstance(lab.get("oos"), dict) else {}
    tm = cand.get("timemachine") if isinstance(cand.get("timemachine"), dict) else {}
    return {"id": call_id(date, symbol, config_id, sig["entry_time"]), "logged_at": clock.iso(now_utc), "date": date,
            "as_of": desk.get("as_of"), "symbol": symbol, "setup": setup, "config_id": config_id,
            "rank": cand.get("rank"), "signal": sig, "lab_oos_exp_R": oos.get("exp_R"), "tm_p_target": tm.get("p_target"),
            "lab_status": lab.get("status"), "name": cand.get("name"), "tier": cand.get("tier"),
            "sector": cand.get("sector"), "flags": list(cand.get("flags") or []), "mode": desk.get("mode"),
            "created_by": "trader-scorecard"}


def lateness(call):
    """(late, minutes): logged_at against the entry bar's close plus one bar, in UTC."""
    logged = parse_iso(call.get("logged_at"), "logged_at")
    k = int(call["signal"]["k"])
    close_utc = clock.eastern_to_utc(clock.slot_time(call["date"], k + 1))
    minutes = (logged - close_utc).total_seconds() / 60.0
    return minutes > LATE_GRACE_MIN, round(minutes, 1)


def cmd_log(a):
    if not a.from_desk:
        raise ScorecardError("log wants --from-desk: the desk board is the only source of calls")
    now = clock.parse_now(a.now)
    desk = read_json(a.desk, "desk board (run desk.py live first)")
    cands = desk.get("candidates")
    if not isinstance(cands, list):
        cands = []
    calls_path, _ = paths(a.dir)
    existing = set(c.get("id") for c in read_lines(calls_path) if isinstance(c, dict) and c.get("id"))
    before = len(existing)
    added, already, bad = [], [], []
    for i, cand in enumerate(cands):
        try:
            call = candidate_call(desk, cand, now)
        except ScorecardError as e:
            bad.append((cand.get("symbol") if isinstance(cand, dict) else "#%d" % (i + 1), str(e)))
            continue
        if call["id"] in existing:
            already.append(call)
            continue
        dtio.append_jsonl(calls_path, call)
        existing.add(call["id"])
        added.append(call)
    if a.json:
        print(json.dumps({"desk": a.desk, "as_of": desk.get("as_of"), "mode": desk.get("mode"), "candidates": len(cands),
                          "logged": [c["id"] for c in added], "already": [c["id"] for c in already],
                          "skipped": [{"symbol": s, "why": w} for s, w in bad], "calls_file": calls_path,
                          "calls_before": before, "calls_after": len(existing)}, indent=2))
        return 0
    print("Desk board %s (%s): %d candidate(s)." % (desk.get("as_of") or "?", desk.get("mode") or "?", len(cands)))
    for c in added + already:
        s = c["signal"]
        late, mins = lateness(c)
        tag = "logged " if c in added else "already"
        print("  %s %s: %s %s entry %.2f at %s, stop %.2f, target %.2f (%gR)%s" % (
            tag, c["id"], c["symbol"], c["config_id"], s["entry"], s["entry_time"], s["stop"], s["target"],
            s.get("target_r") or 0, (" [late: logged %.0f min after the entry bar closed]" % mins) if late and c in added
            else ""))
    for sym, why in bad:
        print("  skipped %s: %s" % (sym, why))
    print("Logged %d new call(s), %d already in %s (now %d lines). Grade them after the close: scorecard.py grade."
          % (len(added), len(already), calls_path, len(existing)))
    return 0


# ---------------------------------------------------------------- grade

class BarSource(object):
    """Bars for (symbol, date): the store, else a saved chart under --offline DIR, else one Yahoo request a symbol."""

    def __init__(self, cache_root, offline, now_utc):
        self.cache_root = cache_root
        self.offline = offline
        self.now = now_utc
        self._series = {}
        self.requests = 0

    def _offline_series(self, symbol):
        names = []
        for s in (symbol, net.to_yahoo(symbol)):
            for stem in ("%s" % s, "%s-5d5m" % s, "%s-60d5m" % s):
                names += ["%s.json" % stem, "%s.json.gz" % stem]
        for name in names:
            p = os.path.join(self.offline, name)
            if os.path.exists(p):
                opener = gzip.open if p.endswith(".gz") else open
                try:
                    with opener(p, "rt", encoding="utf-8") as fh:
                        return net.parse_chart(fh.read(), symbol), p
                except (OSError, ValueError, net.NetError) as e:
                    return None, "could not read %s: %s" % (p, e)
        return None, "no saved chart for %s in %s" % (symbol, self.offline)

    def _fetched_series(self, symbol):
        if symbol not in self._series:
            self.requests += 1
            try:
                self._series[symbol] = (net.chart(symbol, "5d", "5m"), "yahoo")
            except net.NetError as e:
                self._series[symbol] = (None, "Yahoo: %s" % e)
            if self.requests > 1:
                net.SLEEP(net.PAUSE)
        return self._series[symbol]

    def session(self, symbol, date):
        """-> (bars, source) or (None, why)."""
        store = bars.load_store(symbol, "bars5m", self.cache_root)
        rows = ((store or {}).get("sessions") or {}).get(date)
        if rows:
            return bars.bars_of(rows), "store"
        if self.offline:
            series, src = self._offline_series(symbol)
        else:
            series, src = self._fetched_series(symbol)
        if series is None:
            return None, src
        sess = bars.series_to_sessions(series, self.now, include_partial=True)
        b = sess.get(date)
        if not b:
            where = src if self.offline else "Yahoo's 5-day window (refresh the bar store: scripts/fetch_cache.py)"
            return None, "no bars for %s on %s in %s" % (symbol, date, where)
        return b, ("offline" if self.offline else "yahoo")


def session_complete(date, now_utc):
    """(complete, why_not): the session's close has passed by now (New York)."""
    d = datetime.date.fromisoformat(date)
    s = clock.session(d)
    if not s:
        _, why = clock.trading_day(d)
        return False, "%s is not a trading day (%s)" % (date, why)
    close_et = datetime.datetime.combine(d, s[1])
    now_et = clock.to_eastern(now_utc)
    if now_et < close_et:
        return False, "the %s session closes at %s ET; it is %s" % (date, clock.clock_label(s[1]),
                                                                   now_et.strftime("%Y-%m-%d %H:%M"))
    return True, ""


def grade_call(call, b, until, cost_bps, now_utc, source):
    """A call plus its session's bars -> the outcomes.jsonl line, or raise ScorecardError with why not."""
    sig = call["signal"]
    k = int(sig["k"])
    if k + 1 > until:
        raise ScorecardError("the entry bar %d (%s) leaves no bar before the flat-by slot %d" % (k, sig["entry_time"], until))
    if len(b) <= until:
        raise ScorecardError("bars for %s on %s stop at slot %d; the flat-by slot %d has not closed"
                             % (call["symbol"], call["date"], len(b) - 1, until))
    both = sim.play_both(b, sig, until=until, cost_bps=cost_bps)
    head = both["next_open"]
    if head is None or both["close"] is None:
        raise ScorecardError("the signal cannot be played (risk %.4f)" % (sig["entry"] - sig["stop"]))
    late, mins = lateness(call)
    return {"id": call["id"], "graded_at": clock.iso(now_utc), "fill": "next_open", "outcome": head["outcome"],
            "R_net": head["R_net"], "exit_slot": head["exit_slot"], "exit_price": head["exit_price"], "late": late,
            "late_min": mins, "entry_fill": head["entry_fill"], "R_gross": head["R_gross"], "mae_R": head["mae_R"],
            "mfe_R": head["mfe_R"], "bars_held": head["bars_held"], "cost_bps": cost_bps,
            "exit_time": clock.clock12(clock.slot_close_minutes(head["exit_slot"])), "until_slot": until,
            "fills": both, "symbol": call["symbol"], "date": call["date"], "setup": call.get("setup"),
            "config_id": call.get("config_id"), "source": source, "created_by": "trader-scorecard"}


def describe_outcome(call, o):
    nf, cf = o["fills"]["next_open"], o["fills"]["close"]
    return "%-6s %s  %-22s %-9s next-open fill %.2f: %s at %s, %.2f, %+.2fR (plan fill %.2f: %+.2fR)%s  [%s]" % (
        call["symbol"], call["date"], (call.get("config_id") or "")[:22], call["signal"]["entry_time"], nf["entry_fill"],
        nf["outcome"], o["exit_time"], nf["exit_price"], nf["R_net"], cf["entry_fill"], cf["R_net"],
        (" LATE: logged %.0f min after the entry bar closed, out of the headline" % o["late_min"]) if o["late"] else "",
        o["source"])


def cmd_grade(a):
    now = clock.parse_now(a.now)
    cfg = dtio.read_config(a.config)
    if not a.cost_bps >= 0:
        raise ScorecardError("--cost-bps must be 0 or more")
    calls_path, out_path = paths(a.dir)
    calls = [c for c in read_lines(calls_path) if isinstance(c, dict) and c.get("id")]
    if not calls:
        print("No calls in %s yet: desk.py live logs every candidate it shows (scorecard.py log --from-desk)." % calls_path)
        return 0
    graded_ids = set(o.get("id") for o in read_lines(out_path) if isinstance(o, dict))
    want_date = parse_date(a.date, "--date") if a.date else None
    source = BarSource(a.cache_root, a.offline, now)
    todo = [c for c in calls if c["id"] not in graded_ids and (not want_date or c.get("date") == want_date)]
    done, pending, skipped = [], [], len(calls) - len(todo)
    for call in todo:
        try:
            sig = normalize_signal(call.get("signal"))
            call = dict(call, signal=sig, date=parse_date(call.get("date"), "the call's date"))
            ok, why = session_complete(call["date"], now)
            if not ok:
                raise ScorecardError(why)
            b, src = source.session(call["symbol"], call["date"])
            if b is None:
                raise ScorecardError(src)
            until = clock.flat_slot(call["date"], cfg)
            o = grade_call(call, b, until, float(a.cost_bps), now, src)
        except (ScorecardError, clock.ClockError) as e:
            pending.append((call, str(e)))
            continue
        dtio.append_jsonl(out_path, o)
        graded_ids.add(o["id"])
        done.append((call, o))
    if a.json:
        print(json.dumps({"graded": [o for _, o in done], "pending": [{"id": c["id"], "why": w} for c, w in pending],
                          "already_graded_or_other_date": skipped, "outcomes_file": out_path,
                          "requests": source.requests}, indent=2))
        return 0
    for call, o in done:
        print("  " + describe_outcome(call, o))
    for call, why in pending:
        print("  pending %s: %s" % (call["id"], why))
    late = sum(1 for _, o in done if o["late"])
    print("Graded %d call(s)%s, %d pending, %d already graded%s. %s" % (
        len(done), (" (%d late)" % late) if late else "", len(pending), skipped,
        (" or not on %s" % want_date) if want_date else "",
        ("Appended to %s." % out_path) if done else "Nothing appended."))
    if done:
        print("Fills are simulated from 5-minute bars (next-open and plan price), %g bps costs, stop before target "
              "inside a bar; a gap through the stop counts as the stop price here and can fill worse." % a.cost_bps)
    return 0


# ---------------------------------------------------------------- report

def load_record(d, since=None):
    """[(call, outcome or None)] in the order the calls were logged, the latest outcome a call has."""
    calls_path, out_path = paths(d)
    calls = [c for c in read_lines(calls_path) if isinstance(c, dict) and c.get("id") and isinstance(c.get("signal"), dict)]
    outcomes = {}
    for o in read_lines(out_path):
        if isinstance(o, dict) and o.get("id"):
            outcomes[o["id"]] = o
    seen, rows = set(), []
    for c in calls:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        if since and str(c.get("date") or "") < since:
            continue
        rows.append((c, outcomes.get(c["id"])))
    return rows


def trade_of(call, o, fill="next_open"):
    f = (o.get("fills") or {}).get(fill)
    if not isinstance(f, dict):
        if fill != "next_open":
            return None
        f = o
    return {"id": call["id"], "date": call.get("date"), "symbol": call.get("symbol"), "setup": call.get("setup"),
            "config_id": call.get("config_id"), "entry_time": call["signal"].get("entry_time"),
            "k": int(call["signal"].get("k") or 0), "rank": call.get("rank"), "outcome": f.get("outcome"),
            "R_net": float(f.get("R_net") or 0.0), "late": bool(o.get("late")),
            "lab_oos_exp_R": call.get("lab_oos_exp_R"), "tm_p_target": call.get("tm_p_target")}


def order_key(t):
    return (t.get("date") or "", t.get("k") or 0, t.get("symbol") or "")


def week_of(date_str):
    d = datetime.date.fromisoformat(date_str)
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def by_week(trades):
    weeks = {}
    for t in trades:
        weeks.setdefault(week_of(t["date"]), []).append(t)
    out, cum = [], 0.0
    for wk in sorted(weeks):
        ts = weeks[wk]
        rs = [t["R_net"] for t in ts]
        cum += sum(rs)
        out.append({"week": wk, "n": len(ts), "target": sum(1 for t in ts if t["outcome"] == "target"),
                    "stop": sum(1 for t in ts if t["outcome"] == "stop"),
                    "close": sum(1 for t in ts if t["outcome"] not in ("target", "stop")),
                    "win_rate": round(sum(1 for t in ts if t["outcome"] == "target") / float(len(ts)), 4),
                    "R": round(sum(rs), 4), "exp_R": round(statistics.mean(rs), 4), "cum_R": round(cum, 4)})
    return out


def equity(trades, r_usd):
    cum, eq_r, eq_usd = 0.0, [], []
    for t in trades:
        cum += t["R_net"]
        eq_r.append(round(cum, 4))
        eq_usd.append(round(cum * r_usd, 2))
    return eq_r, eq_usd


def mean_or_none(values):
    vals = [float(v) for v in values if v is not None]
    return (round(statistics.mean(vals), 4), len(vals)) if vals else (None, 0)


def build_report(rows, r_usd, now_utc, d, since=None):
    graded = [(c, o) for c, o in rows if o]
    pending = [c for c, o in rows if not o]
    head = sorted([trade_of(c, o) for c, o in graded if not o.get("late")], key=order_key)
    late = sorted([trade_of(c, o) for c, o in graded if o.get("late")], key=order_key)
    plan = sorted([t for t in (trade_of(c, o, "close") for c, o in graded if not o.get("late")) if t], key=order_key)
    st = stats.stats_block(head)
    by_setup = {}
    for name in sorted(set(t.get("setup") or "?" for t in head)):
        by_setup[name] = stats.stats_block([t for t in head if (t.get("setup") or "?") == name])
    eq_r, eq_usd = equity(head, r_usd)
    lab_exp, lab_n = mean_or_none(t.get("lab_oos_exp_R") for t in head)
    gap = round(st["exp_R"] - lab_exp, 4) if (st["exp_R"] is not None and lab_exp is not None) else None
    return {"created_by": "trader-scorecard", "as_of": clock.iso(now_utc), "dir": d, "since": since,
            "calls": len(rows), "graded": len(graded), "pending": len(pending), "late": len(late),
            "headline_n": len(head), "fill": "next_open", "r_usd": r_usd,
            "stats": st, "stats_plan_fill": stats.stats_block(plan), "stats_late": stats.stats_block(late),
            "by_setup": by_setup, "by_week": by_week(head), "equity_R": eq_r, "equity_usd": eq_usd,
            "lab_expectation_R": lab_exp, "lab_expectation_n": lab_n, "realised_R": st["exp_R"], "gap_R": gap,
            "trades": head, "late_trades": late,
            "pending_ids": [c["id"] for c in pending], "small_sample": len(head) < SMALL_SAMPLE, "note": NOTE}


def pct(x):
    return "n/a" if x is None else "%.1f %%" % (100.0 * x)


def ci_pct(ci):
    return "n/a" if not ci or ci[0] is None else "%.1f to %.1f %%" % (100.0 * ci[0], 100.0 * ci[1])


def r_(x):
    return "n/a" if x is None else "%+.2fR" % x


def ci_r(ci):
    return "n/a" if not ci or ci[0] is None else "%+.2f to %+.2f" % (ci[0], ci[1])


def pf_(x):
    return "n/a" if x is None else ("999+" if x >= 999 else "%.2f" % x)


def usd(x):
    return ("-$%s" if x < 0 else "$%s") % format(abs(round(x, 2)), ",.2f")


def describe_report(rep):
    st = rep["stats"]
    lines = ["Scorecard in %s%s: %d call(s), %d graded, %d pending, %d late (graded, out of the headline)." % (
        rep["dir"], (" since %s" % rep["since"]) if rep["since"] else "", rep["calls"], rep["graded"], rep["pending"],
        rep["late"])]
    if not st["n"]:
        lines.append("No graded call in the headline yet. Grade after the close: scorecard.py grade.")
        lines.append(NOTE)
        return lines
    lines += [
        "Headline: %d call(s), next-open fill, stop before target inside a bar, costs taken." % st["n"],
        "  Win rate       %s   (95 %% CI %s; target %d, stop %d, close %d, green %d)" % (
            pct(st["win_rate"]), ci_pct(st["win_ci95"]), st["target"], st["stop"], st["close"], st["green"]),
        "  Expectancy     %s a trade  (95 %% bootstrap CI %s, seed %d)" % (r_(st["exp_R"]), ci_r(st["exp_ci95"]), stats.SEED),
        "  Profit factor  %s   avg win %s, avg loss %s, median %s, max drawdown %.2fR" % (
            pf_(st["pf"]), r_(st["avg_win_R"]), r_(st["avg_loss_R"]), r_(st["median_R"]), st["max_dd_R"] or 0.0),
    ]
    pl = rep["stats_plan_fill"]
    if pl["n"]:
        lines.append("  Plan fill      %s a trade, win rate %s (the trigger bar's close: the idealised number)" % (
            r_(pl["exp_R"]), pct(pl["win_rate"])))
    if rep["equity_R"]:
        peak = max([0.0] + rep["equity_R"])
        lines.append("  Equity         %s over %d call(s) = %s if every call had been taken at one R = %s "
                     "(max_loss_per_trade_usd), simulated fills; peak %s, max drawdown %.2fR" % (
                         r_(rep["equity_R"][-1]), st["n"], usd(rep["equity_usd"][-1]), usd(rep["r_usd"]), r_(peak),
                         st["max_dd_R"] or 0.0))
    if rep["lab_expectation_R"] is not None:
        lines.append("  Lab said       %s a trade (mean OOS expectancy logged with %d of these calls); realised %s; "
                     "gap %s" % (r_(rep["lab_expectation_R"]), rep["lab_expectation_n"], r_(rep["realised_R"]),
                                 r_(rep["gap_R"])))
    else:
        lines.append("  Lab said       nothing logged (no lab_oos_exp_R on these calls), so no gap to show")
    if rep["small_sample"]:
        lines.append("  With %d graded call(s) the record says almost nothing yet: the intervals above are the honest "
                     "width. %d is the least worth reading." % (st["n"], SMALL_SAMPLE))
    lines.append("By setup:")
    lines.append("  %-14s %4s  %-26s %-26s %s" % ("setup", "n", "win rate (95 % CI)", "exp R (95 % CI)", "pf"))
    for name, s in rep["by_setup"].items():
        lines.append("  %-14s %4d  %-26s %-26s %s" % (name, s["n"], "%s (%s)" % (pct(s["win_rate"]), ci_pct(s["win_ci95"])),
                                                       "%s (%s)" % (r_(s["exp_R"]), ci_r(s["exp_ci95"])), pf_(s["pf"])))
    lines.append("By week (Monday):")
    lines.append("  %-10s %4s %6s %5s %5s %9s %8s %8s" % ("week", "n", "target", "stop", "close", "win rate", "R", "cum R"))
    for w in rep["by_week"]:
        lines.append("  %-10s %4d %6d %5d %5d %9s %+8.2f %+8.2f" % (w["week"], w["n"], w["target"], w["stop"], w["close"],
                                                                 pct(w["win_rate"]), w["R"], w["cum_R"]))
    if rep["late_trades"]:
        lines.append("Late (%d, graded but out of the headline: logged more than one bar after the entry bar closed):"
                     % len(rep["late_trades"]))
        for t in rep["late_trades"]:
            lines.append("  %s: %s %s" % (t["id"], t["outcome"], r_(t["R_net"])))
    if rep["pending"]:
        lines.append("Pending (%d, not graded yet): %s" % (rep["pending"], ", ".join(rep["pending_ids"][:8])
                                                             + (", ..." if rep["pending"] > 8 else "")))
    lines.append(NOTE)
    return lines


def cmd_report(a):
    now = clock.parse_now(a.now)
    cfg = dtio.read_config(a.config)
    since = parse_date(a.since, "--since") if a.since else None
    r_usd = a.r_usd if a.r_usd is not None else cfg.get("max_loss_per_trade_usd", DEFAULT_R_USD)
    try:
        r_usd = float(r_usd)
    except (TypeError, ValueError):
        raise ScorecardError("max_loss_per_trade_usd in the config is not a number: %r" % r_usd)
    if not r_usd > 0:
        raise ScorecardError("one R in dollars must be above $0 (got %r)" % r_usd)
    rows = load_record(a.dir, since)
    rep = build_report(rows, r_usd, now, a.dir, since)
    if a.publish:
        dtio.write_json(a.publish, rep)
    if a.json:
        print(json.dumps(dict(rep, published=a.publish), indent=2))
        return 0
    for line in describe_report(rep):
        print(line)
    if a.publish:
        print("Wrote %s." % a.publish)
    return 0


# ---------------------------------------------------------------- show

def describe_call(call, o):
    s = call["signal"]
    lines = ["Call %s" % call["id"],
             "  Logged %s from the board of %s (rank %s, lab %s, OOS expectancy %s, time machine p(target) %s)" % (
                 call.get("logged_at"), call.get("as_of") or "?", call.get("rank") or "?", call.get("lab_status") or "?",
                 r_(call.get("lab_oos_exp_R")), pct(call.get("tm_p_target")) if call.get("tm_p_target") is not None else "n/a"),
             "  Signal %s %s: entry %.2f at %s (bar %d), stop %.2f (%s), target %.2f (%gR), risk %.2f a share%s" % (
                 call["symbol"], call.get("config_id") or "?", s["entry"], s["entry_time"], int(s["k"]), s["stop"],
                 s.get("stop_rule") or "?", s["target"], s.get("target_r") or 0, s["entry"] - s["stop"],
                 (", opening range %.2f to %.2f" % (s["opening_range"]["low"], s["opening_range"]["high"]))
                 if isinstance(s.get("opening_range"), dict) and s["opening_range"].get("high") is not None else "")]
    if call.get("flags"):
        lines.append("  Flags: %s" % ", ".join(str(f) for f in call["flags"]))
    try:
        late, mins = lateness(call)
        lines.append("  %s: logged %.0f min after the entry bar closed (the grace is one bar, %d min)." % (
            "Late" if late else "On time", mins, LATE_GRACE_MIN))
    except ScorecardError:
        pass
    if not o:
        lines.append("No outcome yet: grade after the close with scorecard.py grade.")
        return lines
    lines.append("Outcome graded %s from the %s, held to slot %s (%s), costs %g bps:" % (
        o.get("graded_at"), {"store": "bar store", "yahoo": "Yahoo chart", "offline": "saved chart"}.get(o.get("source"),
                                                                                                     o.get("source")),
        o.get("until_slot"), clock.clock12(clock.slot_close_minutes(int(o.get("until_slot") or 0))), o.get("cost_bps") or 0))
    fills = o.get("fills") or {"next_open": o}
    for name in FILLS:
        f = fills.get(name)
        if not isinstance(f, dict):
            continue
        lines.append("  %-14s %.2f: %s at %s, %.2f, %s net (%s gross), worst %s, best %s, %d bar(s)" % (
            "next-open fill" if name == "next_open" else "plan fill", f["entry_fill"], f["outcome"],
            clock.clock12(clock.slot_close_minutes(int(f["exit_slot"]))), f["exit_price"], r_(f["R_net"]), r_(f["R_gross"]),
            r_(f.get("mae_R")), r_(f.get("mfe_R")), int(f.get("bars_held") or 0)))
    lines.append("  %s" % ("Late: graded, kept, out of the headline." if o.get("late") else "In the headline."))
    return lines


def cmd_show(a):
    rows = load_record(a.dir)
    want = a.id.strip()
    hit = next(((c, o) for c, o in rows if c["id"] == want), None)
    if not hit:
        raise ScorecardError("no call with id %r in %s (%d calls)" % (want, paths(a.dir)[0], len(rows)))
    call, o = hit
    if a.json:
        print(json.dumps({"call": call, "outcome": o}, indent=2))
        return 0
    for line in describe_call(call, o):
        print(line)
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="The scorecard (trader-scorecard): every candidate the desk showed, "
                                             "graded on the real bars.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--dir", default=DEFAULT_DIR, help="the scorecard folder (default %s)" % DEFAULT_DIR)
        p.add_argument("--now", help='pin the clock: "2026-10-19 16:30" (New York) or ISO with Z')
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("log", help="append the desk board's candidates to calls.jsonl (never a duplicate)")
    p.add_argument("--from-desk", action="store_true", help="read the candidates from the desk board")
    p.add_argument("--desk", default=DEFAULT_DESK, help="the desk board (default %s)" % DEFAULT_DESK)
    common(p)
    p = sub.add_parser("grade", help="play every ungraded call of a finished session on the real bars")
    p.add_argument("--date", help="only calls of this session, YYYY-MM-DD")
    p.add_argument("--cache-root", default=DEFAULT_CACHE, help="the bar store (default %s)" % DEFAULT_CACHE)
    p.add_argument("--offline", metavar="DIR", help="a folder of saved Yahoo charts (<SYM>.json), no network")
    p.add_argument("--cost-bps", type=float, default=sim.DEFAULT_COST_BPS,
                   help="round-trip costs in basis points of the entry (default %g)" % sim.DEFAULT_COST_BPS)
    p.add_argument("--config", default="trader/config.json", help="flat_by comes from here (default 15:55)")
    common(p)
    p = sub.add_parser("report", help="the live record with confidence intervals")
    p.add_argument("--since", help="calls from this date on, YYYY-MM-DD")
    p.add_argument("--publish", nargs="?", const=DEFAULT_PUBLISH, metavar="PATH",
                   help="write the report document for the dashboard (default %s)" % DEFAULT_PUBLISH)
    p.add_argument("--config", default="trader/config.json", help="max_loss_per_trade_usd = one R in dollars")
    p.add_argument("--r-usd", type=float, help="one R in dollars, instead of the config's max_loss_per_trade_usd")
    common(p)
    p = sub.add_parser("show", help="one call and its outcome")
    p.add_argument("id", help='the call id, e.g. "2026-10-19|TSLA|orb15|r=1|10:15 AM"')
    common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"log": cmd_log, "grade": cmd_grade, "report": cmd_report, "show": cmd_show}[a.cmd](a)
    except (ScorecardError, clock.ClockError, dtio.IOError_) as e:
        print("scorecard.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
