"""The network: Yahoo Finance charts (unofficial, free, can lag), Nasdaq's screener
and earnings calendar, polite by construction. Everything else in dt is offline."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-desk/1.0; built on day-trader-skill/2.0)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NASDAQ_API = "https://api.nasdaq.com"
SPARK_MAX = 20
PAUSE = 0.5
BACKOFF = 5.0
SLEEP = time.sleep  # tests swap this out


class NetError(Exception):
    pass


def http_get(url, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """query2 first, query1 on a 429 or 5xx, one more round after BACKOFF if a 429 was seen."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body
                raise NetError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise NetError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(sym):
    return sym.upper().replace(".", "-")


def from_yahoo(sym):
    return sym.upper().replace("-", ".")


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(to_yahoo(sym)), rng, interval)


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(urllib.parse.quote(to_yahoo(s)) for s in symbols), rng, interval)


def series_from_result(res):
    """A Yahoo chart result -> {"t", "o", "h", "l", "c", "v", "meta"} plain lists (None where missing)."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else [None] * n
    return {"t": list(t), "o": col("open"), "h": col("high"), "l": col("low"), "c": col("close"),
            "v": col("volume"), "meta": res.get("meta") or {}}


def parse_chart(text_or_doc, sym=""):
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise NetError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise NetError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def parse_spark(text_or_doc, wanted=None):
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise NetError("Yahoo's spark answer is not the shape this tool knows")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(ysym.upper(), from_yahoo(ysym))
        out[sym] = series_from_result(resp[0])
    err = sp.get("error")
    if err and not out:
        raise NetError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def chart(sym, rng="60d", interval="5m"):
    """One v8 chart, parsed. Raises NetError."""
    return parse_chart(yahoo_get(chart_path(sym, rng, interval)), sym)


def spark(symbols, rng="1d", interval="5m", pause=PAUSE, progress=None):
    """Closes for many symbols, 20 a request -> ({SYM: series}, {SYM: why})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for n, batch in enumerate(batches):
        wanted = {to_yahoo(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except NetError as e:
            for s in batch:
                failed[s] = str(e)
            got = {}
        for s in batch:
            if s in got:
                data[s] = got[s]
            elif s not in failed:
                failed[s] = "not in Yahoo's answer"
        if progress and (n + 1) % 10 == 0:
            progress("  spark %d/%d" % (n + 1, len(batches)))
        if pause and n + 1 < len(batches):
            SLEEP(pause)
    return data, failed


# ---------------------------------------------------------------- Nasdaq

NASDAQ_HEADERS = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
                  "Referer": "https://www.nasdaq.com/"}


def nasdaq_get(path):
    try:
        text = http_get(NASDAQ_API + path, headers=NASDAQ_HEADERS, timeout=60)
    except (urllib.error.URLError, OSError) as e:
        raise NetError("Nasdaq answered %s for %s" % (getattr(e, "reason", e), path.split("?")[0]))
    try:
        return json.loads(text)
    except ValueError:
        raise NetError("Nasdaq returned something that is not JSON for %s" % path.split("?")[0])


def nasdaq_screener():
    """Every row of Nasdaq's stock screener: symbol, name, lastsale, volume, marketCap, sector, industry..."""
    doc = nasdaq_get("/api/screener/stocks?tableonly=true&limit=0&download=true")
    rows = ((doc.get("data") or {}).get("rows")) or []
    if not rows:
        raise NetError("Nasdaq's screener came back empty")
    return rows


def nasdaq_earnings(date_str):
    """Nasdaq's earnings calendar for one date -> rows (symbol, name, time, marketCap, epsForecast...)."""
    doc = nasdaq_get("/api/calendar/earnings?date=%s" % date_str)
    data = doc.get("data") or {}
    return data.get("rows") or []


def money_to_float(s):
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
