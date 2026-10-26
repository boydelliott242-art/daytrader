"""Momentum and structure features from the daily store, as of a date, with nothing from that date or after.

features_for(sym, date, daily_rows, ...) reads rows strictly BEFORE `date` (dt.bars.daily_before), so a feature
for a session is what was knowable before its open. features_intraday(bars, k, ctx) reads a session through bar
k only. Anything that cannot be computed is None and named in features["missing"]; nothing is guessed.
A feature here is a number, not a signal: the lab's feature study (lab.py features) is the only place a feature
earns a weight (CONTRACTS.md, "The research desk", Features).
"""

import math
import datetime

from .bars import daily_before, sma, vwap_series, opening_range

WINDOWS = (5, 20, 63, 126, 252)
NAMES = ("roc_5", "roc_20", "roc_63", "roc_126", "roc_252", "rs_spy_20", "rs_spy_63", "rs_sector_20", "ma_structure",
         "sma20_slope_pct", "sma50_slope_pct", "adx_14", "rsi_14", "macd", "macd_signal", "macd_hist", "atr_14", "atr_pct",
         "realized_vol_20", "relvol_20", "vol_accel", "updown_vol_ratio_20", "gap_pct", "dist_52w_high_pct",
         "dist_52w_low_pct", "breakout_20d", "breakdown_20d", "up_days_20", "trend_r2_63", "weekly_roc_4", "weekly_roc_13",
         "weekly_structure", "vol_adj_mom_63", "adv_usd_20", "beta_spy_63", "mtf_agreement")
NUMERIC = tuple(n for n in NAMES if n not in ("ma_structure", "weekly_structure", "mtf_agreement", "breakout_20d", "breakdown_20d"))


def _r(x, places=4):
    return None if x is None else round(float(x), places)


def _closes(rows):
    return [float(r[4]) for r in rows if r[4] is not None]


def ema(values, n):
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / float(n)
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values, n):
    if len(values) < n:
        return []
    k = 2.0 / (n + 1)
    out = [sum(values[:n]) / float(n)]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    """Wilder's RSI over the last n changes."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / float(n)
    al = sum(losses[:n]) / float(n)
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def true_ranges(rows):
    out = []
    for i in range(1, len(rows)):
        h, l, pc = float(rows[i][2]), float(rows[i][3]), float(rows[i - 1][4])
        out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def wilder(values, n):
    if len(values) < n:
        return None
    a = sum(values[:n]) / float(n)
    for v in values[n:]:
        a = (a * (n - 1) + v) / n
    return a


def atr(rows, n=14):
    return wilder(true_ranges(rows), n)


def adx(rows, n=14):
    """Wilder's ADX: needs at least 2n+1 rows."""
    if len(rows) < 2 * n + 1:
        return None
    trs, pdm, ndm = [], [], []
    for i in range(1, len(rows)):
        h, l, ph, pl, pc = float(rows[i][2]), float(rows[i][3]), float(rows[i - 1][2]), float(rows[i - 1][3]), float(rows[i - 1][4])
        up, down = h - ph, pl - l
        pdm.append(up if up > down and up > 0 else 0.0)
        ndm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    tr_s, p_s, n_s = sum(trs[:n]), sum(pdm[:n]), sum(ndm[:n])
    dxs = []
    for i in range(n, len(trs)):
        tr_s = tr_s - tr_s / n + trs[i]
        p_s = p_s - p_s / n + pdm[i]
        n_s = n_s - n_s / n + ndm[i]
        if tr_s <= 0:
            dxs.append(0.0)
            continue
        pdi, ndi = 100.0 * p_s / tr_s, 100.0 * n_s / tr_s
        dxs.append(100.0 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) else 0.0)
    return wilder(dxs, n)


def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    f = ema_series(closes, fast)
    s = ema_series(closes, slow)
    line = [a - b for a, b in zip(f[len(f) - len(s):], s)]
    sig = ema_series(line, signal)
    if not sig:
        return None, None, None
    return line[-1], sig[-1], line[-1] - sig[-1]


def realized_vol(closes, n=20):
    if len(closes) < n + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-n - 1:-1], closes[-n:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def roc(closes, n):
    if len(closes) < n + 1 or closes[-1 - n] <= 0:
        return None
    return (closes[-1] / closes[-1 - n] - 1) * 100.0


def slope_pct(series, n=10):
    if len(series) < n + 1 or series[-1 - n] is None or series[-1] is None or series[-1 - n] <= 0:
        return None
    return (series[-1] / series[-1 - n] - 1) * 100.0


def sma_series(values, n):
    return [None] * (n - 1) + [sum(values[i - n + 1:i + 1]) / float(n) for i in range(n - 1, len(values))]


def r_squared(values):
    """R-squared of a straight line through log(values) on 0..n-1."""
    ys = [math.log(v) for v in values if v > 0]
    n = len(ys)
    if n < 3:
        return None
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return (sxy * sxy) / (sxx * syy)


def beta(sym_closes, spy_closes, n=63):
    if len(sym_closes) < n + 1 or len(spy_closes) < n + 1:
        return None
    a = [sym_closes[i] / sym_closes[i - 1] - 1 for i in range(len(sym_closes) - n, len(sym_closes))]
    b = [spy_closes[i] / spy_closes[i - 1] - 1 for i in range(len(spy_closes) - n, len(spy_closes))]
    ma, mb = sum(a) / n, sum(b) / n
    var = sum((y - mb) ** 2 for y in b)
    if var == 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / var


def weekly_closes(rows):
    """One close per ISO week (the last row of the week), oldest first."""
    weeks = []
    for r in rows:
        d = datetime.date.fromisoformat(r[0])
        key = d.isocalendar()[:2]
        if weeks and weeks[-1][0] == key:
            weeks[-1] = (key, float(r[4]))
        else:
            weeks.append((key, float(r[4])))
    return [c for _, c in weeks]


def structure(close, fast, mid, slow):
    if None in (close, fast, mid, slow):
        return None
    if close > fast > mid > slow:
        return "up"
    if close < fast < mid < slow:
        return "down"
    return "mixed"


def mtf(short_term, long_term):
    if short_term is None or long_term is None:
        return None
    if short_term > 0 and long_term > 0:
        return "agree_up"
    if short_term < 0 and long_term < 0:
        return "agree_down"
    if short_term > 0 and long_term < 0:
        return "short_term_up_long_term_down"
    if short_term < 0 and long_term > 0:
        return "short_term_down_long_term_up"
    return "mixed"


def features_for(sym, date, daily_rows, sessions=None, spy_rows=None, sector_rows=None):
    """-> {feature: value, ..., "missing": [...], "as_of": the last row's date}. Rows are [date,o,h,l,c,v]."""
    rows = daily_before(daily_rows or [], date, 400)
    out = {n: None for n in NAMES}
    out["symbol"], out["date"] = sym, date
    out["as_of"] = rows[-1][0] if rows else None
    missing = []
    if len(rows) < 2:
        out["missing"] = list(NAMES)
        return out
    closes = _closes(rows)
    vols = [float(r[5] or 0) for r in rows]
    c = closes[-1]
    for n in WINDOWS:
        out["roc_%d" % n] = _r(roc(closes, n))
    spy = _closes(daily_before(spy_rows or [], date, 400)) if spy_rows else []
    for n in (20, 63):
        a, b = roc(closes, n), roc(spy, n) if spy else None
        out["rs_spy_%d" % n] = _r(a - b) if (a is not None and b is not None) else None
    sec = _closes(daily_before(sector_rows or [], date, 400)) if sector_rows else []
    a, b = roc(closes, 20), roc(sec, 20) if sec else None
    out["rs_sector_20"] = _r(a - b) if (a is not None and b is not None) else None
    s20, s50, s200 = sma_series(closes, 20), sma_series(closes, 50), sma_series(closes, 200)
    out["ma_structure"] = structure(c, s20[-1] if s20 else None, s50[-1] if s50 else None, s200[-1] if s200 else None)
    out["sma20_slope_pct"] = _r(slope_pct(s20, 10)) if s20 else None
    out["sma50_slope_pct"] = _r(slope_pct(s50, 10)) if s50 else None
    out["adx_14"] = _r(adx(rows, 14), 2)
    out["rsi_14"] = _r(rsi(closes, 14), 2)
    m, ms, mh = macd(closes)
    out["macd"], out["macd_signal"], out["macd_hist"] = _r(m), _r(ms), _r(mh)
    a14 = atr(rows, 14)
    out["atr_14"] = _r(a14)
    out["atr_pct"] = _r(a14 / c * 100.0) if (a14 is not None and c > 0) else None
    out["realized_vol_20"] = _r(realized_vol(closes, 20), 2)
    if len(vols) >= 21 and sum(vols[-21:-1]) > 0:
        out["relvol_20"] = _r(vols[-1] / (sum(vols[-21:-1]) / 20.0))
    if len(vols) >= 20 and sum(vols[-20:]) > 0:
        out["vol_accel"] = _r((sum(vols[-5:]) / 5.0) / (sum(vols[-20:]) / 20.0))
    if len(closes) >= 21:
        up = sum(v for a, b, v in zip(closes[-21:-1], closes[-20:], vols[-20:]) if b > a)
        down = sum(v for a, b, v in zip(closes[-21:-1], closes[-20:], vols[-20:]) if b < a)
        out["updown_vol_ratio_20"] = _r(up / down) if down > 0 else (None if up == 0 else 99.0)
        out["up_days_20"] = _r(sum(1 for a, b in zip(closes[-21:-1], closes[-20:]) if b > a) / 20.0)
    if sessions:
        b0 = sessions[0]
        out["gap_pct"] = _r((float(b0["o"]) / c - 1) * 100.0) if c > 0 else None
    year = rows[-252:]
    hi, lo = max(float(r[2]) for r in year), min(float(r[3]) for r in year)
    out["dist_52w_high_pct"] = _r((c / hi - 1) * 100.0) if hi > 0 else None
    out["dist_52w_low_pct"] = _r((c / lo - 1) * 100.0) if lo > 0 else None
    if len(rows) >= 21:
        out["breakout_20d"] = c > max(float(r[2]) for r in rows[-21:-1])
        out["breakdown_20d"] = c < min(float(r[3]) for r in rows[-21:-1])
    out["trend_r2_63"] = _r(r_squared(closes[-63:])) if len(closes) >= 63 else None
    wk = weekly_closes(rows)
    out["weekly_roc_4"] = _r(roc(wk, 4))
    out["weekly_roc_13"] = _r(roc(wk, 13))
    w10, w40 = sma(wk, 10), sma(wk, 40)
    out["weekly_structure"] = (None if (w10 is None or w40 is None) else
                               "up" if wk[-1] > w10 > w40 else "down" if wk[-1] < w10 < w40 else "mixed")
    rv = out["realized_vol_20"]
    out["vol_adj_mom_63"] = _r(out["roc_63"] / rv) if (out["roc_63"] is not None and rv) else None
    if len(rows) >= 20:
        out["adv_usd_20"] = _r(sum(float(r[4]) * float(r[5] or 0) for r in rows[-20:]) / 20.0, 0)
    out["beta_spy_63"] = _r(beta(closes, spy, 63), 3) if spy else None
    out["mtf_agreement"] = mtf(out["roc_20"], out["weekly_roc_13"] if out["weekly_roc_13"] is not None else out["roc_126"])
    out["missing"] = [n for n in NAMES if out[n] is None]
    return out


def features_intraday(bars, k, ctx=None):
    """The session through bar k: relative volume so far, where price sits against VWAP and the opening
    range, the move from the open, the share of a daily ATR used. Nothing past bar k."""
    ctx = ctx or {}
    b = bars[:k + 1]
    out = {"k": k, "bars": len(b)}
    if not b:
        out["missing"] = ["everything"]
        return out
    vw = vwap_series(b)
    c = float(b[-1]["c"])
    out["close"] = c
    out["vs_vwap_pct"] = _r((c / vw[-1] - 1) * 100.0) if vw and vw[-1] else None
    out["above_vwap"] = (c > vw[-1]) if vw and vw[-1] else None
    if len(b) >= 3:
        oh, ol = opening_range(b, 3)
        out["vs_or_high_pct"] = _r((c / oh - 1) * 100.0) if oh else None
        out["vs_or_low_pct"] = _r((c / ol - 1) * 100.0) if ol else None
        out["or_position"] = "above" if c > oh else "below" if c < ol else "inside"
    o = float(b[0]["o"])
    out["move_from_open_pct"] = _r((c / o - 1) * 100.0) if o else None
    pc = ctx.get("prev_close")
    out["move_from_prev_close_pct"] = _r((c / pc - 1) * 100.0) if pc else None
    hi, lo = max(float(x["h"]) for x in b), min(float(x["l"]) for x in b)
    da = ctx.get("daily_atr")
    out["range_used_atr"] = _r((hi - lo) / da) if da else None
    av = ctx.get("avg_vol20")
    vol = sum(float(x["v"] or 0) for x in b)
    out["relvol_so_far"] = _r(vol / (av * len(b) / 78.0)) if av else None
    out["missing"] = [n for n, v in out.items() if v is None]
    return out


def summary_lines(f):
    """A few plain lines for a card, only from what is present."""
    lines = []
    if f.get("roc_20") is not None:
        lines.append("20-day move %+.1f %%, 63-day %s, 252-day %s" % (f["roc_20"],
                     ("%+.1f %%" % f["roc_63"]) if f.get("roc_63") is not None else "n/a",
                     ("%+.1f %%" % f["roc_252"]) if f.get("roc_252") is not None else "n/a"))
    if f.get("rs_spy_20") is not None:
        lines.append("vs SPY: %+.1f %% over 20 days, %s over 63" % (f["rs_spy_20"],
                     ("%+.1f %%" % f["rs_spy_63"]) if f.get("rs_spy_63") is not None else "n/a"))
    if f.get("ma_structure"):
        lines.append("moving averages %s (SMA20 slope %s, SMA50 slope %s); weekly %s" % (
            f["ma_structure"], ("%+.1f %%" % f["sma20_slope_pct"]) if f.get("sma20_slope_pct") is not None else "n/a",
            ("%+.1f %%" % f["sma50_slope_pct"]) if f.get("sma50_slope_pct") is not None else "n/a", f.get("weekly_structure") or "n/a"))
    if f.get("adx_14") is not None or f.get("rsi_14") is not None:
        lines.append("ADX %s, RSI %s, MACD hist %s" % (f.get("adx_14"), f.get("rsi_14"), f.get("macd_hist")))
    if f.get("atr_pct") is not None:
        lines.append("ATR %.2f %% of price, realised vol %s %%, relative volume %s, ADV $%s" % (
            f["atr_pct"], f.get("realized_vol_20"), f.get("relvol_20"),
            "{:,.0f}".format(f["adv_usd_20"]) if f.get("adv_usd_20") else "n/a"))
    if f.get("mtf_agreement"):
        lines.append("timeframes: %s" % f["mtf_agreement"].replace("_", " "))
    if f.get("missing"):
        lines.append("not computable: %s" % ", ".join(f["missing"]))
    return lines
