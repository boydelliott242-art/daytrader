"""The intel ledger (trader-intel): what the connectors found, and the desk flags computed from it.

The ledger is trader/intel.json, written by trader-intel/intel.py:

    {"created_by": "trader-intel", "as_of": "...Z", "items": [...],
     "earnings": {SYM: {"date", "when", "source", "url", "added_at", "history": [...]}},
     "macro": [{"date", "clock", "event", "impact_level", "source", "url"}]}

load_ledger(path) reads it (an empty ledger when the file is missing).
flags_for(symbol, date_str, ledger, now_utc) is pure: no network, no files. It reads
the items and says which desk flags they raise, with the items behind each one.
"""

import datetime
import hashlib
import re

from .clock import to_eastern, trading_day
from .io import read_json

CREATED_BY = "trader-intel"
KINDS = ("earnings", "macro", "news", "analyst", "insider", "social", "filing", "catalyst", "note")
SOURCES = ("Firecrawl", "FMP", "Bigdata.com", "Quartr", "LunarCrush", "SEC", "Yahoo", "other")
# the upstream trader-news tag set, plus "spike" for a social item whose mentions jumped
TAGS = ("earnings", "analyst", "legal", "deal", "capital", "people", "risk", "halt", "regulatory", "macro", "spike")
IMPACTS = ("+", "-", "0", "?")
WHENS = ("pre", "post", "unknown")
LEVELS = ("HIGH", "MEDIUM", "LOW")
RISK_TAGS = ("halt", "legal", "regulatory", "risk")

# the desk flags, in the order they print, with what each one means in plain words
FLAGS = (
    ("earnings_today", "reports today (any time), or reported after the close of the prior trading day: a gap today"),
    ("earnings_tomorrow", "reports the next trading day before the open or at an unknown time: an overnight hold is exposed"),
    ("earnings_week", "reports within the next 5 trading days"),
    ("macro_high_today", "a HIGH impact macro event today"),
    ("news_negative_24h", "a news or filing item with impact - in the last 24 h"),
    ("news_positive_24h", "a news or filing item with impact + in the last 24 h"),
    ("halt_or_risk_24h", "an item tagged halt, legal, regulatory or risk in the last 24 h"),
    ("analyst_action_24h", "an analyst item in the last 24 h"),
    ("insider_selling_7d", "an insider item with impact - in the last 7 days"),
    ("social_spike", "a social item in the last 24 h whose note or tags say spike"),
    ("catalyst", "a catalyst item within 48 h, behind or ahead"),
)
FLAG_MEANING = dict(FLAGS)
GRACE = datetime.timedelta(minutes=10)  # an item stamped a few minutes past a pinned clock still counts

_FRAC = re.compile(r"(\d{2}:\d{2}(?::\d{2})?)\.\d+")


# ---------------------------------------------------------------- time

def parse_utc(text):
    """An ISO time with a Z or an offset -> naive UTC datetime; None when it is not one.

    A naive time is refused (None): the ledger keeps UTC only, so New York and UTC never mix.
    """
    if isinstance(text, datetime.datetime):
        if text.tzinfo is None:
            return text.replace(microsecond=0)
        return (text - text.utcoffset()).replace(tzinfo=None, microsecond=0)
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t:
        return None
    t = _FRAC.sub(r"\1", t)
    if t[-1] in "Zz":
        t = t[:-1] + "+00:00"
    if " " in t and "T" not in t:
        t = t.replace(" ", "T", 1)
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        return None
    return (d - d.utcoffset()).replace(tzinfo=None, microsecond=0)


def iso_utc(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def et_label(utc):
    """Naive UTC -> '2026-10-26 08:30 ET'."""
    return to_eastern(utc).strftime("%Y-%m-%d %H:%M ET")


def naive_utc(now_utc):
    if now_utc is None:
        return datetime.datetime.utcnow().replace(microsecond=0)
    if now_utc.tzinfo is not None:
        return (now_utc - now_utc.utcoffset()).replace(tzinfo=None)
    return now_utc


def as_date(x):
    """'YYYY-MM-DD' or a date -> date; None when it is not one."""
    if isinstance(x, datetime.datetime):
        return x.date()
    if isinstance(x, datetime.date):
        return x
    if not isinstance(x, str):
        return None
    try:
        return datetime.date.fromisoformat(x.strip())
    except ValueError:
        return None


def next_trading_day(d, n=1):
    """The n-th trading day after d (dt.clock.trading_day: weekends and NYSE holidays skipped)."""
    d = as_date(d)
    while n > 0:
        d += datetime.timedelta(days=1)
        if trading_day(d)[0]:
            n -= 1
    return d


def prev_trading_day(d):
    d = as_date(d)
    while True:
        d -= datetime.timedelta(days=1)
        if trading_day(d)[0]:
            return d


def trading_days_after(d, n):
    """The next n trading days after d, as dates."""
    out = []
    cur = as_date(d)
    while len(out) < n:
        cur = next_trading_day(cur)
        out.append(cur)
    return out


# ---------------------------------------------------------------- ids and the ledger

def item_id(item):
    """sha1 of symbol|kind|headline|date-or-time, first 12 hex characters."""
    sym = (item.get("symbol") or "").upper()
    kind = item.get("kind") or ""
    head = str(item.get("headline") or "").strip()
    stamp = item.get("date") if kind in ("earnings", "macro") and item.get("date") else (item.get("time") or "")
    raw = "|".join([sym, kind, head, str(stamp)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def empty_ledger():
    return {"created_by": CREATED_BY, "as_of": None, "items": [], "earnings": {}, "macro": []}


def load_ledger(path):
    """trader/intel.json -> the ledger dict; an empty ledger when the file is missing."""
    doc = read_json(path, None)
    if not isinstance(doc, dict):
        return empty_ledger()
    out = empty_ledger()
    out.update(doc)
    if not isinstance(out.get("items"), list):
        out["items"] = []
    if not isinstance(out.get("earnings"), dict):
        out["earnings"] = {}
    if not isinstance(out.get("macro"), list):
        out["macro"] = []
    return out


def _newest_first(items):
    return sorted(items, key=lambda i: (str(i.get("time") or ""), str(i.get("added_at") or "")), reverse=True)


def earnings_record(it):
    return {"date": it.get("date"), "when": it.get("when") or "unknown", "source": it.get("source"),
            "url": it.get("url"), "time": it.get("time"), "added_at": it.get("added_at"), "id": it.get("id")}


def derive_earnings(items):
    """{SYM: record} from the earnings items: the newest item (by its time, then when it was added)
    wins and the older ones are kept, newest first, in "history"."""
    by = {}
    for it in items:
        if not isinstance(it, dict) or it.get("kind") != "earnings" or not it.get("symbol") or not it.get("date"):
            continue
        by.setdefault(str(it["symbol"]).upper(), []).append(it)
    out = {}
    for sym in sorted(by):
        lst = _newest_first(by[sym])
        # the newest word wins; at the same minute a confirmed time (pre / post) beats "unknown"
        head = [i for i in lst if i.get("time") == lst[0].get("time")]
        best = next((i for i in head if i.get("when") in ("pre", "post")), lst[0])
        lst = [best] + [i for i in lst if i is not best]
        rec = earnings_record(lst[0])
        rec["history"] = [earnings_record(i) for i in lst[1:]]
        out[sym] = rec
    return out


def macro_record(it):
    return {"date": it.get("date"), "clock": it.get("clock"), "event": it.get("event") or it.get("headline"),
            "impact_level": it.get("impact_level"), "source": it.get("source"), "url": it.get("url"),
            "time": it.get("time"), "id": it.get("id")}


def derive_macro(items):
    """The macro calendar from the macro items, by date then clock."""
    out = [macro_record(it) for it in items
           if isinstance(it, dict) and it.get("kind") == "macro" and it.get("date")]
    out.sort(key=lambda m: (m["date"], m.get("clock") or "99:99", m.get("event") or ""))
    return out


def earnings_of(symbol, ledger, items=None):
    """The symbol's earnings record: the ledger's map first, else derived from its items."""
    symbol = (symbol or "").upper()
    ledger = ledger or {}
    rec = (ledger.get("earnings") or {}).get(symbol)
    if isinstance(rec, dict) and rec.get("date"):
        return rec
    items = ledger.get("items", []) if items is None else items
    return derive_earnings(items).get(symbol)


def macro_entries(ledger, items=None):
    """Macro entries: from the macro items when there are any, else the ledger's macro list."""
    ledger = ledger or {}
    items = ledger.get("items", []) if items is None else items
    out = derive_macro(items)
    if out:
        return out
    return [m for m in (ledger.get("macro") or []) if isinstance(m, dict) and m.get("date")]


# ---------------------------------------------------------------- the flags

def _within(it, now, hours, ahead_hours=0):
    """The item's time is inside the last `hours` hours (or up to ahead_hours ahead)."""
    t = parse_utc(it.get("time"))
    if t is None:
        return False
    lo = now - datetime.timedelta(hours=hours)
    hi = now + (datetime.timedelta(hours=ahead_hours) if ahead_hours else GRACE)
    return lo <= t <= hi


def _tags(it):
    tags = it.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return [str(t).strip().lower() for t in tags if str(t).strip()]


def _says_spike(it):
    note = str(it.get("note") or "").lower()
    return "spike" in note or "spike" in _tags(it)


def flags_for(symbol, date_str, ledger, now_utc):
    """-> {"flags": [...], "items": {flag: [items]}, "earnings": record | None}

    Pure: no network, no files. date_str is the trading date the flags are for
    (default: the New York date of now_utc); now_utc is the clock for the 24 h,
    48 h and 7-day windows. Trading days come from dt.clock.trading_day.
    """
    symbol = (symbol or "").upper()
    now = naive_utc(now_utc)
    today = as_date(date_str) or to_eastern(now).date()
    items = [it for it in (ledger or {}).get("items", []) if isinstance(it, dict)]
    mine = [it for it in items if (it.get("symbol") or "").upper() == symbol]
    found = {}

    earn = earnings_of(symbol, ledger, items)
    if earn and as_date(earn.get("date")):
        ed = as_date(earn["date"])
        when = earn.get("when") or "unknown"
        behind = [it for it in mine if it.get("kind") == "earnings" and it.get("date") == earn["date"]] or [earn]
        if ed == today or (ed == prev_trading_day(today) and when == "post"):
            found["earnings_today"] = behind
        if ed == next_trading_day(today) and when in ("pre", "unknown"):
            found["earnings_tomorrow"] = behind
        if today <= ed <= trading_days_after(today, 5)[-1]:
            found["earnings_week"] = behind

    macro = [m for m in macro_entries(ledger, items)
             if m.get("impact_level") == "HIGH" and as_date(m.get("date")) == today]
    if macro:
        found["macro_high_today"] = macro

    neg = [it for it in mine if it.get("kind") in ("news", "filing") and it.get("impact") == "-" and _within(it, now, 24)]
    if neg:
        found["news_negative_24h"] = neg
    pos = [it for it in mine if it.get("kind") in ("news", "filing") and it.get("impact") == "+" and _within(it, now, 24)]
    if pos:
        found["news_positive_24h"] = pos
    risk = [it for it in mine if set(_tags(it)) & set(RISK_TAGS) and _within(it, now, 24)]
    if risk:
        found["halt_or_risk_24h"] = risk
    analyst = [it for it in mine if it.get("kind") == "analyst" and _within(it, now, 24)]
    if analyst:
        found["analyst_action_24h"] = analyst
    insider = [it for it in mine if it.get("kind") == "insider" and it.get("impact") == "-" and _within(it, now, 24 * 7)]
    if insider:
        found["insider_selling_7d"] = insider
    social = [it for it in mine if it.get("kind") == "social" and _within(it, now, 24) and _says_spike(it)]
    if social:
        found["social_spike"] = social
    cat = [it for it in mine if it.get("kind") == "catalyst" and _within(it, now, 48, ahead_hours=48)]
    if cat:
        found["catalyst"] = cat

    flags = [f for f, _ in FLAGS if f in found]
    return {"flags": flags, "items": dict((f, found[f]) for f in flags), "earnings": earn}
