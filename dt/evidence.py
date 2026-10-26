"""The evidence pack: one JSON of everything the desk knows about a symbol as of a date, every item with an id.

build(sym, date, ...) reads the stores and the working files (never the network) and writes
trader/research/<date>/<SYM>/evidence.json (CONTRACTS.md, "The research desk", The evidence pack). Point in time:
daily rows strictly before the date, the session only through the cutoff bar, ledger items stamped by the cutoff,
lessons resolved by the date. Every section is flattened into "items" so a role can cite E17; "missing" names every
source that was not there, so a role says so instead of guessing. Pure stdlib; no skill is imported.
"""

import datetime
import os

from . import io as dtio, bars as dtbars, clock, features as dtfeatures, regime as dtregime, catalysts as dtcatalysts
from . import risk as dtrisk, setups, stats as dtstats, intel_flags

FORMAT = "evidence/1"
DEPTHS = ("fast", "standard", "deep", "maximum")
CORE = ("bars", "session", "lab", "regime", "intel", "news", "fundamentals", "social", "short_interest", "scorecard")
SECTOR_ETF = {"Technology": "XLK", "Finance": "XLF", "Financials": "XLF", "Energy": "XLE", "Health Care": "XLV",
              "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Industrials": "XLI", "Utilities": "XLU",
              "Basic Materials": "XLB", "Materials": "XLB", "Real Estate": "XLRE", "Telecommunications": "XLC",
              "Communication Services": "XLC"}
SLICE_MIN_N = 50
LESSONS_SAME, LESSONS_CROSS = 5, 3


class EvidenceError(Exception):
    pass


class Paths:
    """Where the working files live, relative to a project root (the folder the skills run in)."""

    def __init__(self, root=".", cache_root=None):
        self.root = root
        self.cache = cache_root or os.path.join(root, "trader", "cache")
        t = os.path.join(root, "trader")
        self.largecap = os.path.join(t, "largecap.json")
        self.edge = os.path.join(t, "lab", "edge.json")
        self.regime = os.path.join(t, "regime.json")
        self.intel = os.path.join(t, "intel.json")
        self.metrics = os.path.join(t, "metrics.json")
        self.news = os.path.join(t, "state", "news.json")
        self.futures = os.path.join(t, "futures.json")
        self.montecarlo = os.path.join(t, "montecarlo.json")
        self.config = os.path.join(t, "config.json")
        self.scorecard = os.path.join(t, "scorecard")
        self.memory = os.path.join(t, "research", "memory.jsonl")
        self.research = os.path.join(t, "research")

    def out(self, date, sym):
        return os.path.join(self.research, date, sym.upper())


class Items:
    def __init__(self):
        self.items = []

    def add(self, kind, source, text, value=None, as_of=None):
        eid = "E%d" % (len(self.items) + 1)
        self.items.append({"id": eid, "kind": kind, "source": source, "as_of": as_of, "text": text, "value": value})
        return eid


def cutoff_slot(cutoff):
    """'10:15' -> the last slot whose bar has closed (10:15 -> 8)."""
    t = clock.parse_hhmm(cutoff, "--at") if isinstance(cutoff, str) else cutoff
    return (clock.minutes_of(t) - clock.OPEN_MIN) // clock.BAR_MIN - 1


def _r(x, p=4):
    return None if x is None else round(float(x), p)


def _stamp(it):
    return str(it.get("time") or it.get("date") or "")


def identity_of(sym, largecap_doc):
    for row in (largecap_doc or {}).get("symbols") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == sym:
            return {"name": row.get("name"), "sector": row.get("sector"), "industry": row.get("industry"), "tier": row.get("tier"),
                    "market_cap": row.get("market_cap"), "country": row.get("country")}
    return None


def lab_entry(edge, cid):
    for ec in (edge or {}).get("configs") or []:
        if isinstance(ec, dict) and ec.get("id") == cid:
            return ec
    return None


def regime_slice(ec, tags):
    """The by_regime slice with the most trades among the session's tags, when it has SLICE_MIN_N or more."""
    best = None
    for tag in tags or []:
        st = ((ec or {}).get("by_regime") or {}).get(tag)
        if st and (st.get("n") or 0) >= SLICE_MIN_N and (best is None or st["n"] > best["stats"]["n"]):
            best = {"tag": tag, "stats": st}
    return best


def session_cut(store, date, lk):
    rows = ((store or {}).get("sessions") or {}).get(date)
    if not rows:
        return None
    b = dtbars.bars_of(rows)
    return b[:lk + 1] if lk is not None else b


def scorecard_record(paths, sym, date):
    calls = [c for c in dtio.read_jsonl(os.path.join(paths.scorecard, "calls.jsonl")) if isinstance(c, dict) and c.get("id")] \
        if os.path.exists(os.path.join(paths.scorecard, "calls.jsonl")) else []
    outs = {}
    opath = os.path.join(paths.scorecard, "outcomes.jsonl")
    if os.path.exists(opath):
        for o in dtio.read_jsonl(opath):
            if isinstance(o, dict) and o.get("id"):
                outs[o["id"]] = o
    graded = [dict(outs[c["id"]], symbol=c.get("symbol"), date=c.get("date")) for c in calls
              if c["id"] in outs and not outs[c["id"]].get("late") and (c.get("date") or "") < date]
    mine = [g for g in graded if g.get("symbol") == sym]
    recent = [{"id": c["id"], "date": c.get("date"), "config_id": c.get("config_id"), "score": c.get("score"),
               "outcome": (outs.get(c["id"]) or {}).get("outcome"), "R_net": (outs.get(c["id"]) or {}).get("R_net")}
              for c in calls if c.get("symbol") == sym and (c.get("date") or "") < date][-10:]
    return {"overall": dtstats.stats_block(graded) if graded else None, "symbol": dtstats.stats_block(mine) if mine else None,
            "recent": recent, "calls_before": len([c for c in calls if (c.get("date") or "") < date])}


def lessons_for(paths, sym, date):
    if not os.path.exists(paths.memory):
        return []
    rows = [r for r in dtio.read_jsonl(paths.memory) if isinstance(r, dict) and r.get("status") == "settled"
            and (r.get("resolved") or "9999") < date]          # strictly before: a card settled on the date itself is not known at the cutoff
    same = [r for r in reversed(rows) if r.get("symbol") == sym][:LESSONS_SAME]
    cross = [r for r in reversed(rows) if r.get("symbol") != sym][:LESSONS_CROSS]
    out = []
    for r in same + cross:
        out.append({"id": r.get("id"), "date": r.get("date"), "symbol": r.get("symbol"), "side": r.get("side"), "rating": r.get("rating"),
                    "outcome": r.get("outcome"), "reflection": r.get("reflection"), "resolved": r.get("resolved"),
                    "same_symbol": r.get("symbol") == sym})
    return out


def news_for(doc, sym, date):
    """The upstream news state is kept loosely: any list of dicts with a ticker/symbol and a time is read."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            t = str(x.get("ticker") or x.get("symbol") or "").upper()
            if t == sym and (x.get("headline") or x.get("title")):
                stamp = _stamp(x)
                if not stamp or stamp[:10] <= date:
                    out.append({"headline": x.get("headline") or x.get("title"), "time": x.get("time") or x.get("date"),
                                "source": x.get("source") or "trader-news", "url": x.get("url") or x.get("link"),
                                "tags": x.get("tags") or []})
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(doc)
    return out[:20]


def fundamentals_for(metrics, sym, date):
    if not metrics:
        return None, "no trader/metrics.json (trader-analyst facts.py --metrics writes it)"
    as_of = str(metrics.get("as_of") or "")
    for row in metrics.get("companies") or []:
        if str(row.get("ticker", "")).upper() == sym:
            if as_of and as_of > date:
                return None, "trader/metrics.json is dated %s, after %s: withheld (not point in time)" % (as_of, date)
            return dict(row, as_of=as_of), None
    return None, "%s is not in trader/metrics.json" % sym


def build(sym, date, cutoff="10:15", root=".", cache_root=None, depth="standard", now_utc=None, paths=None, write=True):
    sym = sym.upper()
    if depth not in DEPTHS:
        raise EvidenceError("depth wants one of %s" % ", ".join(DEPTHS))
    try:
        day = datetime.date.fromisoformat(date)
    except ValueError:
        raise EvidenceError("date wants YYYY-MM-DD, got %r" % date)
    p = paths or Paths(root, cache_root)
    now_utc = now_utc or clock.utc_now()
    items = Items()
    missing = []
    lk = cutoff_slot(cutoff)
    cfg = dtio.read_json(p.config, {}) or {}
    # ------------------------------------------------------------------ bars
    daily = dtbars.daily_rows(sym, p.cache)
    if not daily:
        missing.append("bars")
    spy_daily = dtbars.daily_rows("SPY", p.cache)
    vix_daily = dtbars.daily_rows("^VIX", p.cache)
    vix3m_daily = dtbars.daily_rows("^VIX3M", p.cache)
    store = dtbars.load_store(sym, "bars5m", p.cache)
    bars = session_cut(store, date, lk)
    if not bars:
        missing.append("session")
    spy_store = dtbars.load_store("SPY", "bars5m", p.cache)
    spy_bars = session_cut(spy_store, date, lk)
    largecap = dtio.read_json(p.largecap, None)
    identity = identity_of(sym, largecap) or {"name": None, "sector": None, "industry": None, "tier": None, "market_cap": None}
    if identity.get("name"):
        items.add("DATA", "trader/largecap.json", "%s is %s, %s / %s, tier %s, market cap %s" % (
            sym, identity["name"], identity.get("sector"), identity.get("industry"), identity.get("tier"),
            ("{:,.0f}".format(identity["market_cap"]) if identity.get("market_cap") else "n/a")), identity, (largecap or {}).get("as_of"))
    else:
        missing.append("identity")
    sector_rows = dtbars.daily_rows(SECTOR_ETF.get(identity.get("sector") or ""), p.cache) if SECTOR_ETF.get(identity.get("sector") or "") else None
    # ------------------------------------------------------------------ snapshot and features
    before = dtbars.daily_before(daily, date, 1) if daily else []
    pc = float(before[-1][4]) if before else None
    feats = dtfeatures.features_for(sym, date, daily, bars, spy_daily, sector_rows) if daily else {"missing": list(dtfeatures.NAMES)}
    snapshot = {"last_close": pc, "last_session": ({"date": before[-1][0], "o": before[-1][1], "h": before[-1][2], "l": before[-1][3],
                                                     "c": before[-1][4], "v": before[-1][5]} if before else None),
                "sma20": None, "sma50": None, "sma200": None, "rsi_14": feats.get("rsi_14"), "atr_14": feats.get("atr_14"),
                "atr_pct": feats.get("atr_pct"), "avg_vol_20": dtbars.avg_volume(daily, date) if daily else None,
                "relvol_20": feats.get("relvol_20"), "dist_52w_high_pct": feats.get("dist_52w_high_pct"),
                "dist_52w_low_pct": feats.get("dist_52w_low_pct"), "gap_pct": feats.get("gap_pct"), "intraday": None}
    if daily:
        closes = [float(r[4]) for r in dtbars.daily_before(daily, date, 220)]
        snapshot["sma20"], snapshot["sma50"], snapshot["sma200"] = _r(dtbars.sma(closes, 20)), _r(dtbars.sma(closes, 50)), _r(dtbars.sma(closes, 200))
        ls = snapshot["last_session"]
        items.add("DATA", "trader/cache daily", "%s last session %s: open %.2f high %.2f low %.2f close %.2f volume %s" % (
            sym, ls["date"], ls["o"], ls["h"], ls["l"], ls["c"], "{:,.0f}".format(ls["v"] or 0)), ls, ls["date"])
        items.add("DATA", "dt.features", "%s before %s: SMA20 %s, SMA50 %s, SMA200 %s, RSI14 %s, ATR14 %s (%s %% of price), "
                  "20-day average volume %s, 52-week high %s %% away" % (
                      sym, date, snapshot["sma20"], snapshot["sma50"], snapshot["sma200"], snapshot["rsi_14"], snapshot["atr_14"],
                      snapshot["atr_pct"], "{:,.0f}".format(snapshot["avg_vol_20"] or 0), snapshot["dist_52w_high_pct"]),
                  {k: snapshot[k] for k in ("sma20", "sma50", "sma200", "rsi_14", "atr_14", "atr_pct", "avg_vol_20", "dist_52w_high_pct")},
                  feats.get("as_of"))
        for line in dtfeatures.summary_lines(feats):
            items.add("DATA", "dt.features", "%s: %s" % (sym, line), None, feats.get("as_of"))
        feats_items = {k: feats[k] for k in dtfeatures.NAMES if feats.get(k) is not None}
        items.add("DATA", "dt.features", "%s feature table before %s (every number is from the daily rows)" % (sym, date), feats_items, feats.get("as_of"))
    ctx = {"window": clock.entry_window(day, cfg), "prev_close": pc, "daily_atr": dtbars.daily_atr(daily, date) if daily else None,
           "avg_vol20": snapshot["avg_vol_20"], "spy": spy_bars, "spy_prev_close": dtbars.prev_close(spy_daily, date) if spy_daily else None}
    if bars:
        intra = dtfeatures.features_intraday(bars, len(bars) - 1, ctx)
        snapshot["intraday"] = dict(intra, cutoff=cutoff, bars=len(bars))
        items.add("DATA", "trader/cache bars5m", "%s on %s through %s (%d bars): last %.2f, %s VWAP by %s %%, opening range %s, "
                  "%s %% from the open, relative volume so far %s, %s of a daily ATR used" % (
                      sym, date, cutoff, len(bars), intra["close"], "above" if intra.get("above_vwap") else "below",
                      intra.get("vs_vwap_pct"), intra.get("or_position", "n/a"), intra.get("move_from_open_pct"),
                      intra.get("relvol_so_far"), intra.get("range_used_atr")), intra, "%s %s ET" % (date, cutoff))
    # ------------------------------------------------------------------ regime
    reg = dtregime.daily_context(date, spy_daily, vix_daily, vix3m_daily) if spy_daily else None
    regime_doc = dtio.read_json(p.regime, None)
    regime = {"date": date, "class": None, "rule": None, "tags": [], "daily": None, "live": None}
    if reg:
        regime.update({"class": reg["class"], "rule": reg["class_rule"], "tags": reg["tags"], "daily": {k: v for k, v in reg.items() if k not in ("class_inputs",)}})
        items.add("REGIME", "dt.regime", "Regime before %s: %s (%s); tags %s; SPY %s vs SMA20 %s / SMA50 %s, VIX %s, 5-day %s %%" % (
            date, reg["class"], reg["class_rule"], ", ".join(reg["tags"]), reg.get("spy_close"), reg.get("spy_sma20"), reg.get("spy_sma50"),
            reg.get("vix"), reg.get("ret5d_pct")), regime["daily"], before[-1][0] if before else None)
    else:
        missing.append("regime")
    if regime_doc and regime_doc.get("date") == date and regime_doc.get("live"):
        regime["live"] = regime_doc["live"]
        items.add("REGIME", "trader/regime.json", "Live read at %s: %s" % (regime_doc.get("as_of"), _live_words(regime_doc["live"])), regime_doc["live"], regime_doc.get("as_of"))
    # ------------------------------------------------------------------ setups and the lab
    edge = dtio.read_json(p.edge, None)
    lab = {"present": bool(edge), "configs_tested": (edge or {}).get("configs_tested"), "fill": (edge or {}).get("fill"),
           "cost_bps": (edge or {}).get("cost_bps"), "sessions": (edge or {}).get("sessions"), "split": (edge or {}).get("split"),
           "approved": {"long": [], "short": []}, "watch": {"long": [], "short": []}}
    if edge:
        for ec in edge.get("configs") or []:
            if not isinstance(ec, dict):
                continue
            side = setups.side_of(ec)
            if ec.get("status") in ("approved", "watch"):
                lab[ec["status"]][side].append(ec["id"])
        items.add("STAT", "trader/lab/edge.json", "The lab (%s fill, %s bps, %s sessions %s to %s, OOS %s to %s, %s configs tested): approved long %s, "
                  "approved short %s, watch long %s, watch short %s" % (
                      lab["fill"], lab["cost_bps"], (lab["sessions"] or {}).get("n"), (lab["sessions"] or {}).get("first"),
                      (lab["sessions"] or {}).get("last"), ((lab["split"] or {}).get("oos") or {}).get("first"),
                      ((lab["split"] or {}).get("oos") or {}).get("last"), lab["configs_tested"],
                      ", ".join(lab["approved"]["long"]) or "none", ", ".join(lab["approved"]["short"]) or "none",
                      ", ".join(lab["watch"]["long"]) or "none", ", ".join(lab["watch"]["short"]) or "none"),
                  {"approved": lab["approved"], "watch": lab["watch"], "configs_tested": lab["configs_tested"]}, edge.get("as_of"))
    else:
        missing.append("lab")
    fired = {"long": [], "short": []}
    if bars:
        try:
            sigs = setups.run_all(bars, ctx, setups.all_configs())
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            sigs = {}
            items.add("UNCERTAINTY", "dt.setups", "the setups could not run on %s %s: %s" % (sym, date, e))
        for cid, sig in sigs.items():
            side = setups.side_of(sig)
            ec = lab_entry(edge, cid)
            rs = regime_slice(ec, regime["tags"]) if ec else None
            rec = {"config_id": cid, "side": side, "signal": sig, "late_by": (len(bars) - 1) - sig["k"],
                   "lab": ({"status": ec.get("status"), "oos": ec.get("oos"), "regime_slice": rs} if ec else {"status": "untested", "oos": None, "regime_slice": None})}
            fired[side].append(rec)
            oos = (ec or {}).get("oos") or {}
            items.add("SIGNAL", "dt.setups", "%s %s %s fired at %s (bar %d): %s %.2f, stop %.2f, target %.2f (%gR, risk %.2f a share)%s; lab OOS: %s%s" % (
                sym, side, cid, sig["entry_time"], sig["k"], "sell short at" if side == "short" else "buy above", sig["entry"], sig["stop"],
                sig["target"], sig["target_r"], sig.get("risk_per_share") or 0, (", %d bars before the cutoff" % rec["late_by"]) if rec["late_by"] > 1 else "",
                rec["lab"]["status"], (" n %s, win %s %% [%s, %s], exp %+.3fR [%+.3f, %+.3f], pf %s" % (
                    oos.get("n"), _pct(oos.get("win_rate")), _pct((oos.get("win_ci95") or [None, None])[0]), _pct((oos.get("win_ci95") or [None, None])[1]),
                    oos.get("exp_R") or 0, (oos.get("exp_ci95") or [0, 0])[0] or 0, (oos.get("exp_ci95") or [0, 0])[1] or 0, oos.get("pf")) if oos else "")),
                {"signal": sig, "lab": rec["lab"]}, "%s %s ET" % (date, cutoff))
            if rs:
                items.add("STAT", "trader/lab/edge.json", "%s in regime %s: n %s, exp %+.3fR [%+.3f, %+.3f], win %s %%" % (
                    cid, rs["tag"], rs["stats"].get("n"), rs["stats"].get("exp_R") or 0, (rs["stats"].get("exp_ci95") or [0, 0])[0] or 0,
                    (rs["stats"].get("exp_ci95") or [0, 0])[1] or 0, _pct(rs["stats"].get("win_rate"))), rs, edge.get("as_of"))
        if not sigs:
            items.add("SIGNAL", "dt.setups", "no rule fired on %s by %s on %s (%d bars read)" % (sym, cutoff, date, len(bars)), None, "%s %s ET" % (date, cutoff))
    for side in fired:
        fired[side].sort(key=lambda r: (r["late_by"] > 1, -(((r["lab"].get("oos") or {}).get("exp_ci95") or [-9, 0])[0] or -9)))
    # ------------------------------------------------------------------ the record
    record = scorecard_record(p, sym, date)
    if record["overall"]:
        o = record["overall"]
        items.add("RECORD", "trader/scorecard", "The desk's live record before %s: %d graded calls, win %s %% [%s, %s], exp %+.3fR [%+.3f, %+.3f]" % (
            date, o["n"], _pct(o["win_rate"]), _pct(o["win_ci95"][0]), _pct(o["win_ci95"][1]), o["exp_R"], o["exp_ci95"][0], o["exp_ci95"][1]), o, date)
        if record["symbol"]:
            s = record["symbol"]
            items.add("RECORD", "trader/scorecard", "%s on the desk before %s: %d graded calls, exp %+.3fR" % (sym, date, s["n"], s["exp_R"]), s, date)
    else:
        missing.append("scorecard")
    # ------------------------------------------------------------------ intel, news, social, macro, fundamentals
    ledger = intel_flags.load_ledger(p.intel) if os.path.exists(p.intel) else None
    intel_items, social, macro = [], [], []
    if ledger and ledger.get("items"):
        cut = "%sT23:59:59Z" % date
        for it in ledger["items"]:
            if not isinstance(it, dict) or _stamp(it) > cut:
                continue
            if str(it.get("symbol") or "").upper() == sym:
                row = {k: it.get(k) for k in ("id", "kind", "source", "url", "time", "date", "headline", "note", "tags", "impact", "when")}
                (social if it.get("kind") == "social" else intel_items).append(row)
                items.add("SOCIAL" if it.get("kind") == "social" else "NEWS" if it.get("kind") in ("news", "analyst", "insider") else
                          "FILING" if it.get("kind") == "filing" else "EVENT", it.get("source") or "ledger",
                          "%s %s: %s (%s, %s%s)" % (sym, it.get("kind"), it.get("headline") or it.get("note"), it.get("source"), it.get("time") or it.get("date"),
                                                   (", impact %s" % it["impact"]) if it.get("impact") else ""), row, it.get("time") or it.get("date"))
            elif it.get("kind") == "macro" and it.get("date") and abs((datetime.date.fromisoformat(it["date"][:10]) - day).days) <= 3:
                macro.append({k: it.get(k) for k in ("date", "clock", "event", "impact_level", "source", "url", "headline")})
                items.add("MACRO", it.get("source") or "ledger", "macro %s %s %s (%s)" % (it.get("date"), it.get("clock") or "", it.get("event") or it.get("headline"),
                                                                                    it.get("impact_level") or "?"), macro[-1], it.get("date"))
    else:
        missing.append("intel")
    if not intel_items:
        missing.append("news")
    if not social:
        missing.append("social")
    news_doc = dtio.read_json(p.news, None)
    news = news_for(news_doc, sym, date) if news_doc else []
    for n in news:
        items.add("NEWS", n.get("source") or "trader-news", "%s news: %s (%s)" % (sym, n["headline"], n.get("time")), n, n.get("time"))
    if "news" in missing and news:
        missing.remove("news")
    metrics = dtio.read_json(p.metrics, None)
    fundamentals, why = fundamentals_for(metrics, sym, date)
    if fundamentals:
        items.add("FILING", fundamentals.get("source") or "trader/metrics.json", "%s fundamentals as of %s (period ending %s): revenue TTM %s, net income TTM %s, EPS TTM %s, "
                  "revenue growth %s %%, EPS growth %s %%, gross margin %s %%, operating margin %s %%, net margin %s %%" % (
                      sym, fundamentals.get("as_of"), fundamentals.get("period_end"), _usd(fundamentals.get("revenue_ttm")), _usd(fundamentals.get("net_income_ttm")),
                      fundamentals.get("eps_ttm"), fundamentals.get("revenue_growth_yoy"), fundamentals.get("eps_growth_yoy"), fundamentals.get("gross_margin"),
                      fundamentals.get("operating_margin"), fundamentals.get("net_margin")), fundamentals, fundamentals.get("as_of"))
    else:
        missing.append("fundamentals")
        items.add("UNCERTAINTY", "trader/metrics.json", "fundamentals unavailable: %s" % why)
    missing.append("short_interest")
    items.add("UNCERTAINTY", "ledger", "short interest, days to cover, borrow availability and crowding are not in any source here: unknown")
    # ------------------------------------------------------------------ catalysts and risk
    cat = dtcatalysts.catalysts_for(sym, date, ledger, daily, bars, now_utc)
    items.add("EVENT", "dt.catalysts", "Catalysts for %s on %s: %s" % (sym, date, dtcatalysts.words(cat)), cat, date)
    risk = {}
    for side in ("long", "short"):
        best = fired[side][0] if fired[side] else None
        sig = best["signal"] if best else None
        ec = lab_entry(edge, best["config_id"]) if best else None
        rs = best["lab"].get("regime_slice") if best else None
        rb = dtrisk.risk_block(sym, side, date, feats, sig, cat, regime["class"], (ledger or {}).get("items"), daily, (ec or {}).get("oos"), rs)
        rb["score"] = dtrisk.score(rb)
        risk[side] = rb
        items.add("STAT", "dt.risk", "Risk, %s side: verdict %s (%s); liquidity %s (ADV $%s), ATR %s %%, 95th-percentile overnight gap %s %%, "
                  "earnings in %s sessions, squeeze risk %s, regime fit %s, risk score %s" % (
                      side, rb["verdict"], "; ".join(rb["reasons"][:3]), rb["liquidity"], "{:,.0f}".format(rb["adv_usd"] or 0), rb["atr_pct"],
                      rb["gap_risk_pct_95"], rb["event_risk"].get("earnings_sessions_ahead"), rb["squeeze_risk"], rb["regime_fit"], rb["score"]), rb, date)
    # ------------------------------------------------------------------ futures, monte carlo, lessons
    fut = dtio.read_json(p.futures, None)
    futures = None
    if fut and fut.get("date") == date:
        futures = fut
        items.add("MACRO", "trader/futures.json", "Overnight futures for %s: %s" % (date, ", ".join(fut.get("tags") or []) or "no tags"), {"tags": fut.get("tags")}, fut.get("as_of"))
    mc = dtio.read_json(p.montecarlo, None)
    montecarlo = None
    if mc and mc.get("books"):
        ids = set(r["config_id"] for s in fired.values() for r in s)
        books = [b for b in mc["books"] if b.get("config_id") in ids]
        if books:
            montecarlo = {"kind": mc.get("kind"), "resamples": mc.get("resamples"), "books": books}
            for b in books:
                w = b.get("window") or {}
                items.add("STAT", "trader/montecarlo.json", "Monte Carlo book for %s at the sizing rules: a %s-day window ends red in %s %% of %s resamples, median %sR" % (
                    b["config_id"], w.get("days"), _pct(w.get("p_red")), mc.get("resamples"), (w.get("total_R") or {}).get("p50")), b, mc.get("as_of"))
    lessons = lessons_for(p, sym, date)
    for l in lessons:
        items.add("LESSON", "trader/research/memory.jsonl", "%s %s %s rated %s, outcome %s: %s" % (
            "same symbol" if l["same_symbol"] else "another symbol", l["symbol"], l["date"], l["rating"], l["outcome"], l["reflection"]), l, l["resolved"])
    # ------------------------------------------------------------------ the document
    doc = {"format": FORMAT, "symbol": sym, "date": date, "cutoff": cutoff, "as_of": "%s %s ET" % (date, cutoff), "built": clock.iso(now_utc),
           "depth": depth, "identity": identity, "snapshot": snapshot, "features": feats, "regime": regime, "setups": fired, "lab": lab,
           "scorecard": record, "catalysts": cat, "risk": risk, "fundamentals": fundamentals, "news": news, "intel": intel_items,
           "social": social, "macro": macro, "futures": futures, "montecarlo": montecarlo, "lessons": lessons, "items": items.items,
           "missing": sorted(set(missing)), "note": "Every line is from a file on this machine as of the date and cutoff; nothing here is a prediction."}
    if write:
        out = p.out(date, sym)
        os.makedirs(out, exist_ok=True)
        dtio.write_json(os.path.join(out, "evidence.json"), doc)
        doc["path"] = os.path.join(out, "evidence.json")
    return doc


def _pct(x):
    return "n/a" if x is None else "%.1f" % (x * 100)


def _usd(x):
    return "n/a" if x is None else "{:,.0f}".format(x)


def _live_words(live):
    bits = []
    for key in ("spy", "qqq"):
        s = live.get(key) or {}
        if s.get("gap_pct") is not None:
            bits.append("%s gap %+.2f %%" % (key.upper(), s["gap_pct"]))
        if s.get("vs_vwap"):
            bits.append("%s %s VWAP" % (key.upper(), s["vs_vwap"]))
    b = live.get("breadth") or {}
    if b.get("pct_up") is not None:
        bits.append("breadth %s %% up" % b["pct_up"])
    return ", ".join(bits) or "no live fields"


def item_index(doc):
    return {it["id"]: it for it in doc.get("items") or []}


def numbers_in(item):
    """Every number in an item's text and value, rounded to 2 places (for the validator's quote check)."""
    import re
    out = set()
    for m in re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", item.get("text") or ""):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            out.add(round(float(v), 2))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(item.get("value"))
    return out


def load(path):
    doc = dtio.read_json(path, None)
    if not doc or doc.get("format") != FORMAT:
        raise EvidenceError("%s is not an evidence pack" % path)
    return doc
