"""Seven regime classes on top of the regime tags (CONTRACTS.md, "The research desk", Regime classes).

classify() is pure and reads only numbers computed from rows strictly before the session (inputs_from_rows), so the
lab can tag every past session without look-ahead. The rule that fired is returned beside the class, so a card can say
why the day is what it is. fit(side, class) says whether a side is with, against or indifferent to the class.
"""

from .bars import daily_before

CLASSES = ("strong_bull", "weak_bull", "range", "high_vol", "bear", "crisis", "transitional")
TAG_PREFIX = "regime_"
VIX_CRISIS = 30.0
VIX_HIGH = 25.0
DRAWDOWN_CRISIS_PCT = -15.0
VOL_PCTILE_HIGH = 0.8
BREADTH_STRONG_PCT = 60.0
CROSS_WINDOW = 10

FIT = {
    "long": {"strong_bull": "for", "weak_bull": "for", "range": "neutral", "high_vol": "neutral", "bear": "against",
             "crisis": "against", "transitional": "neutral"},
    "short": {"strong_bull": "against", "weak_bull": "neutral", "range": "neutral", "high_vol": "neutral", "bear": "for",
              "crisis": "for", "transitional": "neutral"},
}


def _sma(values, n, i):
    if i + 1 < n:
        return None
    return sum(values[i - n + 1:i + 1]) / float(n)


def _trend(c, s20, s50):
    if None in (c, s20, s50):
        return None
    if c > s20 > s50:
        return "trend_up"
    if c < s20 < s50:
        return "trend_down"
    return "trend_flat"


def _realized_vol(closes, end, n=20):
    import math
    if end - n < 0:
        return None
    rets = []
    for i in range(end - n + 1, end + 1):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))


def inputs_from_rows(date_str, spy_rows, vix=None):
    """What classify() needs, from SPY rows strictly before date_str (and the VIX close the caller already has)."""
    spy = daily_before(spy_rows or [], date_str, 320)
    closes = [float(r[4]) for r in spy]
    n = len(closes)
    out = {"trend": None, "vix": vix, "drawdown_pct": None, "vol_pctile": None, "crossed_within_%d" % CROSS_WINDOW: None,
           "sma50_rising": None, "rows": n}
    if n >= 50:
        s20, s50 = _sma(closes, 20, n - 1), _sma(closes, 50, n - 1)
        out["trend"] = _trend(closes[-1], s20, s50)
        if n >= 50 + CROSS_WINDOW:
            s20b, s50b = _sma(closes, 20, n - 1 - CROSS_WINDOW), _sma(closes, 50, n - 1 - CROSS_WINDOW)
            out["crossed_within_%d" % CROSS_WINDOW] = (s20 - s50 > 0) != (s20b - s50b > 0)
            out["sma50_rising"] = s50 > s50b
    if n >= 21:
        year = spy[-252:]
        hi = max(float(r[2]) for r in year)
        out["drawdown_pct"] = round((closes[-1] / hi - 1) * 100.0, 3) if hi > 0 else None
        vols = [v for v in (_realized_vol(closes, i, 20) for i in range(max(20, n - 252), n)) if v is not None]
        if vols and vols[-1] is not None:
            latest = vols[-1]
            out["vol_pctile"] = round(sum(1 for v in vols if v <= latest) / float(len(vols)), 3)
    return out


def classify(trend=None, vix=None, drawdown_pct=None, vol_pctile=None, crossed_within=None, sma50_rising=None,
             breadth_pct=None):
    """-> (class, rule). Rules in order; the first that holds names the class."""
    if vix is not None and vix >= VIX_CRISIS:
        return "crisis", "VIX %.1f at or above %g" % (vix, VIX_CRISIS)
    if drawdown_pct is not None and drawdown_pct <= DRAWDOWN_CRISIS_PCT and trend == "trend_down":
        return "crisis", "SPY %.1f %% under its 52-week high in a downtrend" % drawdown_pct
    if vix is not None and vix >= VIX_HIGH:
        return "high_vol", "VIX %.1f at or above %g" % (vix, VIX_HIGH)
    if vol_pctile is not None and vol_pctile >= VOL_PCTILE_HIGH:
        return "high_vol", "SPY realised volatility in its top %d %% of the year" % int(round((1 - VOL_PCTILE_HIGH) * 100))
    if trend == "trend_down":
        return "bear", "SPY under its 20-day average, which is under its 50-day"
    if crossed_within:
        return "transitional", "the 20-day average crossed the 50-day within the last %d sessions" % CROSS_WINDOW
    if trend == "trend_up":
        if sma50_rising and (breadth_pct is None or breadth_pct >= BREADTH_STRONG_PCT):
            return "strong_bull", ("SPY above a rising 20-day over a rising 50-day" +
                                   (", breadth %.0f %% above the 50-day" % breadth_pct if breadth_pct is not None else ""))
        return "weak_bull", "SPY in an uptrend without a rising 50-day or broad participation"
    if trend is None:
        return "range", "not enough SPY history for a trend: range by default"
    return "range", "SPY between its averages"


def classify_rows(date_str, spy_rows, vix=None, breadth_pct=None):
    """-> {"class", "rule", "inputs"} from rows strictly before date_str."""
    inp = inputs_from_rows(date_str, spy_rows, vix)
    cls, rule = classify(inp["trend"], vix, inp["drawdown_pct"], inp["vol_pctile"], inp["crossed_within_%d" % CROSS_WINDOW],
                         inp["sma50_rising"], breadth_pct)
    return {"class": cls, "rule": rule, "inputs": inp}


def tag(cls):
    return TAG_PREFIX + cls


def fit(side, cls):
    """for | against | neutral: how a side sits in a class (a short in strong_bull is against)."""
    return FIT.get(side or "long", FIT["long"]).get(cls, "neutral")
