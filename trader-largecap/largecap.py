#!/usr/bin/env python3
"""The large-cap universe for trader-largecap. Standard library only.

Reads Nasdaq's stock screener (one request: symbol, name, market cap, sector,
industry, country), keeps the rows at or above the cap floor whose symbol is
in the upstream trader/universe.txt (the common-stock and ADR rule written by
scan.py universe), and writes the list every other desk tool scans.

    python3 largecap.py build --write                     # Nasdaq screener -> trader/largecap.json + trader/largecap.txt
    python3 largecap.py build --min-cap 50e9 --write      # a higher floor
    python3 largecap.py build --offline screener.json     # a saved screener answer, no network
    python3 largecap.py show --tier mega --top 20         # the biggest names in the list
    python3 largecap.py show --sector Technology --json
    python3 largecap.py check TSLA                        # in the list? cap, tier, sector, industry

The floor is --min-cap, else largecap_min_cap_usd in trader/config.json, else
$10B. Tiers, used by the lab and the desk everywhere: mega at or above $200B,
large from $50B to $200B, mid-large from $10B to $50B.

Files (CONTRACTS.md, "The universe"):
  trader/universe.txt    the upstream list, one SYMBOL|Name a line (scan.py universe writes it)
  trader/largecap.json   {"created_by", "as_of", "min_cap_usd", "count", "symbols": [{"symbol", "name",
                         "market_cap", "tier", "sector", "industry", "country", "ipo_year"}]}, largest first
  trader/largecap.txt    one SYMBOL|Name a line, the shape scan.py --watchlist reads

A symbol with a slash in the screener (BRK/B) is written with a dot (BRK.B),
the way the upstream lists spell it. A row without a market cap is dropped.
Being large is not a reason to trade a stock: this is the list the lab
measures and the desk scans, nothing more.

Data: https://api.nasdaq.com/api/screener/stocks?download=true (free,
unofficial, can lag). Exit codes: 0 = done (check: the symbol is in the list),
1 = refused, an error, or (check) not in the list.
"""

import argparse
import datetime
import gzip
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE if os.path.isdir(os.path.join(HERE, "dt")) else os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dt import clock, net, universe  # noqa: E402
from dt import io as dtio  # noqa: E402

DEFAULT_MIN_CAP = 10e9
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_JSON = "trader/largecap.json"
DEFAULT_TXT = "trader/largecap.txt"
TIER_WORDS = {"mega": "mega (at or above $200B)", "large": "large ($50B to $200B)",
              "mid-large": "mid-large ($10B to $50B)", "small": "small (under $10B)"}
TIER_ORDER = ("mega", "large", "mid-large", "small")
NOTE = ("A list of what is big, not of what to trade: the lab measures every name on it and the desk shows the "
        "odds before the money. Most people who day trade lose money.")


class LargecapError(Exception):
    pass


# ---------------------------------------------------------------- words and numbers

def tier_possible(t, min_cap):
    """Can a cap at or above the floor fall in this tier? (the tier's ceiling is above the floor)"""
    ceiling = {"mega": float("inf"), "large": 200e9, "mid-large": 50e9, "small": 10e9}[t]
    return ceiling > float(min_cap)


def money_short(x):
    """5114261000000 -> $5.11T, 49024608422 -> $49.0B, 10e9 -> $10B, 850e6 -> $850M."""
    if x is None:
        return "n/a"
    x = float(x)
    if x >= 1e12:
        return "$%.2fT" % (x / 1e12)
    if x >= 1e9:
        b = x / 1e9
        if abs(b - round(b)) < 0.005:
            return "$%dB" % int(round(b))
        return "$%.1fB" % b
    if x >= 1e6:
        return "$%.0fM" % (x / 1e6)
    return "$%s" % format(int(round(x)), ",")


def n(x):
    return "{:,}".format(int(x))


def cap_of(row):
    return net.money_to_float(row.get("marketCap"))


# ---------------------------------------------------------------- the screener

def read_screener(path):
    """A saved screener answer: the API document ({"data": {"rows": [...]}}) or the rows list, .json or .json.gz."""
    if not os.path.exists(path):
        raise LargecapError("no screener file at %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise LargecapError("could not read %s: %s" % (path, e))
    rows = doc
    if isinstance(doc, dict):
        rows = ((doc.get("data") or {}).get("rows") if isinstance(doc.get("data"), dict) else None) \
            or doc.get("rows") or doc.get("symbols") or []
    if not isinstance(rows, list):
        raise LargecapError("%s is not a screener answer (no rows)" % path)
    return [r for r in rows if isinstance(r, dict)]


def fetch_screener():
    try:
        return net.nasdaq_screener()
    except net.NetError as e:
        raise LargecapError(str(e))


def resolve_floor(min_cap, cfg):
    if min_cap is not None:
        v, src = min_cap, "--min-cap"
    elif cfg.get("largecap_min_cap_usd") is not None:
        v, src = cfg.get("largecap_min_cap_usd"), "largecap_min_cap_usd in the config"
    else:
        v, src = DEFAULT_MIN_CAP, "the default"
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise LargecapError("%s is not a dollar amount: %r" % (src, v))
    if not v > 0:
        raise LargecapError("the cap floor must be above $0 (%s gave %r)" % (src, v))
    return v, src


def resolve_universe(path):
    """The upstream symbol list to filter by, or None when the user says 'none' (screener only)."""
    if path is None or str(path).strip().lower() in ("none", "-", ""):
        return None
    if not os.path.exists(path):
        raise LargecapError("no %s: run scan.py universe first (it writes the common-stock list), pass --universe PATH, "
                            "or --universe none to take the screener alone" % path)
    return path


def dedupe(doc):
    """One row per symbol (the first, which is the largest cap after the sort)."""
    seen, out = set(), []
    for s in doc["symbols"]:
        if s["symbol"] in seen:
            continue
        seen.add(s["symbol"])
        out.append(s)
    doc["symbols"] = out
    doc["count"] = len(out)
    return doc


def build(rows, min_cap, universe_path, now_utc):
    """rows (screener) -> (the largecap.json document, the counts). Raises LargecapError when nothing is kept."""
    if not rows:
        raise LargecapError("the screener has no rows: nothing to build the list from")
    doc = dedupe(universe.build_largecap(rows, min_cap, universe_path))
    doc["as_of"] = clock.iso(now_utc)
    allowed = None
    if universe_path:
        allowed = set(dtio.symbols_from_file(universe_path))
        doc["universe_symbols"] = len(allowed)
    at_floor = sum(1 for r in rows if (cap_of(r) or 0) >= min_cap)
    counts = {"rows": len(rows), "at_or_above_floor": at_floor, "kept": doc["count"],
              "dropped_below_floor": len(rows) - at_floor,
              "dropped_not_in_universe": at_floor - doc["count"],
              "by_tier": {t: sum(1 for s in doc["symbols"] if s["tier"] == t) for t in TIER_ORDER
                          if tier_possible(t, min_cap)},
              "by_sector": dict(Counter((s.get("sector") or "(no sector)") for s in doc["symbols"]).most_common()),
              "universe_symbols": len(allowed) if allowed is not None else None}
    doc["counts"] = counts
    if not doc["symbols"]:
        where = (" in %s" % universe_path) if universe_path else ""
        raise LargecapError("kept 0 of %s screener rows: nothing at or above %s%s" % (n(len(rows)), money_short(min_cap),
                                                                                       where))
    return doc, counts


def load_largecap(path):
    if not os.path.exists(path):
        raise LargecapError("no %s yet: run largecap.py build --write first" % path)
    try:
        doc = dtio.read_json(path)
    except dtio.IOError_ as e:
        raise LargecapError(str(e))
    if not isinstance(doc, dict) or not isinstance(doc.get("symbols"), list):
        raise LargecapError("%s is not a large-cap list (no symbols): run largecap.py build --write" % path)
    return doc


# ---------------------------------------------------------------- commands

def describe_build(doc, counts, source, written):
    tiers = ", ".join("%s %s" % (n(counts["by_tier"][t]), TIER_WORDS[t]) for t in TIER_ORDER if t in counts["by_tier"])
    top_sectors = list(counts["by_sector"].items())[:5]
    rule = ("%s (%s symbols)" % (doc.get("universe_rule"), n(doc["universe_symbols"]))
            if doc.get("universe_symbols") is not None else "none (the screener alone)")
    lines = [
        "Nasdaq screener (%s): %s rows. Floor %s. Universe rule: %s." % (source, n(counts["rows"]),
                                                                            money_short(doc["min_cap_usd"]), rule),
        "Kept %s large caps: %s." % (n(counts["kept"]), tiers),
        "Dropped %s under the floor or without a market cap, %s not in the universe file."
        % (n(counts["dropped_below_floor"]), n(counts["dropped_not_in_universe"])),
        "Sectors: %s (%d of %d)." % (", ".join("%s %s" % (k, n(v)) for k, v in top_sectors), len(top_sectors),
                                     len(counts["by_sector"])),
        "Largest: %s." % ", ".join("%s %s" % (s["symbol"], money_short(s["market_cap"])) for s in doc["symbols"][:5]),
    ]
    if written:
        lines.append("Wrote %s and %s (%s SYMBOL|Name lines)." % (written[0], written[1], n(counts["kept"])))
    else:
        lines.append("Nothing written (add --write for %s and %s)." % (DEFAULT_JSON, DEFAULT_TXT))
    lines.append(NOTE)
    return lines


def cmd_build(a):
    now = clock.parse_now(a.now)
    cfg = dtio.read_config(a.config)
    min_cap, floor_src = resolve_floor(a.min_cap, cfg)
    upath = resolve_universe(a.universe)
    if a.offline:
        rows, source = read_screener(a.offline), a.offline
    else:
        rows, source = fetch_screener(), "api.nasdaq.com"
    doc, counts = build(rows, min_cap, upath, now)
    doc["source"] = source
    doc["floor_from"] = floor_src
    written = None
    if a.write:
        universe.write_largecap(doc, a.out, a.txt)
        written = (a.out, a.txt)
    if getattr(a, "publish", False):
        dtio.write_json(os.path.join(a.docs_data, "largecap.json"), doc)
    if a.json:
        out = dict(doc, written=list(written) if written else None)
        print(json.dumps(out, indent=2))
        return 0
    for line in describe_build(doc, counts, source, written):
        print(line)
    return 0


def filter_symbols(doc, tier=None, sector=None):
    """[(rank, row)] of the list, rank 1 = the largest cap, filtered by tier and sector (case-insensitive)."""
    out = []
    for i, s in enumerate(doc["symbols"]):
        if tier and s.get("tier") != tier:
            continue
        if sector:
            have = (s.get("sector") or "").lower()
            want = sector.lower().strip()
            if have != want and want not in have:
                continue
        out.append((i + 1, s))
    return out


def cmd_show(a):
    doc = load_largecap(a.largecap)
    matched = filter_symbols(doc, a.tier, a.sector)
    shown = matched[: a.top] if a.top and a.top > 0 else matched
    what = ", ".join(x for x in (("tier %s" % a.tier) if a.tier else "", ("sector %s" % a.sector) if a.sector else "")
                     if x) or "every tier and sector"
    if a.json:
        print(json.dumps({"as_of": doc.get("as_of"), "min_cap_usd": doc.get("min_cap_usd"), "count": len(doc["symbols"]),
                          "filter": {"tier": a.tier, "sector": a.sector, "top": a.top}, "matched": len(matched),
                          "shown": len(shown), "symbols": [dict(s, rank=r) for r, s in shown]}, indent=2))
        return 0 if matched else 1
    if not matched:
        print("No symbol in %s matches %s (%s symbols, floor %s)." % (a.largecap, what, n(len(doc["symbols"])),
                                                                       money_short(doc.get("min_cap_usd"))))
        return 1
    print("Large caps in %s: %s of %s match %s, showing %s (as of %s, floor %s)." % (
        a.largecap, n(len(matched)), n(len(doc["symbols"])), what, n(len(shown)), doc.get("as_of"),
        money_short(doc.get("min_cap_usd"))))
    print("  %4s  %-7s %-38s %9s  %-9s  %s" % ("rank", "symbol", "name", "cap", "tier", "sector"))
    for r, s in shown:
        name = (s.get("name") or "")[:38]
        print("  %4d  %-7s %-38s %9s  %-9s  %s" % (r, s["symbol"], name, money_short(s.get("market_cap")),
                                                   s.get("tier") or "", s.get("sector") or ""))
    print(NOTE)
    return 0


def cmd_check(a):
    doc = load_largecap(a.largecap)
    sym = universe.norm_symbol(a.symbol)
    if not sym:
        raise LargecapError("check wants a ticker symbol")
    found = next(((i + 1, s) for i, s in enumerate(doc["symbols"]) if s["symbol"] == sym), None)
    count = len(doc["symbols"])
    if a.json:
        out = {"symbol": sym, "in_list": bool(found), "count": count, "min_cap_usd": doc.get("min_cap_usd"),
               "as_of": doc.get("as_of")}
        if found:
            out.update(found[1])
            out["rank"] = found[0]
        print(json.dumps(out, indent=2))
        return 0 if found else 1
    if not found:
        print("%s is not in %s (%s symbols at or above %s, as of %s): under the floor, not a common stock or ADR in "
              "trader/universe.txt, or not in Nasdaq's screener." % (sym, a.largecap, n(count),
                                                                      money_short(doc.get("min_cap_usd")), doc.get("as_of")))
        return 1
    rank, s = found
    print("%s is in the large-cap list: #%d of %s, %s, tier %s." % (sym, rank, n(count), money_short(s.get("market_cap")),
                                                                     TIER_WORDS.get(s.get("tier"), s.get("tier"))))
    print("  %s. Sector %s, industry %s, country %s%s." % (
        (s.get("name") or "(no name)").rstrip("."), s.get("sector") or "n/a", s.get("industry") or "n/a", s.get("country") or "n/a",
        (", listed %s" % s["ipo_year"]) if s.get("ipo_year") else ""))
    print("  List as of %s, floor %s. In the list means the lab measures it; it says nothing about today." % (
        doc.get("as_of"), money_short(doc.get("min_cap_usd"))))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The large-cap universe (trader-largecap): Nasdaq's screener market caps "
                                             "filtered by the upstream common-stock rule.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("build", help="read the screener, apply the floor and the universe rule, print the counts")
    p.add_argument("--min-cap", type=float, help="market-cap floor in dollars, e.g. 10e9 (default: "
                                                 "largecap_min_cap_usd in the config, else 10e9)")
    p.add_argument("--universe", default=DEFAULT_UNIVERSE,
                   help="the upstream SYMBOL|Name list the screener is filtered by (default %s); 'none' skips the rule"
                        % DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="FILE", help="a saved screener answer (.json or .json.gz), no network")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--out", default=DEFAULT_JSON, help="where --write puts the JSON list (default %s)" % DEFAULT_JSON)
    p.add_argument("--txt", default=DEFAULT_TXT, help="where --write puts the SYMBOL|Name list (default %s)" % DEFAULT_TXT)
    p.add_argument("--write", action="store_true", help="write the two files")
    p.add_argument("--publish", action="store_true", help="also copy the list to docs/data/largecap.json for the dashboard")
    p.add_argument("--docs-data", default=os.path.join("docs", "data"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--now", help='pin the clock: "2026-10-19 10:15" (New York) or ISO with Z')
    p = sub.add_parser("show", help="print the list, filtered")
    p.add_argument("--largecap", default=DEFAULT_JSON)
    p.add_argument("--tier", choices=list(TIER_ORDER))
    p.add_argument("--sector", help="sector name (case-insensitive, a part of the name works)")
    p.add_argument("--top", type=int, default=25, help="rows to show (0 = all), default 25")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check", help="is a symbol in the list? its cap, tier, sector, industry")
    p.add_argument("symbol")
    p.add_argument("--largecap", default=DEFAULT_JSON)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"build": cmd_build, "show": cmd_show, "check": cmd_check}[a.cmd](a)
    except (LargecapError, clock.ClockError, dtio.IOError_) as e:
        print("largecap.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
