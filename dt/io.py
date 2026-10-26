"""Files: JSON in and out, the config, the project root."""

import datetime
import gzip
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER = os.path.join(ROOT, "trader")
CACHE = os.path.join(TRADER, "cache")
PUBLISH = os.path.join(TRADER, "publish")      # what --publish writes; scripts/lock.py seals it into docs/data/bundle.enc
DOCS_DATA = PUBLISH


class IOError_(Exception):
    pass


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise IOError_("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc, indent=2):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    if path.endswith(".gz"):
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
    else:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=indent)
            fh.write("\n")
    os.replace(tmp, path)


def append_jsonl(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, separators=(",", ":")) + "\n")


def read_jsonl(path):
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def read_config(path=None):
    path = path or os.path.join(TRADER, "config.json")
    cfg = read_json(path, {})
    return cfg if isinstance(cfg, dict) else {}


def symbols_from_file(path):
    """A text file of SYMBOL|Name lines, or a JSON list / {hits:[{symbol}]} like the scanner reads."""
    if not os.path.exists(path):
        raise IOError_("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        doc = json.loads(text)
        items = doc.get("hits", doc.get("symbols", [])) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out
