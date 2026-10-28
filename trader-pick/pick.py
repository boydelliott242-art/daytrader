#!/usr/bin/env python3
"""The 8:25 pick (trader-pick): one large cap a morning from what is known by 8:05 ET, with its measured record printed
beside the base rates.

    python3 pick.py train [--rule up|net|ret] [--workers N] [--publish] [--no-pit]   # the walk-forward + the live model
    python3 pick.py morning [--date D] [--at 08:05] [--write] [--publish] [--no-premarket] [--replace]
    python3 pick.py grade [--write] [--publish]                            # settle past picks from the daily store
    python3 pick.py report                                                  # every rule's record by half, the lifts, the live record

Every number is counted, none is predicted: every scored morning in the record was ranked by a model fit only on the
mornings before it. The card prints the record of past picks (up AND down, with intervals) beside the base rates and
says "likely to MOVE 2 percent" unless the rule's out-of-sample pick closed up 2 percent more often than down with
separated intervals in both halves, in which case it says "likely to close 2 percent ABOVE its open". A pick is not a
buy. Nothing here places, sizes or approves an order. Most people who day trade lose money.
"""

import argparse
import datetime
import glob
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from dt import pick as dtpick, bars as dtbars, clock, io as dtio, net, intel_flags, stats as dtstats  # noqa: E402

MODEL = os.path.join("trader", "pick", "model.json")
RECORD = os.path.join("trader", "pick", "record.jsonl")
SHADOW = os.path.join("trader", "pick", "shadow.jsonl")   # the highest-ATR name each morning, a lead tracked forward, never the pick
OUT = os.path.join("trader", "pick.json")
NQ_TAGS = (os.path.join("trader", "nq", "session_tags_0805.json"), os.path.join("trader", "nq", "session_tags.json"))
LATE = datetime.time(9, 25)          # a card written after this on its own date is posted marked late and never counted
STALE_SHARE = 0.05                   # more than this share of names a session behind: the store is stale
WORDS = ("A pick is a count over past mornings, not a prediction: its odds are the record of past picks, printed beside the "
         "base rates. It is not a buy. Most people who day trade lose money.")


class Error(Exception):
    pass


def say(msg):
    print(msg)


def _pct(x, d=1):
    return "n/a" if x is None else ("%." + str(d) + "f %%") % (100 * x)


def _ci(ci, d=0):
    if not ci or ci[0] is None or ci[1] is None:
        return ""
    return " [%s, %s]" % (("%." + str(d) + "f") % (100 * ci[0]), ("%." + str(d) + "f") % (100 * ci[1]))


# ---------------------------------------------------------------- the universe
def universe(a):
    """The names the morning scores: tonight's large-cap list (or --symbols)."""
    lc = dtio.read_json(a.largecap, None) or {}
    syms = [r["symbol"] for r in lc.get("symbols") or [] if isinstance(r, dict) and r.get("symbol")]
    if not syms:
        root = os.path.join(a.cache_root, "daily")
        syms = sorted(os.path.splitext(os.path.splitext(f)[0])[0] for f in os.listdir(root) if f.endswith(".json.gz"))
    syms = [s for s in syms if s not in dtpick.CONTEXT and not s.startswith("^") and not s.startswith("_")]
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    return syms


def list_docs(a):
    """Tonight's list and every dated copy in trader/largecap_history (scripts/fetch_cache.py keeps one an evening)."""
    docs = []
    hist = os.path.join(os.path.dirname(a.largecap) or ".", "largecap_history")
    for path in sorted(glob.glob(os.path.join(hist, "*.json"))):
        d = dtio.read_json(path, None)
        if isinstance(d, dict) and d.get("symbols"):
            docs.append(d)
    cur = dtio.read_json(a.largecap, None)
    if isinstance(cur, dict) and cur.get("symbols"):
        docs.append(cur)
    return docs


def train_universe(a):
    """-> (symbols, caps, docs): the names the record may count (tonight's list plus every name a kept list ever held, so
    a name that left keeps its history) and each name's cap for the point-in-time rule ({} with --no-pit)."""
    live = universe(a)
    docs = [] if a.no_pit else list_docs(a)
    caps = dtpick.universe_caps(docs) if docs else {}
    if a.symbols:
        return live, caps, docs
    syms = list(live)
    for s in sorted(caps):
        if s not in syms and s not in dtpick.CONTEXT:
            syms.append(s)
    return syms, caps, docs


def nq_tags():
    for path in NQ_TAGS:
        doc = dtio.read_json(path, None)
        if doc and doc.get("sessions"):
            return doc, path
    return None, None


# ---------------------------------------------------------------- the claim the card may make
def claim_of(wf, rule):
    """'up' only when the rule's pick closed +2 % more often than -2 % with separated intervals in BOTH halves; else 'move'."""
    r = ((wf or {}).get("rules") or {}).get(rule) or {}
    halves = [(r.get("pick") or {}).get(h) or {} for h in ("h1", "h2")]
    if all(h.get("n") and h["up_ci"][0] > h["dn_ci"][1] for h in halves):
        return "up"
    return "move"


def direction_kind(wf, rule):
    """'up', 'down' or 'none': the same test as direction_words, for the card's second sentence."""
    r = ((wf or {}).get("rules") or {}).get(rule) or {}
    p = (r.get("pick") or {}).get("all") or {}
    if not p.get("n"):
        return "none"
    if claim_of(wf, rule) == "up":
        return "up"
    if p["dn_ci"][0] > p["up_ci"][1]:
        return "down"
    return "none"


def direction_words(wf, rule):
    r = ((wf or {}).get("rules") or {}).get(rule) or {}
    p = (r.get("pick") or {}).get("all") or {}
    if not p.get("n"):
        return "no out-of-sample record yet"
    a = "the pick closed 2 %% or more above its open on %s [%s, %s] of %d mornings and 2 %% or more below on %s [%s, %s]" % (
        _pct(p["up_rate"], 0), _pct(p["up_ci"][0], 0), _pct(p["up_ci"][1], 0), p["n"], _pct(p["dn_rate"], 0), _pct(p["dn_ci"][0], 0), _pct(p["dn_ci"][1], 0))
    kind = direction_kind(wf, rule)
    if kind == "up":
        return "direction has a measured edge in both halves: " + a
    if kind == "down":
        return "it fell 2 % or more MORE often than it rose: " + a
    return "direction has no measured edge: " + a


def record_fields(model, rule):
    """What every card carries about the record, from the model's walk-forward: the claim, the direction, the rule's
    pick and top five by half, the base rates and the highest-ATR name, every rule tested, how often the floor bound."""
    wf = model.get("walk_forward") or {}
    rules = wf.get("rules") or {}
    rr = rules.get(rule) or {}
    return {"rule": rule, "claim": claim_of(wf, rule), "direction": direction_words(wf, rule), "direction_kind": direction_kind(wf, rule),
            "configs_tested": len(rules), "rules_tested": sorted(rules),
            "base_up": round(model["base_up"], 4) if model.get("base_up") is not None else None,
            "base_dn": round(model["base_dn"], 4) if model.get("base_dn") is not None else None,
            "oos": {"first": wf.get("oos_first"), "last": wf.get("oos_last"), "sessions": wf.get("oos_sessions"), "h2_first": wf.get("h2_first"),
                    "pick": rr.get("pick") or {}, "top5": rr.get("top5") or {}, "base": wf.get("base"), "highest_atr": wf.get("baseline_highest_atr"),
                    "no_pick_days": rr.get("no_pick_days")},
            "oos_rules": {k: {"pick": v.get("pick"), "top5": v.get("top5"), "no_pick_days": v.get("no_pick_days")} for k, v in rules.items()},
            "universe_rule": (model.get("universe") or {}).get("rule"),
            "model": {"trained_at": model.get("trained_at"), "sessions_last": model.get("sessions_last"), "rows": model.get("rows"),
                      "nq_tags": model.get("nq_tags"), "extra": (model.get("spec") or {}).get("extra")}}


def left_out_heading(model):
    """The record's own exclusions, from the switches the model was trained with."""
    parts = []
    if model.get("exclude_earnings", True):
        parts.append("earnings sessions")
    if model.get("special_move_pct") is not None:
        parts.append("special situations")
    if (model.get("universe") or {}).get("min_cap_usd"):
        parts.append("sessions before a name was a large cap")
    parts.append("half days")
    return ", ".join(parts[:-1]) + " and " + parts[-1] + " left out"


def no_pick_words(model, rule, floor_x, reason=None):
    if reason:
        return "No pick today: %s. Nothing is logged." % reason
    wf = model.get("walk_forward") or {}
    rr = (wf.get("rules") or {}).get(rule) or {}
    n, nd = wf.get("oos_sessions"), rr.get("no_pick_days")
    s = "No pick: nothing cleared %s." % dtpick.floor_words(rule, model, floor_x)
    if n:
        s += (" The floor bound on none of the %d mornings in the record, so a morning like this points at the inputs (the store, the "
              "model, the calendar)." % n) if not nd else " That happened on %d of the %d mornings in the record." % (nd, n)
    return s + " Nothing is logged."


# ---------------------------------------------------------------- train
def cmd_train(a):
    syms, caps, docs = train_universe(a)
    min_cap = a.min_cap if caps else None
    tags, tags_path = nq_tags()
    spec = dtpick.base_spec(a.extra) if a.extra else dtpick.base_spec()
    say("The pick study: %d symbols%s, the daily store at %s%s, %d worker%s, rule %s." % (
        len(syms), (" (%d dated lists; a session counts while the name was a large cap)" % len(docs)) if caps else "", a.cache_root,
        (", NQ gap at 8:05 ET from %s" % tags_path) if tags else ", no NQ overnight file (the nq_gap bucket is na)", a.workers,
        "" if a.workers == 1 else "s", a.rule))
    rows, dates, market, info = dtpick.build_rows(a.cache_root, syms, a.workers, tags, spec, not a.keep_earnings, not a.keep_special,
                                                  caps or None, min_cap)
    if len(dates) < a.min_train + 20:
        raise Error("only %d sessions: the walk-forward needs %d training sessions and more to score" % (len(dates), a.min_train))
    wf = dtpick.walk_forward(rows, dates, spec, a.min_train, a.refit_every, a.min_adv, a.floor_x)
    model = dtpick.train(rows, None, spec)
    live = set(universe(a))
    uni = {"rule": ("a session counts only while the name's estimated cap at the prior close was at least $%.0fB (its cap at the newest "
                    "list holding it x the close then / the price at that list); names that left the list keep their history" % (min_cap / 1e9))
                   if caps else "tonight's large-cap list applied to every session (no point-in-time rule)",
           "symbols": len(syms), "lists": [((d.get("as_of") or "")[:10]) for d in docs], "min_cap_usd": min_cap,
           "departed": sorted(s for s in caps if s not in live and s not in dtpick.CONTEXT) if caps else []}
    model.update({"created_by": "trader-pick", "trained_at": clock.iso(clock.utc_now()), "cache_root": a.cache_root, "symbols": len(syms),
                  "rows": len(rows), "sessions_first": dates[0], "sessions_last": dates[-1], "target_pct": dtpick.TARGET_PCT,
                  "rule": a.rule, "min_adv_usd": a.min_adv, "floor_x": a.floor_x, "nq_tags": tags_path, "earnings": info, "universe": uni,
                  "exclude_earnings": not a.keep_earnings, "special_move_pct": info.get("special_move_pct"),
                  "claim": claim_of(wf, a.rule), "walk_forward": wf, "note": WORDS})
    dtio.write_json(a.model, model)
    say("Wrote %s: %s rows, %d sessions %s to %s; base rates %s up, %s down." % (
        a.model, "{:,}".format(len(rows)), len(dates), dates[0], dates[-1], _pct(model["base_up"]), _pct(model["base_dn"])))
    say("Left out: %d earnings sessions (%d of them the session after a report; the calendar is on disk for %d days), %d special "
        "situations (%s), %d sessions before the name was a large cap, %d half-day sessions." % (
            info["earnings_dropped"], info.get("reaction_dropped") or 0, info["earnings_dates_on_disk"], info.get("special_dropped") or 0,
            ("a move of %g %% or more the session before" % info["special_move_pct"]) if info.get("special_move_pct") else "kept: --keep-special",
            info.get("small_dropped") or 0, info.get("half_day_dropped") or 0))
    if uni["departed"]:
        say("Names that have left the list but count while they were large caps: %s." % ", ".join(uni["departed"]))
    print_record(wf, model)
    say("The card's claim for rule %s: %s." % (a.rule, "more likely to close 2 % ABOVE its open than below it" if model["claim"] == "up" else "likely to MOVE 2 % (direction not proven)"))
    if a.publish:
        publish(a, None)
    return 0


def print_record(wf, model=None):
    say("Out of sample, %s to %s (%d mornings; the second half from %s), %s; %d rules tested:" % (
        wf["oos_first"], wf["oos_last"], wf["oos_sessions"], wf["h2_first"], left_out_heading(model or {}), len(wf.get("rules") or {})))
    for seg in ("h1", "h2"):
        b, t = wf["base"][seg], wf["baseline_highest_atr"][seg]
        say("  %s  any large cap: up %s%s, down %s%s | the highest-ATR name: up %s%s, down %s%s, average %+.2f %%" % (
            seg, _pct(b["up_rate"]), _ci(b.get("up_ci"), 1), _pct(b["dn_rate"]), _ci(b.get("dn_ci"), 1), _pct(t["up_rate"]), _ci(t.get("up_ci")),
            _pct(t["dn_rate"]), _ci(t.get("dn_ci")), t["mean_oc"]))
    for rule, r in wf["rules"].items():
        for seg in ("h1", "h2"):
            p = r["pick"][seg]
            if not p.get("n"):
                say("  %-4s %s  no picks" % (rule, seg))
                continue
            say("  %-4s %s  pick: up %s%s, down %s%s, 2 to 5 %% up %s%s, average %+.2f %% [%+.2f, %+.2f] over %d" % (
                rule, seg, _pct(p["up_rate"]), _ci(p["up_ci"]), _pct(p["dn_rate"]), _ci(p["dn_ci"]),
                _pct(p.get("band_rate")), _ci(p.get("band_ci")), p["mean_oc"], p["mean_oc_ci"][0] or 0, p["mean_oc_ci"][1] or 0, p["n"]))


# ---------------------------------------------------------------- morning: inputs
def premarket_quotes(symbols, date, cutoff_et, pause=0.25, say_=None):
    """{SYM: {"last", "time", "vs_prev_close_pct", "prev_close"}} from Yahoo's chart with pre-market bars: the close of the
    last 5-minute bar of the date that ENDED by the cutoff (a bar is stamped with its start), and its end time."""
    out = {}
    for i, sym in enumerate(symbols):
        try:
            raw = net.yahoo_get(net.chart_path(sym, "1d", "5m") + "&includePrePost=true")
            doc = json.loads(raw) if isinstance(raw, str) else raw
            ser = net.series_from_result(doc["chart"]["result"][0])
        except Exception as e:  # one name's quote must not stop the pick
            out[sym] = {"error": str(e)[:80]}
            continue
        meta = ser.get("meta") or {}
        pc = meta.get("chartPreviousClose") or meta.get("previousClose")
        last, when = None, None
        for t, c in zip(ser["t"], ser["c"]):
            if c is None:
                continue
            et = clock.to_eastern(clock.utc_of(t))
            end = et + datetime.timedelta(minutes=5)
            if et.date().isoformat() == date and end.time() <= cutoff_et and end.date() == et.date():
                last, when = float(c), end.strftime("%H:%M")
        out[sym] = {"last": last, "time": when, "prev_close": pc, "vs_prev_close_pct": round((last / pc - 1) * 100.0, 2) if (last and pc) else None}
        if pause and i + 1 < len(symbols):
            net.SLEEP(pause)
        if say_ and (i + 1) % 10 == 0:
            say_("  pre-market %d/%d" % (i + 1, len(symbols)))
    return out


def _calendar(a, date):
    try:
        doc = dtio.read_json(os.path.join(a.cache_root, "earnings", date + ".json"), None)
    except dtio.IOError_:                          # a broken file is no file: the source words say what stood in
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("rows"), list) else None


def _cal_syms(doc):
    return set(str(r.get("symbol") or "").upper().replace("/", ".") for r in doc["rows"] if isinstance(r, dict) and r.get("symbol"))


def reporting_today(a, date):
    """Who reports today: the Nasdaq calendar file for the date (the backtest's own source). A file with no rows on a
    trading day is an answer that failed, not a quiet day: then the intel ledger, and the card says so."""
    doc = _calendar(a, date)
    if doc is not None and doc["rows"]:
        return _cal_syms(doc), "Nasdaq calendar for %s" % date
    why = ("the Nasdaq calendar for %s came back empty" % date) if doc is not None else ("no Nasdaq calendar file for %s" % date)
    ledger = intel_flags.load_ledger(a.intel) if os.path.exists(a.intel) else None
    if ledger:
        return set(s for s, rec in intel_flags.derive_earnings(ledger.get("items") or []).items() if rec.get("date") == date), why + "; the intel ledger instead"
    return set(), why + "; nobody left out for reporting today"


def reacting_today(a, date):
    """Who reacts today: every name on the calendars from the previous session through yesterday (the previous session's
    reports, and any Saturday or holiday report), the backtest's "reaction" sessions (dt.pick.earnings_session)."""
    d = datetime.date.fromisoformat(date)
    p = intel_flags.prev_trading_day(d)
    days = [(p + datetime.timedelta(days=k)).isoformat() for k in range((d - p).days)]
    syms, notes = set(), []
    for day in days:
        doc = _calendar(a, day)
        if doc is None or (day == p.isoformat() and not doc["rows"]):
            if day == p.isoformat():
                notes.append("no calendar rows for %s: the names that reported then are not left out" % day)
            continue
        syms |= _cal_syms(doc)
    words = "Nasdaq calendars for %s" % (days[0] if len(days) == 1 else "%s to %s" % (days[0], days[-1]))
    return syms, words + ("; " + "; ".join(notes) if notes else "")


def reasons_of(raw, market):
    r = raw or {}
    out = []
    if r.get("atr_pct") is not None:
        out.append("moves %.1f %% a day on average (ATR)" % r["atr_pct"])
    if r.get("ret_1d") is not None:
        out.append("yesterday %+.1f %% on %.1fx its volume, closing at %.0f %% of its range" % (r["ret_1d"], r.get("relvol_1d") or 0, 100 * (r.get("close_pos_1d") or 0)))
    if r.get("ret_5d") is not None and r.get("ret_20d") is not None:
        out.append("5 days %+.1f %%, 20 days %+.1f %%" % (r["ret_5d"], r["ret_20d"]))
    if r.get("sym_hit_60") is not None:
        out.append("its own +2 %% days: %.0f %% of the last 60; its -2 %% days: %.0f %%" % (100 * r["sym_hit_60"], 100 * (r.get("sym_dn_60") or 0)))
    if r.get("rsi_14") is not None:
        out.append("RSI %.0f" % r["rsi_14"])
    return out


def records(path):
    """The rows of a JSONL record. A line that does not parse is skipped with a warning: one bad line must not stop every
    morning (or the failure card) from here on."""
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for k, ln in enumerate(fh, 1):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                print("pick.py: skipped line %d of %s: not valid JSON" % (k, path), file=sys.stderr)
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def logged_shadow(a, date):
    rows = [r for r in records(a.shadow_record) if r.get("date") == date and r.get("status") != "superseded"]
    return rows[-1] if rows else None


def logged_row(a, date):
    """The pick already logged for the date (not superseded), or None."""
    rows = [r for r in records(a.record) if r.get("date") == date and r.get("status") != "superseded"]
    return rows[-1] if rows else None


# ---------------------------------------------------------------- morning
def cmd_morning(a):
    try:
        return morning(a)
    except Exception as e:
        if a.write:
            fail_card(a, e)
        raise


def fail_card(a, e):
    """A morning that failed still posts a dated card, so the site says why instead of "Not yet" all day."""
    try:
        now_utc = clock.parse_now(a.now) if a.now else clock.utc_now()
        date = a.date or clock.to_eastern(now_utc).date().isoformat()
        if logged_row(a, date):
            print("pick.py: the morning failed, but %s's pick is already logged; its card stays up" % date, file=sys.stderr)
            return
        model = dtio.read_json(a.model, None) or {}
        rule = a.rule or model.get("rule") or dtpick.DEFAULT_RULE
        doc = record_fields(model, rule) if model.get("walk_forward") else {"rule": rule}
        doc.update({"created_by": "trader-pick", "date": date, "cutoff": a.at, "created_at": clock.iso(now_utc), "written_at": clock.iso(clock.utc_now()),
                    "pick": None, "no_pick": True, "failed": True, "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
                    "no_pick_words": "No pick today: the morning job failed (%s). Nothing was logged." % (str(e)[:160] or type(e).__name__),
                    "next_session": intel_flags.next_trading_day(date).isoformat(), "live": live_record(a), "note": WORDS})
        dtio.write_json(a.out, doc)
        if a.publish:
            publish(a, doc)
    except Exception:  # the failure card must never hide the failure itself
        traceback.print_exc()


def morning(a):
    model = dtio.read_json(a.model, None)
    if not model or "lift_up" not in model:
        raise Error("no model at %s: pick.py train first" % a.model)
    now_utc = clock.parse_now(a.now) if a.now else clock.utc_now()
    now_et = clock.to_eastern(now_utc)
    date = a.date or now_et.date().isoformat()
    cutoff = clock.parse_hhmm(a.at, "--at")
    rule = a.rule or model.get("rule") or dtpick.DEFAULT_RULE
    spec = model["spec"]
    real_now = clock.utc_now() if not a.now else now_utc
    real_et = clock.to_eastern(real_now)
    real_day = real_et.date().isoformat()
    # a run before its own cutoff (or for a later date) is a preview: shown, never logged or published, so a manual run at
    # 07:30 cannot stand in for the 08:08 job's pick
    early = real_day < date or (real_day == date and real_et.time() < cutoff)
    closed = not clock.trading_day(datetime.date.fromisoformat(date))[0]
    syms = universe(a)
    spy = dtbars.daily_rows("SPY", a.cache_root)
    vix = dtbars.daily_rows("^VIX", a.cache_root)
    before_spy = [r for r in spy if r[0] < date]
    last_row = before_spy[-1][0] if before_spy else None          # what the store knew before this morning
    prev = intel_flags.prev_trading_day(date).isoformat()
    fut = dtio.read_json(a.futures, None)
    fut = fut if isinstance(fut, dict) else {}
    fut_today = fut.get("date") == date
    nq = (fut.get("nq") or {}) if fut_today else {}
    nq_gap = nq.get("gap_pct") if isinstance(nq, dict) else None
    if nq_gap is not None:
        futures_words = "NQ %+.2f %% at %s ET" % (nq_gap, fut.get("cutoff") or "?")
    elif fut_today:
        futures_words = "no NQ read this morning (the futures job's NQ fetch failed)"
    else:
        futures_words = "no NQ read this morning (the futures file is from %s)" % (fut.get("date") or "nowhere")
    market = dtpick.market_for_today(spy, date, vix, nq_gap)
    if market is None:
        raise Error("not enough SPY history before %s" % date)
    graded = grade_pending(a, quiet=True) if a.write and not early else []
    excl_earn = model.get("exclude_earnings", True)
    reporting, rep_source = reporting_today(a, date) if excl_earn else (set(), "kept: the model was trained with --keep-earnings")
    reacting, react_source = reacting_today(a, date) if excl_earn else (set(), "kept: the model was trained with --keep-earnings")
    reacting -= reporting
    special_pct = model.get("special_move_pct")
    extra = dtpick.load_extra(spec["extra"]).compute if spec.get("extra") else None
    earnings_all = dtpick.load_earnings(a.cache_root) if extra else {}
    cands, raws, behind, with_rows = [], {}, [], 0
    for sym in syms:
        rows = dtbars.daily_rows(sym, a.cache_root)
        if not rows:
            continue
        with_rows += 1
        before = [r for r in rows if r[0] < date]
        if not before or before[-1][0] != prev:                # this name's store is not through the previous session
            behind.append(sym)
            continue
        raw, adv = dtpick.today_raw(sym, before, date, extra, {"cache_root": a.cache_root, "earnings": earnings_all.get(sym) or {}})
        if raw is None:
            continue
        raws[sym] = raw
        cands.append((sym, dtpick.encode(spec, raw, market), adv, None))
    store_stale = last_row != prev or len(behind) > STALE_SHARE * max(1, with_rows)
    half_day = date in dtpick.HALF_DAYS
    min_adv = a.min_adv or model.get("min_adv_usd", dtpick.MIN_ADV_USD)
    floor_x = a.floor_x or model.get("floor_x", dtpick.PICK_FLOOR_X)
    adv_of = {c[0]: c[2] for c in cands}
    special = {}
    if special_pct is not None:
        for sym, raw in raws.items():
            why = dtpick.special_of(raw, special_pct)
            if why and sym not in reporting and sym not in reacting and (adv_of.get(sym) or 0) >= min_adv:
                special[sym] = why
    exclude = set(reporting) | reacting | set(special) | set(s.strip().upper() for s in (a.exclude.split(",") if a.exclude else []) if s.strip())
    pick, ranked = dtpick.rank(model, cands, rule, min_adv, floor_x, exclude)
    reason = None
    if closed:
        reason = "the market is closed on %s" % date
    elif store_stale and last_row != prev:
        reason = ("the price store ends %s, not the previous session %s (the 17:30 refresh did not run), so the history would "
                  "describe the wrong morning" % (last_row, prev))
    elif store_stale:
        reason = ("%d of %d names are a session behind in the price store (the 17:30 refresh did not finish), so the history would "
                  "describe the wrong morning" % (len(behind), with_rows))
    elif half_day:
        reason = "a 1:00 p.m. close today; the record holds full sessions only"
    if reason:
        pick = None
    # a re-run never changes the pick already logged for the day (the 08:18 retry, a manual run): it keeps that name
    # unless --replace says otherwise, so the card and the record always name the same stock
    logged = logged_row(a, date)
    kept = None
    if logged and not a.replace and (pick or {}).get("symbol") != logged.get("symbol"):
        forced = [c for c in cands if c[0] == logged["symbol"]]
        if forced:
            pick = dtpick.score_candidates(model, forced)[0]
        else:
            pick = {"symbol": logged["symbol"], "p_up": logged.get("p_up"), "p_dn": logged.get("p_dn"), "net": (logged.get("p_up") or 0) - (logged.get("p_dn") or 0),
                    "e_oc": logged.get("e_oc") or 0.0, "s_up": None, "s_dn": None, "adv_usd": None, "payload": None}
        kept = "the pick logged for %s at %s was %s; this run kept it (pass --replace to post another)" % (date, logged.get("logged_at"), logged["symbol"])
        reason = None
    # the shadow: the most volatile eligible liquid name, the one lead the rule research left (it touched +2 % more often in
    # both halves, but the second half's margin was inside the noise); tracked forward, graded each evening, never posted as
    # the pick, and joining the model only through scripts/pick_research.py
    shadow = None
    if not reason:
        pool = [c for c in cands if c[0] not in exclude and c[0] not in dtpick.CONTEXT and (c[2] or 0) >= min_adv
                and isinstance((raws.get(c[0]) or {}).get("atr_pct"), (int, float))]
        if pool:
            s_sym = max(pool, key=lambda c: (raws[c[0]]["atr_pct"], c[0]))[0]
            shadow = {"symbol": s_sym, "atr_pct": round(raws[s_sym]["atr_pct"], 3), "rule": "the highest-ATR eligible liquid name"}
    ls = logged_shadow(a, date)
    if ls and not a.replace:
        shadow = {"symbol": ls["symbol"], "atr_pct": ls.get("atr_pct"), "rule": "the highest-ATR eligible liquid name"}
    # the names after the pick, in the model's order (a kept pick is not ranked first, and never listed twice)
    top = [r for r in ranked if not pick or r["symbol"] != pick["symbol"]][:a.top]
    quote_syms = ([pick["symbol"]] if pick else []) + [r["symbol"] for r in top if not pick or r["symbol"] != pick["symbol"]]
    quotes = {}
    if not a.no_premarket and quote_syms:
        quotes = premarket_quotes(quote_syms, date, cutoff, a.pause, None if a.quiet else (lambda m: print(m, file=sys.stderr)))
    late = (real_day > date or (real_day == date and real_et.time() > LATE)) and not logged
    wf = model.get("walk_forward") or {}
    t_rec = (wf.get("baseline_highest_atr") or {})
    p_rec = (((wf.get("rules") or {}).get(rule) or {}).get("pick") or {})
    if (t_rec.get("h1") or {}).get("n") and (p_rec.get("h1") or {}).get("n"):
        shadow_why = ("the most volatile eligible liquid name, tracked forward: in the record it touched +2 %% on %s and %s of mornings "
                      "in the two halves against the pick's %s and %s, and the rule research of 2026-10-27 found the second half's margin "
                      "inside the noise; not the pick" % (_pct(t_rec["h1"].get("touch_up_rate"), 0), _pct(t_rec["h2"].get("touch_up_rate"), 0),
                                                          _pct(p_rec["h1"].get("touch_up_rate"), 0), _pct(p_rec["h2"].get("touch_up_rate"), 0)))
    else:
        shadow_why = "the most volatile eligible liquid name, tracked forward; not the pick"

    def card(r):
        same = [x for x in ranked if x["p_up"] == r.get("p_up") and x["p_dn"] == r.get("p_dn")]
        return {"symbol": r["symbol"], "p_up": r.get("p_up"), "p_dn": r.get("p_dn"), "bin_shared": len(same),
                "net": round(r.get("net") or 0, 4), "e_oc": round(r.get("e_oc") or 0, 4), "adv_usd": r.get("adv_usd"),
                "premarket": quotes.get(r["symbol"]), "reasons": reasons_of(raws.get(r["symbol"]), market),
                "raw": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in (raws.get(r["symbol"]) or {}).items()}}
    doc = record_fields(model, rule)
    doc.update({"created_by": "trader-pick", "as_of": "%s %s ET" % (date, a.at), "created_at": clock.iso(now_utc), "written_at": clock.iso(real_now),
                "date": date, "cutoff": a.at, "target": "close at least %g %% above (or below) the open" % dtpick.TARGET_PCT,
                "band": [dtpick.TARGET_PCT, dtpick.BAND_HI_PCT], "store_through": last_row, "store_stale": store_stale,
                "behind_today": len(behind), "behind_names": sorted(behind)[:12], "half_day": half_day, "market": market,
                "futures_date": fut.get("date"), "futures_at": fut.get("cutoff"), "nq_gap_pct": nq_gap, "futures_words": futures_words,
                "floor": dtpick.floor_words(rule, model, floor_x), "universe": len(syms), "scored": len(ranked),
                "reporting_today": len(reporting & set(raws)), "reporting_source": rep_source,
                "reacting_today": len(reacting & set(raws)), "reacting_source": react_source,
                "special_today": [{"symbol": s, "reason": special[s]} for s in sorted(special)], "special_move_pct": special_pct,
                "pick": card(pick) if pick else None, "no_pick": pick is None, "kept_note": kept, "late": bool(late and pick),
                "ranked": [card(r) for r in top], "next_session": intel_flags.next_trading_day(date).isoformat(),
                "live": live_record(a), "graded_now": graded,
                "shadow": dict(shadow or {}, live=live_record(a, a.shadow_record), why=shadow_why), "preview": early,
                "premarket_note": "pre-market prices are Yahoo's, delayed and thin; they are shown, not scored: the model saw none of them in its history",
                "odds_note": ("the record's odds for the pick; the model's own top-bin rates are in-sample and shared by every name in that bin, so "
                              "they are not this name's odds"),
                "no_pick_words": no_pick_words(model, rule, floor_x, reason), "note": WORDS})
    if a.write and early:
        print("pick.py: a preview (the run is before %s ET on %s): nothing written, logged or published; the 08:08 job posts the pick" % (
            a.at, date), file=sys.stderr)
    elif a.write:
        dtio.write_json(a.out, doc)
        if pick and not doc["late"]:
            log_pick(a, doc)
            if shadow:
                log_shadow(a, doc, shadow)
        elif not pick and logged and a.replace:
            supersede_day(a, date, None, doc["written_at"])
        doc["live"] = live_record(a)
        doc["shadow"]["live"] = live_record(a, a.shadow_record)
        dtio.write_json(a.out, doc)
        if a.publish:
            publish(a, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print_pick(doc)
    return 0


def log_pick(a, doc):
    """Log the day's pick once. With --replace, an earlier different pick for the day is marked superseded: never graded,
    never counted."""
    path = a.record
    rows = records(path)
    p = doc["pick"]
    same_day = [r for r in rows if r.get("date") == doc["date"] and r.get("status") != "superseded"]
    if same_day and same_day[-1].get("symbol") == p["symbol"]:
        return False
    if same_day:
        for r in same_day:
            r.update({"status": "superseded", "superseded_by": p["symbol"], "superseded_at": doc["written_at"]})
        _rewrite(path, rows)
    dtio.append_jsonl(path, {"date": doc["date"], "symbol": p["symbol"], "rule": doc["rule"], "claim": doc["claim"], "p_up": p["p_up"], "p_dn": p["p_dn"],
                             "e_oc": p["e_oc"], "base_up": doc["base_up"], "base_dn": doc["base_dn"], "logged_at": doc["created_at"],
                             "written_at": doc["written_at"], "cutoff": doc["cutoff"], "store_through": doc["store_through"],
                             "futures_at": doc["futures_at"], "premarket": p.get("premarket"), "status": "pending", "oc_pct": None,
                             "cc_pct": None, "up": None, "dn": None, "created_by": "trader-pick"})
    return True


def supersede_day(a, date, by, when):
    """--replace that ends in NO PICK: the day's logged pick is marked superseded, never graded, never counted."""
    rows = records(a.record)
    hit = False
    for r in rows:
        if r.get("date") == date and r.get("status") != "superseded":
            r.update({"status": "superseded", "superseded_by": by, "superseded_at": when})
            hit = True
    if hit:
        _rewrite(a.record, rows)
    return hit


def log_shadow(a, doc, shadow):
    """Log the day's shadow name once (it is deterministic for a day's data; --replace logs again)."""
    rows = records(a.shadow_record)
    if any(r.get("date") == doc["date"] and r.get("status") != "superseded" for r in rows):
        if not a.replace:
            return False
        for r in rows:
            if r.get("date") == doc["date"] and r.get("status") != "superseded":
                r.update({"status": "superseded", "superseded_at": doc["written_at"]})
        _rewrite(a.shadow_record, rows)
    dtio.append_jsonl(a.shadow_record, {"date": doc["date"], "symbol": shadow["symbol"], "rule": "atr", "atr_pct": shadow["atr_pct"],
                                        "logged_at": doc["created_at"], "written_at": doc["written_at"], "status": "pending", "oc_pct": None,
                                        "up": None, "dn": None, "created_by": "trader-pick"})
    return True


def _rewrite(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def grade_pending(a, quiet=False):
    graded = grade_file(a, a.record)
    grade_file(a, a.shadow_record)
    if not quiet:
        say("Graded %d pick(s): %s" % (len(graded), ", ".join(graded) or "none pending with a session in the store"))
    return graded


def grade_file(a, path):
    rows = records(path)
    changed, graded = False, []
    for r in rows:
        if r.get("status") != "pending":
            continue
        daily = dtbars.daily_rows(r["symbol"], a.cache_root)
        row = next((x for x in daily if x[0] == r["date"]), None)
        if not row or not row[1] or not row[4]:
            continue
        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        oc = (c / o - 1) * 100.0
        prev = next((x for x in reversed(daily) if x[0] < r["date"]), None)
        r.update({"status": "graded", "open": o, "high": h, "low": l, "close": c, "oc_pct": round(oc, 3),
                  "cc_pct": round((c / float(prev[4]) - 1) * 100.0, 3) if prev and prev[4] else None,
                  "hi_pct": round((h / o - 1) * 100.0, 3), "lo_pct": round((l / o - 1) * 100.0, 3),
                  "up": 1 if oc >= dtpick.TARGET_PCT else 0, "dn": 1 if oc <= -dtpick.TARGET_PCT else 0,
                  "in_band": bool(dtpick.TARGET_PCT <= oc <= dtpick.BAND_HI_PCT), "graded_at": clock.iso(clock.utc_now())})
        graded.append(r["date"])
        changed = True
    if changed:
        _rewrite(path, rows)
    return graded


def live_record(a, path=None):
    rows = [r for r in records(path or a.record) if r.get("status") != "superseded"]
    g = [r for r in rows if r.get("status") == "graded"]
    n = len(g)
    up = sum(r.get("up") or 0 for r in g)
    dn = sum(r.get("dn") or 0 for r in g)
    ulo, uhi = dtstats.wilson(up, n) if n else (None, None)
    dlo, dhi = dtstats.wilson(dn, n) if n else (None, None)
    ocs = [r["oc_pct"] for r in g if r.get("oc_pct") is not None]
    words = ("%d graded: closed 2 %% up on %d (%s [%s, %s]), 2 %% down on %d (%s [%s, %s]), average %+.2f %%" % (
        n, up, _pct(up / float(n), 0), _pct(ulo, 0), _pct(uhi, 0), dn, _pct(dn / float(n), 0), _pct(dlo, 0), _pct(dhi, 0), sum(ocs) / len(ocs))) if n else "no graded pick yet"
    return {"picks": len(rows), "graded": n, "pending": len([r for r in rows if r.get("status") == "pending"]), "up": up, "dn": dn,
            "up_rate": round(up / float(n), 4) if n else None, "up_ci": [ulo, uhi], "dn_rate": round(dn / float(n), 4) if n else None, "dn_ci": [dlo, dhi],
            "mean_oc_pct": round(sum(ocs) / len(ocs), 3) if ocs else None, "in_band": sum(1 for r in g if r.get("in_band")),
            "small_sample": n < 30, "last": [{k: r.get(k) for k in ("date", "symbol", "oc_pct", "hi_pct", "lo_pct", "up", "dn", "status")} for r in rows[-10:]],
            "words": words}


def print_pick(doc):
    print("The 8:25 pick for %s (data as of %s ET; the store runs through %s%s; rule %s):" % (
        doc["date"], doc["cutoff"], doc.get("store_through"), "; STALE: the cache job has not run" if doc.get("store_stale") else "", doc["rule"]))
    m = doc.get("market") or {}
    print("  market before the open: SPY %+.2f %% yesterday, %+.2f %% over 5 days, trend %s, VIX %s, %s" % (
        m.get("spy_ret_1d") or 0, m.get("spy_ret_5d") or 0, m.get("spy_trend"), ("%.1f" % m["vix"]) if isinstance(m.get("vix"), (int, float)) else "n/a",
        doc.get("futures_words") or "no NQ read"))
    p = doc.get("pick")
    o = (((doc.get("oos") or {}).get("pick") or {}).get("all") or {})
    b = (((doc.get("oos") or {}).get("base") or {}).get("all") or {})
    t = (((doc.get("oos") or {}).get("highest_atr") or {}).get("all") or {})
    if not p:
        print("  NO PICK: " + doc["no_pick_words"])
    else:
        what = "more likely to close 2 % or more ABOVE its open than below it" if doc["claim"] == "up" else "likely to MOVE 2 % or more from its open"
        print("  %s: %s.%s" % (p["symbol"], what, "  (" + doc["kept_note"] + ")" if doc.get("kept_note") else ""))
        if o.get("n"):
            print("  its odds are the record of past picks (%d mornings): closed 2 %% up %s%s, 2 %% down %s%s; any large cap %s%s and %s%s; "
                  "the most volatile liquid name %s%s and %s%s." % (
                      o["n"], _pct(o["up_rate"], 0), _ci(o["up_ci"]), _pct(o["dn_rate"], 0), _ci(o["dn_ci"]), _pct(b.get("up_rate"), 0), _ci(b.get("up_ci")),
                      _pct(b.get("dn_rate"), 0), _ci(b.get("dn_ci")), _pct(t.get("up_rate"), 0), _ci(t.get("up_ci")), _pct(t.get("dn_rate"), 0), _ci(t.get("dn_ci"))))
        print("  the model's top bin this morning: %s up, %s down, shared by %d names (in-sample bin rates; the order among them comes from the raw score)" % (
            _pct(p.get("p_up"), 0), _pct(p.get("p_dn"), 0), p.get("bin_shared") or 1))
        print("  why: " + "; ".join(p.get("reasons") or []))
        pm = p.get("premarket") or {}
        if pm.get("last"):
            print("  pre-market: %.2f at %s ET, %+.2f %% vs the prior close %.2f (shown, not scored)" % (pm["last"], pm.get("time"), pm.get("vs_prev_close_pct") or 0, pm.get("prev_close") or 0))
        if doc.get("late"):
            print("  LATE: written after 9:25 ET on its date, so it is not logged or counted in the live record")
    print("  out of sample: " + doc["direction"] + ".")
    if o.get("n"):
        print("  intraday: the pick traded 2 %% above its open at some point on %s%s of those mornings, 2 %% below on %s%s; it closed 2 to 5 %% up on %s%s." % (
            _pct(o.get("touch_up_rate"), 0), _ci(o.get("touch_up_ci")), _pct(o.get("touch_dn_rate"), 0), _ci(o.get("touch_dn_ci")),
            _pct(o.get("band_rate"), 0), _ci(o.get("band_ci"))))
    print("  live record: %s%s." % ((doc.get("live") or {}).get("words"), " (small sample)" if (doc.get("live") or {}).get("small_sample") else ""))
    print("  left out, they report today: %d (%s)" % (doc.get("reporting_today") or 0, doc.get("reporting_source")))
    print("  left out, they react today to a report since the previous session: %d (%s)" % (doc.get("reacting_today") or 0, doc.get("reacting_source")))
    sp = doc.get("special_today") or []
    print("  left out, special situations: %s" % ("; ".join("%s %s" % (x["symbol"], x["reason"]) for x in sp) if sp else "none"))
    if doc.get("behind_today"):
        print("  left out, a session behind in the store: %d (%s)" % (doc["behind_today"], ", ".join(doc.get("behind_names") or [])))
    sh = doc.get("shadow") or {}
    if sh.get("symbol"):
        print("  shadow (not the pick): %s, the most volatile eligible liquid name (ATR %.1f %%); its live record: %s" % (
            sh["symbol"], sh.get("atr_pct") or 0, (sh.get("live") or {}).get("words")))
    print("  the next nine:")
    if doc.get("preview"):
        print("  PREVIEW: run before the cutoff; nothing was written, logged or published")
    for r in [x for x in (doc.get("ranked") or []) if not p or x["symbol"] != p["symbol"]][:9]:
        pm = r.get("premarket") or {}
        print("    %-6s ATR %.1f %%%s" % (r["symbol"], (r.get("raw") or {}).get("atr_pct") or 0,
                                      ("  pre-market %+.2f %% at %s" % (pm["vs_prev_close_pct"], pm.get("time"))) if pm.get("vs_prev_close_pct") is not None else ""))
    print("  " + doc["note"])


# ---------------------------------------------------------------- grade, report, publish
def cmd_grade(a):
    grade_pending(a)
    rec = live_record(a)
    say("Live record: %s%s." % (rec["words"], " (small sample)" if rec["small_sample"] else ""))
    if a.write and os.path.exists(a.out):
        doc = dtio.read_json(a.out, None) or {}
        doc["live"] = rec
        if isinstance(doc.get("shadow"), dict):
            doc["shadow"]["live"] = live_record(a, a.shadow_record)
        dtio.write_json(a.out, doc)
        if a.publish:
            publish(a, doc)
    return 0


def cmd_report(a):
    model = dtio.read_json(a.model, None)
    if not model:
        raise Error("no model at %s: pick.py train first" % a.model)
    say("Model trained %s on %s rows, %d symbols, sessions %s to %s; base rates %s up, %s down; rule %s; claim %s." % (
        model.get("trained_at"), "{:,}".format(model.get("rows") or 0), model.get("symbols") or 0, model.get("sessions_first"), model.get("sessions_last"),
        _pct(model["base_up"]), _pct(model["base_dn"]), model.get("rule"), model.get("claim")))
    if model.get("universe"):
        say("Universe: " + model["universe"]["rule"] + ".")
    print_record(model.get("walk_forward") or {}, model)
    say("Biggest lifts toward +2 % (feature: bucket lift):")
    spec = model["spec"]
    for f, lifts in zip(model["features"], model["lift_up"]):
        hi = max(range(len(lifts)), key=lambda k: lifts[k])
        lo = min(range(len(lifts)), key=lambda k: lifts[k])
        say("  %-16s %s %+.2f .. %s %+.2f" % (f, dtpick.label_of(spec, f, lo), lifts[lo], dtpick.label_of(spec, f, hi), lifts[hi]))
    rec = live_record(a)
    say("Live: " + rec["words"] + (" (small sample)" if rec["small_sample"] else ""))
    say(WORDS)
    return 0


def publish(a, today_doc):
    doc = today_doc or (dtio.read_json(a.out, None) or {})
    if not doc:
        model = dtio.read_json(a.model, None) or {}
        rule = model.get("rule") or dtpick.DEFAULT_RULE
        doc = record_fields(model, rule) if model.get("walk_forward") else {"rule": rule}
        today = clock.to_eastern(clock.utc_now()).date()
        nxt = today if clock.trading_day(today)[0] else intel_flags.next_trading_day(today)
        doc.update({"created_by": "trader-pick", "date": None, "no_pick": True, "pick": None, "live": live_record(a),
                    "next_session": nxt.isoformat(), "note": WORDS})
    os.makedirs(a.publish_dir, exist_ok=True)
    dtio.write_json(os.path.join(a.publish_dir, "pick.json"), doc)
    say("Published %s." % os.path.join(a.publish_dir, "pick.json"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--cache-root", default=os.path.join("trader", "cache"))
        p.add_argument("--largecap", default=os.path.join("trader", "largecap.json"))
        p.add_argument("--symbols")
        p.add_argument("--model", default=MODEL)
        p.add_argument("--record", default=RECORD)
        p.add_argument("--shadow-record", default=SHADOW)
        p.add_argument("--out", default=OUT)
        p.add_argument("--publish", action="store_true")
        p.add_argument("--publish-dir", default=os.path.join("trader", "publish"))
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("train"); common(p)
    p.add_argument("--rule", default=dtpick.DEFAULT_RULE, choices=dtpick.RULES)
    p.add_argument("--extra", help="a feature module (CUTS + compute) the research confirmed")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--min-train", type=int, default=dtpick.MIN_TRAIN_SESSIONS)
    p.add_argument("--refit-every", type=int, default=dtpick.REFIT_EVERY)
    p.add_argument("--min-adv", type=float, default=dtpick.MIN_ADV_USD)
    p.add_argument("--floor-x", type=float, default=dtpick.PICK_FLOOR_X)
    p.add_argument("--min-cap", type=float, default=dtpick.LARGE_CAP_USD, help="the point-in-time large-cap floor in dollars")
    p.add_argument("--no-pit", action="store_true", help="apply tonight's list to every session (no point-in-time rule)")
    p.add_argument("--keep-earnings", action="store_true")
    p.add_argument("--keep-special", action="store_true", help="keep the special situations (a 20 %% move the session before) in")
    p = sub.add_parser("morning"); common(p)
    p.add_argument("--date"); p.add_argument("--at", default="08:05"); p.add_argument("--now"); p.add_argument("--rule", choices=dtpick.RULES)
    p.add_argument("--futures", default=os.path.join("trader", "futures.json")); p.add_argument("--intel", default=os.path.join("trader", "intel.json"))
    p.add_argument("--exclude"); p.add_argument("--top", type=int, default=10); p.add_argument("--pause", type=float, default=0.25)
    p.add_argument("--min-adv", type=float); p.add_argument("--floor-x", type=float)
    p.add_argument("--no-premarket", action="store_true"); p.add_argument("--write", action="store_true")
    p.add_argument("--replace", action="store_true", help="post this run's pick even if another was already logged today (that one is marked superseded)")
    p = sub.add_parser("grade"); common(p); p.add_argument("--write", action="store_true")
    p = sub.add_parser("report"); common(p)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"train": cmd_train, "morning": cmd_morning, "grade": cmd_grade, "report": cmd_report}[a.cmd](a)
    except (Error, dtpick.PickError, dtio.IOError_, clock.ClockError) as e:
        print("pick.py: %s" % e, file=sys.stderr)
        return 1
    except Exception as e:  # anything else is a bug: say so plainly, with the trace, and fail
        traceback.print_exc()
        print("pick.py: unexpected %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
