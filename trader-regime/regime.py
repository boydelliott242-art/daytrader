#!/usr/bin/env python3
"""Market regime for trader-regime: the context around every setup, from the bar store and
from live Yahoo bars. Standard library only. It never places an order and never predicts.

    python3 regime.py daily                               # the next session's daily block + tags, from the stores
    python3 regime.py daily --date 2026-10-23 --write     # a past session, written to trader/regime.json
    python3 regime.py live --at 10:30 --write             # the intraday read at a cutoff (Yahoo: 13 charts + spark)
    python3 regime.py live --at 10:30 --offline tests/fixtures/trader-regime --now "2026-10-23 10:30"
    python3 regime.py live --at 10:30 --from-cache --now "2026-10-20 10:30"   # replay a stored session, no network
    python3 regime.py tags 2026-10-23                     # the tags the lab uses for a session date

daily: the SPY close against its 20- and 50-day averages (trend up when close > 20-day > 50-day,
down when close < 20-day < 50-day, flat otherwise), the 5-day SPY return, the VIX close and its
bucket (<15, 15-20, 20-30, >30), VIX against VIX3M (backwardation when the VIX is above it), and
the prior day's SPY range against its 20-day ATR (quiet under 0.7, wide over 1.3). Only daily rows
dated BEFORE the session count: the block for a date is what was knowable before that open, and
dt.regime.session_tags gives the lab the same tags for every past session (no look-ahead).

live: SPY and QQQ at a cutoff from their 5-minute bars (completed bars only): the gap from the
prior close, above / inside / below the 15-minute opening range (the 9:30, 9:35 and 9:40 bars),
the distance from VWAP, the move since the open; the 11 sector ETFs ranked by their move since
9:30; breadth across the large-cap list from ONE spark pass (closes only, so "above the open"
compares the last completed close with the 9:30 bar's close, and "up on the day" uses Yahoo's
chartPreviousClose).

Writes trader/regime.json (the CONTRACTS.md shape); --publish copies it to trader/publish/regime.json.
Refused, in one plain line: no SPY store, a date with no prior rows, a weekend or holiday, a live
cutoff before 9:50 (the opening range is still forming). Times are New York; --now "2026-10-23
10:30" pins the clock. Exit code 0 = a read, 1 = refused or an error.
"""

import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _base in (HERE, ROOT):
    if os.path.isdir(os.path.join(_base, "dt")):
        if _base not in sys.path:
            sys.path.insert(0, _base)
        break

from dt import bars, clock, net  # noqa: E402
from dt import io as dtio  # noqa: E402
from dt import regime as daily_ctx  # noqa: E402

SECTORS = [("XLK", "Technology"), ("XLF", "Financials"), ("XLE", "Energy"), ("XLV", "Health Care"),
           ("XLY", "Consumer Discretionary"), ("XLP", "Consumer Staples"), ("XLI", "Industrials"),
           ("XLU", "Utilities"), ("XLB", "Materials"), ("XLRE", "Real Estate"), ("XLC", "Communication Services")]
SECTOR_NAMES = dict(SECTORS)
CONTEXT_SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "^VIX", "^VIX3M"] + [e for e, _ in SECTORS]
OR_MINUTES = 15
OR_BARS = OR_MINUTES // clock.BAR_MIN     # slots 0, 1, 2
FIRST_LIVE_SLOT = OR_BARS                 # the first bar after the range: slot 3, closes at 9:50
GAP_FLAT_PCT = 0.3                        # a gap inside +-0.3 % reads as flat
BREADTH_BROAD_PCT = 70.0                  # 70 % up (or down) reads as broad
MAX_BACK_DAYS = 6                         # Yahoo's free 5-minute "5d" window, the scanner's rule
DEFAULT_CACHE = os.path.join("trader", "cache")
DEFAULT_OUT = os.path.join("trader", "regime.json")
DEFAULT_DOCS = os.path.join("trader", "publish", "regime.json")
DEFAULT_SYMBOLS = os.path.join("trader", "largecap.txt")
VIX_WORDS = {"vix_lt15": "<15", "vix_15_20": "15-20", "vix_20_30": "20-30", "vix_gt30": ">30"}
TREND_WORDS = {"trend_up": "up", "trend_down": "down", "trend_flat": "flat"}
RANGE_WORDS = {"prev_quiet": "quiet", "prev_normal": "normal", "prev_wide": "wide"}
DAILY_KEYS = ("spy_close", "spy_sma20", "spy_sma50", "trend", "ret5d_pct", "vix", "vix_bucket", "vix3m",
              "backwardation", "prev_range_atr")
LIVE_KEYS = ("at", "spy_gap_pct", "spy_or", "spy_vs_vwap_pct", "breadth_above_open_pct", "breadth_up_pct", "sectors")
NOTE = ("Context, not a forecast: a regime tag says what the market did before this session, never what it "
        "will do. Most people who day trade lose money.")
BREADTH_NOTE = ("spark carries closes only: 'above the open' compares each stock's last completed close with its "
                "9:30 bar's close, 'up on the day' with Yahoo's chartPreviousClose")


class RegimeError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def r4(x):
    return None if x is None else round(float(x), 4)


def r2(x):
    return None if x is None else round(float(x), 2)


def r3(x):
    return None if x is None else round(float(x), 3)


def pct_of(a, b):
    """a over b, as a percent change, 3 decimals; None when b is missing or zero."""
    if a is None or not b:
        return None
    return round((float(a) / float(b) - 1.0) * 100.0, 3)


def signed(x, nd=2):
    return ("%+." + str(nd) + "f") % x


def parse_date(text, what="--date"):
    try:
        return datetime.date.fromisoformat(str(text).strip())
    except ValueError:
        raise RegimeError("%s wants YYYY-MM-DD, got %r" % (what, text))


def next_trading_day(d):
    d = d + datetime.timedelta(days=1)
    while not clock.trading_day(d)[0]:
        d += datetime.timedelta(days=1)
    return d


def prev_trading_day(d):
    d = d - datetime.timedelta(days=1)
    while not clock.trading_day(d)[0]:
        d -= datetime.timedelta(days=1)
    return d


def need_trading_day(d, what):
    ok, why = clock.trading_day(d)
    if not ok:
        raise RegimeError("No session on %s (%s): %s wants a trading day." % (clock.day_label(d), why, what))


def bar_close_label(day, k):
    """'10:25 AM': the clock time at which slot k's bar closes."""
    return clock.clock_label(clock.slot_time(day, k + 1))


# ---------------------------------------------------------------- the stores

def load_stores(root, need_spy=True):
    """{"SPY": rows, "^VIX": rows, "^VIX3M": rows} from the daily stores; refuses without SPY when need_spy."""
    spy = bars.daily_rows("SPY", root)
    if not spy:
        if need_spy:
            raise RegimeError("no SPY daily store at %s: run scripts/fetch_cache.py --only SPY,^VIX,^VIX3M first"
                              % bars.store_path("SPY", "daily", root))
        return None
    return {"SPY": spy, "^VIX": bars.daily_rows("^VIX", root), "^VIX3M": bars.daily_rows("^VIX3M", root)}


def empty_daily():
    d = dict((k, None) for k in DAILY_KEYS)
    d.update({"through": None, "spy_rows": 0, "prev_range": None, "spy_atr20": None})
    return d


def daily_block(date_str, stores):
    """(the CONTRACTS daily block, tags) for a session date, from rows strictly before it (no look-ahead)."""
    spy = stores["SPY"]
    before = [r for r in spy if r[0] < date_str and r[4] is not None]
    if not before:
        raise RegimeError("no SPY daily rows before %s in the store (its first row is %s)" % (date_str, spy[0][0]))
    c = daily_ctx.daily_context(date_str, spy, stores.get("^VIX") or None, stores.get("^VIX3M") or None)
    last = before[-1]
    prev_range = (last[2] - last[3]) if (last[2] is not None and last[3] is not None) else None
    daily = {"spy_close": r2(c["spy_close"]), "spy_sma20": r2(c["spy_sma20"]), "spy_sma50": r2(c["spy_sma50"]),
             "trend": TREND_WORDS.get(c["trend"]), "ret5d_pct": c["ret5d_pct"],
             "vix": c["vix"], "vix_bucket": VIX_WORDS.get(c["vix_bucket"]), "vix3m": c["vix3m"],
             "backwardation": c["backwardation"], "prev_range_atr": c["prev_range_atr"],
             "through": last[0], "spy_rows": len(before), "prev_range": r2(prev_range),
             "spy_atr20": r2(bars.daily_atr(spy, date_str, 20))}
    return daily, list(c["tags"])


def staleness(date_str, daily):
    """A warning when the store does not hold the session right before the date (a stale store)."""
    d = datetime.date.fromisoformat(date_str)
    if d.year not in clock.CALENDAR_YEARS or not daily.get("through"):
        return None
    want = prev_trading_day(d).isoformat()
    if daily["through"] < want:
        return ("the last stored SPY session is %s, not %s (the session before %s): refresh the store with "
                "scripts/fetch_cache.py" % (daily["through"], want, date_str))
    return None


def default_date(spy_rows, now_utc):
    """The session the daily block is for: today before the open on a trading day, else the next
    trading day after the last stored SPY session."""
    now_et = clock.to_eastern(now_utc)
    today = now_et.date()
    if clock.trading_day(today)[0] and now_et.time() < clock.OPEN:
        return today
    return next_trading_day(datetime.date.fromisoformat(spy_rows[-1][0]))


# ---------------------------------------------------------------- the words

def fmt1(x):
    """One decimal, rounded half-up from the stored two decimals (770.15 -> 770.2, never 770.1)."""
    import decimal
    return str(decimal.Decimal(repr(float(x))).quantize(decimal.Decimal("0.1"), rounding=decimal.ROUND_HALF_UP))


def trend_words(d):
    c, s20, s50 = d["spy_close"], d["spy_sma20"], d["spy_sma50"]
    if c is None:
        return "SPY: no close in the store."
    if s20 is None or s50 is None:
        return "SPY %.2f: not enough history for the 20- and 50-day averages (%d daily rows)." % (c, d.get("spy_rows") or 0)

    def rel(a, b):
        return "above" if a > b else "below" if a < b else "at"
    if d["trend"] == "up":
        return "SPY %.2f above its 20-day %s and 50-day %s: trend up." % (c, fmt1(s20), fmt1(s50))
    if d["trend"] == "down":
        return "SPY %.2f below its 20-day %s and 50-day %s: trend down." % (c, fmt1(s20), fmt1(s50))
    return "SPY %.2f %s its 20-day %s, the 20-day %s the 50-day %s: trend flat (mixed)." % (
        c, rel(c, s20), fmt1(s20), rel(s20, s50), fmt1(s50))


def vix_words(d):
    if d["vix"] is None:
        return "VIX: no ^VIX store."
    s = "VIX %.2f (%s)." % (d["vix"], d["vix_bucket"])
    if d["vix3m"] is None:
        return s + " VIX3M: no ^VIX3M store."
    if d["backwardation"]:
        return s + " Over VIX3M %.2f: backwardation, near-term fear above the 3-month." % d["vix3m"]
    return s + " VIX3M %.2f: no backwardation." % d["vix3m"]


def range_words(d, tags):
    if d["prev_range_atr"] is None:
        return "Prior day: no 20-day ATR yet."
    word = "quiet" if d["prev_range_atr"] < 0.7 else "wide" if d["prev_range_atr"] > 1.3 else "normal"
    return "Prior session %s: SPY range %.2f, %.2f x its 20-day ATR %.2f: %s." % (
        clock.day_label(datetime.date.fromisoformat(d["through"])), d["prev_range"] or 0.0, d["prev_range_atr"],
        d["spy_atr20"] or 0.0, word)


def ret_words(d):
    return "" if d["ret5d_pct"] is None else " 5-day return %s %%." % signed(d["ret5d_pct"])


def daily_lines(doc, root):
    d, tags = doc["daily"], doc["tags"]
    day = datetime.date.fromisoformat(doc["date"])
    through = ("daily closes through %s" % clock.day_label(datetime.date.fromisoformat(d["through"]))
               if d.get("through") else "no daily closes")
    out = ["Regime for %s, from %s (SPY, ^VIX, ^VIX3M stores in %s)." % (clock.day_label(day), through, root)]
    if d.get("spy_close") is None:
        out.append("  No SPY daily rows: the daily block is empty.")
    else:
        out.append("  " + trend_words(d) + ret_words(d))
        out.append("  " + vix_words(d))
        out.append("  " + range_words(d, tags))
    out.append("  Tags: %s" % (" ".join(tags) if tags else "(none)"))
    return out


# ---------------------------------------------------------------- live: the sources

class YahooSource(object):
    """v8 charts and one spark pass, 0.5 s apart, like the scanner."""

    def __init__(self, rng, pause):
        self.rng, self.pause, self.name = rng, pause, "yahoo"
        self.calls = 0

    def _pause(self):
        if self.calls and self.pause:
            net.SLEEP(self.pause)
        self.calls += 1

    def chart(self, sym):
        self._pause()
        return net.chart(sym, self.rng, "5m")

    def spark(self, symbols):
        self._pause()
        return net.spark(symbols, self.rng, "5m", pause=self.pause)

    def symbols(self):
        return []


class OfflineSource(object):
    """A folder of saved Yahoo answers: <SYM>.json (v8 charts) and spark.json (one spark answer)."""

    def __init__(self, folder):
        if not os.path.isdir(folder):
            raise RegimeError("no offline folder at %s" % folder)
        self.folder, self.name = folder, "offline"
        self._spark = None

    def _read(self, names):
        for n in names:
            p = os.path.join(self.folder, n)
            if os.path.exists(p):
                return dtio.read_json(p)
        return None

    def chart(self, sym):
        y = net.to_yahoo(sym)
        doc = self._read(["%s.json" % sym, "%s.json.gz" % sym, "%s.json" % y, "%s.json.gz" % y])
        if doc is None:
            raise net.NetError("no %s.json in %s" % (sym, self.folder))
        return net.parse_chart(doc, sym)

    def _spark_all(self):
        if self._spark is None:
            doc = self._read(["spark.json", "spark.json.gz"])
            if doc is None:
                raise RegimeError("no spark.json in %s (the breadth pass needs one)" % self.folder)
            self._spark = net.parse_spark(doc)
        return self._spark

    def spark(self, symbols):
        got = self._spark_all()
        data = dict((s, got[s]) for s in symbols if s in got)
        failed = dict((s, "not in spark.json") for s in symbols if s not in got)
        return data, failed

    def symbols(self):
        return sorted(self._spark_all())

    def last_day(self):
        s = self.chart("SPY")
        days = [clock.to_eastern(clock.utc_of(t)).date() for t in s["t"]]
        return max(days) if days else None


class CacheSource(object):
    """The bar store (trader/cache): a stored session as a Yahoo-shaped series, no network."""

    def __init__(self, root, day):
        self.root, self.day, self.name = root, day, "cache"

    def _series(self, sym):
        store = bars.load_store(sym, "bars5m", self.root)
        rows = ((store or {}).get("sessions") or {}).get(self.day.isoformat())
        if not rows:
            raise net.NetError("no %s session for %s in %s" % (sym, self.day.isoformat(), self.root))
        pc = bars.prev_close(bars.daily_rows(sym, self.root), self.day.isoformat())
        return {"t": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows],
                "l": [r[3] for r in rows], "c": [r[4] for r in rows], "v": [r[5] for r in rows],
                "meta": {"symbol": sym, "chartPreviousClose": pc, "dataGranularity": "5m"}}

    def chart(self, sym):
        return self._series(sym)

    def spark(self, symbols):
        data, failed = {}, {}
        for s in symbols:
            try:
                data[s] = self._series(s)
            except net.NetError as e:
                failed[s] = str(e)
        return data, failed

    def symbols(self):
        d = os.path.join(self.root, "bars5m")
        if not os.path.isdir(d):
            return []
        out = []
        for f in sorted(os.listdir(d)):
            if not f.endswith(".json.gz") or f.startswith("_"):
                continue
            sym = f[:-len(".json.gz")]
            if sym not in CONTEXT_SYMBOLS:
                out.append(sym)
        return out


# ---------------------------------------------------------------- live: the reads

def live_window(now_utc, at, explicit_day=None):
    """-> (day, cutoff naive ET, note) or a RegimeError with the plain message."""
    now = clock.to_eastern(now_utc)
    day = explicit_day or now.date()
    need_trading_day(day, "the live read")
    s = clock.session(day)
    close_dt = datetime.datetime.combine(day, s[1])
    first_close = clock.slot_time(day, FIRST_LIVE_SLOT + 1)
    note = ""
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise RegimeError("--at %s is later than now (%s ET)." % (at.strftime("%H:%M"), clock.clock_label(now)))
        if cutoff > close_dt:
            raise RegimeError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock.clock_label(close_dt),
                                                                         clock.day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff > close_dt:
            cutoff = close_dt
            note = "the market closed at %s ET: this is the full-session read" % clock.clock_label(close_dt)
    if cutoff < first_close:
        raise RegimeError("It is %s ET: the %d-minute opening range runs 9:30 to %s and the first bar after it "
                          "closes at %s. The range is still forming; run live again at %s or later."
                          % (clock.clock_label(cutoff), OR_MINUTES, clock.clock_label(clock.slot_time(day, OR_BARS)),
                             clock.clock_label(first_close), clock.clock_label(first_close)))
    return day, cutoff, note


def session_at(series, day, cutoff):
    """The day's completed 5-minute bars by the cutoff (naive ET): a contiguous prefix from 9:30."""
    sess = bars.series_to_sessions(series, clock.eastern_to_utc(cutoff), include_partial=True)
    return sess.get(day.isoformat()) or []


def session_closes(series, day, cutoff):
    """[(slot, close)] of the day's on-grid regular-session bars complete by the cutoff, closes only (spark)."""
    out = {}
    cs = series.get("c") or []
    for i, ts in enumerate(series.get("t") or []):
        c = cs[i] if i < len(cs) else None
        if c is None:
            continue
        et = clock.to_eastern(clock.utc_of(ts))
        if et.date() != day:
            continue
        k = clock.slot_of(et)
        if k is None or et + datetime.timedelta(minutes=clock.BAR_MIN) > cutoff:
            continue
        out[k] = float(c)
    return [(k, out[k]) for k in sorted(out)]


def prior_close(sym, series, day, root):
    """(close, source): the daily store's close of the session before the day, else the chart's chartPreviousClose."""
    want = prev_trading_day(day).isoformat()
    for r in bars.daily_rows(sym, root):
        if r[0] == want and r[4] is not None:
            return float(r[4]), "daily store"
    pc = (series.get("meta") or {}).get("chartPreviousClose")
    if pc:
        return float(pc), "chart meta"
    return None, None


def index_read(sym, b, day, cutoff, pc, pc_source, atr20=None):
    """SPY or QQQ at the cutoff: gap, the 15-minute opening range, VWAP, the move since the open."""
    if len(b) <= OR_BARS:
        raise RegimeError("%s has only %d completed bar(s) on %s by %s: a hole in the data or a halt; try again"
                          % (sym, len(b), day.isoformat(), clock.clock_label(cutoff)))
    oh, ol = bars.opening_range(b, OR_BARS)
    last = b[-1]["c"]
    vw = bars.vwap_series(b)[-1]
    hi = max(x["h"] for x in b)
    lo = min(x["l"] for x in b)
    return {"symbol": sym, "last": r2(last), "open": r2(b[0]["o"]), "high": r2(hi), "low": r2(lo),
            "bars": len(b), "through": bar_close_label(day, len(b) - 1),
            "prev_close": r2(pc), "prev_close_source": pc_source, "gap_pct": pct_of(b[0]["o"], pc),
            "day_pct": pct_of(last, pc),
            "or": "above" if last > oh else "below" if last < ol else "inside",
            "opening_range": {"high": r2(oh), "low": r2(ol), "minutes": OR_MINUTES},
            "vwap": r2(vw), "vs_vwap_pct": pct_of(last, vw), "move_pct": pct_of(last, b[0]["o"]),
            "range_used_atr": r3((hi - lo) / atr20) if atr20 else None}


def breadth_read(data, failed, day, cutoff):
    """One spark pass -> the two breadth shares and their counts."""
    above = up = with_bars = with_prev = 0
    for sym in sorted(data):
        closes = session_closes(data[sym], day, cutoff)
        if not closes or closes[0][0] != 0:
            continue
        with_bars += 1
        first, last = closes[0][1], closes[-1][1]
        if last > first:
            above += 1
        pc = (data[sym].get("meta") or {}).get("chartPreviousClose")
        if pc:
            with_prev += 1
            if last > float(pc):
                up += 1
    return {"symbols": len(data) + len(failed), "answered": len(data), "with_bars": with_bars,
            "above_open": above, "with_prev_close": with_prev, "up": up,
            "above_open_pct": round(100.0 * above / with_bars, 1) if with_bars else None,
            "up_pct": round(100.0 * up / with_prev, 1) if with_prev else None,
            "failed": len(failed), "note": BREADTH_NOTE}


def sector_read(src, day, cutoff, spy_move, progress=None):
    """Every sector ETF's move since its 9:30 open, best first; the ones that failed, with why."""
    rows, failed = [], {}
    for etf, name in SECTORS:
        try:
            b = session_at(src.chart(etf), day, cutoff)
        except net.NetError as e:
            failed[etf] = str(e)
            continue
        if not b:
            failed[etf] = "no completed bars by %s" % clock.clock_label(cutoff)
            continue
        move = pct_of(b[-1]["c"], b[0]["o"])
        rows.append({"etf": etf, "name": name, "move_pct": move, "last": r2(b[-1]["c"]),
                     "vs_spy_pct": None if (move is None or spy_move is None) else round(move - spy_move, 3)})
        if progress:
            progress("  %s %s" % (etf, "" if move is None else signed(move) + " %"))
    rows.sort(key=lambda r: (-(r["move_pct"] if r["move_pct"] is not None else -1e9), r["etf"]))
    return rows, failed


def live_labels(spy, breadth):
    out = []
    g = spy.get("gap_pct")
    if g is not None:
        out.append("spy_gap_up" if g >= GAP_FLAT_PCT else "spy_gap_down" if g <= -GAP_FLAT_PCT else "spy_gap_flat")
    out.append("spy_or_" + spy["or"])
    v = spy.get("vs_vwap_pct")
    if v is not None:
        out.append("spy_above_vwap" if v > 0 else "spy_below_vwap" if v < 0 else "spy_at_vwap")
    u = breadth.get("up_pct")
    if u is not None:
        out.append("breadth_broad_up" if u >= BREADTH_BROAD_PCT else "breadth_broad_down" if u <= 100 - BREADTH_BROAD_PCT
                   else "breadth_mixed")
    return out


def live_read(src, day, cutoff, root, symbols, atr20=None, progress=None):
    """The CONTRACTS live block (plus the detail under it) at the cutoff."""
    if progress:
        progress("Fetching SPY and QQQ (%s)..." % src.name)
    reads = {}
    for sym in ("SPY", "QQQ"):
        series = src.chart(sym)
        pc, pc_src = prior_close(sym, series, day, root)
        reads[sym] = index_read(sym, session_at(series, day, cutoff), day, cutoff, pc, pc_src, atr20)
    spy, qqq = reads["SPY"], reads["QQQ"]
    if progress:
        progress("Sector ETFs since the open (%d charts)..." % len(SECTORS))
    sectors, sect_failed = sector_read(src, day, cutoff, spy["move_pct"], progress)
    if progress:
        progress("Breadth: one spark pass over %d symbols..." % len(symbols))
    data, failed = src.spark(symbols) if symbols else ({}, {})
    breadth = breadth_read(data, failed, day, cutoff)
    live = {"at": cutoff.strftime("%H:%M"), "date": day.isoformat(), "through": spy["through"], "bars": spy["bars"],
            "spy_gap_pct": spy["gap_pct"], "spy_or": spy["or"], "spy_vs_vwap_pct": spy["vs_vwap_pct"],
            "spy_move_pct": spy["move_pct"], "spy": spy, "qqq": qqq,
            "breadth_above_open_pct": breadth["above_open_pct"], "breadth_up_pct": breadth["up_pct"],
            "breadth": breadth,
            "sectors": sectors, "sectors_failed": sect_failed,
            "labels": live_labels(spy, breadth), "source": src.name, "interval": "5m"}
    return live


def futures_for(date_str, out_path):
    """trader/futures.json (trader-montecarlo futures --write) when it is for this session: {nq, es, tags, cutoff}."""
    path = os.path.join(os.path.dirname(out_path) or "trader", "futures.json")
    try:
        doc = dtio.read_json(path, None)
    except dtio.IOError_:
        return None
    if not isinstance(doc, dict) or doc.get("date") != date_str or not (doc.get("nq") or doc.get("es")):
        return None
    return {"nq": doc.get("nq"), "es": doc.get("es"), "tags": list(doc.get("tags") or []), "cutoff": doc.get("cutoff"),
            "as_of": doc.get("as_of"), "path": path}


def live_lines(doc):
    live = doc["live"]
    day = datetime.date.fromisoformat(live["date"])
    spy, qqq, br = live["spy"], live["qqq"], live["breadth"]
    out = ["Live read, %s at %s ET: %d completed 5-minute bars, the last closing at %s, from %s." % (
        clock.day_label(day), clock.clock_label(clock.parse_hhmm(live["at"])), live["bars"], live["through"],
        {"yahoo": "Yahoo", "offline": "offline files", "cache": "the bar store"}.get(live["source"], live["source"]))]
    for r in (spy, qqq):
        gap = ("gap %s %% from the prior close %.2f (%s)" % (signed(r["gap_pct"]), r["prev_close"], r["prev_close_source"])
               if r["gap_pct"] is not None else "no prior close for the gap")
        out.append("  %s %.2f: %s, %s its %d-minute opening range (%.2f to %.2f), %s %% vs VWAP %.2f, %s %% since the 9:30 open."
                   % (r["symbol"], r["last"], gap, r["or"], OR_MINUTES, r["opening_range"]["low"], r["opening_range"]["high"],
                      signed(r["vs_vwap_pct"]), r["vwap"], signed(r["move_pct"])))
    if spy.get("range_used_atr") is not None:
        out.append("  SPY range so far %.2f to %.2f: %.2f x its 20-day ATR." % (spy["low"], spy["high"], spy["range_used_atr"]))
    if br["with_bars"]:
        out.append("  Breadth, %d of %d large caps with bars (spark closes only): %.1f %% above their 9:30 close, %s up on the day against the prior close%s."
                   % (br["with_bars"], br["symbols"], br["above_open_pct"],
                      ("%.1f %%" % br["up_pct"]) if br["up_pct"] is not None else "no share",
                      "" if br["with_prev_close"] == br["with_bars"] else " (%d without a prior close skipped)" % (br["with_bars"] - br["with_prev_close"])))
    else:
        out.append("  Breadth: no symbols with bars (%d asked, %d failed)." % (br["symbols"], br["failed"]))
    if live["sectors"]:
        out.append("  Sectors since the open (move %, and against SPY):")
        for s in live["sectors"]:
            out.append("    %-4s %-24s %s  (%s)" % (s["etf"], s["name"], signed(s["move_pct"]) if s["move_pct"] is not None else "n/a",
                                                  signed(s["vs_spy_pct"]) if s["vs_spy_pct"] is not None else "n/a"))
    if live["sectors_failed"]:
        out.append("  Sectors missing: %s." % ", ".join("%s (%s)" % kv for kv in sorted(live["sectors_failed"].items())))
    d = doc["daily"]
    if d.get("spy_close") is not None:
        out.append("  Daily context (through %s): trend %s, VIX %s (%s), prior day %s. Tags: %s"
                   % (d["through"], d["trend"] or "n/a", "%.2f" % d["vix"] if d["vix"] is not None else "n/a",
                      d["vix_bucket"] or "n/a",
                      "quiet" if (d["prev_range_atr"] or 0) < 0.7 else "wide" if (d["prev_range_atr"] or 0) > 1.3 else "normal",
                      " ".join(doc["tags"]) or "(none)"))
    else:
        out.append("  Daily context: none (no SPY daily store).")
    if live.get("futures"):
        f = live["futures"]
        for key, name in (("nq", "NQ"), ("es", "ES")):
            r = f.get(key)
            if r:
                out.append("  %s overnight (to %s): %s %% vs the prior settle %s, range %sx its 20-day average. Tags: %s"
                           % (name, f.get("cutoff") or "the open", signed(r.get("gap_pct")), r.get("prev_settle"),
                              "%.2f" % r["range_vs_20d"] if r.get("range_vs_20d") is not None else "n/a", " ".join(r.get("tags") or [])))
    out.append("  Live labels: %s" % " ".join(live["labels"]))
    return out


# ---------------------------------------------------------------- the document

def read_existing(path):
    try:
        doc = dtio.read_json(path, None)
    except dtio.IOError_:
        return None
    return doc if isinstance(doc, dict) and doc.get("created_by") == "trader-regime" else None


def build_doc(now_utc, date_str, daily, tags, live, source):
    return {"created_by": "trader-regime", "as_of": clock.iso(now_utc), "date": date_str, "daily": daily,
            "live": live, "tags": tags, "source": source, "note": NOTE}


def write_doc(doc, out, publish):
    """-> the paths written."""
    paths = []
    if out:
        dtio.write_json(out, doc)
        paths.append(out)
    if publish:
        dtio.write_json(publish, doc)
        paths.append(publish)
    return paths


def read_symbols(a, src):
    path = a.symbols_file
    if os.path.exists(path):
        syms = dtio.symbols_from_file(path)
    elif src.name in ("offline", "cache"):
        syms = src.symbols()
    else:
        raise RegimeError("no symbols file at %s: run largecap.py build first (or scripts/fetch_cache.py "
                          "--build-universe)" % path)
    syms = [s for s in syms if s not in CONTEXT_SYMBOLS]
    if a.max_symbols and a.max_symbols > 0:
        syms = syms[:a.max_symbols]
    return syms


# ---------------------------------------------------------------- commands

def cmd_daily(a):
    now = clock.parse_now(a.now)
    stores = load_stores(a.cache_root)
    day = parse_date(a.date) if a.date else default_date(stores["SPY"], now)
    need_trading_day(day, "the daily block")
    daily, tags = daily_block(day.isoformat(), stores)
    live = None
    existing = read_existing(a.out)
    if existing and existing.get("date") == day.isoformat() and existing.get("live"):
        live = existing["live"]
    doc = build_doc(now, day.isoformat(), daily, tags, live, {"cache_root": a.cache_root, "kind": "daily"})
    warn = staleness(day.isoformat(), daily)
    if warn:
        doc["warnings"] = [warn]
    paths = write_doc(doc, a.out if (a.write or a.publish) else None, a.publish)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    for ln in daily_lines(doc, a.cache_root):
        print(ln)
    if live:
        print("  Kept the live read at %s ET already in %s." % (live.get("at"), a.out))
    if warn:
        print("Note: " + warn + ".")
    for p in paths:
        print("Wrote %s." % p)
    print(NOTE)
    return 0


def cmd_live(a):
    now = clock.parse_now(a.now)
    at = clock.parse_hhmm(a.at, "--at") if a.at else None
    explicit_day = None
    if a.offline and a.from_cache:
        raise RegimeError("use --offline or --from-cache, not both")
    if a.offline:
        src = OfflineSource(a.offline)
        if not a.now:
            explicit_day = src.last_day()
            if at is None:
                raise RegimeError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff, note = live_window(now, at, explicit_day)
    today = clock.to_eastern(clock.utc_now()).date()
    if a.from_cache:
        src = CacheSource(a.cache_root, day)
    elif not a.offline:
        if (today - day).days > MAX_BACK_DAYS:
            raise RegimeError("Yahoo's free 5-minute history is short: replay %s with --from-cache (the bar store) "
                              "or --offline DIR" % day.isoformat())
        src = YahooSource("1d" if day == today else "5d", a.pause)
    stores = load_stores(a.cache_root, need_spy=False)
    existing = read_existing(a.out)
    warnings = []
    if stores:
        daily, tags = daily_block(day.isoformat(), stores)
        atr20 = daily.get("spy_atr20")
        w = staleness(day.isoformat(), daily)
        if w:
            warnings.append(w)
    elif existing and existing.get("date") == day.isoformat() and existing.get("daily"):
        daily, tags = existing["daily"], list(existing.get("tags") or [])
        atr20 = daily.get("spy_atr20")
        warnings.append("no SPY daily store at %s: kept the daily block already in %s" % (a.cache_root, a.out))
    else:
        daily, tags, atr20 = empty_daily(), [], None
        warnings.append("no SPY daily store at %s: the daily block is empty; run scripts/fetch_cache.py" % a.cache_root)
    symbols = read_symbols(a, src)
    progress = None if (a.json or a.quiet) else (lambda s: print(s, file=sys.stderr, flush=True))
    try:
        live = live_read(src, day, cutoff, a.cache_root, symbols, atr20, progress)
    except RegimeError:
        raise
    except Exception as e:  # the free endpoint is down, rate-limited or unreachable: say so in one line
        if isinstance(src, YahooSource):
            raise RegimeError("Yahoo did not answer (%s: %s): replay %s with --from-cache (the bar store) or "
                              "--offline DIR, or try again in a minute" % (type(e).__name__, e, day.isoformat()))
        raise
    if note:
        live["note"] = note
    fut = futures_for(day.isoformat(), a.out)
    if fut:
        live["futures"] = fut
        for t in fut.get("tags") or []:
            if t not in tags:
                tags.append(t)
    doc = build_doc(now, day.isoformat(), daily, tags, live,
                    {"cache_root": a.cache_root, "kind": "live", "bars": src.name, "symbols_file": a.symbols_file,
                     "symbols": len(symbols)})
    if warnings:
        doc["warnings"] = warnings
    paths = write_doc(doc, a.out if (a.write or a.publish) else None, a.publish)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    for ln in live_lines(doc):
        print(ln)
    if note:
        print("Note: %s." % note)
    for w in warnings:
        print("Note: %s." % w)
    for p in paths:
        print("Wrote %s." % p)
    print(NOTE)
    return 0


def cmd_tags(a):
    stores = load_stores(a.cache_root)
    day = parse_date(a.date, "DATE")
    need_trading_day(day, "tags")
    daily, tags = daily_block(day.isoformat(), stores)
    if a.json:
        print(json.dumps({"date": day.isoformat(), "tags": tags, "daily": daily}, indent=2))
        return 0
    print("%s: %s" % (day.isoformat(), " ".join(tags) if tags else "(no tags: not enough history)"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Market regime (trader-regime): the context around every setup.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, writes=True):
        p.add_argument("--cache-root", default=DEFAULT_CACHE, help="the bar store (default trader/cache)")
        p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-23 10:30"')
        if writes:
            p.add_argument("--out", default=DEFAULT_OUT, help="where --write puts the regime (default trader/regime.json)")
            p.add_argument("--write", action="store_true", help="save trader/regime.json")
            p.add_argument("--publish", nargs="?", const=DEFAULT_DOCS, metavar="PATH",
                           help="also copy it to trader/publish/regime.json for the dashboard (implies --write)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="the daily block + tags for a session date, from the stores")
    p.add_argument("--date", help="the session, YYYY-MM-DD (default: the next session after the last stored one)")
    common(p)
    p = sub.add_parser("live", help="the intraday read at a cutoff: SPY, QQQ, sectors, breadth")
    p.add_argument("--at", help="the cutoff, HH:MM New York (default: now)")
    p.add_argument("--symbols-file", default=DEFAULT_SYMBOLS, help="the large-cap list for breadth")
    p.add_argument("--offline", metavar="DIR", help="saved Yahoo answers: <SYM>.json charts and spark.json")
    p.add_argument("--from-cache", action="store_true", help="replay a stored session from the bar store, no network")
    p.add_argument("--max-symbols", type=int, default=0, help="breadth over the first N symbols only")
    p.add_argument("--pause", type=float, default=net.PAUSE, help="seconds between Yahoo requests")
    p.add_argument("--quiet", action="store_true", help="no progress lines")
    common(p)
    p = sub.add_parser("tags", help="the tags the lab uses for a session date")
    p.add_argument("date", metavar="DATE")
    common(p, writes=False)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"daily": cmd_daily, "live": cmd_live, "tags": cmd_tags}[a.cmd](a)
    except (RegimeError, clock.ClockError, net.NetError, dtio.IOError_) as e:
        print("regime.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
