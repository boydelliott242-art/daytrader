#!/usr/bin/env python3
"""Cut the offline fixture for tests/test_regime.py from Yahoo (the network is for fixtures only).

    python3 scripts/cut_regime_fixture.py                         # -> tests/fixtures/trader-regime/
    python3 scripts/cut_regime_fixture.py --range 5d --n 40

Saves one v8 chart (range=1d, interval=5m) per context symbol as <SYM>.json (SPY, QQQ and the
11 sector ETFs), one merged v7 spark answer of 5-minute closes for the first --n symbols of the
large-cap list as spark.json, and that list as largecap.txt. Everything is the raw Yahoo JSON,
compacted, so regime.py --offline DIR reads it exactly like a live answer. Keep it under 1 MB.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dt import io, net  # noqa: E402

CONTEXT = ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "tests", "fixtures", "trader-regime"))
    ap.add_argument("--range", dest="rng", default="1d")
    ap.add_argument("--symbols-file", default=os.path.join(ROOT, "trader", "largecap.txt"))
    ap.add_argument("--n", type=int, default=40)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    total = 0
    for sym in CONTEXT:
        text = net.yahoo_get(net.chart_path(sym, a.rng, "5m"))
        doc = json.loads(text)
        net.parse_chart(doc, sym)  # raises when Yahoo answered with an error
        path = os.path.join(a.out, "%s.json" % sym)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        total += os.path.getsize(path)
        print("  %s %d bytes" % (sym, os.path.getsize(path)), flush=True)
        net.SLEEP(net.PAUSE)
    syms = io.symbols_from_file(a.symbols_file)[:a.n]
    merged = {"spark": {"result": []}}
    for i in range(0, len(syms), net.SPARK_MAX):
        batch = syms[i:i + net.SPARK_MAX]
        doc = json.loads(net.yahoo_get(net.spark_path(batch, a.rng, "5m")))
        merged["spark"]["result"].extend((doc.get("spark") or {}).get("result") or [])
        net.SLEEP(net.PAUSE)
    got = net.parse_spark(merged)
    path = os.path.join(a.out, "spark.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, separators=(",", ":"))
    total += os.path.getsize(path)
    print("  spark.json %d symbols (%d answered), %d bytes" % (len(syms), len(got), os.path.getsize(path)))
    with open(os.path.join(a.out, "largecap.txt"), "w", encoding="utf-8") as fh:
        for s in syms:
            fh.write(s + "\n")
    print("Wrote %s: %d bytes in all." % (a.out, total))
    return 0 if total < 1000000 else 1


if __name__ == "__main__":
    sys.exit(main())
