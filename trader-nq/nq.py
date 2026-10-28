#!/usr/bin/env python3
"""trader-nq: the desk's setups on fifteen years of Nasdaq-100 futures, with real fees and slippage.

    python3 nq.py check                                   # is the NQ backtester installed and usable?
    python3 nq.py setups [--configs all|approved|a,b] [--start 2011-10-16] [--end 2026-10-16] [--publish]
    python3 nq.py overnight [--start] [--end]             # 15 years of overnight gaps and what the ORB did after them
    python3 nq.py session-tags [--days 60]                # nq_gap_*/nq_*_overnight tags for the lab's sessions
    python3 nq.py run orb [--instrument MNQ ...]          # passthrough to the backtester's own runner
    python3 nq.py report [--config ID] [--by year|hour|gap|dow]

The backtester (github: the user's ~/Claude/Projects/nq-backtester) holds 1-minute E-mini Nasdaq-100 bars from
2011 and an engine that charges per-contract fees, slippage on every fill and the cost of rolls. This skill
translates the desk's 5-minute setups into its signals and reads the answer back in the lab's shapes. NQ is one
instrument: a record on it tests the rules, not any stock. Most people who day trade lose money. Standard
library here; the adapter runs under the backtester's own venv.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(HERE, "dt")):
    sys.path.insert(0, HERE)
    REPO = HERE
else:
    sys.path.insert(0, os.path.dirname(HERE))
    REPO = os.path.dirname(HERE)

from dt import io  # noqa: E402

DEFAULT_BT = os.path.expanduser("~/Claude/Projects/nq-backtester")
ADAPTER = os.path.join(HERE, "nq_adapter.py")
NOTE = ("NQ is one instrument. Fifteen years of 5-minute setups on it are a real test of the rules, not of any stock. "
        "Most people who day trade lose money.")


class NQError(Exception):
    pass


def backtester(a):
    path = a.backtester or io.read_config(getattr(a, "config", "trader/config.json")).get("nq_backtester_path") or DEFAULT_BT
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        raise NQError("no NQ backtester at %s (give --backtester PATH or set nq_backtester_path in trader/config.json)" % path)
    py = a.python or os.path.join(path, ".venv", "bin", "python")
    if not os.path.exists(py):
        raise NQError("no venv python at %s: in %s run python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" % (py, path))
    return path, py


def data_span(path):
    files = sorted(f for f in os.listdir(os.path.join(path, "data", "nq_1m")) if f.endswith(".parquet")) \
        if os.path.isdir(os.path.join(path, "data", "nq_1m")) else []
    years = [f.split("_")[-1].split(".")[0] for f in files]
    rolls = os.path.join(path, "data", "rolls.csv")
    n_rolls, first, last = 0, None, None
    if os.path.exists(rolls):
        with open(rolls, newline="") as fh:
            rows = list(csv.DictReader(fh))
        n_rolls = len(rows)
        dates = sorted(v for r in rows for k, v in r.items() if k and "date" in k.lower() and v)
        if dates:
            first, last = dates[0], dates[-1]
    return {"years": years, "parquet_files": len(files), "rolls": n_rolls, "first_roll": first, "last_roll": last}


def venv_run(py, args, cwd, quiet=False, timeout=7200):
    try:
        r = subprocess.run([py] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise NQError("the backtester took longer than %d s" % timeout)
    if not quiet and r.stderr:
        sys.stderr.write(r.stderr)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        raise NQError("the backtester failed: %s" % (tail[-1] if tail else "exit %d" % r.returncode))
    return r.stdout


def cmd_check(a):
    path, py = backtester(a)
    span = data_span(path)
    out = venv_run(py, ["-c", "import sys; import nqbt, numpy, pandas; print(sys.version.split()[0], nqbt.__version__, numpy.__version__, pandas.__version__)"],
                   cwd=path, quiet=True).strip().split()
    doc = {"backtester": path, "python": py, "python_version": out[0] if out else None, "nqbt": out[1] if len(out) > 1 else None,
           "numpy": out[2] if len(out) > 2 else None, "pandas": out[3] if len(out) > 3 else None, "data": span, "adapter": ADAPTER}
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    print("NQ backtester at %s: Python %s, nqbt %s, numpy %s, pandas %s." % (path, doc["python_version"], doc["nqbt"], doc["numpy"], doc["pandas"]))
    print("Data: 1-minute bars in %d yearly files (%s to %s), %d contract rolls%s." % (
        span["parquet_files"], span["years"][0] if span["years"] else "?", span["years"][-1] if span["years"] else "?", span["rolls"],
        (" (%s to %s)" % (span["first_roll"], span["last_roll"])) if span["first_roll"] else ""))
    print("Usable. nq.py setups runs the desk's 31 configs through it; nq.py run orb runs its own example strategy.")
    return 0


def cmd_run(a):
    path, py = backtester(a)
    args = [os.path.join(path, "scripts", "run_backtest.py"), a.strategy] + list(a.args or [])
    print(venv_run(py, args, cwd=path))
    src = os.path.join(path, "reports", "%s_%s.html" % (a.strategy, "MNQ" if "MNQ" in (a.args or []) else "NQ"))
    if os.path.exists(src):
        os.makedirs(os.path.join("trader", "nq", "reports"), exist_ok=True)
        dst = os.path.join("trader", "nq", "reports", os.path.basename(src))
        shutil.copy(src, dst)
        print("Report copied to %s." % dst)
    print(NOTE)
    return 0


def approved_ids(edge_path):
    doc = io.read_json(edge_path, None)
    if not isinstance(doc, dict):
        raise NQError("--configs approved needs the lab's %s" % edge_path)
    ids = [c["id"] for c in doc.get("configs", []) if isinstance(c, dict) and c.get("status") == "approved"]
    if not ids:
        raise NQError("no approved config in %s" % edge_path)
    return ids


def cmd_setups(a):
    path, py = backtester(a)
    configs = a.configs
    if configs == "approved":
        configs = ",".join(approved_ids(a.edge))
    args = [ADAPTER, "setups", "--backtester", path, "--repo", REPO, "--out-dir", os.path.abspath(a.out_dir), "--configs", configs,
            "--fees", a.fees, "--slippage", a.slippage, "--instrument", a.instrument, "--capital", str(a.capital),
            "--config", os.path.abspath(a.config)]
    if a.start:
        args += ["--start", a.start]
    if a.end:
        args += ["--end", a.end]
    if a.quiet:
        args.append("--quiet")
    venv_run(py, args, cwd=path, quiet=a.quiet)
    edge = io.read_json(os.path.join(a.out_dir, "edge.json"), None)
    if a.publish and edge:
        io.write_json(os.path.join(a.docs_data, "nq.json"), {"edge": trimmed(edge), "overnight": io.read_json(os.path.join(a.out_dir, "overnight.json"), None)})
    if a.json:
        print(json.dumps(trimmed(edge), indent=2))
        return 0
    for ln in report_lines(edge, a.top):
        print(ln)
    return 0


def cmd_overnight(a):
    path, py = backtester(a)
    args = [ADAPTER, "overnight", "--backtester", path, "--repo", REPO, "--out-dir", os.path.abspath(a.out_dir)]
    if a.start:
        args += ["--start", a.start]
    if a.end:
        args += ["--end", a.end]
    if a.quiet:
        args.append("--quiet")
    venv_run(py, args, cwd=path, quiet=a.quiet)
    doc = io.read_json(os.path.join(a.out_dir, "overnight.json"), None)
    if a.publish and doc:
        edge = io.read_json(os.path.join(a.out_dir, "edge.json"), None)
        io.write_json(os.path.join(a.docs_data, "nq.json"), {"edge": trimmed(edge) if edge else None, "overnight": doc})
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    for ln in overnight_lines(doc):
        print(ln)
    return 0


def cmd_session_tags(a):
    path, py = backtester(a)
    venv_run(py, [ADAPTER, "session-tags", "--backtester", path, "--repo", REPO, "--out", os.path.abspath(a.out), "--days", str(a.days)]
             + (["--at", a.at] if getattr(a, "at", None) else []) + (["--quiet"] if a.quiet else []), cwd=path, quiet=a.quiet)
    doc = io.read_json(a.out, None)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    sess = (doc or {}).get("sessions") or {}
    print("NQ session tags for the last %d sessions -> %s" % (len(sess), a.out))
    for d in sorted(sess)[-5:]:
        s = sess[d]
        print("  %s  gap %s %%  overnight range %sx  %s" % (d, "%+.2f" % s["nq_gap_pct"] if s["nq_gap_pct"] is not None else "n/a",
                                                          "%.2f" % s["nq_overnight_range_vs_20d"] if s["nq_overnight_range_vs_20d"] is not None else "n/a",
                                                          " ".join(s["tags"])))
    print("The lab merges these into its regime tags when the file is present (lab.py run --nq-tags).")
    return 0


# ---------------------------------------------------------------- report

def trimmed(edge):
    if not edge:
        return edge
    out = dict(edge)
    out["configs"] = [{k: v for k, v in c.items()} for c in edge.get("configs", [])]
    return out


def ci(block, key="exp_ci95"):
    v = (block or {}).get(key) or [None, None]
    if v[0] is None:
        return "[n/a]"
    return "[%+.3f, %+.3f]" % (v[0], v[1])


def wci(block):
    v = (block or {}).get("win_ci95") or [None, None]
    return "[%.0f, %.0f]" % (100 * v[0], 100 * v[1]) if v[0] is not None else "[n/a]"


def report_lines(edge, top=40, cid=None, by=None):
    if not edge:
        return ["no edge.json yet: run nq.py setups first"]
    s = edge["sessions"]
    out = ["NQ setups: %s, %d full sessions %s to %s (%d skipped), fees %s, slippage %s, %s fill. configs tested: %s (%d run%s)."
           % (edge["instrument"], s["n"], s["first"], s["last"], s.get("skipped", 0), edge["costs"]["fees"], edge["costs"]["slippage"], edge["fill"],
              edge["configs_tested"], edge["configs_run"], ("; not applicable on NQ: " + ", ".join(edge["not_applicable"])) if edge.get("not_applicable") else "")]
    sp = edge["split"]
    out.append("In sample %s to %s (%d sessions), out of sample %s to %s (%d). Status from the out-of-sample block."
               % (sp["is"]["first"], sp["is"]["last"], sp["is"]["n"], sp["oos"]["first"], sp["oos"]["last"], sp["oos"]["n"]))
    te = edge.get("two_engines") or {}
    if te.get("compared"):
        out.append("Two engines: the minute engine and dt.sim agree on the outcome of %.1f %% of %d trades (dt better %d, engine better %d)."
                   % (100 * te["agreement"], te["compared"], te["dt_better"], te["engine_better"]))
    out.append("%-26s %-14s %6s  %-18s %-24s %6s %7s %10s %8s" % ("config", "status", "n", "win % [95 CI]", "exp R [95 CI]", "PF", "maxDD R", "engine net$", "b/e tk"))
    rows = edge["configs"]
    if cid:
        rows = [c for c in rows if c["id"] == cid]
    for c in rows[:top]:
        o = c["oos"]
        e = c.get("engine") or {}
        be = e.get("breakeven_slip_ticks")
        out.append("%-26s %-14s %6s  %5s %-12s %+7.3f %-16s %6s %7s %10s %8s" % (
            c["id"], c["status"], o.get("n") or 0, ("%.0f" % (100 * o["win_rate"])) if o.get("win_rate") is not None else "n/a",
            wci(o), o.get("exp_R") or 0.0, ci(o), ("%.2f" % o["pf"]) if o.get("pf") is not None else "n/a",
            ("%.1f" % o["max_dd_R"]) if o.get("max_dd_R") is not None else "n/a",
            ("{:,.0f}".format(e["net_pnl"])) if e.get("net_pnl") is not None else "n/a", ("%.1f" % be) if be is not None else "n/a"))
    if cid and rows and by:
        c = rows[0]
        key = {"year": "by_year", "hour": "by_hour", "dow": "by_dow", "gap": "by_regime"}.get(by, "by_" + by)
        out.append("%s by %s:" % (cid, by))
        for g, st in sorted((c.get(key) or {}).items()):
            out.append("  %-20s n %5d  win %3.0f %% %s  exp %+.3fR %s" % (g, st["n"], 100 * st["win_rate"], wci(st), st["exp_R"], ci(st)))
        r = c.get("rolling_by_year") or {}
        if r.get("selected_years"):
            out.append("  rolling by year (selected on the prior 3 years): traded %s; %s" % (", ".join(r["selected_years"]), stats_words(r.get("stats"))))
    out.append("Status: approved = out-of-sample n at or above 100 and a 95 %% expectancy lower bound above zero after the engine's fees and slippage. %s" % NOTE)
    return out


def stats_words(st):
    if not st or not st.get("n"):
        return "no trades"
    return "n %d, win %.0f %% %s, exp %+.3fR %s" % (st["n"], 100 * st["win_rate"], wci(st), st["exp_R"], ci(st))


def overnight_lines(doc):
    if not doc:
        return ["no overnight.json yet: run nq.py overnight first"]
    p = doc["gap_pct_percentiles"]
    out = ["NQ overnight, %d sessions %s to %s: the 08:30 CT open against the prior RTH close, gap percentiles p5 %s %% p25 %s %% median %s %% p75 %s %% p95 %s %%."
           % (doc["sessions"], doc["first"], doc["last"], p["p5"], p["p25"], p["p50"], p["p75"], p["p95"]),
           "The RTH range exceeded the overnight range on %.0f %% of sessions." % (100 * (doc["share_rth_range_exceeds_overnight"] or 0)),
           "orb15|r=1 after the gap (%d trades, dt.sim next-open fill, $%.2f a round turn in R):" % (doc["orb15_r1_trades"], doc.get("cost_usd_round_turn") or 22.24)]
    for k, st in doc["by_gap_bucket"].items():
        out.append("  gap %-10s %s" % (k, stats_words(st)))
    out.append("orb15|r=1 by the overnight range against its 20-day average:")
    for k, st in doc["by_range_bucket"].items():
        out.append("  %-12s %s" % (k, stats_words(st)))
    out.append(doc["note"])
    return out


def cmd_report(a):
    edge = io.read_json(os.path.join(a.lab_dir, "edge.json"), None)
    if a.json:
        print(json.dumps(trimmed(edge), indent=2))
        return 0
    for ln in report_lines(edge, a.top, a.config_id, a.by):
        print(ln)
    over = io.read_json(os.path.join(a.lab_dir, "overnight.json"), None)
    if over and not a.config_id:
        print()
        for ln in overnight_lines(over):
            print(ln)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--backtester", help="the NQ backtester folder (default: config nq_backtester_path, else %s)" % DEFAULT_BT)
        p.add_argument("--python", help="the backtester's venv python")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("check", help="is the backtester installed and usable?")
    common(p)
    p = sub.add_parser("run", help="passthrough to the backtester's own runner")
    p.add_argument("strategy")
    p.add_argument("args", nargs=argparse.REMAINDER)
    common(p)
    p = sub.add_parser("setups", help="the desk's setups on NQ, through the engine")
    p.add_argument("--configs", default="all", help="all | approved | a,b,c")
    p.add_argument("--edge", default=os.path.join("trader", "lab", "edge.json"))
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--fees", default="ninjatrader_free")
    p.add_argument("--slippage", default="default")
    p.add_argument("--instrument", default="NQ", choices=["NQ", "MNQ"])
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--out-dir", default=os.path.join("trader", "nq"))
    p.add_argument("--docs-data", default=os.path.join("trader", "publish"))
    p.add_argument("--publish", action="store_true")
    p.add_argument("--top", type=int, default=40)
    common(p)
    p = sub.add_parser("overnight", help="fifteen years of overnight gaps and the ORB after them")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--out-dir", default=os.path.join("trader", "nq"))
    p.add_argument("--docs-data", default=os.path.join("trader", "publish"))
    p.add_argument("--publish", action="store_true")
    common(p)
    p = sub.add_parser("session-tags", help="nq tags for the lab's recent sessions")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--at", help="Chicago HH:MM: the gap as of that minute of the overnight (07:05 = 8:05 ET, for the morning pick)")
    p.add_argument("--out", default=os.path.join("trader", "nq", "session_tags.json"))
    common(p)
    p = sub.add_parser("report", help="the edge tables from trader/nq/edge.json")
    p.add_argument("--lab-dir", default=os.path.join("trader", "nq"))
    p.add_argument("--config", dest="config_id")
    p.add_argument("--by", choices=["year", "hour", "dow", "gap", "regime"])
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "run": cmd_run, "setups": cmd_setups, "overnight": cmd_overnight, "session-tags": cmd_session_tags,
                "report": cmd_report}[a.cmd](a)
    except (NQError, io.IOError_) as e:
        print("nq.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
