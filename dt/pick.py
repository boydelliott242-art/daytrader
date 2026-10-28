"""The 8:25 pick: one large cap a day, ranked from what is known by 8:05 ET, with its own measured record beside the
base rates (CONTRACTS.md, "The morning pick").

Nothing here predicts. It counts. Over the daily store (about 500 sessions, every large cap) each session's features are
computed from the rows strictly before it (through the prior close) plus the market's context that morning; the
outcomes are the session's own open-to-close move: UP when it closed at least TARGET_PCT above its open, DOWN when it
closed at least TARGET_PCT below. Three naive scorers are fit from bucket counts (no tuned weights): the up odds, the
down odds and the expected open-to-close move. Three rules rank the morning's names:

    up    the highest calibrated odds of a +2 percent close (finds names that MOVE; direction is not part of it)
    net   the highest up odds minus down odds (asks for direction)
    ret   the highest expected open-to-close move (asks for direction, in percent)

The walk-forward refits every REFIT_EVERY sessions on everything before and scores the next block, so every scored
morning was ranked by a model that had not seen it. Results come split in two halves of the out-of-sample window: a rule
or a feature chosen on the first half is honest only if the second half agrees. Earnings sessions (the report day, and
the session after a report made after the close or with no time given) and special situations (a 20 percent move the
session before) are left out of training and scoring, as the live pick leaves them out. Pure stdlib.
"""

import datetime
import glob
import importlib.util
import json
import math
import multiprocessing
import os

from . import bars as dtbars, stats as dtstats

TARGET_PCT = 2.0
BAND_HI_PCT = 5.0
MIN_ADV_USD = 20e6
MIN_HISTORY = 25
MIN_TRAIN_SESSIONS = 120
REFIT_EVERY = 20
SMOOTH = 20.0
CAL_BINS = 20
CAL_EVERY = 10
PICK_FLOOR_X = 1.5          # rule up: the pick needs calibrated up odds of at least 1.5 x the base rate
NET_FLOOR = 0.02            # rule net: up odds at least 2 points above down odds
RET_FLOOR = 0.10            # rule ret: an expected open-to-close move of at least +0.10 percent
SPECIAL_MOVE_PCT = 20.0     # a name that moved this much close to close the session before is a special situation (left out)
PRE_MARKET = "time-pre-market"
RULES = ("up", "net", "ret")
DEFAULT_RULE = "up"
NA = "na"
CONTEXT = {"SPY", "QQQ", "IWM", "DIA", "^VIX", "^VIX3M", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"}
DOW = ("mon", "tue", "wed", "thu", "fri")

CUTS = {
    "atr_pct": (1.5, 2.5, 4.0),
    "ret_1d": (-3.0, -1.0, 0.0, 1.0, 3.0),
    "ret_5d": (-5.0, -2.0, 0.0, 2.0, 5.0),
    "ret_20d": (-10.0, -3.0, 0.0, 3.0, 10.0),
    "relvol_1d": (0.7, 1.0, 1.5, 2.5),
    "close_pos_1d": (0.25, 0.5, 0.75),
    "dist_20d_high": (-10.0, -5.0, -2.0, -0.5),
    "dist_20d_low": (0.5, 2.0, 5.0, 10.0),
    "rsi_14": (30.0, 40.0, 50.0, 60.0, 70.0),
    "gap_1d": (-2.0, -0.5, 0.5, 2.0),
    "vol_20": (15.0, 25.0, 40.0, 60.0),
    "sym_hit_60": (0.08, 0.12, 0.18, 0.25),
    "sym_dn_60": (0.08, 0.12, 0.18, 0.25),
    "spy_ret_1d": (-1.0, -0.3, 0.3, 1.0),
    "spy_ret_5d": (-3.0, -1.0, 1.0, 3.0),
    "vix": (15.0, 20.0, 25.0, 30.0),
    "nq_gap": (-1.0, -0.3, 0.3, 1.0),
}
CATEGORICAL = {"spy_trend": ("up", "flat", "down"), "dow": DOW}
FEATURES = tuple(CUTS) + tuple(CATEGORICAL)
MARKET_KEYS = ("spy_ret_1d", "spy_ret_5d", "spy_trend", "vix", "nq_gap", "dow")

# an encoded row
DATE, SYM, CODES, UP, DN, OC, CC, HI, LO, ADV, ATR = range(11)


class PickError(Exception):
    pass


# ------------------------------------------------------------------------------------------- the feature spec
def base_spec(extra=None):
    """The features and their cut points; extra = a module path whose CUTS / CATEGORICAL add features (research)."""
    spec = {"cuts": {k: list(v) for k, v in CUTS.items()}, "categorical": {k: list(v) for k, v in CATEGORICAL.items()}, "extra": None}
    if extra:
        mod = load_extra(extra)
        for k, v in (getattr(mod, "CUTS", None) or {}).items():
            spec["cuts"][k] = list(v)
        for k, v in (getattr(mod, "CATEGORICAL", None) or {}).items():
            spec["categorical"][k] = list(v)
        spec["extra"] = os.path.abspath(extra)
    return spec


BASE = base_spec()
_EXTRA = {}


def load_extra(path):
    path = os.path.abspath(path)
    if path not in _EXTRA:
        s = importlib.util.spec_from_file_location("pick_extra_%d" % len(_EXTRA), path)
        mod = importlib.util.module_from_spec(s)
        s.loader.exec_module(mod)
        if not hasattr(mod, "compute"):
            raise PickError("%s has no compute(sym, rows, ctx)" % path)
        _EXTRA[path] = mod
    return _EXTRA[path]


def features_of(spec):
    return tuple(spec["cuts"]) + tuple(spec["categorical"])


def nbuckets(spec, f):
    return len(spec["cuts"][f]) + 2 if f in spec["cuts"] else len(spec["categorical"][f]) + 1


def code_of(spec, f, value):
    if f in spec["cuts"]:
        cuts = spec["cuts"][f]
        if value is None or (isinstance(value, float) and value != value):
            return len(cuts) + 1
        for i, c in enumerate(cuts):
            if value < c:
                return i
        return len(cuts)
    cats = spec["categorical"][f]
    return cats.index(value) if value in cats else len(cats)


def label_of(spec, f, code):
    if f in spec["cuts"]:
        return NA if code == len(spec["cuts"][f]) + 1 else "b%d" % code
    cats = spec["categorical"][f]
    return cats[code] if code < len(cats) else NA


def bucket(name, value, spec=None):
    spec = spec or BASE
    return label_of(spec, name, code_of(spec, name, value))


def encode(spec, raw, market):
    out = []
    for f in features_of(spec):
        v = raw.get(f) if (raw and f in raw) else (market or {}).get(f)
        out.append(code_of(spec, f, v))
    return tuple(out)


def labels(spec, codes):
    return {f: label_of(spec, f, c) for f, c in zip(features_of(spec), codes)}


# ------------------------------------------------------------------------------------------- features per symbol
def symbol_table(sym, rows, extra=None, ctx=None):
    """[(date, raw, oc, cc, hi, lo, adv)] for every session with MIN_HISTORY rows before it. Everything in raw is from
    rows strictly before the session; oc, cc, hi and lo are the session's own result (percent of its open, cc of the
    prior close)."""
    out = []
    n = len(rows)
    if n < MIN_HISTORY + 1:
        return out
    closes = [float(r[4]) for r in rows]
    opens = [float(r[1]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    vols = [float(r[5] or 0) for r in rows]
    ups = [0] * n
    dns = [0] * n
    for i in range(n):
        if opens[i] > 0:
            m = (closes[i] / opens[i] - 1) * 100.0
            ups[i] = 1 if m >= TARGET_PCT else 0
            dns[i] = 1 if m <= -TARGET_PCT else 0
    ex = extra(sym, rows, ctx or {}) if extra else {}
    for i in range(MIN_HISTORY, n):
        j = i - 1
        c, h, l = closes[j], highs[j], lows[j]
        ret_1d = (c / closes[j - 1] - 1) * 100.0 if closes[j - 1] > 0 else None
        ret_5d = (c / closes[j - 5] - 1) * 100.0 if closes[j - 5] > 0 else None
        ret_20d = (c / closes[j - 20] - 1) * 100.0 if closes[j - 20] > 0 else None
        trs = [max(highs[k] - lows[k], abs(highs[k] - closes[k - 1]), abs(lows[k] - closes[k - 1])) for k in range(j - 13, j + 1)]
        atr = sum(trs) / 14.0
        atr_pct = atr / c * 100.0 if c > 0 else None
        av20 = sum(vols[j - 19:j + 1]) / 20.0
        relvol = vols[j] / av20 if av20 > 0 else None
        close_pos = (c - l) / (h - l) if h > l else None
        hi20, lo20 = max(highs[j - 19:j + 1]), min(lows[j - 19:j + 1])
        gain = sum(max(closes[k] - closes[k - 1], 0.0) for k in range(j - 13, j + 1)) / 14.0
        loss = sum(max(closes[k - 1] - closes[k], 0.0) for k in range(j - 13, j + 1)) / 14.0
        rets = [math.log(closes[k] / closes[k - 1]) for k in range(j - 19, j + 1) if closes[k - 1] > 0 and closes[k] > 0]
        if len(rets) >= 2:
            mu = sum(rets) / len(rets)
            vol20 = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(252) * 100.0
        else:
            vol20 = None
        lo60 = max(0, j - 59)
        span = j + 1 - lo60
        raw = {"atr_pct": atr_pct, "ret_1d": ret_1d, "ret_5d": ret_5d, "ret_20d": ret_20d, "relvol_1d": relvol, "close_pos_1d": close_pos,
               "dist_20d_high": (c / hi20 - 1) * 100.0 if hi20 > 0 else None, "dist_20d_low": (c / lo20 - 1) * 100.0 if lo20 > 0 else None,
               "rsi_14": 100.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss),
               "gap_1d": (opens[j] / closes[j - 1] - 1) * 100.0 if closes[j - 1] > 0 else None, "vol_20": vol20,
               "sym_hit_60": sum(ups[lo60:j + 1]) / float(span) if span >= 20 else None,
               "sym_dn_60": sum(dns[lo60:j + 1]) / float(span) if span >= 20 else None}
        if ex:
            raw.update(ex.get(rows[i][0]) or {})
        adv = sum(closes[k] * vols[k] for k in range(j - 19, j + 1)) / 20.0
        o = opens[i]
        if o > 0:
            oc = (closes[i] / o - 1) * 100.0
            hi = (highs[i] / o - 1) * 100.0
            lo = (lows[i] / o - 1) * 100.0
        else:
            oc = hi = lo = None
        cc = (closes[i] / c - 1) * 100.0 if c > 0 else None
        out.append((rows[i][0], raw, oc, cc, hi, lo, adv))
    return out


def today_raw(sym, rows, date, extra=None, ctx=None):
    """The raw features for a date not yet in the store (this morning), from the rows strictly before it."""
    before = [r for r in rows if r[0] < date]
    if len(before) < MIN_HISTORY:
        return None, None
    p = before[-1][4]
    fake = before + [[date, p, p, p, p, 0]]
    table = symbol_table(sym, fake, extra, ctx)
    if not table:
        return None, None
    return table[-1][1], table[-1][6]


def today_features(rows, date):
    raw, _ = today_raw("?", rows, date)
    return raw


# ------------------------------------------------------------------------------------------- market context per date
def market_table(spy_rows, vix_rows=None, nq_tags=None):
    """{date: {"spy_ret_1d", "spy_ret_5d", "spy_trend", "vix", "nq_gap", "dow"}} from rows strictly before each date."""
    out = {}
    closes = [float(r[4]) for r in spy_rows]
    dates = [r[0] for r in spy_rows]
    vix = sorted((r[0], float(r[4])) for r in (vix_rows or []))
    nq = (nq_tags or {}).get("sessions") or {}
    vi = 0
    last_v = None
    for i in range(51, len(spy_rows)):
        d = dates[i]
        j = i - 1
        while vi < len(vix) and vix[vi][0] < d:
            last_v = vix[vi][1]
            vi += 1
        c = closes[j]
        s20 = sum(closes[j - 19:j + 1]) / 20.0
        s50 = sum(closes[j - 49:j + 1]) / 50.0
        wd = datetime.date.fromisoformat(d).weekday()
        out[d] = {"spy_ret_1d": (c / closes[j - 1] - 1) * 100.0, "spy_ret_5d": (c / closes[j - 5] - 1) * 100.0,
                  "spy_trend": "up" if c > s20 > s50 else "down" if c < s20 < s50 else "flat", "vix": last_v,
                  "nq_gap": (nq.get(d) or {}).get("nq_gap_pct"), "dow": DOW[wd] if wd < 5 else NA}
    return out


def market_for_today(spy_rows, date, vix_rows=None, nq_gap=None):
    before = [r for r in spy_rows if r[0] < date]
    if len(before) < 51:
        return None
    p = before[-1][4]
    m = market_table(before + [[date, p, p, p, p, 0]], [r for r in (vix_rows or []) if r[0] < date]).get(date)
    if m is not None:
        m["nq_gap"] = nq_gap
    return m


# ------------------------------------------------------------------------------------------- earnings
def load_earnings(cache_root):
    """{SYM: {date: row}} from trader/cache/earnings/*.json (scripts/fetch_earnings_history.py)."""
    out = {}
    for path in glob.glob(os.path.join(cache_root, "earnings", "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        d = doc.get("date") or os.path.basename(path)[:10]
        for r in doc.get("rows") or []:
            sym = str(r.get("symbol") or "").upper().replace("/", ".")
            if sym:
                out.setdefault(sym, {})[d] = r
    return out


def earnings_dates_known(cache_root):
    return sorted(os.path.basename(p)[:10] for p in glob.glob(os.path.join(cache_root, "earnings", "*.json")))


def earnings_session(reports, date, prev_date):
    """"report" when the name reports on the date, "reaction" when it reported on the session before and the report was
    not marked before the open (after the close, or no time given: most of Nasdaq's calendar, and the move lands on the
    next session), else None. reports: {date: calendar row} for one name. Both kinds are left out."""
    reports = reports or {}
    if date in reports:
        return "report"
    row = reports.get(prev_date) if prev_date else None
    if row is not None and (row.get("time") if isinstance(row, dict) else None) != PRE_MARKET:
        return "reaction"
    return None


def special_of(raw, pct=None):
    """Why the name is a special situation this morning (it moved pct, by default SPECIAL_MOVE_PCT, or more close to
    close the session before: news, a deal, a crash, or a split the store has not adjusted for), else None. Its history
    does not describe such a day, so it is left out of training, scoring and the pick, as earnings sessions are."""
    pct = SPECIAL_MOVE_PCT if pct is None else pct
    v = (raw or {}).get("ret_1d")
    if isinstance(v, (int, float)) and v == v and abs(v) >= pct:
        return "moved %+.1f %% the session before" % v
    return None


# ------------------------------------------------------------------------------------------- building the rows
_WORK = {}


def _init_worker(spec_extra, cache_root, earnings):
    _WORK.update({"extra": spec_extra, "cache_root": cache_root, "earnings": earnings})


def _raw_job(args):
    sym, rows = args
    extra = load_extra(_WORK["extra"]).compute if _WORK.get("extra") else None
    ctx = {"cache_root": _WORK["cache_root"], "earnings": (_WORK["earnings"] or {}).get(sym) or {}}
    prev = {rows[i][0]: rows[i - 1][0] for i in range(1, len(rows))}
    return [(d, sym, raw, oc, cc, hi, lo, adv, earnings_session(ctx["earnings"], d, prev.get(d)))
            for d, raw, oc, cc, hi, lo, adv in symbol_table(sym, rows, extra, ctx)]


def _pool_map(fn, jobs, workers, initargs):
    if workers > 1 and len(jobs) > 1:
        pool = multiprocessing.Pool(min(workers, len(jobs)), initializer=_init_worker, initargs=initargs)
        try:
            return pool.map(fn, jobs, chunksize=8)
        finally:
            pool.close()
            pool.join()
    _init_worker(*initargs)
    return [fn(j) for j in jobs]


def raw_rows(cache_root, symbols, workers=1, extra=None):
    """[(date, sym, raw, oc, cc, hi, lo, adv, earnings)] for every symbol with daily rows; earnings is "report",
    "reaction" or None (earnings_session)."""
    earnings = load_earnings(cache_root)
    jobs = []
    for sym in symbols:
        if sym in CONTEXT:
            continue
        rows = dtbars.daily_rows(sym, cache_root)
        if rows:
            jobs.append((sym, rows))
    out = []
    for part in _pool_map(_raw_job, jobs, workers, (extra, cache_root, earnings)):
        out.extend(part)
    return out


def encode_rows(raws, market, spec, exclude_earnings=True, exclude_special=True, info=None):
    """-> (rows, dates, dropped): the encoded rows of the sessions the market table covers; dropped counts the earnings
    sessions left out (raws flag them "report" or "reaction"; a base built before the reaction rule flags report days
    only). exclude_special leaves out the special situations too (special_of), as the live model does; a dict passed as
    info gets "earnings_dropped", "reaction_dropped" (the part that is the session after a report) and "special_dropped"."""
    rows, dropped, reaction, special = [], 0, 0, 0
    for d, sym, raw, oc, cc, hi, lo, adv, rep in raws:
        if oc is None or d not in market:
            continue
        if exclude_earnings and rep:
            dropped += 1
            reaction += 1 if rep == "reaction" else 0
            continue
        if exclude_special and special_of(raw):
            special += 1
            continue
        rows.append((d, sym, encode(spec, raw, market[d]), 1 if oc >= TARGET_PCT else 0, 1 if oc <= -TARGET_PCT else 0,
                     oc, cc, hi, lo, adv, raw.get("atr_pct")))
    if info is not None:
        info.update({"earnings_dropped": dropped, "reaction_dropped": reaction, "special_dropped": special})
    return rows, sorted(set(r[DATE] for r in rows)), dropped


def build_rows(cache_root, symbols, workers=1, nq_tags=None, spec=None, exclude_earnings=True, exclude_special=True):
    """-> (rows, dates, market, info): every encoded row (earnings sessions and special situations dropped when asked), the
    sorted dates, the market table, and what was dropped."""
    spec = spec or base_spec()
    spy = dtbars.daily_rows("SPY", cache_root)
    vix = dtbars.daily_rows("^VIX", cache_root)
    if not spy:
        raise PickError("no SPY daily rows under %s" % cache_root)
    market = market_table(spy, vix, nq_tags)
    raws = raw_rows(cache_root, symbols, workers, spec.get("extra"))
    info = {}
    rows, dates, dropped = encode_rows(raws, market, spec, exclude_earnings, exclude_special, info)
    covered = earnings_dates_known(cache_root)
    info.update({"earnings_dates_on_disk": len(covered), "earnings_first": covered[0] if covered else None,
                 "earnings_last": covered[-1] if covered else None, "exclude_earnings": bool(exclude_earnings),
                 "special_move_pct": SPECIAL_MOVE_PCT if exclude_special else None})
    return rows, dates, market, info


# ------------------------------------------------------------------------------------------- the scorers
def new_counts(spec):
    return {"features": list(features_of(spec)), "tables": [[[0, 0, 0, 0.0] for _ in range(nbuckets(spec, f))] for f in features_of(spec)],
            "tot": [0, 0, 0, 0.0]}


def add_row(counts, codes, up, dn, oc):
    t = counts["tot"]
    t[0] += 1
    t[1] += up
    t[2] += dn
    t[3] += oc
    for table, c in zip(counts["tables"], codes):
        cell = table[c]
        cell[0] += 1
        cell[1] += up
        cell[2] += dn
        cell[3] += oc


def fit(counts, sample, spec):
    n, up, dn, oc_sum = counts["tot"]
    if n == 0:
        raise PickError("no training rows")
    p_up, p_dn, mean = up / float(n), dn / float(n), oc_sum / float(n)
    lu_all, ld_all, dv_all = [], [], []
    for table in counts["tables"]:
        lu, ld, dv = [], [], []
        for cn, cu, cd, co in table:
            lu.append(math.log(((cu + SMOOTH * p_up) / (cn + SMOOTH)) / p_up) if p_up > 0 else 0.0)
            ld.append(math.log(((cd + SMOOTH * p_dn) / (cn + SMOOTH)) / p_dn) if p_dn > 0 else 0.0)
            dv.append((co + SMOOTH * mean) / (cn + SMOOTH) - mean)
        lu_all.append(lu)
        ld_all.append(ld)
        dv_all.append(dv)
    model = {"spec": spec, "features": list(counts["features"]), "n": n, "base_up": p_up, "base_dn": p_dn, "mean_oc": mean,
             "lift_up": lu_all, "lift_dn": ld_all, "dev_oc": dv_all, "n_by_code": [[cell[0] for cell in t] for t in counts["tables"]]}
    su, sd = [], []
    for codes, u, d in sample:
        a, b, _ = score_codes(model, codes)
        su.append((a, u))
        sd.append((b, d))
    model["cal_up"] = calibrate(su)
    model["cal_dn"] = calibrate(sd)
    return model


def train(rows, dates=None, spec=None):
    """The model over every row (or the rows of the given dates)."""
    spec = spec or base_spec()
    want = set(dates) if dates is not None else None
    counts = new_counts(spec)
    sample = []
    k = 0
    for r in rows:
        if want is not None and r[DATE] not in want:
            continue
        add_row(counts, r[CODES], r[UP], r[DN], r[OC])
        k += 1
        if k % CAL_EVERY == 0:
            sample.append((r[CODES], r[UP], r[DN]))
    if counts["tot"][0] and not sample:
        sample = [(r[CODES], r[UP], r[DN]) for r in rows if want is None or r[DATE] in want]
    return fit(counts, sample, spec)


def score_codes(model, codes):
    su = sd = e = 0.0
    for lu, ld, dv, c in zip(model["lift_up"], model["lift_dn"], model["dev_oc"], codes):
        su += lu[c]
        sd += ld[c]
        e += dv[c]
    return su, sd, model["mean_oc"] + e


def calibrate(pairs, bins=CAL_BINS):
    pairs = sorted(pairs, key=lambda x: x[0])
    n = len(pairs)
    out = []
    for q in range(bins):
        chunk = pairs[q * n // bins:(q + 1) * n // bins]
        if chunk:
            out.append({"lo": round(chunk[0][0], 5), "hi": round(chunk[-1][0], 5), "n": len(chunk),
                        "rate": round(sum(h for _, h in chunk) / float(len(chunk)), 5)})
    return out


def probability(cal, s):
    if not cal:
        return None
    for b in cal:
        if s <= b["hi"]:
            return b["rate"]
    return cal[-1]["rate"]


def score_candidates(model, cands):
    """cands: [(sym, codes, adv, payload)] -> [{"symbol", "p_up", "p_dn", "net", "e_oc", "s_up", "s_dn", "adv_usd", "payload"}]"""
    out = []
    for sym, codes, adv, payload in cands:
        su, sd, e = score_codes(model, codes)
        pu, pd = probability(model["cal_up"], su), probability(model["cal_dn"], sd)
        out.append({"symbol": sym, "s_up": su, "s_dn": sd, "p_up": pu, "p_dn": pd, "net": (pu or 0) - (pd or 0), "e_oc": e,
                    "adv_usd": adv, "payload": payload})
    return out


def rule_key(rule):
    if rule == "up":
        return lambda r: (r["p_up"], r["s_up"])
    if rule == "net":
        return lambda r: (r["net"], r["s_up"] - r["s_dn"])
    if rule == "ret":
        return lambda r: (r["e_oc"],)
    raise PickError("rule wants one of %s" % ", ".join(RULES))


def clears(rule, r, model, floor_x=PICK_FLOOR_X):
    if rule == "up":
        return r["p_up"] is not None and r["p_up"] >= floor_x * model["base_up"]
    if rule == "net":
        return r["net"] >= NET_FLOOR
    return r["e_oc"] >= RET_FLOOR


def floor_words(rule, model, floor_x=PICK_FLOOR_X):
    if rule == "up":
        return "calibrated up odds of at least %.1f %% (%.1fx the base rate)" % (100 * floor_x * model["base_up"], floor_x)
    if rule == "net":
        return "up odds at least %.0f points above down odds" % (100 * NET_FLOOR)
    return "an expected open-to-close move of at least %+.2f %%" % RET_FLOOR


def rank(model, cands, rule, min_adv=MIN_ADV_USD, floor_x=PICK_FLOOR_X, exclude=()):
    """-> (pick or None, ranked): the liquid names in the rule's order; the pick is the first if it clears the floor."""
    ok = [c for c in cands if c[0] not in CONTEXT and c[0] not in exclude and c[2] is not None and c[2] >= min_adv]
    ranked = score_candidates(model, ok)
    ranked.sort(key=lambda r: tuple(-x for x in rule_key(rule)(r)) + (r["symbol"],))
    pick = ranked[0] if ranked and clears(rule, ranked[0], model, floor_x) else None
    return pick, ranked


# ------------------------------------------------------------------------------------------- the walk-forward
def seg_stats(recs):
    n = len(recs)
    if not n:
        return {"n": 0}
    up = sum(r["up"] for r in recs)
    dn = sum(r["dn"] for r in recs)
    ocs = [r["oc"] for r in recs]
    ulo, uhi = dtstats.wilson(up, n)
    dlo, dhi = dtstats.wilson(dn, n)
    if n > 3000:                                    # the normal interval: a bootstrap over this many is slow and no wiser
        mu = sum(ocs) / float(n)
        sd = math.sqrt(sum((x - mu) ** 2 for x in ocs) / (n - 1))
        mlo, mhi = mu - 1.96 * sd / math.sqrt(n), mu + 1.96 * sd / math.sqrt(n)
    else:
        mlo, mhi = dtstats.bootstrap_mean_ci(ocs) if n >= 2 else (None, None)
    s = sorted(ocs)
    return {"n": n, "up": up, "up_rate": round(up / float(n), 4), "up_ci": [round(ulo, 4), round(uhi, 4)],
            "dn": dn, "dn_rate": round(dn / float(n), 4), "dn_ci": [round(dlo, 4), round(dhi, 4)],
            "net_rate": round((up - dn) / float(n), 4), "band": sum(1 for x in ocs if TARGET_PCT <= x <= BAND_HI_PCT),
            "touch_up_rate": round(sum(1 for r in recs if r["hi"] is not None and r["hi"] >= TARGET_PCT) / float(n), 4),
            "touch_dn_rate": round(sum(1 for r in recs if r["lo"] is not None and r["lo"] <= -TARGET_PCT) / float(n), 4),
            "green_rate": round(sum(1 for x in ocs if x > 0) / float(n), 4), "mean_oc": round(sum(ocs) / n, 4),
            "mean_oc_ci": [round(mlo, 4) if mlo is not None else None, round(mhi, 4) if mhi is not None else None],
            "median_oc": round(s[n // 2], 4)}


def walk_forward(rows, dates, spec=None, min_train=MIN_TRAIN_SESSIONS, refit_every=REFIT_EVERY, min_adv=MIN_ADV_USD, floor_x=PICK_FLOOR_X,
                 top=5, rules=RULES):
    spec = spec or base_spec()
    by_date = {}
    for r in rows:
        by_date.setdefault(r[DATE], []).append(r)
    counts = new_counts(spec)
    sample = []
    added = 0
    model = None
    refits = 0
    oos = dates[min_train:]
    half = oos[len(oos) // 2] if oos else None
    per = {rule: {"pick": [], "top": [], "no_pick": []} for rule in rules}
    base_recs, atr_recs = [], []
    for i, d in enumerate(dates):
        day = by_date.get(d, [])
        if i >= min_train and day:
            if model is None or (i - min_train) % refit_every == 0:
                model = fit(counts, sample, spec)
                refits += 1
            liquid = [r for r in day if r[ADV] is not None and r[ADV] >= min_adv and r[SYM] not in CONTEXT]
            res = {r[SYM]: {"date": d, "symbol": r[SYM], "up": r[UP], "dn": r[DN], "oc": r[OC], "hi": r[HI], "lo": r[LO]} for r in liquid}
            base_recs.extend(res.values())
            if liquid:
                a = max(liquid, key=lambda r: (r[ATR] or 0.0, r[SYM]))
                atr_recs.append(res[a[SYM]])
            cands = [(r[SYM], r[CODES], r[ADV], None) for r in liquid]
            for rule in rules:
                pick, ranked = rank(model, cands, rule, min_adv, floor_x)
                if pick:
                    rec = dict(res[pick["symbol"]], p_up=round(pick["p_up"], 4), p_dn=round(pick["p_dn"], 4), e_oc=round(pick["e_oc"], 4))
                    per[rule]["pick"].append(rec)
                else:
                    per[rule]["no_pick"].append(d)
                per[rule]["top"].extend(res[x["symbol"]] for x in ranked[:top])
        for r in day:
            add_row(counts, r[CODES], r[UP], r[DN], r[OC])
            added += 1
            if added % CAL_EVERY == 0:
                sample.append((r[CODES], r[UP], r[DN]))

    def split(recs):
        return {"all": seg_stats(recs), "h1": seg_stats([x for x in recs if x["date"] < half]), "h2": seg_stats([x for x in recs if x["date"] >= half])}
    out = {"kind": "walk_forward", "oos_first": oos[0] if oos else None, "oos_last": oos[-1] if oos else None, "oos_sessions": len(oos),
           "h2_first": half, "refits": refits, "min_train": min_train, "refit_every": refit_every, "min_adv_usd": min_adv, "floor_x": floor_x,
           "features": list(features_of(spec)), "extra": spec.get("extra"), "base": split(base_recs), "baseline_highest_atr": split(atr_recs), "rules": {}}
    for rule in rules:
        p = per[rule]
        out["rules"][rule] = {"pick": split(p["pick"]), "top%d" % top: split(p["top"]), "no_pick_days": len(p["no_pick"]),
                              "no_pick_h2": len([d for d in p["no_pick"] if half and d >= half]), "recent": p["pick"][-40:]}
    out["note"] = ("Every pick was ranked by a model fit only on the sessions before it (refit every %d). UP is a close at least %g %% above the "
                   "open, DN at least %g %% below; base is every liquid large cap in the window, with the rows' own exclusions (earnings sessions, special situations). h1 and h2 are the two "
                   "halves of the out-of-sample window: anything chosen on h1 is honest only if h2 agrees." % (refit_every, TARGET_PCT, TARGET_PCT))
    return out


def words(result, rule=DEFAULT_RULE):
    r = ((result or {}).get("rules") or {}).get(rule) or {}
    p = (r.get("pick") or {}).get("all") or {}
    b = ((result or {}).get("base") or {}).get("all") or {}
    if not p.get("n"):
        return "no out-of-sample picks yet for the rule %s" % rule
    return ("rule %s over %d out-of-sample mornings: the pick closed 2 %% or more above its open on %d of %d days (%.0f %% [%.0f, %.0f]) and "
            "2 %% or more below on %d (%.0f %% [%.0f, %.0f]); base rates %.0f %% up and %.0f %% down; average open-to-close %+.2f %% [%+.2f, %+.2f]" % (
                rule, result.get("oos_sessions") or 0, p["up"], p["n"], 100 * p["up_rate"], 100 * p["up_ci"][0], 100 * p["up_ci"][1],
                p["dn"], 100 * p["dn_rate"], 100 * p["dn_ci"][0], 100 * p["dn_ci"][1], 100 * (b.get("up_rate") or 0), 100 * (b.get("dn_rate") or 0),
                p["mean_oc"], p["mean_oc_ci"][0] or 0, p["mean_oc_ci"][1] or 0))


def leak_check(extra_path, cache_root, symbols, samples=40, seed=7):
    """Recompute an extra module's features with the rows cut at each sampled date (plus a dummy row for the date):
    any difference means the module read the session it is meant to forecast. -> {"checked", "mismatches": [...]}"""
    import random
    mod = load_extra(extra_path)
    rnd = random.Random(seed)
    earnings = load_earnings(cache_root)
    checked, bad = 0, []
    syms = [s for s in symbols if s not in CONTEXT]
    for _ in range(samples):
        sym = rnd.choice(syms)
        rows = dtbars.daily_rows(sym, cache_root)
        if len(rows) < MIN_HISTORY + 40:
            continue
        i = rnd.randrange(MIN_HISTORY + 30, len(rows))
        d = rows[i][0]
        ctx = {"cache_root": cache_root, "earnings": earnings.get(sym) or {}}
        full = (mod.compute(sym, rows, ctx) or {}).get(d) or {}
        p = rows[i - 1][4]
        cut = rows[:i] + [[d, p, p, p, p, 0]]
        part = (mod.compute(sym, cut, ctx) or {}).get(d) or {}
        checked += 1
        for k in set(full) | set(part):
            a, b = full.get(k), part.get(k)
            if a != b and not (isinstance(a, float) and isinstance(b, float) and abs(a - b) < 1e-9):
                bad.append({"symbol": sym, "date": d, "feature": k, "full": a, "cut": b})
    return {"checked": checked, "mismatches": bad[:20], "leak": bool(bad)}
