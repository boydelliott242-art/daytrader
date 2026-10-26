"""Market context from the daily stores, computed only from closes BEFORE the session (no look-ahead)."""

from .bars import daily_before, sma, daily_atr, session_day_type
from . import regimes


def vix_bucket(v):
    if v is None:
        return None
    if v < 15:
        return "vix_lt15"
    if v < 20:
        return "vix_15_20"
    if v < 30:
        return "vix_20_30"
    return "vix_gt30"


def daily_context(date_str, spy_rows, vix_rows=None, vix3m_rows=None, breadth_pct=None):
    """The 'daily' block + tags for a session date, from rows strictly before that date. The seven-class regime
    (dt.regimes) rides along as "class", "class_rule" and the tag regime_<class>; breadth_pct (the share of large
    caps above their 50-day) sharpens strong_bull when the caller has it."""
    spy = daily_before(spy_rows, date_str, 60)
    closes = [r[4] for r in spy]
    out = {"spy_close": closes[-1] if closes else None, "spy_sma20": sma(closes, 20), "spy_sma50": sma(closes, 50),
           "trend": None, "ret5d_pct": None, "vix": None, "vix_bucket": None, "vix3m": None, "backwardation": None,
           "prev_range_atr": None}
    if out["spy_close"] and out["spy_sma20"] and out["spy_sma50"]:
        c, s20, s50 = out["spy_close"], out["spy_sma20"], out["spy_sma50"]
        if c > s20 and s20 > s50:
            out["trend"] = "trend_up"
        elif c < s20 and s20 < s50:
            out["trend"] = "trend_down"
        else:
            out["trend"] = "trend_flat"
    if len(closes) >= 6:
        out["ret5d_pct"] = round((closes[-1] / closes[-6] - 1) * 100, 3)
    if vix_rows:
        vb = daily_before(vix_rows, date_str, 1)
        if vb:
            out["vix"] = round(vb[-1][4], 2)
            out["vix_bucket"] = vix_bucket(out["vix"])
    if vix3m_rows:
        v3 = daily_before(vix3m_rows, date_str, 1)
        if v3:
            out["vix3m"] = round(v3[-1][4], 2)
            if out["vix"] is not None:
                out["backwardation"] = out["vix"] > out["vix3m"]
    a = daily_atr(spy_rows, date_str, 20)
    if a and spy:
        out["prev_range_atr"] = round((spy[-1][2] - spy[-1][3]) / a, 3) if spy[-1][2] and spy[-1][3] else None
    cls = regimes.classify_rows(date_str, spy_rows, out["vix"], breadth_pct)
    out["class"], out["class_rule"], out["class_inputs"] = cls["class"], cls["rule"], cls["inputs"]
    tags = []
    if out["trend"]:
        tags.append(out["trend"])
    if out["vix_bucket"]:
        tags.append(out["vix_bucket"])
    if out["backwardation"]:
        tags.append("vix_backwardation")
    if out["prev_range_atr"] is not None:
        tags.append("prev_quiet" if out["prev_range_atr"] < 0.7 else "prev_wide" if out["prev_range_atr"] > 1.3
                    else "prev_normal")
    tags.append(regimes.tag(out["class"]))
    out["tags"] = tags
    return out


def session_tags(date_str, stores):
    """stores: {"SPY": daily rows, "^VIX": rows, "^VIX3M": rows} -> tags list."""
    return daily_context(date_str, stores.get("SPY") or [], stores.get("^VIX"), stores.get("^VIX3M"))["tags"]


def hindsight(spy_bars):
    return {"day_type": session_day_type(spy_bars)} if spy_bars else {"day_type": None}
