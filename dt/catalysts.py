"""Catalysts: KNOWN, POSSIBLE or NONE, from the intel ledger, the earnings calendar and the bars.

A KNOWN catalyst has a date (or a timestamp) and a source; a POSSIBLE one is an undated mention. Nothing is
invented: no ledger, no calendar, no unusual bar means {"none": true} and the card says "no identified catalyst"
(CONTRACTS.md, "The research desk", Catalysts). Pure: no network, no files.
"""

import datetime

from .bars import daily_before
from .intel_flags import as_date, earnings_of, derive_macro, trading_days_after

DATED_TAGS = ("deal", "capital", "people", "legal", "regulatory", "halt", "earnings", "analyst")
RELVOL_EVENT = 3.0
GAP_EVENT_PCT = 5.0
NEWS_WINDOW_DAYS = 3


def _direction(item):
    imp = (item or {}).get("impact")
    return {"+": "up", "-": "down", "0": "none"}.get(imp, "unknown")


def _item_date(item):
    d = item.get("date")
    if d:
        return str(d)[:10]
    t = item.get("time")
    return str(t)[:10] if t else None


def _entry(kind, text, date, source=None, url=None, direction="unknown", when=None, item_id=None):
    return {"kind": kind, "text": text, "date": date, "when": when, "source": source, "url": url, "direction": direction,
            "id": item_id}


def catalysts_for(sym, date_str, ledger=None, daily_rows=None, sessions=None, now_utc=None):
    """-> {"known": [...], "possible": [...], "none": bool, "next_earnings": {...} | None, "sources": [...]}"""
    sym = (sym or "").upper()
    ledger = ledger or {}
    known, possible, sources = [], [], set()
    day = as_date(date_str)
    items = [it for it in (ledger.get("items") or []) if isinstance(it, dict) and (it.get("symbol") or "").upper() == sym]
    # earnings: the calendar entry nearest at or after the date
    earn = earnings_of(sym, ledger) if ledger else None
    next_earnings = None
    if earn and earn.get("date"):
        ed = as_date(earn["date"])
        if ed and day and ed >= day:
            ahead = trading_days_after(day, 30)
            sessions_ahead = 0 if ed == day else (ahead.index(ed) + 1 if ed in ahead else None)
            next_earnings = {"date": earn["date"], "when": earn.get("when") or "unknown", "sessions_ahead": sessions_ahead,
                             "source": earn.get("source"), "url": earn.get("url")}
            known.append(_entry("earnings", "%s reports %s (%s)" % (sym, earn["date"], earn.get("when") or "unknown"),
                                earn["date"], earn.get("source"), earn.get("url"), "unknown", earn.get("when")))
            sources.add(earn.get("source") or "ledger")
    # macro prints on the date, HIGH impact (market wide; the card says so)
    for m in derive_macro(ledger.get("items") or []) if ledger.get("items") else (ledger.get("macro") or []):
        if (m.get("date") == date_str) and str(m.get("impact_level") or "").upper() == "HIGH":
            known.append(_entry("macro", "%s %s (HIGH impact, market wide)" % (m.get("clock") or "", m.get("event") or "macro print"),
                                m.get("date"), m.get("source"), m.get("url"), "unknown", m.get("clock")))
            sources.add(m.get("source") or "ledger")
    # dated company items in the window before the date: filings, analyst actions, deals, legal, halts
    for it in items:
        kind = it.get("kind")
        d = _item_date(it)
        if kind in ("earnings", "macro"):
            continue
        tags = set(it.get("tags") or [])
        text = it.get("headline") or it.get("note") or kind or "item"
        if d and day and as_date(d) and 0 <= (day - as_date(d)).days <= NEWS_WINDOW_DAYS and (
                kind in ("filing", "analyst", "catalyst") or tags & set(DATED_TAGS)):
            known.append(_entry(kind or "news", text, d, it.get("source"), it.get("url"), _direction(it), None, it.get("id")))
            sources.add(it.get("source") or "ledger")
        elif kind in ("catalyst", "note", "social", "news"):
            possible.append(_entry(kind, text, d, it.get("source"), it.get("url"), _direction(it), None, it.get("id")))
            sources.add(it.get("source") or "ledger")
    # measured on the bars: a volume event yesterday, a gap today
    rows = daily_before(daily_rows or [], date_str, 25)
    if len(rows) >= 21:
        vols = [float(r[5] or 0) for r in rows]
        avg = sum(vols[-21:-1]) / 20.0
        if avg > 0 and vols[-1] / avg >= RELVOL_EVENT:
            known.append(_entry("volume", "%s traded %.1fx its 20-day average on %s" % (sym, vols[-1] / avg, rows[-1][0]),
                                rows[-1][0], "bars", None, "up" if rows[-1][4] >= rows[-1][1] else "down"))
            sources.add("bars")
    if sessions and rows:
        pc = float(rows[-1][4])
        o = float(sessions[0]["o"])
        if pc > 0:
            gap = (o / pc - 1) * 100.0
            if abs(gap) >= GAP_EVENT_PCT:
                known.append(_entry("gap", "%s opened %+.1f %% against the prior close" % (sym, gap), date_str, "bars", None,
                                    "up" if gap > 0 else "down"))
                sources.add("bars")
    known.sort(key=lambda e: (e.get("date") or "", e["kind"]))
    return {"known": known, "possible": possible, "none": not known and not possible, "next_earnings": next_earnings,
            "sources": sorted(sources)}


def words(cat):
    """One line for a card."""
    if cat.get("none"):
        return "no identified catalyst (no ledger item, no calendar entry, no unusual bar)"
    parts = []
    if cat.get("known"):
        parts.append("known: " + "; ".join(e["text"] for e in cat["known"][:4]))
    if cat.get("possible"):
        parts.append("possible: " + "; ".join(e["text"][:60] for e in cat["possible"][:3]))
    return " | ".join(parts)
