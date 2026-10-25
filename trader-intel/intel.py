#!/usr/bin/env python3
"""Intel ledger for trader-intel. Standard library only; it never calls a connector or the network.

Claude gathers intelligence through the connectors on claude.ai (Firecrawl for live web search and
page scraping, FMP, Bigdata.com, Quartr, LunarCrush) and writes what it found as a JSON list of items.
This tool validates, merges, dedupes, prunes and reports those items in trader/intel.json, and
computes the flags the desk shows next to a candidate.

    python3 intel.py template TSLA NVDA                          # -> trader/intel/items-<date>-<HHMM>.json, fill it in
    python3 intel.py import --from trader/intel/items-2026-10-26-0845.json
    python3 intel.py add --symbol TSLA --kind news --source FMP --time 2026-10-26T12:30:00Z \\
                         --headline "..." --url https://... --tags deal --impact +
    python3 intel.py add-earnings TSLA 2026-10-28 --when post --source Firecrawl --url https://ir.tesla.com/...
    python3 intel.py add-macro 2026-10-29 "FOMC rate decision" --impact HIGH --clock 14:00 --source Bigdata.com
    python3 intel.py show TSLA --hours 24                        # newest first, New York times, source and link
    python3 intel.py flags TSLA                                  # the desk flags and the items behind each one
    python3 intel.py prune --days 14                             # drop old items; future earnings and macro stay

Every command takes --ledger (default trader/intel.json, relative to the directory you run in),
--now "YYYY-MM-DD HH:MM" (New York; or ...Z for UTC) to pin the clock, and --json.
Exit 0 when it did what was asked, 1 when it refused or found nothing; the reason is one plain line.

An item is what a source said, with its link, at the time it said it. Headlines are leads, not
facts; nothing here is a prediction, and nothing here trades.
"""

import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _root in (HERE, os.path.dirname(HERE)):  # a sibling dt/ next to this tool (installed skill), then the repo root
    if os.path.isdir(os.path.join(_root, "dt")):
        if _root not in sys.path:
            sys.path.insert(0, _root)
        break

from dt import clock, io  # noqa: E402
from dt import intel_flags as fl  # noqa: E402

LEDGER = os.path.join("trader", "intel.json")
ITEMS_DIR = os.path.join("trader", "intel")
SYMBOL_RX = re.compile(r"^[A-Z0-9.^-]{1,10}$")
HELP_KEY = "_help"
EXAMPLE = "EXAMPLE:"
FOOTER = "Headlines are leads, not facts; open the story before acting. Nothing here is a prediction or advice."


class IntelError(Exception):
    pass


# ---------------------------------------------------------------- items

def canon_source(text):
    s = str(text or "").strip()
    if not s:
        return ""
    low = s.lower().replace(" ", "")
    for name in fl.SOURCES:
        n = name.lower()
        if low == n or low == n.replace(".com", "") or low.startswith(n + "/") or low.startswith(n + ":"):
            return name
    return s


def one_line(text):
    return " ".join(str(text or "").split())


def parse_tags(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise IntelError("tags must be a list like [\"earnings\", \"deal\"]")
    out = []
    for t in value:
        t = str(t or "").strip().lower()
        if not t:
            continue
        if t not in fl.TAGS:
            raise IntelError("tag %r is not one of %s" % (t, ", ".join(fl.TAGS)))
        if t not in out:
            out.append(t)
    return out


def parse_clock(value):
    if value in (None, ""):
        return None
    t = clock.parse_hhmm(value, "clock")
    return "%02d:%02d" % (t.hour, t.minute)


def validate_item(raw):
    """One raw item -> (clean item, None) or (None, reason)."""
    try:
        return _validate(raw), None
    except (IntelError, clock.ClockError) as exc:
        return None, str(exc)


def _validate(raw):
    if not isinstance(raw, dict):
        raise IntelError("not an object")
    missing = [k for k in ("symbol", "kind", "time") if k not in raw]
    if missing:
        raise IntelError("missing %s" % ", ".join(missing))
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in fl.KINDS:
        raise IntelError("kind %r is not one of %s" % (raw.get("kind"), ", ".join(fl.KINDS)))
    symbol = raw.get("symbol")
    if symbol is not None:
        symbol = str(symbol).strip().upper()
        if not symbol:
            symbol = None
        elif not SYMBOL_RX.match(symbol):
            raise IntelError("symbol %r is not a ticker" % raw.get("symbol"))
    if kind == "earnings" and not symbol:
        raise IntelError("an earnings item needs a symbol")
    source = canon_source(raw.get("source"))
    url = raw.get("url")
    if url is not None:
        url = str(url).strip() or None
        if url and "://" not in url:
            raise IntelError("url %r does not start with http:// or https://" % url)
    if not source and not url:
        raise IntelError("needs a source or a url")
    if not source:
        source = "other"
    t = fl.parse_utc(raw.get("time"))
    if t is None:
        raise IntelError("time must be UTC ISO like 2026-10-26T12:30:00Z (got %r)" % raw.get("time"))
    headline = one_line(raw.get("headline"))
    note = one_line(raw.get("note")) or None
    tags = parse_tags(raw.get("tags"))
    impact = str(raw.get("impact") or "?").strip() or "?"
    if impact not in fl.IMPACTS:
        raise IntelError("impact %r is not one of + - 0 ?" % raw.get("impact"))
    item = {"symbol": symbol, "kind": kind, "source": source, "url": url, "time": fl.iso_utc(t),
            "headline": headline, "note": note, "tags": tags, "impact": impact}
    if kind == "earnings":
        d = fl.as_date(raw.get("date"))
        if d is None:
            raise IntelError("an earnings item needs a date YYYY-MM-DD (got %r)" % raw.get("date"))
        when = str(raw.get("when") or "unknown").strip().lower()
        if when not in fl.WHENS:
            raise IntelError("when %r is not one of pre, post, unknown" % raw.get("when"))
        item["date"] = d.isoformat()
        item["when"] = when
        if "earnings" not in item["tags"]:
            item["tags"].insert(0, "earnings")
        if not headline:
            item["headline"] = earnings_headline(symbol, d.isoformat(), when)
    elif kind == "macro":
        d = fl.as_date(raw.get("date"))
        if d is None:
            raise IntelError("a macro item needs a date YYYY-MM-DD (got %r)" % raw.get("date"))
        event = one_line(raw.get("event")) or headline
        if not event:
            raise IntelError("a macro item needs an event")
        level = str(raw.get("impact_level") or "").strip().upper()
        if level not in fl.LEVELS:
            raise IntelError("impact_level %r is not one of HIGH, MEDIUM, LOW" % raw.get("impact_level"))
        item["date"] = d.isoformat()
        item["event"] = event
        item["impact_level"] = level
        item["clock"] = parse_clock(raw.get("clock"))
        if "macro" not in item["tags"]:
            item["tags"].insert(0, "macro")
        if not headline:
            item["headline"] = event
    elif not headline:
        raise IntelError("needs a headline")
    item["id"] = fl.item_id(item)
    return item


def earnings_headline(symbol, date, when):
    label = {"pre": "before the open", "post": "after the close", "unknown": "time not confirmed"}[when]
    return "%s reports %s, %s" % (symbol, date, label)


def validate_items(raws):
    """A JSON list -> (valid items, [{"index", "reason", "item"}]). A {"_help": ...} element is the template's
    vocabulary block and is ignored; anything else that is not a valid item is listed, never dropped quietly."""
    if isinstance(raws, dict) and isinstance(raws.get("items"), list):
        raws = raws["items"]  # {"note": ..., "items": [...]} is fine too
    if not isinstance(raws, list):
        raise IntelError("the file must hold a JSON list of items (see intel.py template)")
    good, bad = [], []
    for i, raw in enumerate(raws):
        if isinstance(raw, dict) and HELP_KEY in raw:
            continue
        item, reason = validate_item(raw)
        if item is None:
            bad.append({"index": i, "reason": reason, "item": raw})
        else:
            good.append(item)
    return good, bad


# ---------------------------------------------------------------- the ledger

def rederive(ledger, now):
    ledger["created_by"] = fl.CREATED_BY
    ledger["as_of"] = fl.iso_utc(now)
    ledger["items"] = fl._newest_first(ledger["items"])
    ledger["count"] = len(ledger["items"])
    ledger["earnings"] = fl.derive_earnings(ledger["items"])
    ledger["macro"] = fl.derive_macro(ledger["items"])
    return ledger


def merge(ledger, items, now):
    """Items into the ledger: a new id is added (stamped added_at), a known id is a duplicate and
    only fills blanks (url, note, tags, impact) on the stored item. -> (new, duplicates)."""
    index = dict((it["id"], it) for it in ledger["items"] if isinstance(it, dict) and it.get("id"))
    new, dup = [], []
    for it in items:
        old = index.get(it["id"])
        if old is None:
            it["added_at"] = fl.iso_utc(now)
            ledger["items"].append(it)
            index[it["id"]] = it
            new.append(it)
            continue
        if it.get("url") and not old.get("url"):
            old["url"] = it["url"]
        if it.get("note") and not old.get("note"):
            old["note"] = it["note"]
        if it.get("tags") and not old.get("tags"):
            old["tags"] = it["tags"]
        if it.get("impact") not in (None, "?") and old.get("impact") in (None, "?"):
            old["impact"] = it["impact"]
        dup.append(old)
    rederive(ledger, now)
    return new, dup


def save(ledger, path):
    io.write_json(path, ledger)


# ---------------------------------------------------------------- rendering

def render_item(it, indent="  "):
    """Two lines: time (New York), symbol, kind, impact, source, headline; then tags, note, link."""
    t = fl.parse_utc(it.get("time"))
    when = fl.et_label(t) if t else "time unknown"
    sym = it.get("symbol") or "MARKET"
    kind = it.get("kind") or ""
    if kind == "macro":
        mark = "%s %s" % (it.get("impact_level") or "?", it.get("clock") or "--:--")
    elif kind == "earnings":
        mark = "%s %s" % (it.get("date") or "", it.get("when") or "")
    else:
        mark = it.get("impact") or "?"
    head = it.get("headline") or it.get("event") or ""
    lines = ["%s%s  %-7s %-9s %-16s %-11s %s" % (indent, when, sym, kind, mark, it.get("source") or "", head)]
    extra = []
    tags = [t for t in (it.get("tags") or []) if not (kind == "macro" and t == "macro") and not (kind == "earnings" and t == "earnings")]
    if tags:
        extra.append("tags: " + ", ".join(tags))
    if it.get("note"):
        extra.append("note: " + it["note"])
    extra.append(it.get("url") or "(no link; source: %s)" % (it.get("source") or "?"))
    lines.append(indent + "      " + "  ".join(extra))
    return lines


def render_earnings(sym, rec):
    if not rec or not rec.get("date"):
        return "%s: no earnings date in the ledger; confirm it on the company's IR page." % sym
    label = {"pre": "before the open", "post": "after the close"}.get(rec.get("when"), "time not confirmed")
    out = "%s reports %s, %s (%s%s)" % (sym, rec["date"], label, rec.get("source") or "?",
                                        (", added " + rec["added_at"]) if rec.get("added_at") else "")
    if rec.get("url"):
        out += " " + rec["url"]
    hist = rec.get("history") or []
    if hist:
        out += "  [earlier: " + "; ".join("%s %s (%s)" % (h.get("date"), h.get("when"), h.get("source")) for h in hist) + "]"
    return out


def render_macro(m):
    return "%s %s  %-6s %s (%s)%s" % (m.get("date"), m.get("clock") or "--:--", m.get("impact_level") or "?",
                                     m.get("event") or "", m.get("source") or "?",
                                     (" " + m["url"]) if m.get("url") else "")


# ---------------------------------------------------------------- commands

def cmd_import(a, now):
    try:
        raws = io.read_json(a.src, None)
    except io.IOError_ as exc:
        print("intel: %s" % exc)
        return 1
    if raws is None:
        print("intel: no file at %s (write one with: intel.py template)" % a.src)
        return 1
    try:
        good, bad = validate_items(raws)
    except IntelError as exc:
        print("intel: %s" % exc)
        return 1
    return _ingest(a, now, good, bad, "import of %s" % a.src)


def _ingest(a, now, good, bad, what):
    ledger = fl.load_ledger(a.ledger)
    new, dup = ([], [])
    if good:
        new, dup = merge(ledger, good, now)
        save(ledger, a.ledger)
    examples = [it for it in new if it.get("headline", "").startswith(EXAMPLE)]
    if a.json:
        print(json.dumps({"what": what, "ledger": a.ledger, "as_of": fl.iso_utc(now), "imported": len(new),
                          "duplicates": len(dup), "invalid": bad, "examples": len(examples),
                          "count": len(ledger["items"]), "new": new,
                          "earnings": dict((s, ledger["earnings"][s]) for s in
                                           sorted(set(it["symbol"] for it in new if it["kind"] == "earnings"))),
                          "macro": [m for m in ledger["macro"] if any(m.get("id") == it["id"] for it in new)]},
                         indent=2))
        return 1 if bad else 0
    print("%s: %d new, %d already in the ledger, %d invalid -> %s (%d items, as of %s)" % (
        what, len(new), len(dup), len(bad), a.ledger, len(ledger["items"]), fl.et_label(now)))
    for b in bad:
        print("  item %d skipped: %s" % (b["index"], b["reason"]))
    for it in new:
        for ln in render_item(it):
            print(ln)
    if examples:
        print("note: %d of the new items still say %s; they came from the template, not a connector. "
              "Replace them in the items file and import again, or prune." % (len(examples), EXAMPLE))
    if not good and not bad:
        print("nothing to import: the file holds no items.")
        return 1
    return 1 if bad else 0


def item_from_args(a, now):
    raw = {"symbol": a.symbol, "kind": a.kind, "source": a.source, "url": a.url,
           "time": a.time or fl.iso_utc(now), "headline": a.headline, "note": a.note,
           "tags": a.tags, "impact": a.impact}
    if a.kind == "earnings":
        raw["date"] = a.date
        raw["when"] = a.when or "unknown"
    if a.kind == "macro":
        raw["date"] = a.date
        raw["event"] = a.event or a.headline
        raw["impact_level"] = a.impact_level
        raw["clock"] = a.clock
    return raw


def cmd_add(a, now):
    raw = item_from_args(a, now)
    item, reason = validate_item(raw)
    if item is None:
        print("intel: %s" % reason)
        return 1
    return _ingest(a, now, [item], [], "add")


def cmd_add_earnings(a, now):
    raw = {"symbol": a.symbol, "kind": "earnings", "source": a.source, "url": a.url,
           "time": a.time or fl.iso_utc(now), "headline": a.headline, "note": a.note,
           "date": a.date, "when": a.when, "impact": "?"}
    item, reason = validate_item(raw)
    if item is None:
        print("intel: %s" % reason)
        return 1
    return _ingest(a, now, [item], [], "add-earnings")


def cmd_add_macro(a, now):
    raw = {"symbol": None, "kind": "macro", "source": a.source, "url": a.url,
           "time": a.time or fl.iso_utc(now), "headline": a.event, "event": a.event, "note": a.note,
           "date": a.date, "impact_level": a.impact, "clock": a.clock, "impact": "?"}
    item, reason = validate_item(raw)
    if item is None:
        print("intel: %s" % reason)
        return 1
    return _ingest(a, now, [item], [], "add-macro")


def select(ledger, now, symbol=None, hours=24, kinds=None):
    out = []
    cutoff = now - datetime.timedelta(hours=hours) if hours and hours > 0 else None
    for it in ledger["items"]:
        if symbol and (it.get("symbol") or "").upper() != symbol:
            continue
        if kinds and it.get("kind") not in kinds:
            continue
        t = fl.parse_utc(it.get("time"))
        if cutoff is not None and (t is None or t < cutoff):
            continue
        if t is not None and t > now + fl.GRACE:
            continue  # stamped after a pinned clock
        out.append(it)
    return fl._newest_first(out)


def cmd_show(a, now):
    ledger = fl.load_ledger(a.ledger)
    symbol = (a.symbol or "").upper() or None
    kinds = [k.strip().lower() for k in (a.kind or "").split(",") if k.strip()]
    for k in kinds:
        if k not in fl.KINDS:
            print("intel: --kind %r is not one of %s" % (k, ", ".join(fl.KINDS)))
            return 1
    items = select(ledger, now, symbol, a.hours, kinds)
    who = symbol or "every symbol"
    window = ("last %g h" % a.hours) if a.hours and a.hours > 0 else "all time"
    if a.json:
        print(json.dumps({"symbol": symbol, "hours": a.hours, "kinds": kinds, "as_of": fl.iso_utc(now),
                          "ledger_as_of": ledger.get("as_of"), "count": len(items), "items": items,
                          "earnings": ledger["earnings"].get(symbol) if symbol else ledger["earnings"],
                          "macro": ledger["macro"]}, indent=2))
        return 0 if items else 1
    if not items:
        print("Nothing in %s for %s in the %s%s, as of %s." % (
            a.ledger, who, window, (" (kind %s)" % ",".join(kinds)) if kinds else "", fl.et_label(now)))
        return 1
    print("%s, %s, %d item%s, as of %s (ledger written %s):" % (
        who, window, len(items), "" if len(items) == 1 else "s", fl.et_label(now), ledger.get("as_of") or "never"))
    for it in items:
        for ln in render_item(it):
            print(ln)
    if symbol:
        print(render_earnings(symbol, ledger["earnings"].get(symbol)))
    print(FOOTER)
    return 0


def cmd_flags(a, now):
    ledger = fl.load_ledger(a.ledger)
    symbol = a.symbol.upper()
    date = a.date or clock.to_eastern(now).date().isoformat()
    if fl.as_date(date) is None:
        print("intel: --date wants YYYY-MM-DD, got %r" % a.date)
        return 1
    res = fl.flags_for(symbol, date, ledger, now)
    if a.json:
        print(json.dumps(dict(res, symbol=symbol, date=date, as_of=fl.iso_utc(now), ledger=a.ledger,
                              meaning=dict((f, fl.FLAG_MEANING[f]) for f in res["flags"])), indent=2))
        return 0
    if not res["flags"]:
        print("%s, %s, as of %s: no flags in %s." % (symbol, date, fl.et_label(now), a.ledger))
    else:
        print("%s, %s, as of %s: %d flag%s" % (symbol, date, fl.et_label(now), len(res["flags"]),
                                              "" if len(res["flags"]) == 1 else "s"))
        for f in res["flags"]:
            print("  %-19s %s" % (f, fl.FLAG_MEANING[f]))
            for it in res["items"][f]:
                if it.get("kind") == "macro" or ("impact_level" in it and "kind" not in it):
                    print("        " + render_macro(it))
                elif "kind" in it:
                    for ln in render_item(it, indent="        "):
                        print(ln)
                else:
                    print("        " + render_earnings(symbol, it))
    print(render_earnings(symbol, res["earnings"]))
    print("Flags say what the sources said, as of the time shown. " + FOOTER)
    return 0


def cmd_prune(a, now):
    ledger = fl.load_ledger(a.ledger)
    if a.days <= 0:
        print("intel: --days wants a positive number of days")
        return 1
    cutoff = now - datetime.timedelta(days=a.days)
    today = clock.to_eastern(now).date()
    keep, dropped = [], []
    for it in ledger["items"]:
        t = fl.parse_utc(it.get("time"))
        d = fl.as_date(it.get("date")) if it.get("kind") in ("earnings", "macro") else None
        if (t is not None and t >= cutoff) or (d is not None and d >= today):
            keep.append(it)
        else:
            dropped.append(it)
    ledger["items"] = keep
    rederive(ledger, now)
    if dropped:
        save(ledger, a.ledger)
    if a.json:
        print(json.dumps({"ledger": a.ledger, "days": a.days, "dropped": len(dropped), "kept": len(keep),
                          "dropped_ids": [it.get("id") for it in dropped], "as_of": fl.iso_utc(now)}, indent=2))
        return 0
    print("pruned %d item%s older than %g days from %s; %d kept (future earnings and macro dates stay), as of %s" % (
        len(dropped), "" if len(dropped) == 1 else "s", a.days, a.ledger, len(keep), fl.et_label(now)))
    return 0


def template_items(symbols, now):
    """Example items in the exact import shape. Every headline starts with EXAMPLE: so a stray one is obvious."""
    stamp = fl.iso_utc(now)
    today = clock.to_eastern(now).date()
    report = fl.next_trading_day(today, 2).isoformat()
    help_block = {
        HELP_KEY: "Replace every EXAMPLE with what a connector returned, delete the items you did not find, keep this "
                  "block or delete it (import ignores it). One item per fact, each with its source and its link. "
                  "time is UTC ISO (Z). Never invent a headline, a date or a number.",
        "kinds": list(fl.KINDS), "sources": list(fl.SOURCES), "tags": list(fl.TAGS), "impact": list(fl.IMPACTS),
        "when": list(fl.WHENS), "impact_level": list(fl.LEVELS),
        "then": "python3 intel.py import --from THIS_FILE; python3 intel.py flags SYM",
    }
    out = [help_block]
    for sym in symbols:
        out += [
            {"symbol": sym, "kind": "earnings", "source": "FMP", "url": "https://example.com/earnings-calendar",
             "time": stamp, "headline": "%s %s reports %s after the close (replace with the connector's date and time)" % (EXAMPLE, sym, report),
             "note": "%s say which source gave the date; the company's IR page beats a data vendor" % EXAMPLE,
             "tags": ["earnings"], "impact": "?", "date": report, "when": "post"},
            {"symbol": sym, "kind": "news", "source": "FMP", "url": "https://example.com/story",
             "time": stamp, "headline": "%s the headline exactly as the connector returned it" % EXAMPLE,
             "note": "%s one line on what the story itself says, after reading it (Firecrawl scrape)" % EXAMPLE,
             "tags": ["deal"], "impact": "+"},
            {"symbol": sym, "kind": "analyst", "source": "FMP", "url": "https://example.com/grades",
             "time": stamp, "headline": "%s Firm upgrades %s to Overweight, target 500 (from 450)" % (EXAMPLE, sym),
             "note": None, "tags": ["analyst"], "impact": "+"},
            {"symbol": sym, "kind": "insider", "source": "FMP", "url": "https://example.com/insider",
             "time": stamp, "headline": "%s Officer sold 10,000 shares on 2026-10-23 (Form 4)" % EXAMPLE,
             "note": None, "tags": [], "impact": "-"},
            {"symbol": sym, "kind": "social", "source": "LunarCrush", "url": "https://example.com/social",
             "time": stamp, "headline": "%s %s mentions 2.4x the 7-day average in 24 h" % (EXAMPLE, sym),
             "note": "%s say spike here (or tag it) only when the connector shows one" % EXAMPLE,
             "tags": [], "impact": "0"},
            {"symbol": sym, "kind": "catalyst", "source": "Quartr", "url": "https://example.com/events",
             "time": stamp, "headline": "%s %s investor day on %s" % (EXAMPLE, sym, report),
             "note": None, "tags": [], "impact": "?"},
        ]
    out.append({"symbol": None, "kind": "macro", "source": "Bigdata.com", "url": None, "time": stamp,
                "headline": "%s CPI (September)" % EXAMPLE, "event": "%s CPI (September)" % EXAMPLE,
                "date": today.isoformat(), "impact_level": "HIGH", "clock": "08:30",
                "note": "%s US calendar, HIGH and MEDIUM only" % EXAMPLE, "tags": ["macro"], "impact": "?"})
    return out


def cmd_template(a, now):
    symbols = [s.upper() for s in (a.symbols or ["TSLA"])]
    items = template_items(symbols, now)
    et = clock.to_eastern(now)
    path = a.out or os.path.join(ITEMS_DIR, "items-%s-%s.json" % (et.date().isoformat(), et.strftime("%H%M")))
    if path == "-":
        print(json.dumps(items, indent=2))
        return 0
    io.write_json(path, items)
    n = len(items) - 1
    if a.json:
        print(json.dumps({"out": path, "items": n, "symbols": symbols, "as_of": fl.iso_utc(now)}, indent=2))
        return 0
    print("wrote %d example items for %s to %s" % (n, ", ".join(symbols), path))
    print("Replace every %s with what the connectors returned (source and link on each), delete what you did not "
          "find, then: python3 intel.py import --from %s" % (EXAMPLE, path))
    return 0


# ---------------------------------------------------------------- main

def _common(p):
    p.add_argument("--ledger", default=LEDGER, help="default trader/intel.json")
    p.add_argument("--now", help='pin the clock: "2026-10-26 09:40" (New York) or ...Z (UTC)')
    p.add_argument("--json", action="store_true")


def build_parser():
    ap = argparse.ArgumentParser(prog="intel.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("import", help="merge a JSON list of items into the ledger")
    p.add_argument("--from", dest="src", required=True, metavar="FILE.json")
    _common(p)

    p = sub.add_parser("add", help="one item from flags")
    p.add_argument("--symbol", help="ticker; leave out for a market-wide item")
    p.add_argument("--kind", required=True, choices=fl.KINDS)
    p.add_argument("--source", help="one of %s" % ", ".join(fl.SOURCES))
    p.add_argument("--url")
    p.add_argument("--time", help="UTC ISO like 2026-10-26T12:30:00Z (default: now)")
    p.add_argument("--headline")
    p.add_argument("--note")
    p.add_argument("--tags", help="comma separated: %s" % ", ".join(fl.TAGS))
    p.add_argument("--impact", default="?", choices=fl.IMPACTS)
    p.add_argument("--date", help="earnings or macro: YYYY-MM-DD")
    p.add_argument("--when", choices=fl.WHENS, help="earnings: pre, post or unknown")
    p.add_argument("--event", help="macro: the event")
    p.add_argument("--impact-level", dest="impact_level", choices=fl.LEVELS, help="macro: HIGH, MEDIUM or LOW")
    p.add_argument("--clock", help="macro: HH:MM New York")
    _common(p)

    p = sub.add_parser("add-earnings", help="the next report date for a symbol")
    p.add_argument("symbol")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--when", default="unknown", choices=fl.WHENS)
    p.add_argument("--source", required=True)
    p.add_argument("--url")
    p.add_argument("--time", help="when the source said it, UTC ISO (default: now)")
    p.add_argument("--headline")
    p.add_argument("--note")
    _common(p)

    p = sub.add_parser("add-macro", help="a macro calendar event")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("event")
    p.add_argument("--impact", required=True, choices=fl.LEVELS, help="HIGH, MEDIUM or LOW")
    p.add_argument("--clock", help="HH:MM New York")
    p.add_argument("--source", required=True)
    p.add_argument("--url")
    p.add_argument("--time", help="when the source said it, UTC ISO (default: now)")
    p.add_argument("--note")
    _common(p)

    p = sub.add_parser("show", help="items newest first, New York times")
    p.add_argument("symbol", nargs="?")
    p.add_argument("--hours", type=float, default=24, help="window (0 = everything)")
    p.add_argument("--kind", help="comma separated kinds")
    _common(p)

    p = sub.add_parser("flags", help="the desk flags for a symbol and the items behind each")
    p.add_argument("symbol")
    p.add_argument("--date", help="the trading date (default: today in New York)")
    _common(p)

    p = sub.add_parser("prune", help="drop old items; future earnings and macro dates stay")
    p.add_argument("--days", type=float, default=14)
    _common(p)

    p = sub.add_parser("template", help="write an example items file to fill in")
    p.add_argument("symbols", nargs="*")
    p.add_argument("--out", help="default trader/intel/items-<date>-<HHMM>.json; - prints it")
    _common(p)
    return ap


COMMANDS = {"import": cmd_import, "add": cmd_add, "add-earnings": cmd_add_earnings, "add-macro": cmd_add_macro,
            "show": cmd_show, "flags": cmd_flags, "prune": cmd_prune, "template": cmd_template}


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        now = clock.parse_now(a.now)
    except clock.ClockError as exc:
        print("intel: %s" % exc)
        return 1
    try:
        return COMMANDS[a.cmd](a, now)
    except (IntelError, io.IOError_, clock.ClockError) as exc:
        print("intel: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
