#!/usr/bin/env python3
"""Export the desk's published tables for the unified research layer (~/Desktop/trading-unified).

Read-only and local: it copies every JSON file under trader/publish/ into one bundle at the path given and writes
nothing else. Standard library only, Python 3.8.

    python3 scripts/export_unified.py --to /path/to/daytrader.json
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLISH = os.path.join(HERE, "..", "trader", "publish")


def export(publish_dir, out_path):
    """Return the bundle written to out_path: {"source", "exported_at", "files": {name: parsed json}, "skipped": [...]}."""
    bundle = {"source": "daytrader", "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "publish_dir": os.path.abspath(publish_dir),
              "files": {}, "skipped": []}
    names = sorted(n for n in os.listdir(publish_dir) if n.endswith(".json")) if os.path.isdir(publish_dir) else []
    for name in names:
        path = os.path.join(publish_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                bundle["files"][name] = json.load(fh)
        except (OSError, ValueError) as exc:
            bundle["skipped"].append({"file": name, "why": type(exc).__name__})
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, out_path)
    return bundle


def main(argv=None):
    ap = argparse.ArgumentParser(description="export the desk's published JSON for the unified layer")
    ap.add_argument("--to", required=True, help="output JSON path")
    ap.add_argument("--publish", default=PUBLISH, help="folder to read (default trader/publish)")
    args = ap.parse_args(argv)
    bundle = export(args.publish, args.to)
    print("exported %d file(s), %d skipped -> %s" % (len(bundle["files"]), len(bundle["skipped"]), args.to))
    return 0


if __name__ == "__main__":
    sys.exit(main())
