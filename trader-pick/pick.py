#!/usr/bin/env python3
"""The 8:25 pick (trader-pick): one large cap a morning from what is known by 8:05 ET, with its measured record printed
beside the base rates.

    python3 pick.py train [--rule up|net|ret] [--workers N] [--publish]   # the walk-forward + the live model -> trader/pick/model.json
    python3 pick.py morning [--date D] [--at 08:05] [--write] [--publish] [--no-premarket]
    python3 pick.py grade [--write] [--publish]                            # settle past picks from the daily store
    python3 pick.py report                                                  # the out-of-sample record by rule and half, the lifts, the live record

Every number is counted, none is predicted: every scored morning in the record was ranked by a model fit only on the
mornings before it. The card says "most likely to MOVE 2 percent" unless the rule's out-of-sample record shows the pick
closing up 2 percent more often than down in both halves, in which case it says "most likely to close 2 percent ABOVE
its open". A pick is not a buy. Nothing here places, sizes or approves an order. Most people who day trade lose money.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from dt import pick as dtpick, bars as dtbars, clock, io as dtio, net, intel_flags, stats as dtstats  # noqa: E402

MODEL = os.path.join("trader", "pick", "model.json")
RECORD = os.path.join("trader", "pick", "record.jsonl")
OUT = os.path.join("trader", "pick.json")
NQ_TAGS = (os.path.join("trader", "nq", "session_tags_0805.json"), os.path.join("trader", "nq", "session_tags.json"))
WORDS = ("A pick is the name whose morning evidence carried the best measured odds; the odds are counts over past mornings, not a "
         "prediction, and the base rate is printed beside them. It is not a buy. Most people who day trade lose money.")


class Error(Exception):
    pass


def say(msg):
    print(msg)


def universe(a):
    lc = dtio.read_json(a.largecap, None) or {}
    syms = [r["symbol"] for r in lc.get("symbols") or [] if isinstance(r, dict) and r.get("symbol")]
    if not syms:
        root = os.path.join(a.cache_root, "daily")
        syms = sorted(os.path.splitext(os.path.splitext(f)[0])[0] for f in os.listdir(root) if f.endswith(".json.gz"))
    syms = [s for s in syms if s not in dtpick.CONTEXT and not s.startswith("^") and not s.startswith("_")]
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    return syms


def nq_tags():
    for path in NQ_TAGS:
        doc = dtio.read_json(path, None)
        if doc and doc.get("sessions"):
            return doc, path
    return None, None


def _pct(x, d=1):
    return "n/a" if x is None else ("%." + str(d) + "f %%") % (100 * x)


# ---------------------------------------------------------------- the claim the card may make
def claim_of(wf, rule):
    """'up' only when the rule's pick closed +2 % more often than -2 % with separated intervals in BOTH halves; else 'move'."""
    r = ((wf or {}).get("rules") or {}).get(rule) or {}
    halves = [(r.get("pick") or {}).get(h) or {} for h in ("h1", "h2")]
    if all(h.get("n") and h["up_ci"][0] > h["dn_ci"][1] for h in halves):
        return "up"
    return "move"


def direction_words(wf, rule):
    r = ((wf or {}).get("rules") or {}).get(rule) or {}
    p = (r.get("pick") or {}).get("all") or {}
    if not p.get("n"):
        return "no out-of-sample record yet"
    a = "the pick closed 2 %% or more above its open on %s [%s, %s] of %d mornings and 2 %% or more below on %s [%s, %s]" % (
        _pct(p["up_rate"], 0), _pct(p["up_ci"][0], 0), _pct(p["up_ci"][1], 0), p["n"], _pct(p["dn_rate"], 0), _pct(p["dn_ci"][0], 0), _pct(p["dn_ci"][1], 0))
    if claim_of(wf, rule) == "up":
        return "direction has a measured edge in both halves: " + a
    if p["dn_ci"][0] > p["up_ci"][1]:
        return "it fell 2 % or more MORE often than it rose: " + a
    return "direction has no measured edge: " + a


# ---------------------------------------------------------------- train
def cmd_train(a):
    syms = universe(a)
    tags, tags_path = nq_tags()
    spec = dtpick.base_spec(a.extra) if a.extra else dtpick.base_spec()
    say("The pick study: %d symbols, the daily store at %s%s, %d worker%s, rule %s." % (len(syms), a.cache_root,
        (", NQ gap at 8:05 ET from %s" % tags_path) if tags else ", no NQ overnight file (the nq_gap bucket is na)", a.workers,
        "" if a.workers == 1 else "s", a.rule))
    rows, dates, market, info = dtpick.build_rows(a.cache_root, syms, a.workers, tags, spec, not a.keep_earnings, not a.keep_special)
    if len(dates) < a.min_train + 20:
        raise Error("only %d sessions: the walk-forward needs %d training sessions and more to score" % (len(dates), a.min_train))
    wf = dtpick.walk_forward(rows, dates, spec, a.min_train, a.refit_every, a.min_adv, a.floor_x)
    model = dtpick.train(rows, None, spec)
    model.update({"created_by": "trader-pick", "trained_at": clock.iso(clock.utc_now()), "cache_root": a.cache_root, "symbols": len(syms),
                  "rows": len(rows), "sessions_first": dates[0], "sessions_last": dates[-1], "target_pct": dtpick.TARGET_PCT,
                  "rule": a.rule, "min_adv_usd": a.min_adv, "floor_x": a.floor_x, "nq_tags": tags_path, "earnings": info,
                  "exclude_earnings": not a.keep_earnings, "special_move_pct": info.get("special_move_pct"),
                  "claim": claim_of(wf, a.rule), "walk_forward": wf, "note": WORDS})
    dtio.write_json(a.model, model)
    say("Wrote %s: %s rows, %d sessions %s to %s; base rates %s up, %s down." % (
        a.model, "{:,}".format(len(rows)), len(dates), dates[0], dates[-1], _pct(model["base_up"]), _pct(model["base_dn"])))
    say("Left out: %d earnings sessions (%d of them the session after a report made after the close or with no time; the calendar is on "
        "disk for %d dates) and %d special situations (%s)." % (
            info["earnings_dropped"], info.get("reaction_dropped") or 0, info["earnings_dates_on_disk"], info.get("special_dropped") or 0,
            ("a move of %g %% or more the session before" % info["special_move_pct"]) if info.get("special_move_pct") else "kept: --keep-special"))
    print_record(wf)
    say("The card's claim for rule %s: %s." % (a.rule, "most likely to close 2 % ABOVE its open" if model["claim"] == "up" else "most likely to MOVE 2 % (direction not proven)"))
    if a.publish:
        publish(a, None)
    return 0


def print_record(wf):
    say("Out of sample, %s to %s (%d mornings; the second half from %s), earnings sessions and special situations left out:" % (wf["oos_first"], wf["oos_last"], wf["oos_sessions"], wf["h2_first"]))
    for seg in ("h1", "h2"):
        b, t = wf["base"][seg], wf["baseline_highest_atr"][seg]
        say("  %s  any large cap: up %s, down %s | the highest-ATR name: up %s, down %s, average %+.2f %%" % (
            seg, _pct(b["up_rate"]), _pct(b["dn_rate"]), _pct(t["up_rate"]), _pct(t["dn_rate"]), t["mean_oc"]))
    for rule, r in wf["rules"].items():
        for seg in ("h1", "h2"):
            p = r["pick"][seg]
            if not p.get("n"):
                say("  %-4s %s  no picks" % (rule, seg))
                continue
            say("  %-4s %s  pick: up %s [%s, %s], down %s [%s, %s], average %+.2f %% [%+.2f, %+.2f] over %d" % (
                rule, seg, _pct(p["up_rate"]), _pct(p["up_ci"][0], 0), _pct(p["up_ci"][1], 0), _pct(p["dn_rate"]), _pct(p["dn_ci"][0], 0), _pct(p["dn_ci"][1], 0),
                p["mean_oc"], p["mean_oc_ci"][0] or 0, p["mean_oc_ci"][1] or 0, p["n"]))


# ---------------------------------------------------------------- morning
def premarket_quotes(symbols, cutoff_et, pause=0.25, say_=None):
    """{SYM: {"last", "time", "vs_prev_close_pct", "prev_close"}} from Yahoo's chart with pre-market bars, through the cutoff."""
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
            if et.time() <= cutoff_et:
                last, when = float(c), et.strftime("%H:%M")
        out[sym] = {"last": last, "time": when, "prev_close": pc, "vs_prev_close_pct": round((last / pc - 1) * 100.0, 2) if (last and pc) else None}
        if pause and i + 1 < len(symbols):
            net.SLEEP(pause)
        if say_ and (i + 1) % 10 == 0:
            say_("  pre-market %d/%d" % (i + 1, len(symbols)))
    return out


def _calendar(a, date):
    doc = dtio.read_json(os.path.join(a.cache_root, "earnings", date + ".json"), None)
    return doc if doc and isinstance(doc.get("rows"), list) else None


def _cal_syms(doc, keep=None):
    return set(str(r.get("symbol") or "").upper().replace("/", ".") for r in doc["rows"]
               if isinstance(r, dict) and r.get("symbol") and (keep is None or keep(r)))


def reporting_today(a, date):
    """Who reports today: the Nasdaq calendar file for the date (the backtest's own source), else the intel ledger."""
    doc = _calendar(a, date)
    if doc:
        return _cal_syms(doc), "Nasdaq calendar (%s)" % os.path.join(a.cache_root, "earnings", date + ".json")
    ledger = intel_flags.load_ledger(a.intel) if os.path.exists(a.intel) else None
    if ledger:
        return set(s for s, rec in intel_flags.derive_earnings(ledger.get("items") or []).items() if rec.get("date") == date), "intel ledger (no Nasdaq file for the date)"
    return set(), "no earnings source for the date: nobody left out"


def reacting_today(a, date):
    """Who reacts today to a report on the previous session that was not marked before the open (after the close, or no
    time given): the backtest's "reaction" sessions (dt.pick.earnings_session), from the previous session's calendar."""
    prev = intel_flags.prev_trading_day(date).isoformat()
    doc = _calendar(a, prev)
    if not doc:
        return set(), "no Nasdaq calendar for %s: nobody left out for reacting to a report" % prev
    return _cal_syms(doc, lambda r: r.get("time") != dtpick.PRE_MARKET), "Nasdaq calendar for %s, reports not marked before the open" % prev


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


def cmd_morning(a):
    model = dtio.read_json(a.model, None)
    if not model or "lift_up" not in model:
        raise Error("no model at %s: pick.py train first" % a.model)
    now_utc = clock.parse_now(a.now) if a.now else clock.utc_now()
    date = a.date or clock.to_eastern(now_utc).date().isoformat()
    cutoff = clock.parse_hhmm(a.at, "--at")
    rule = a.rule or model.get("rule") or dtpick.DEFAULT_RULE
    spec = model["spec"]
    syms = universe(a)
    spy = dtbars.daily_rows("SPY", a.cache_root)
    vix = dtbars.daily_rows("^VIX", a.cache_root)
    before_spy = [r for r in spy if r[0] < date]
    last_row = before_spy[-1][0] if before_spy else None          # what the store knew before this morning
    prev = intel_flags.prev_trading_day(date).isoformat()
    stale = last_row != prev
    fut = dtio.read_json(a.futures, None)
    nq_gap = (fut or {}).get("nq", {}).get("gap_pct") if fut and fut.get("date") == date else None
    market = dtpick.market_for_today(spy, date, vix, nq_gap)
    if market is None:
        raise Error("not enough SPY history before %s" % date)
    graded = grade_pending(a, quiet=True)
    reporting, rep_source = reporting_today(a, date)
    reacting, react_source = reacting_today(a, date) if model.get("exclude_earnings", True) else (set(), "kept: the model was trained with --keep-earnings")
    reacting -= reporting
    special_pct = model["special_move_pct"] if "special_move_pct" in model else dtpick.SPECIAL_MOVE_PCT
    extra = dtpick.load_extra(spec["extra"]).compute if spec.get("extra") else None
    earnings_all = dtpick.load_earnings(a.cache_root) if extra else {}
    cands, raws = [], {}
    for sym in syms:
        rows = dtbars.daily_rows(sym, a.cache_root)
        if not rows:
            continue
        raw, adv = dtpick.today_raw(sym, rows, date, extra, {"cache_root": a.cache_root, "earnings": earnings_all.get(sym) or {}})
        if raw is None:
            continue
        raws[sym] = raw
        cands.append((sym, dtpick.encode(spec, raw, market), adv, None))
    min_adv = a.min_adv or model.get("min_adv_usd", dtpick.MIN_ADV_USD)
    adv_of = {c[0]: c[2] for c in cands}
    special = {}
    if special_pct is not None:
        for sym, raw in raws.items():
            why = dtpick.special_of(raw, special_pct)
            if why and sym not in reporting and sym not in reacting and (adv_of.get(sym) or 0) >= min_adv:
                special[sym] = why
    exclude = set(reporting) | reacting | set(special) | set(s.strip().upper() for s in (a.exclude.split(",") if a.exclude else []) if s.strip())
    pick, ranked = dtpick.rank(model, cands, rule, min_adv, a.floor_x or model.get("floor_x", dtpick.PICK_FLOOR_X), exclude)
    top = ranked[:a.top]
    quotes = {}
    if not a.no_premarket and top:
        quotes = premarket_quotes([r["symbol"] for r in top], cutoff, a.pause, None if a.quiet else (lambda m: print(m, file=sys.stderr)))
    wf = model.get("walk_forward") or {}

    def card(r):
        return {"symbol": r["symbol"], "p_up": r["p_up"], "p_dn": r["p_dn"], "net": round(r["net"], 4), "e_oc": round(r["e_oc"], 4),
                "adv_usd": r["adv_usd"], "premarket": quotes.get(r["symbol"]), "reasons": reasons_of(raws.get(r["symbol"]), market),
                "raw": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in (raws.get(r["symbol"]) or {}).items()}}
    rr = (wf.get("rules") or {}).get(rule) or {}
    doc = {"created_by": "trader-pick", "as_of": "%s %s ET" % (date, a.at), "created_at": clock.iso(now_utc), "date": date, "cutoff": a.at,
           "rule": rule, "claim": model.get("claim") or claim_of(wf, rule), "target": "close at least %g %% above (or below) the open" % dtpick.TARGET_PCT,
           "band": [dtpick.TARGET_PCT, dtpick.BAND_HI_PCT], "store_through": last_row, "store_stale": stale, "market": market,
           "futures_date": (fut or {}).get("date"), "nq_gap_pct": nq_gap, "base_up": round(model["base_up"], 4), "base_dn": round(model["base_dn"], 4),
           "floor": dtpick.floor_words(rule, model, model.get("floor_x", dtpick.PICK_FLOOR_X)), "universe": len(syms), "scored": len(ranked),
           "reporting_today": len(reporting & set(raws)), "reporting_source": rep_source,
           "reacting_today": len(reacting & set(raws)), "reacting_source": react_source,
           "special_today": [{"symbol": s, "reason": special[s]} for s in sorted(special)], "special_move_pct": special_pct,
           "pick": card(pick) if pick else None, "no_pick": pick is None, "ranked": [card(r) for r in top],
           "oos": {"first": wf.get("oos_first"), "last": wf.get("oos_last"), "sessions": wf.get("oos_sessions"), "h2_first": wf.get("h2_first"),
                   "pick": (rr.get("pick") or {}), "top5": (rr.get("top5") or {}), "base": wf.get("base"), "highest_atr": wf.get("baseline_highest_atr")},
           "oos_rules": {k: {"pick": v.get("pick"), "top5": v.get("top5"), "no_pick_days": v.get("no_pick_days")} for k, v in (wf.get("rules") or {}).items()},
           "direction": direction_words(wf, rule), "live": live_record(a), "graded_now": graded,
           "model": {"trained_at": model.get("trained_at"), "sessions_last": model.get("sessions_last"), "rows": model.get("rows"),
                     "nq_tags": model.get("nq_tags"), "extra": spec.get("extra")},
           "premarket_note": "pre-market prices are Yahoo's, delayed and thin; they are shown, not scored: the model saw none of them in its history",
           "no_pick_words": "No pick: nothing cleared %s. That is a normal morning." % dtpick.floor_words(rule, model, model.get("floor_x", dtpick.PICK_FLOOR_X)),
           "note": WORDS}
    if a.write:
        dtio.write_json(a.out, doc)
        if pick:
            log_pick(a, doc)
        doc["live"] = live_record(a)
        dtio.write_json(a.out, doc)
        if a.publish:
            publish(a, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print_pick(doc)
    return 0


def log_pick(a, doc):
    path = a.record
    rows = [r for r in dtio.read_jsonl(path)] if os.path.exists(path) else []
    if any(isinstance(r, dict) and r.get("date") == doc["date"] for r in rows):
        return False
    p = doc["pick"]
    dtio.append_jsonl(path, {"date": doc["date"], "symbol": p["symbol"], "rule": doc["rule"], "claim": doc["claim"], "p_up": p["p_up"], "p_dn": p["p_dn"],
                             "e_oc": p["e_oc"], "base_up": doc["base_up"], "base_dn": doc["base_dn"], "logged_at": doc["created_at"], "cutoff": doc["cutoff"],
                             "premarket": p.get("premarket"), "status": "pending", "oc_pct": None, "cc_pct": None, "up": None, "dn": None,
                             "created_by": "trader-pick"})
    return True


def grade_pending(a, quiet=False):
    path = a.record
    rows = [r for r in dtio.read_jsonl(path)] if os.path.exists(path) else []
    changed, graded = False, []
    for r in rows:
        if not isinstance(r, dict) or r.get("status") != "pending":
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
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    if not quiet:
        say("Graded %d pick(s): %s" % (len(graded), ", ".join(graded) or "none pending with a session in the store"))
    return graded


def live_record(a):
    path = a.record
    rows = [r for r in dtio.read_jsonl(path) if isinstance(r, dict)] if os.path.exists(path) else []
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
    print("  market before the open: SPY %+.2f %% yesterday, %+.2f %% over 5 days, trend %s, VIX %s, NQ at 8:05 %s" % (
        m.get("spy_ret_1d") or 0, m.get("spy_ret_5d") or 0, m.get("spy_trend"), m.get("vix"),
        ("%+.2f %%" % doc["nq_gap_pct"]) if doc.get("nq_gap_pct") is not None else "n/a (no futures file for today)"))
    p = doc.get("pick")
    if not p:
        print("  NO PICK: " + doc["no_pick_words"])
    else:
        what = "close 2 % or more ABOVE its open" if doc["claim"] == "up" else "MOVE 2 % or more from its open"
        print("  %s: the name most likely to %s. Odds %s up, %s down (any large cap: %s up, %s down)." % (
            p["symbol"], what, _pct(p["p_up"], 0), _pct(p["p_dn"], 0), _pct(doc["base_up"], 0), _pct(doc["base_dn"], 0)))
        print("  why: " + "; ".join(p.get("reasons") or []))
        pm = p.get("premarket") or {}
        if pm.get("last"):
            print("  pre-market: %.2f at %s ET, %+.2f %% vs the prior close %.2f (shown, not scored)" % (pm["last"], pm.get("time"), pm.get("vs_prev_close_pct") or 0, pm.get("prev_close") or 0))
    print("  out of sample: " + doc["direction"] + ".")
    o = (((doc.get("oos") or {}).get("pick") or {}).get("all") or {})
    if o.get("n"):
        print("  intraday: the pick traded 2 %% above its open at some point on %s of those mornings, 2 %% below on %s." % (_pct(o.get("touch_up_rate"), 0), _pct(o.get("touch_dn_rate"), 0)))
    print("  live record: %s%s." % ((doc.get("live") or {}).get("words"), " (small sample)" if (doc.get("live") or {}).get("small_sample") else ""))
    print("  left out, they report today: %d (%s)" % (doc.get("reporting_today") or 0, doc.get("reporting_source")))
    print("  left out, they react today to a report after the prior close: %d (%s)" % (doc.get("reacting_today") or 0, doc.get("reacting_source")))
    sp = doc.get("special_today") or []
    print("  left out, special situations: %s" % ("; ".join("%s %s" % (x["symbol"], x["reason"]) for x in sp) if sp else "none"))
    print("  the next nine:")
    for r in (doc.get("ranked") or [])[1:10]:
        pm = r.get("premarket") or {}
        print("    %-6s up %s down %s expected %+.2f %%%s" % (r["symbol"], _pct(r["p_up"], 0), _pct(r["p_dn"], 0), r["e_oc"],
                                                         ("  pre-market %+.2f %%" % pm["vs_prev_close_pct"]) if pm.get("vs_prev_close_pct") is not None else ""))
    print("  " + doc["note"])


# ---------------------------------------------------------------- grade, report, publish
def cmd_grade(a):
    grade_pending(a)
    rec = live_record(a)
    say("Live record: %s%s." % (rec["words"], " (small sample)" if rec["small_sample"] else ""))
    if a.write and os.path.exists(a.out):
        doc = dtio.read_json(a.out, None) or {}
        doc["live"] = rec
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
    print_record(model.get("walk_forward") or {})
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
        wf = model.get("walk_forward") or {}
        rule = model.get("rule") or dtpick.DEFAULT_RULE
        doc = {"created_by": "trader-pick", "date": None, "no_pick": True, "pick": None, "rule": rule, "claim": model.get("claim"),
               "direction": direction_words(wf, rule), "live": live_record(a), "note": WORDS}
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
    p.add_argument("--keep-earnings", action="store_true")
    p.add_argument("--keep-special", action="store_true", help="keep the special situations (a 20 %% move the session before) in")
    p = sub.add_parser("morning"); common(p)
    p.add_argument("--date"); p.add_argument("--at", default="08:05"); p.add_argument("--now"); p.add_argument("--rule", choices=dtpick.RULES)
    p.add_argument("--futures", default=os.path.join("trader", "futures.json")); p.add_argument("--intel", default=os.path.join("trader", "intel.json"))
    p.add_argument("--exclude"); p.add_argument("--top", type=int, default=10); p.add_argument("--pause", type=float, default=0.25)
    p.add_argument("--min-adv", type=float); p.add_argument("--floor-x", type=float)
    p.add_argument("--no-premarket", action="store_true"); p.add_argument("--write", action="store_true")
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


if __name__ == "__main__":
    sys.exit(main())
