#!/usr/bin/env python3
"""Paper broker for trader-paper. Standard library only. It never talks to a broker.

It takes the ONE bracket order the user already said yes to (trader-risk passed
it, ticket.py approve recorded the yes in trader/state/broker.json) and plays
it on the stock's own 5-minute bars, so the whole chain runs end to end without
an Alpaca key. Every fill here is simulated from 5-minute bars: a real fill can
be worse, a gap through the stop fills past it, and a limit at the target can go
unfilled while the price touches it.

  place     fills the entry at the OPEN of the first completed 5-minute bar that
            starts at or after the approval time, plus half the round-trip cost
            as slippage. Records it through the upstream tool (ticket.py result
            --status placed --filled-qty --filled-price), so trader/state/broker.json
            and trader/portfolio.json keep the upstream shapes, then opens the
            bracket in trader/paper/positions.json. It refuses, in one plain
            line, unless the order exists, the config says paper, trader-risk
            passed that exact order today, the latest decision on it today is
            the user's yes, and nothing was placed for it yet.
  sync      walks every open position's bars from the fill bar on: the stop is
            checked before the target inside each bar (a bar that touches both
            is the stop), the stop fills at the stop price minus slippage, the
            target at the target price (a limit), and a position still open at
            the flat-by bar sells at that bar's close minus slippage. Each exit
            is recorded through ticket.py fill and closed in the book with the
            exit price, the time, R against the plan's risk and the realised
            P&L in dollars, labelled simulated. The book is the broker's log:
            the one place a realised figure belongs.
  status    the book, open and closed, with the caveat on every line.
  cancel    marks an open position closed without a fill, only when the user asks.

    python3 paper.py place --index 1                                  # the order the user approved
    python3 paper.py place --index 1 --now "2026-10-19 10:25" --offline TSLA.json
    python3 paper.py sync                                             # every open position, today's bars
    python3 paper.py sync --now "2026-10-19 16:05" --offline charts/  # a folder of <SYM>.json charts
    python3 paper.py status
    python3 paper.py cancel --id paper-2026-10-19-1

Bars come from Yahoo's v8 chart (range=1d&interval=5m, free and unofficial, can
lag) or --offline FILE (a saved chart JSON, .json or .json.gz, or a folder of
<SYM>.json). A past day's session is read from the bar store (--cache-root,
default trader/cache) when it is there. Only bars that have CLOSED by --now
count; a live in-progress bar is never used.
Costs: --cost-bps (default 4) is the round trip; half of it is added to the buy
and taken off every market sell (the stop, the flat-by close). The target is a
limit and fills at its price.
Exit codes: 0 = done, 1 = refused, waiting, or an error (one plain line on stderr).
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
for _cand in (HERE, PARENT):
    if os.path.isfile(os.path.join(_cand, "dt", "__init__.py")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from dt import bars, clock, net, stats  # noqa: E402
from dt.io import IOError_, read_json, write_json, now_iso  # noqa: E402

ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
DEFAULT_COST_BPS = 4.0
SIM = "simulated from 5-minute bars"
CREATED_BY = "trader-paper"
BOOK_NOTE = ("Paper broker log. Every fill here is simulated from 5-minute bars (Yahoo, unofficial): a real fill "
             "can be worse. The realised figures are this log's record, not a projection.")
BAR = datetime.timedelta(minutes=clock.BAR_MIN)
DEFAULT_BOOK = os.path.join("trader", "paper", "positions.json")
REASON_WORDS = {"stop": "stop", "target": "target", "flat": "flat by the close", "cancelled": "cancelled"}


class PaperError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _p(x):
    return "%.2f" % float(x)


def _money(x, signed=False):
    v = float(x)
    s = "$" + "{:,.2f}".format(abs(v))
    if v < 0:
        return "-" + s
    return ("+" + s) if signed else s


def _r(x):
    return ("%+.2f" % x) if x is not None else "n/a"


def fingerprint(order):
    """Same function as trader-risk/check.py and trader-broker/ticket.py: the exact order."""
    core = {k: order[k] for k in ALPACA_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def rec_day(rec):
    """The trading day a broker-log record belongs to (ticket.py's rule)."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def parse_now(text):
    try:
        return clock.parse_now(text)
    except clock.ClockError as e:
        raise PaperError(str(e))


def parse_at(text):
    """A broker-log time ("2026-10-19T10:16:00-04:00", or ...Z) -> naive UTC."""
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        raise PaperError("the approval's time %r is not a time this tool can read" % (text,))
    if dt.tzinfo is None:
        return clock.eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def hhmm(et):
    return et.strftime("%H:%M")


def buy_fill(open_px, cost_bps):
    """The entry fill: the bar's open plus half the round trip, to the cent (half up)."""
    return bars.half_up(float(open_px) * (1.0 + float(cost_bps) / 2.0 / 10000.0))


def sell_fill(px, cost_bps):
    """A market sell (the stop, the flat-by close): the price minus half the round trip, to the cent."""
    return bars.half_up(float(px) * (1.0 - float(cost_bps) / 2.0 / 10000.0))


def first_slot_at_or_after(date_str, at_et):
    """The first 5-minute slot of the session whose bar STARTS at or after at_et, or None."""
    for k in range(clock.slots_of_day(date_str)):
        if clock.slot_time(date_str, k) >= at_et:
            return k
    return None


# ---------------------------------------------------------------- files

def _read(path, what, required=True):
    try:
        doc = read_json(path)
    except IOError_ as e:
        raise PaperError(str(e))
    if doc is None and required:
        raise PaperError("no %s at %s" % (what, path))
    return doc


def load_orders(path):
    doc = _read(path, "orders file (run trader-budget first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise PaperError("no orders in %s" % path)
    return orders


def pick(orders, index):
    if index is None:
        raise PaperError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise PaperError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def read_config(path):
    cfg = _read(path, "config", required=False) or {}
    return cfg if isinstance(cfg, dict) else {}


def require_paper(cfg, path):
    v = cfg.get("paper", True)
    if v is not True:
        raise PaperError("\"paper\" is %s in %s: the paper broker fills paper trades only. It never touches a "
                         "live account" % (json.dumps(v), path))


def load_state(path):
    doc = _read(path, "broker state", required=False)
    if doc is None:
        return {"orders": []}
    if not isinstance(doc, dict) or not isinstance(doc.get("orders"), list):
        raise PaperError("%s is not a trader-broker log" % path)
    return doc


def decisions(state, fp, today):
    """Every record in the broker log for this fingerprint on this day, oldest first."""
    return [r for r in state.get("orders", []) if r.get("fingerprint") == fp and rec_day(r) == today]


def risk_pass(risk, fp, today, index):
    """The PASS record trader-risk wrote for this exact order today, or a plain refusal."""
    if not risk:
        raise PaperError("trader-risk has not checked order %d (no risk file): run check.py, then ticket.py approve"
                         % index)
    if risk.get("date") != today:
        raise PaperError("the risk check is from %s, not %s: run check.py again" % (risk.get("date"), today))
    for r in risk.get("results") or []:
        if r.get("fingerprint") == fp:
            if r.get("verdict") != "PASS":
                raise PaperError("trader-risk blocked order %d: %s" % (index, "; ".join(r.get("reasons") or [])))
            return r
    raise PaperError("order %d was not in the last risk check (it changed or is new): run check.py again" % index)


def new_book():
    return {"created_by": CREATED_BY, "note": BOOK_NOTE, "created_at": now_iso(), "positions": []}


def load_book(path):
    doc = _read(path, "paper book", required=False)
    if doc is None:
        return new_book()
    if not isinstance(doc, dict) or not isinstance(doc.get("positions"), list):
        raise PaperError("%s is not a paper book (it needs a \"positions\" list)" % path)
    doc.setdefault("note", BOOK_NOTE)
    return doc


def save_book(path, book):
    book["updated_at"] = now_iso()
    write_json(path, book)


def ticket_path(explicit=None):
    """trader-broker/ticket.py next to this skill's folder (the repo, or ~/.claude/skills)."""
    if explicit:
        if not os.path.exists(explicit):
            raise PaperError("no ticket.py at %s" % explicit)
        return explicit
    for cand in (os.path.join(PARENT, "trader-broker", "ticket.py"), os.path.join(HERE, "ticket.py")):
        if os.path.exists(cand):
            return cand
    raise PaperError("no trader-broker/ticket.py next to this skill: the paper broker records every fill through it "
                     "(pass --ticket PATH)")


def run_ticket(path, args):
    """Run the upstream tool; its refusal becomes ours, in one line."""
    cmd = [sys.executable, path] + [str(x) for x in args]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    except OSError as e:
        raise PaperError("could not run %s: %s" % (path, e))
    if p.returncode != 0:
        lines = [ln for ln in (p.stderr.strip().splitlines() + p.stdout.strip().splitlines()) if ln.strip()]
        line = lines[-1] if lines else "exit code %d" % p.returncode
        if line.startswith("ticket.py: "):
            line = line[len("ticket.py: "):]
        raise PaperError("ticket.py did not record it: %s" % line)
    return p.stdout


# ---------------------------------------------------------------- bars

def chart_range(date_str, today_str):
    back = (datetime.date.fromisoformat(today_str) - datetime.date.fromisoformat(date_str)).days
    return "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"


def read_offline(path, sym):
    """A saved Yahoo chart JSON (.json or .json.gz), or a folder holding <SYM>.json(.gz)."""
    if os.path.isdir(path):
        names = []
        for base in (sym.upper(), net.to_yahoo(sym)):
            names += [base + ".json", base + ".json.gz"]
        for name in names:
            cand = os.path.join(path, name)
            if os.path.exists(cand):
                path = cand
                break
        else:
            raise PaperError("no %s.json(.gz) in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise PaperError("no offline chart file %s" % path)
    doc = _read(path, "offline chart")
    try:
        series = net.parse_chart(doc, sym)
    except net.NetError as e:
        raise PaperError(str(e))
    got = str((series.get("meta") or {}).get("symbol") or "").upper()
    if got and got != net.to_yahoo(sym).upper():
        raise PaperError("the chart file %s holds %s, not %s" % (path, got, sym.upper()))
    return series


def session_bars(sym, date_str, now_utc, offline=None, cache_root=None):
    """(bars, source): sym's completed regular-session 5-minute bars on date_str by now_utc.
    --offline first; a past day's full session from the bar store when it is there; else Yahoo."""
    if offline:
        series, source = read_offline(offline, sym), "offline"
    else:
        today = clock.to_eastern(now_utc).date().isoformat()
        if date_str < today and cache_root:
            try:
                store = bars.load_store(sym, "bars5m", cache_root)
            except (IOError_, ValueError):
                store = None
            rows = ((store or {}).get("sessions") or {}).get(date_str)
            if rows:
                return bars.bars_of(rows), "cache"
        try:
            series, source = net.chart(sym, chart_range(date_str, today), "5m"), "yahoo"
        except net.NetError as e:
            raise PaperError(str(e))
    sessions = bars.series_to_sessions(series, now_utc, include_partial=True)
    return sessions.get(date_str) or [], source


# ---------------------------------------------------------------- the simulation

def settle(session, pos, flat_k, cost_bps):
    """Walk the bars from the fill bar on. Stop before target inside a bar (a bar touching both is
    the stop); the stop fills at the stop minus slippage, the target at its price, a position
    still open at the flat-by bar sells at that bar's close minus slippage. None = still open."""
    k = entry_slot_of(pos)
    stop, target = float(pos["stop"]), float(pos["target"])
    last = min(flat_k, len(session) - 1)
    for j in range(k, last + 1):
        b = session[j]
        if float(b["l"]) <= stop:
            return _exit("stop", j, k, stop, sell_fill(stop, cost_bps))
        if float(b["h"]) >= target:
            return _exit("target", j, k, target, bars.half_up(target))
    if len(session) > flat_k:
        c = float(session[flat_k]["c"])
        return _exit("flat", flat_k, k, c, sell_fill(c, cost_bps))
    return None


def _exit(reason, j, k, raw, fill):
    return {"reason": reason, "slot": j, "raw": round(float(raw), 4), "exit": fill,
            "slippage": round(float(raw) - fill, 4), "bars_held": j - k + 1}


def entry_slot_of(pos):
    k = pos.get("entry_slot")
    if k is None and pos.get("entry_bar_start"):
        try:
            h, m = str(pos["entry_bar_start"]).split(":")
            k = clock.slot_of(datetime.datetime.combine(datetime.date.fromisoformat(pos["date"]),
                                                        datetime.time(int(h), int(m))))
        except (ValueError, TypeError):
            k = None
    if k is None:
        raise PaperError("position %s has no entry bar" % pos.get("id"))
    return int(k)


def close_position(pos, res, date_str, recorded, error=None):
    entry, stop, qty = float(pos["entry"]), float(pos["stop"]), int(pos["qty"])
    risk = entry - stop
    r = (res["exit"] - entry) / risk if risk > 0 else None
    start = clock.slot_time(date_str, res["slot"])
    pos.update({
        "status": "closed", "exit": res["exit"], "exit_reason": res["reason"], "exit_price_raw": res["raw"],
        "exit_slippage": res["slippage"], "exit_slot": res["slot"], "exit_bar_start": hhmm(start),
        "exit_time": hhmm(start + BAR), "bars_held": res["bars_held"],
        "R": round(r, 3) if r is not None else None, "pnl_usd": round((res["exit"] - entry) * qty, 2),
        "pnl_label": SIM, "fill_recorded": bool(recorded), "closed_at": now_iso(),
    })
    if error:
        pos["fill_error"] = error
    elif "fill_error" in pos:
        del pos["fill_error"]
    return pos


# ---------------------------------------------------------------- words

def describe_open(pos):
    return ("%s  %s %d at %s (the %s bar's open %s plus %s slippage), stop %s, target %s, flat by %s, %s  %s"
            % (pos["id"], pos["symbol"], int(pos["qty"]), _p(pos["entry"]), pos.get("entry_bar_start"),
               _p(pos.get("entry_open", pos["entry"])), _p(pos.get("slippage", 0)), _p(pos["stop"]),
               _p(pos["target"]), pos.get("flat_by"), pos.get("date"), SIM))


def describe_closed(pos):
    if pos.get("exit_reason") == "cancelled":
        return "%s  %s %d at %s, cancelled by you, no fill  %s" % (pos["id"], pos["symbol"], int(pos["qty"]),
                                                                  _p(pos["entry"]), SIM)
    how = exit_words(pos)
    rec = "" if pos.get("fill_recorded") else "; NOT recorded in the broker log (%s)" % pos.get("fill_error", "")
    return ("%s  %s %d at %s -> %s, %s, R %s, %s%s  %s"
            % (pos["id"], pos["symbol"], int(pos["qty"]), _p(pos["entry"]), _p(pos["exit"]), how,
               _r(pos.get("R")), _money(pos.get("pnl_usd") or 0, signed=True), rec, SIM))


def exit_words(pos):
    reason = pos.get("exit_reason")
    if reason == "stop":
        return "stop in the %s bar (stop %s minus %s slippage)" % (pos.get("exit_bar_start"), _p(pos["exit_price_raw"]),
                                                                  _p(pos.get("exit_slippage", 0)))
    if reason == "target":
        return "target in the %s bar (limit at %s)" % (pos.get("exit_bar_start"), _p(pos["exit_price_raw"]))
    if reason == "flat":
        return "flat at %s (the %s bar's close %s minus %s slippage)" % (
            pos.get("exit_time"), pos.get("exit_bar_start"), _p(pos["exit_price_raw"]), _p(pos.get("exit_slippage", 0)))
    return str(reason)


def summary(book):
    closed = [p for p in book["positions"] if p.get("status") == "closed" and p.get("exit_reason") != "cancelled"]
    cancelled = [p for p in book["positions"] if p.get("exit_reason") == "cancelled"]
    opened = [p for p in book["positions"] if p.get("status") == "open"]
    n = len(closed)
    out = {"open": len(opened), "closed": n, "cancelled": len(cancelled),
           "stop": sum(1 for p in closed if p["exit_reason"] == "stop"),
           "target": sum(1 for p in closed if p["exit_reason"] == "target"),
           "flat": sum(1 for p in closed if p["exit_reason"] == "flat"),
           "target_rate": None, "target_ci95": [None, None], "green": sum(1 for p in closed if (p.get("R") or 0) > 0),
           "R_total": round(sum(p.get("R") or 0 for p in closed), 3),
           "pnl_usd_total": round(sum(p.get("pnl_usd") or 0 for p in closed), 2), "pnl_label": SIM}
    if n:
        lo, hi = stats.wilson(out["target"], n)
        out["target_rate"] = round(out["target"] / float(n), 4)
        out["target_ci95"] = [round(lo, 4), round(hi, 4)]
    return out


def summary_lines(s):
    if not s["closed"]:
        return ["Closed: none yet."]
    lines = ["Closed %d: target %d, stop %d, flat %d; target first %d of %d (%.0f %%, 95 %% CI %.0f to %.0f %%); "
             "green %d of %d; R total %s; %s %s."
             % (s["closed"], s["target"], s["stop"], s["flat"], s["target"], s["closed"], 100 * s["target_rate"],
                100 * s["target_ci95"][0], 100 * s["target_ci95"][1], s["green"], s["closed"], _r(s["R_total"]),
                _money(s["pnl_usd_total"], signed=True), SIM)]
    if s["closed"] < 30:
        lines.append("%d closed trades say little: the interval above is the honest width." % s["closed"])
    return lines


# ---------------------------------------------------------------- commands

def cmd_place(a):
    now = parse_now(a.now)
    now_et = clock.to_eastern(now)
    today = now_et.date().isoformat()
    orders = load_orders(a.orders)
    order = pick(orders, a.index)
    cfg = read_config(a.config)
    require_paper(cfg, a.config)
    sym = str(order.get("symbol") or "").strip().upper()
    if not sym:
        raise PaperError("order %d has no symbol" % a.index)
    if str(order.get("side") or "").lower() != "buy":
        raise PaperError("order %d is a %s: the paper broker fills bracket buys only (v2 is long only); an exit is "
                         "recorded by sync" % (a.index, order.get("side")))
    stop, target = _f(order.get("stop_loss_stop_price")), _f(order.get("take_profit_limit_price"))
    if not stop or not target:
        raise PaperError("order %d is not a bracket (it needs stop_loss_stop_price and take_profit_limit_price, "
                         "from trader-budget): nothing to play" % a.index)
    q = _f(order.get("qty"))
    if not q or q <= 0 or abs(q - round(q)) > 1e-9:
        raise PaperError("order %d needs a whole-share qty, got %r" % (a.index, order.get("qty")))
    qty = int(round(q))
    fp = fingerprint(order)
    risk_pass(_read(a.risk, "risk check", required=False), fp, today, a.index)
    state = load_state(a.state)
    recs = decisions(state, fp, today)
    if not recs:
        raise PaperError("no yes recorded for order %d today in %s: ticket.py approve records it first"
                         % (a.index, a.state))
    placed = [r for r in recs if r.get("status") == "placed"]
    if placed:
        raise PaperError("order %d was already placed today%s: nothing to fill twice"
                         % (a.index, (" (" + placed[-1]["broker_order_id"] + ")") if placed[-1].get("broker_order_id")
                            else ""))
    rec = recs[-1]
    if rec.get("status") != "approved":
        raise PaperError("the latest decision on order %d today is %s, not a yes" % (a.index, rec.get("status")))
    if rec.get("paper") is False:
        raise PaperError("order %d was approved under a live config (\"paper\": false): the paper broker stays out"
                         % a.index)
    at_utc = parse_at(rec.get("at"))
    at_et = clock.to_eastern(at_utc)
    if at_utc > now:
        raise PaperError("the yes for order %d was recorded at %s New York, after %s: nothing to fill yet"
                         % (a.index, at_et.strftime("%Y-%m-%d %H:%M"), now_et.strftime("%Y-%m-%d %H:%M")))
    date_str = rec_day(rec)
    plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
    flat_by = str(plan.get("flat_by") or cfg.get("flat_by") or "15:55")
    try:
        flat_k = clock.flat_slot(date_str, {"flat_by": flat_by})
    except clock.ClockError as e:
        raise PaperError(str(e))
    k = first_slot_at_or_after(date_str, at_et)
    if k is None:
        raise PaperError("the yes at %s New York is after the last bar of %s: nothing to fill"
                         % (hhmm(at_et), date_str))
    start = clock.slot_time(date_str, k)
    if k >= flat_k:
        raise PaperError("the first bar after the yes is the %s bar, at or after the flat-by bar (%s on %s): no time "
                         "left to hold it" % (hhmm(start), flat_by, date_str))
    if not os.path.exists(a.portfolio) and not _f(cfg.get("budget")):
        raise PaperError("no portfolio at %s and no \"budget\" in %s: ticket.py could not record the fill"
                         % (a.portfolio, a.config))
    cost_bps = DEFAULT_COST_BPS if a.cost_bps is None else float(a.cost_bps)
    if cost_bps < 0:
        raise PaperError("--cost-bps wants a number at or above 0")
    session, source = session_bars(sym, date_str, now, a.offline, a.cache_root)
    if k >= len(session):
        if start + BAR > now_et:
            raise PaperError("waiting for the %s bar (it closes at %s New York; it is %s)"
                             % (hhmm(start), hhmm(start + BAR), hhmm(now_et)))
        if not session:
            raise PaperError("no %s bars for %s in the chart yet (Yahoo can lag): try again in a minute"
                             % (sym, date_str))
        raise PaperError("the %s bar of %s is not in the chart yet (Yahoo can lag; the chart stops at the %s bar): "
                         "try again in a minute" % (hhmm(start), sym, hhmm(clock.slot_time(date_str, len(session) - 1))))
    bar = session[k]
    open_px = float(bar["o"])
    fill = buy_fill(open_px, cost_bps)
    slip = round(fill - open_px, 4)
    book = load_book(a.book)
    n = sum(1 for p in book["positions"] if p.get("date") == date_str) + 1
    pid = "paper-%s-%d" % (date_str, n)
    ticket = ticket_path(a.ticket)
    run_ticket(ticket, ["result", "--index", a.index, "--status", "placed", "--broker-id", pid,
                        "--filled-qty", qty, "--filled-price", _p(fill), "--now", clock.iso(now),
                        "--orders", a.orders, "--state", a.state, "--risk", a.risk, "--config", a.config,
                        "--portfolio", a.portfolio])
    pos = {"id": pid, "symbol": sym, "side": "buy", "qty": qty, "entry": fill, "entry_open": round(open_px, 4),
           "slippage": slip, "cost_bps": cost_bps, "entry_bar_start": hhmm(start), "entry_slot": k,
           "entry_time": hhmm(start + BAR), "stop": bars.half_up(stop), "target": bars.half_up(target),
           "risk_per_share": round(fill - bars.half_up(stop), 2), "flat_by": flat_by, "date": date_str,
           "status": "open", "index": a.index, "fingerprint": fp, "approved_at": rec.get("at"),
           "placed_at": clock.iso(now), "source": source, "note": SIM}
    if pos["risk_per_share"] <= 0:
        pos["warning"] = "the fill is at or under the stop: the next bar decides it"
    book["positions"].append(pos)
    save_book(a.book, book)
    if a.json:
        print(json.dumps({"placed": pos, "book": a.book, "note": SIM}, indent=2))
        return 0
    print("Placed %s on paper: %s %d at %s, the %s bar's open %s plus %s slippage (%s bps round trip, half each way). "
          "%s." % (pid, sym, qty, _p(fill), hhmm(start), _p(open_px), _p(slip), ("%g" % cost_bps), SIM.capitalize()))
    print("  Stop %s, target %s, risk %s a share, %s at the stop. Flat by %s New York."
          % (_p(pos["stop"]), _p(pos["target"]), _p(pos["risk_per_share"]), _money(pos["risk_per_share"] * qty),
             flat_by))
    print("  Recorded through ticket.py result (broker log %s, portfolio %s) and opened in %s."
          % (a.state, a.portfolio, a.book))
    if pos.get("warning"):
        print("  Note: " + pos["warning"] + ".")
    print("Next: paper.py sync after each bar (or at the close) walks the stop, the target and the flat-by time. "
          "A paper fill proves the chain, not the trade: most people who day trade lose money.")
    return 0


def cmd_sync(a):
    now = parse_now(a.now)
    now_et = clock.to_eastern(now)
    cfg = read_config(a.config)
    book = load_book(a.book)
    open_ = [p for p in book["positions"] if p.get("status") == "open"]
    if not open_:
        if a.json:
            print(json.dumps({"synced": [], "open": 0, "note": SIM}, indent=2))
        else:
            print("No open paper positions in %s. Nothing to sync." % a.book)
        return 0
    ticket = ticket_path(a.ticket)
    results, failed, lines = [], False, []
    for pos in open_:
        sym, date_str = str(pos.get("symbol") or "").upper(), str(pos.get("date") or "")
        cost_bps = float(a.cost_bps) if a.cost_bps is not None else float(pos.get("cost_bps", DEFAULT_COST_BPS))
        flat_by = str(pos.get("flat_by") or cfg.get("flat_by") or "15:55")
        try:
            flat_k = clock.flat_slot(date_str, {"flat_by": flat_by})
        except (clock.ClockError, ValueError) as e:
            raise PaperError("position %s: %s" % (pos.get("id"), e))
        session, source = session_bars(sym, date_str, now, a.offline, a.cache_root)
        res = settle(session, pos, flat_k, cost_bps)
        if res is None:
            last = hhmm(clock.slot_time(date_str, len(session) - 1)) + " bar" if session else "no bar yet"
            lines.append("%s  %s %d at %s: still open after the %s (%s New York now), stop %s, target %s, flat by %s  %s"
                         % (pos["id"], sym, int(pos["qty"]), _p(pos["entry"]), last, hhmm(now_et), _p(pos["stop"]),
                            _p(pos["target"]), flat_by, SIM))
            results.append({"id": pos["id"], "status": "open", "last_bar": last, "source": source})
            continue
        exit_utc = clock.utc_of(session[res["slot"]]["t"]) + BAR
        recorded, error = True, None
        try:
            run_ticket(ticket, ["fill", "--symbol", sym, "--side", "sell", "--qty", int(pos["qty"]), "--price",
                                _p(res["exit"]), "--date", date_str, "--now", clock.iso(exit_utc),
                                "--portfolio", a.portfolio, "--config", a.config, "--state", a.state])
        except PaperError as e:
            recorded, error, failed = False, str(e), True
        close_position(pos, res, date_str, recorded, error)
        pos["exit_source"] = source
        lines.append(describe_closed(pos))
        results.append({"id": pos["id"], "status": "closed", "exit_reason": pos["exit_reason"], "exit": pos["exit"],
                        "R": pos["R"], "pnl_usd": pos["pnl_usd"], "fill_recorded": recorded, "source": source})
    save_book(a.book, book)
    if a.json:
        print(json.dumps({"synced": results, "book": a.book, "note": SIM}, indent=2))
    else:
        print("Paper sync at %s New York, fills %s (stop before target inside a bar):" % (hhmm(now_et), SIM))
        for ln in lines:
            print("  " + ln)
        if any(r["status"] == "closed" and r["fill_recorded"] for r in results):
            print("Exits were recorded through ticket.py fill (%s, %s)." % (a.portfolio, a.state))
        if failed:
            print("A fill could not be recorded: fix the line above and record it with ticket.py fill by hand.")
        print("Most people who day trade lose money; a paper record does not change that.")
    return 1 if failed else 0


def cmd_status(a):
    book = load_book(a.book)
    opened = [p for p in book["positions"] if p.get("status") == "open"]
    closed = [p for p in book["positions"] if p.get("status") == "closed"]
    s = summary(book)
    if a.json:
        print(json.dumps({"book": a.book, "summary": s, "positions": book["positions"], "note": SIM}, indent=2))
        return 0
    print("Paper book %s: every fill %s." % (a.book, SIM))
    if not book["positions"]:
        print("Empty. paper.py place --index N fills an order you already said yes to.")
        return 0
    print("Open (%d):" % len(opened))
    for p in opened:
        print("  " + describe_open(p))
    if not opened:
        print("  none")
    print("Closed (%d):" % len(closed))
    for p in closed:
        print("  " + describe_closed(p))
    if not closed:
        print("  none")
    for ln in summary_lines(s):
        print(ln)
    print("Most people who day trade lose money; a paper record does not change that.")
    return 0


def cmd_cancel(a):
    book = load_book(a.book)
    pos = None
    for p in book["positions"]:
        if str(p.get("id")) == str(a.id):
            pos = p
    if pos is None:
        raise PaperError("no position %s in %s" % (a.id, a.book))
    if pos.get("status") != "open":
        raise PaperError("%s is already %s%s: nothing to cancel"
                         % (a.id, pos.get("status"), (" (" + str(pos.get("exit_reason")) + ")") if pos.get("exit_reason") else ""))
    pos.update({"status": "closed", "exit_reason": "cancelled", "exit": None, "R": None, "pnl_usd": None,
                "fill_recorded": False, "cancelled_at": now_iso(), "cancel_note": a.note or ""})
    save_book(a.book, book)
    if a.json:
        print(json.dumps({"cancelled": pos, "book": a.book}, indent=2))
        return 0
    print("Cancelled %s: %s %d at %s marked closed without a fill, because you asked."
          % (pos["id"], pos["symbol"], int(pos["qty"]), _p(pos["entry"])))
    print("Nothing was recorded in the broker log or the portfolio: if %s still holds the shares, record the real "
          "exit with ticket.py fill --symbol %s --side sell --qty %d --price P." % (a.portfolio, pos["symbol"], int(pos["qty"])))
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper broker (trader-paper): fills one approved bracket from 5-minute "
                                             "bars. Never talks to a broker.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--book", default=DEFAULT_BOOK, help="the paper book (default trader/paper/positions.json)")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--state", default="trader/state/broker.json", help="trader-broker's log")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--ticket", metavar="PATH", help="trader-broker/ticket.py (default: next to this skill)")
        p.add_argument("--now", help="pretend it is this New York time, e.g. \"2026-10-19 10:25\"")
        p.add_argument("--json", action="store_true")

    def bars_flags(p):
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json/.json.gz) or a folder "
                                                          "of <SYM>.json, instead of Yahoo")
        p.add_argument("--cost-bps", type=float, default=None,
                       help="round-trip cost in basis points (default 4): half on the buy, half on a market sell")
        p.add_argument("--cache-root", default=os.path.join("trader", "cache"),
                       help="the bar store, read for a past day's session (default trader/cache)")

    p = sub.add_parser("place", help="fill the entry of an order the user already said yes to")
    p.add_argument("--index", type=int, required=True, help="the order's number in the orders file")
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--risk", default="trader/state/risk.json")
    common(p)
    bars_flags(p)
    p = sub.add_parser("sync", help="walk every open position's later bars: stop, target, flat by")
    common(p)
    bars_flags(p)
    p = sub.add_parser("status", help="the book, open and closed")
    common(p)
    p = sub.add_parser("cancel", help="mark an open position closed without a fill (when the user asks)")
    p.add_argument("--id", required=True, help="the position id, e.g. paper-2026-10-19-1")
    p.add_argument("--note", help="why, in the user's words")
    common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"place": cmd_place, "sync": cmd_sync, "status": cmd_status, "cancel": cmd_cancel}[a.cmd](a)
    except PaperError as e:
        print("paper.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
