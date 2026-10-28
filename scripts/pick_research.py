#!/usr/bin/env python3
"""Research harness for the morning pick: does a candidate feature add a directional edge out of sample?

    python3 scripts/pick_research.py --base-cache CACHE.pickle [--build-base] [--workers 14]
    python3 scripts/pick_research.py --base-cache CACHE.pickle --extra my_feature.py [--rules up,net,ret] [--drop f1,f2]
                                     [--workers 3] [--out result.json] [--leak-samples 40]

An extra module defines CUTS = {"name": (cut, cut, ...)} (and optionally CATEGORICAL = {"name": ("a", "b")}) and
compute(sym, rows, ctx) -> {date: {"name": value}}, where rows are the symbol's daily rows [date, o, h, l, c, v] oldest
first and ctx = {"cache_root", "earnings": {date: Nasdaq calendar row}}. The value for a date may use only rows strictly
before that date (and earnings rows the market knew by 8:05 ET that morning). The harness checks the first rule itself:
it recomputes sampled dates with the rows cut there and reports any difference as a LEAK.

The output: per rule (up, net, ret), the pick's and the top five's UP rate, DN rate and mean open-to-close move, with
intervals, in the first half (h1) and the second half (h2) of the out-of-sample window, the base rates and the
highest-ATR baseline. Choose on h1, believe only what h2 confirms. The rows are the live model's: every name a kept
large-cap list held, a session counted only while the name was a large cap (--no-pit drops that rule), and left out
the same way: earnings sessions (the report day, and the session after any report, a Saturday report included), special
situations (a 20 percent move the session before) and half days; --keep-earnings and --keep-special put those back. A
base cache built before these rules says so when it is read, and --build-base rebuilds it. Stdlib only; workers via
multiprocessing.
"""

import argparse
import glob
import json
import os
import pickle
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dt import pick, bars as dtbars, io as dtio  # noqa: E402


EARNINGS_RULE = "report+reaction(any time, weekends)+cap_est"   # what the base's raws carry (dt.pick.earnings_session, cap_est)


def list_docs(largecap):
    docs = []
    hist = os.path.join(os.path.dirname(largecap) or ".", "largecap_history")
    for path in sorted(glob.glob(os.path.join(hist, "*.json"))) + [largecap]:
        d = dtio.read_json(path, None)
        if isinstance(d, dict) and d.get("symbols"):
            docs.append(d)
    return docs


def symbols_of(largecap):
    """Tonight's names, then every name a kept list held (the live model's training universe)."""
    lc = dtio.read_json(largecap, None) or {}
    syms = [r["symbol"] for r in lc.get("symbols") or [] if isinstance(r, dict) and r.get("symbol") and r["symbol"] not in pick.CONTEXT]
    for s in sorted(pick.universe_caps(list_docs(largecap))):
        if s not in syms and s not in pick.CONTEXT:
            syms.append(s)
    return syms


def market_of(cache_root, tags_path):
    tags = dtio.read_json(tags_path, None) if tags_path else None
    return pick.market_table(dtbars.daily_rows("SPY", cache_root), dtbars.daily_rows("^VIX", cache_root), tags)


def build_base(a):
    syms = symbols_of(a.largecap)
    t = time.time()
    raws = pick.raw_rows(a.cache_root, syms, a.workers, None, pick.universe_caps(list_docs(a.largecap)))
    market = market_of(a.cache_root, a.nq_tags)
    with open(a.base_cache, "wb") as fh:
        pickle.dump({"raws": raws, "market": market, "symbols": syms, "built": time.strftime("%Y-%m-%d %H:%M"),
                     "earnings_rule": EARNINGS_RULE}, fh, protocol=4)
    print("base cache: %d raw rows over %d symbols in %.0f s -> %s" % (len(raws), len(syms), time.time() - t, a.base_cache))


_EX = {}


def _extra_init(path, cache_root, earnings):
    _EX.update({"path": path, "cache_root": cache_root, "earnings": earnings})


def _extra_job(sym):
    mod = pick.load_extra(_EX["path"])
    rows = dtbars.daily_rows(sym, _EX["cache_root"])
    ctx = {"cache_root": _EX["cache_root"], "earnings": (_EX["earnings"] or {}).get(sym) or {}}
    try:
        return sym, (mod.compute(sym, rows, ctx) or {}) if rows else {}
    except Exception as e:  # a broken feature is a finding, not a crash
        return sym, {"__error__": "%s: %s" % (type(e).__name__, e)}


def extra_features(path, cache_root, symbols, workers):
    import multiprocessing
    earnings = pick.load_earnings(cache_root)
    if workers > 1:
        pool = multiprocessing.Pool(workers, initializer=_extra_init, initargs=(path, cache_root, earnings))
        try:
            parts = pool.map(_extra_job, symbols, chunksize=8)
        finally:
            pool.close()
            pool.join()
    else:
        _extra_init(path, cache_root, earnings)
        parts = [_extra_job(s) for s in symbols]
    errors = {s: v["__error__"] for s, v in parts if "__error__" in v}
    return {s: v for s, v in parts if "__error__" not in v}, errors


def compact(stats):
    if not stats or not stats.get("n"):
        return {"n": 0}
    return {"n": stats["n"], "up": stats["up_rate"], "up_ci": stats["up_ci"], "dn": stats["dn_rate"], "dn_ci": stats["dn_ci"],
            "net": stats["net_rate"], "oc": stats["mean_oc"], "oc_ci": stats["mean_oc_ci"], "touch_up": stats["touch_up_rate"],
            "touch_dn": stats["touch_dn_rate"], "green": stats["green_rate"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-root", default=os.path.join(ROOT, "trader", "cache"))
    ap.add_argument("--largecap", default=os.path.join(ROOT, "trader", "largecap.json"))
    ap.add_argument("--nq-tags", default=os.path.join(ROOT, "trader", "nq", "session_tags_0805.json"))
    ap.add_argument("--base-cache", required=True)
    ap.add_argument("--build-base", action="store_true")
    ap.add_argument("--extra")
    ap.add_argument("--drop", default="")
    ap.add_argument("--rules", default=",".join(pick.RULES))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--min-train", type=int, default=pick.MIN_TRAIN_SESSIONS)
    ap.add_argument("--refit-every", type=int, default=pick.REFIT_EVERY)
    ap.add_argument("--keep-earnings", action="store_true", help="do not leave out earnings sessions")
    ap.add_argument("--keep-special", action="store_true", help="do not leave out special situations (a 20 %% move the session before)")
    ap.add_argument("--no-pit", action="store_true", help="count every session of every name (no point-in-time large-cap rule)")
    ap.add_argument("--leak-samples", type=int, default=40)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if a.build_base or not os.path.exists(a.base_cache):
        build_base(a)
    t0 = time.time()
    with open(a.base_cache, "rb") as fh:
        base = pickle.load(fh)
    raws, symbols = base["raws"], base["symbols"]
    base_rule = base.get("earnings_rule") or "report only"
    if base_rule != EARNINGS_RULE:
        print("NOTE: %s was built before tonight's rules (its earnings flag or its cap_est may be missing: the live model leaves out "
              "the session after any report and the sessions before a name was a large cap). Rebuild with --build-base to match it." % a.base_cache)
    market = market_of(a.cache_root, a.nq_tags)
    spec = pick.base_spec(a.extra) if a.extra else pick.base_spec()
    drop = [d.strip() for d in a.drop.split(",") if d.strip()]
    for d in drop:
        spec["cuts"].pop(d, None)
        spec["categorical"].pop(d, None)
    leak = None
    errors = {}
    if a.extra:
        ex, errors = extra_features(a.extra, a.cache_root, symbols, a.workers)
        merged = []
        for d, sym, raw, oc, cc, hi, lo, adv, rep in raws:
            add = (ex.get(sym) or {}).get(d)
            if add:
                raw = dict(raw)
                raw.update(add)
            merged.append((d, sym, raw, oc, cc, hi, lo, adv, rep))
        raws = merged
        leak = pick.leak_check(a.extra, a.cache_root, symbols, a.leak_samples)
    info = {}
    rows, dates, dropped = pick.encode_rows(raws, market, spec, not a.keep_earnings, not a.keep_special, info, None if a.no_pit else pick.LARGE_CAP_USD)
    rules = tuple(r.strip() for r in a.rules.split(",") if r.strip())
    res = pick.walk_forward(rows, dates, spec, a.min_train, a.refit_every, rules=rules)
    out = {"extra": a.extra, "drop": drop, "features": list(pick.features_of(spec)), "rows": len(rows), "sessions": len(dates),
           "earnings_dropped": dropped, "reaction_dropped": info.get("reaction_dropped"), "special_dropped": info.get("special_dropped"),
           "base_earnings_rule": base_rule, "small_dropped": info.get("small_dropped"), "half_day_dropped": info.get("half_day_dropped"),
           "switches": {"keep_earnings": a.keep_earnings, "keep_special": a.keep_special, "no_pit": a.no_pit, "special_move_pct": pick.SPECIAL_MOVE_PCT},
           "oos_first": res["oos_first"], "h2_first": res["h2_first"], "oos_last": res["oos_last"],
           "leak": leak, "errors": dict(list(errors.items())[:5]), "n_errors": len(errors),
           "base": {k: compact(v) for k, v in res["base"].items()}, "atr": {k: compact(v) for k, v in res["baseline_highest_atr"].items()},
           "rules": {r: {"pick": {k: compact(v) for k, v in res["rules"][r]["pick"].items()},
                         "top5": {k: compact(v) for k, v in res["rules"][r]["top5"].items()},
                         "no_pick_days": res["rules"][r]["no_pick_days"], "no_pick_h2": res["rules"][r]["no_pick_h2"]} for r in rules},
           "seconds": round(time.time() - t0, 1)}
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
    print("features %d, rows %d, sessions %d (OOS %s to %s, h2 from %s), left out: earnings %d (reaction %d), special %d, before large cap %d, "
          "half days %d, %.0f s" % (len(out["features"]), out["rows"], out["sessions"], out["oos_first"], out["oos_last"], out["h2_first"], dropped,
                                    info.get("reaction_dropped") or 0, info.get("special_dropped") or 0, info.get("small_dropped") or 0,
                                    info.get("half_day_dropped") or 0, out["seconds"]))
    if leak is not None:
        print("LEAK CHECK: %s (%d sampled dates%s)" % ("LEAK" if leak["leak"] else "clean", leak["checked"],
                                                       "; e.g. %s" % leak["mismatches"][0] if leak["leak"] else ""))
    if errors:
        print("compute() raised on %d symbols, e.g. %s" % (len(errors), list(errors.items())[0]))
    for seg in ("h1", "h2"):
        b, t = out["base"][seg], out["atr"][seg]
        print("%s base up %.3f dn %.3f oc %+.3f | highest ATR up %.3f dn %.3f oc %+.3f" % (seg, b["up"], b["dn"], b["oc"], t["up"], t["dn"], t["oc"]))
    for r in rules:
        for seg in ("h1", "h2"):
            p, q = out["rules"][r]["pick"][seg], out["rules"][r]["top5"][seg]
            if not p.get("n"):
                print("%-4s %s no picks" % (r, seg))
                continue
            print("%-4s %s pick n %3d up %.3f dn %.3f oc %+.3f [%+.3f, %+.3f] | top5 n %4d up %.3f dn %.3f oc %+.3f [%+.3f, %+.3f]" % (
                r, seg, p["n"], p["up"], p["dn"], p["oc"], p["oc_ci"][0], p["oc_ci"][1], q["n"], q["up"], q["dn"], q["oc"], q["oc_ci"][0], q["oc_ci"][1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
