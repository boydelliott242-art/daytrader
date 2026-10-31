#!/usr/bin/env python3
"""The ten-year record of the 8:25 pick: the live model's own walk-forward (dt.pick) run over trader/cache_long, the
research store of ten years of daily bars, with every exclusion the live model applies (earnings sessions, special
situations, half days, sessions before a name was a large cap), written as a record the full dashboard shows beside the
two-year one: the pick, the base rate and the highest-ATR name by half and by calendar year, and the picks grouped by
their own ATR at fixed cuts (the "likely to MOVE" question: does the word hold for every pick, or only the volatile ones).

    python3 scripts/pick_long_study.py --workers 6 --publish                # the Saturday lab job (several GB in memory)
    python3 scripts/pick_long_study.py --base-cache X.pickle --publish      # reuse a base scripts/pick_research.py built

Writes trader/pick/long_record.json and, with --publish, trader/publish/pick_long.json (sealed into the site). A count
over past mornings, refit as it went: not a prediction, not a buy. Most people who day trade lose money. Stdlib only.
"""

import argparse
import os
import pickle
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from dt import clock, io as dtio, pick  # noqa: E402
import pick_research as pr  # noqa: E402

ATR_CUTS = (4.0, 5.0, 6.0, 7.0, 8.0, 10.0)      # fixed before the research of 2026-10-31 looked at outcomes
OUT = os.path.join("trader", "pick", "long_record.json")
NOTE = ("The live model's own walk-forward over ten years of daily bars (research only): after 120 training sessions the model is refit "
        "every 20 and scores the next 20, so every pick was ranked by a model that saw only the sessions before it. h1 and h2 are the two "
        "halves of the out-of-sample window; anything chosen on h1 is believed only if h2 agrees. The universe is the point-in-time rule of "
        "the live record (today's lists run backward with constant share counts; names that fell or were delisted before the lists were "
        "kept are missing), so the long record is a measure, not the truth. A count over past mornings, not a prediction, not a buy.")


def split(recs, half):
    return {"all": pick.seg_stats(recs), "h1": pick.seg_stats([r for r in recs if r["date"] < half]),
            "h2": pick.seg_stats([r for r in recs if r["date"] >= half])}


def rec_of(r):
    return {"date": r[pick.DATE], "symbol": r[pick.SYM], "up": r[pick.UP], "dn": r[pick.DN], "oc": r[pick.OC], "hi": r[pick.HI], "lo": r[pick.LO]}


def study(rows, dates, workers_note=None, rules=pick.RULES, min_adv=pick.MIN_ADV_USD):
    """The record from the encoded rows: the walk-forward, then the per-year and per-ATR-cut tables rebuilt from the same
    rows (the base rate and the highest-ATR name each morning exactly as dt.pick.walk_forward defines them)."""
    res = pick.walk_forward(rows, dates, keep=(pick.DEFAULT_RULE,))
    half = res["h2_first"]
    oos = set(dates[pick.MIN_TRAIN_SESSIONS:])
    by_day = {}
    atr_of = {}
    for r in rows:
        if r[pick.DATE] in oos and r[pick.ADV] is not None and r[pick.ADV] >= min_adv and r[pick.SYM] not in pick.CONTEXT:
            by_day.setdefault(r[pick.DATE], []).append(r)
            atr_of[(r[pick.DATE], r[pick.SYM])] = r[pick.ATR]
    base_recs, atr_recs = [], []
    for d in sorted(by_day):
        day = by_day[d]
        base_recs.extend(rec_of(r) for r in day)
        atr_recs.append(rec_of(max(day, key=lambda r: (r[pick.ATR] or 0.0, r[pick.SYM]))))
    picks = res["rules"][pick.DEFAULT_RULE].pop("picks", [])
    for p in picks:
        p["atr"] = atr_of.get((p["date"], p["symbol"]))
    years = sorted(set(d[:4] for d in dates[pick.MIN_TRAIN_SESSIONS:]))
    by_year = []
    for y in years:
        by_year.append({"year": y, "pick": pick.seg_stats([p for p in picks if p["date"][:4] == y]),
                        "highest_atr": pick.seg_stats([r for r in atr_recs if r["date"][:4] == y]),
                        "base": pick.seg_stats([r for r in base_recs if r["date"][:4] == y])})
    known = [p for p in picks if isinstance(p.get("atr"), (int, float))]
    cuts = []
    for c in ATR_CUTS:
        cuts.append({"cut": c, "high": split([p for p in known if p["atr"] >= c], half), "low": split([p for p in known if p["atr"] < c], half)})
    out = {"kind": "ten_year_record", "created_by": "trader-pick", "live_rule": pick.DEFAULT_RULE,
           "oos": {"first": res["oos_first"], "last": res["oos_last"], "sessions": res["oos_sessions"], "h2_first": half},
           "refits": res["refits"], "rules": res["rules"], "base": res["base"], "highest_atr": res["baseline_highest_atr"],
           "by_year": by_year,
           "atr_cuts": {"cuts": list(ATR_CUTS), "rows": cuts, "unknown_atr": len(picks) - len(known),
                        "note": ("the live rule's picks grouped by their own ATR at the prior close (14 sessions, percent of the close), at cuts "
                                 "fixed before the question was asked: 'high' is at or above the cut, 'low' below it; a cut is believed only "
                                 "when the research chose it on h1 and h2 agreed")},
           "configs_tested": len(res["rules"]) + len(ATR_CUTS), "rules_tested": sorted(res["rules"]), "note": NOTE}
    if workers_note:
        out["workers"] = workers_note
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-root", default=os.path.join("trader", "cache_long"))
    ap.add_argument("--largecap", default=os.path.join("trader", "largecap.json"))
    ap.add_argument("--nq-tags", default=os.path.join("trader", "nq", "session_tags_0805.json"))
    ap.add_argument("--base-cache", default=None, help="a base pickle from scripts/pick_research.py to reuse (else the rows are built here)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--publish-dir", default=os.path.join("trader", "publish"))
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    def say(msg):
        if not a.quiet:
            print(msg, flush=True)
    t0 = time.time()
    if a.base_cache and os.path.exists(a.base_cache):
        with open(a.base_cache, "rb") as fh:
            base = pickle.load(fh)
        raws, syms = base["raws"], base["symbols"]
        say("base %s: %d raw rows over %d symbols" % (a.base_cache, len(raws), len(syms)))
    else:
        syms = pr.symbols_of(a.largecap)
        caps = pick.universe_caps(pr.list_docs(a.largecap))
        raws = pick.raw_rows(a.cache_root, syms, a.workers, None, caps)
        say("raw rows: %d over %d symbols in %.0f s" % (len(raws), len(syms), time.time() - t0))
    market = pr.market_of(a.cache_root, a.nq_tags if a.nq_tags and os.path.exists(a.nq_tags) else None)
    info = {}
    rows, dates, _ = pick.encode_rows(raws, market, pick.base_spec(), True, True, info, pick.LARGE_CAP_USD)
    if len(dates) <= pick.MIN_TRAIN_SESSIONS:
        print("pick_long_study: only %d sessions in %s; nothing to walk forward" % (len(dates), a.cache_root), file=sys.stderr)
        return 1
    say("rows %d, sessions %d (%s to %s), left out %s, %.0f s" % (len(rows), len(dates), dates[0], dates[-1], info, time.time() - t0))
    out = study(rows, dates)
    out.update({"built_at": dtio.now_iso(), "cache_root": a.cache_root, "symbols": len(syms), "rows": len(rows), "sessions": len(dates),
                "sessions_first": dates[0], "sessions_last": dates[-1], "left_out": info, "seconds": round(time.time() - t0),
                "universe_rule": ("a session counts only while the name's estimated cap at the prior close was at least $%.0fB (its cap at the "
                                  "newest list holding it x the close then / the price at that list); names that left the list keep their history"
                                  % (pick.LARGE_CAP_USD / 1e9))})
    dtio.write_json(a.out, out)
    say("wrote %s in %.0f s" % (a.out, time.time() - t0))
    if a.publish:
        os.makedirs(a.publish_dir, exist_ok=True)
        dtio.write_json(os.path.join(a.publish_dir, "pick_long.json"), out)
        say("published %s" % os.path.join(a.publish_dir, "pick_long.json"))
    p = out["rules"][pick.DEFAULT_RULE]["pick"]
    for seg in ("h1", "h2"):
        s = p.get(seg) or {}
        if s.get("n"):
            say("  %s: %d mornings, closed 2 %% up %.1f %% [%.1f, %.1f], 2 %% down %.1f %% [%.1f, %.1f], moved 2 %% %.1f %% [%.1f, %.1f]" % (
                seg, s["n"], 100 * s["up_rate"], 100 * s["up_ci"][0], 100 * s["up_ci"][1], 100 * s["dn_rate"], 100 * s["dn_ci"][0], 100 * s["dn_ci"][1],
                100 * s["move_rate"], 100 * s["move_ci"][0], 100 * s["move_ci"][1]))
    say("configs tested: %d. %s" % (out["configs_tested"], NOTE.split(". ")[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
