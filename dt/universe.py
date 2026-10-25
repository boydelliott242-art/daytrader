"""The large-cap universe: Nasdaq's screener market caps, filtered by the upstream common-stock rule."""

import os

from .io import now_iso, symbols_from_file, write_json
from .net import money_to_float

TIERS = (("mega", 200e9), ("large", 50e9), ("mid-large", 10e9))


def tier(cap):
    for name, floor in TIERS:
        if cap is not None and cap >= floor:
            return name
    return "small"


def norm_symbol(s):
    return str(s or "").strip().upper().replace("/", ".")


def build_largecap(rows, min_cap=10e9, universe_path="trader/universe.txt"):
    allowed = set(symbols_from_file(universe_path)) if universe_path and os.path.exists(universe_path) else None
    out = []
    for r in rows:
        cap = money_to_float(r.get("marketCap"))
        if cap is None or cap < min_cap:
            continue
        sym = norm_symbol(r.get("symbol"))
        if not sym or (allowed is not None and sym not in allowed):
            continue
        out.append({"symbol": sym, "name": (r.get("name") or "").strip(), "market_cap": cap, "tier": tier(cap),
                    "sector": (r.get("sector") or "").strip() or None, "industry": (r.get("industry") or "").strip() or None,
                    "country": (r.get("country") or "").strip() or None, "ipo_year": (r.get("ipoyear") or "").strip() or None,
                    "last_sale": money_to_float(r.get("lastsale")), "volume": money_to_float(r.get("volume"))})
    out.sort(key=lambda x: -x["market_cap"])
    return {"created_by": "trader-largecap", "as_of": now_iso(), "min_cap_usd": min_cap,
            "universe_rule": "trader/universe.txt" if allowed is not None else "none (screener only)",
            "count": len(out), "symbols": out}


def write_largecap(doc, json_path="trader/largecap.json", txt_path="trader/largecap.txt"):
    write_json(json_path, doc)
    d = os.path.dirname(txt_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as fh:
        for s in doc["symbols"]:
            fh.write("%s|%s\n" % (s["symbol"], s["name"]))
