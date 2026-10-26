#!/usr/bin/env python3
"""The site lock: seal the pages and the published data into docs/data/bundle.enc, which only the passphrase opens.

    python3 scripts/lock.py build                      # site/*.html + trader/publish/*.json -> docs/data/bundle.enc
    python3 scripts/lock.py verify                     # open the bundle with the stored passphrase and list what is in it
    python3 scripts/lock.py header                     # the public header: iterations, salt, nonce, size
    python3 scripts/lock.py set-passphrase             # store a new passphrase in the login Keychain (asked twice, not echoed)
    python3 scripts/lock.py check-url https://boydelliott242-art.github.io/daytrader/
                                                       # the live site serves the same bundle, it opens, and nothing is in the clear
    python3 scripts/lock.py restore --out /tmp/site    # write the opened bundle out as plain files, to look at locally

The passphrase comes from DAYTRADER_SITE_PASSPHRASE, the Keychain or trader/site.pass (see dt/lockbox.py). It is never
printed, logged or written by this script. A build that cannot find it fails closed: nothing is published.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dt import lockbox  # noqa: E402

SITE = os.path.join(ROOT, "site")
PUBLISH = os.path.join(ROOT, "trader", "publish")
OUT = os.path.join(ROOT, "docs", "data", "bundle.enc")
PLAIN_NAMES = ("full.html", "data/desk.json", "data/edge.json", "data/regime.json")


def note(msg):
    print(msg, file=sys.stderr)


def read_blob(path):
    with open(path, "rb") as fh:
        return fh.read()


def describe(doc, blob):
    pages = ", ".join(sorted(doc["pages"]))
    data = ", ".join("%s (%s)" % (k, _size(len(json.dumps(v)))) for k, v in sorted(doc["data"].items()))
    print("built %s (%s)" % (doc.get("built_et"), doc.get("built")))
    print("pages: %s" % pages)
    print("data: %s" % data)
    print("sealed: %s, sha256 %s" % (_size(len(blob)), hashlib.sha256(blob).hexdigest()[:16]))


def _size(n):
    return "%.0f KB" % (n / 1024.0) if n >= 1024 else "%d B" % n


def cmd_build(a):
    passphrase = lockbox.read_passphrase(ROOT)
    bundle = lockbox.build_bundle(a.site, a.publish_dir)
    plain = lockbox.bundle_bytes(bundle)
    blob = lockbox.seal(passphrase, plain, a.iterations)
    # prove it opens before it is written: a bundle that does not open is never published
    back = lockbox.load_bundle(passphrase, blob)
    if back["built"] != bundle["built"] or sorted(back["data"]) != sorted(bundle["data"]):
        raise lockbox.LockError("the sealed bundle did not open to what went in")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, a.out)
    print("Wrote %s: %s plain, %s sealed, %d PBKDF2 rounds." % (a.out, _size(len(plain)), _size(len(blob)), a.iterations))
    describe(bundle, blob)


def cmd_verify(a):
    passphrase = lockbox.read_passphrase(ROOT)
    blob = read_blob(a.out)
    doc = lockbox.load_bundle(passphrase, blob)
    print("Opens.")
    describe(doc, blob)


def cmd_header(a):
    blob = read_blob(a.out)
    iterations, salt, nonce = lockbox.parse_header(blob)
    print(json.dumps({"file": a.out, "bytes": len(blob), "iterations": iterations, "salt": salt.hex(), "nonce": nonce.hex(),
                      "sha256": hashlib.sha256(blob).hexdigest()}, indent=2))


def cmd_set_passphrase(a):
    import getpass
    one = getpass.getpass("New site passphrase: ")
    two = getpass.getpass("Again: ")
    if one != two:
        raise lockbox.LockError("they differ; nothing changed")
    if len(one) < 12:
        note("That is under 12 characters. The bundle is public, so guesses run offline at full speed; four or five random words is the honest floor.")
    lockbox.keychain_write(one)
    print("Stored in the login Keychain (%s / %s). Rebuild and push: scripts/lock.py build, then commit docs/data/bundle.enc."
          % (lockbox.KEYCHAIN_ACCOUNT, lockbox.KEYCHAIN_SERVICE))


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "daytrader-lock-check", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def cmd_check_url(a):
    base = a.url if a.url.endswith("/") else a.url + "/"
    ok = True
    status, shell = _get(base + "?nocache=" + os.urandom(4).hex())
    if status != 200 or b"DTLOCK1" not in shell or b"PBKDF2" not in shell:
        print("FAIL the lock screen at %s (HTTP %s)" % (base, status))
        ok = False
    else:
        print("ok   lock screen served (%s)" % _size(len(shell)))
    status, blob = _get(base + "data/bundle.enc?nocache=" + os.urandom(4).hex())
    if status != 200:
        print("FAIL bundle: HTTP %s" % status)
        return 1
    local = read_blob(a.out) if os.path.exists(a.out) else None
    same = local is not None and hashlib.sha256(local).digest() == hashlib.sha256(blob).digest()
    print("%s bundle served: %s, %s the local file" % ("ok  " if same else "WAIT", _size(len(blob)), "same as" if same else "NOT the same as"))
    ok = ok and same
    try:
        doc = lockbox.load_bundle(lockbox.read_passphrase(ROOT), blob)
        print("ok   opens with the stored passphrase: built %s, data %s" % (doc.get("built_et"), ", ".join(sorted(doc["data"]))))
    except lockbox.LockError as e:
        print("FAIL %s" % e)
        ok = False
    for name in PLAIN_NAMES:
        status, body = _get(base + name + "?nocache=" + os.urandom(4).hex())
        if status == 200:
            print("FAIL %s is served in the clear (HTTP 200)" % name)
            ok = False
        else:
            print("ok   %s not served (HTTP %s)" % (name, status))
    print("LOCKED" if ok else "NOT YET")
    return 0 if ok else 1


def cmd_restore(a):
    passphrase = lockbox.read_passphrase(ROOT)
    doc = lockbox.load_bundle(passphrase, read_blob(a.out))
    os.makedirs(os.path.join(a.dir, "data"), exist_ok=True)
    for name, html in doc["pages"].items():
        with open(os.path.join(a.dir, ("index" if name == "idea" else name) + ".html"), "w", encoding="utf-8") as fh:
            fh.write(html)
    for name, data in doc["data"].items():
        with open(os.path.join(a.dir, "data", name), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    print("Restored %d pages and %d data files to %s (plain; delete when done)." % (len(doc["pages"]), len(doc["data"]), a.dir))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("build")
    p.add_argument("--site", default=SITE)
    p.add_argument("--publish-dir", default=PUBLISH)
    p.add_argument("--out", default=OUT)
    p.add_argument("--iterations", type=int, default=lockbox.DEFAULT_ITERATIONS)
    p = sub.add_parser("verify")
    p.add_argument("--out", default=OUT)
    p = sub.add_parser("header")
    p.add_argument("--out", default=OUT)
    sub.add_parser("set-passphrase")
    p = sub.add_parser("check-url")
    p.add_argument("url")
    p.add_argument("--out", default=OUT)
    p = sub.add_parser("restore")
    p.add_argument("--out", default=OUT)
    p.add_argument("--dir", required=True)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return {"build": cmd_build, "verify": cmd_verify, "header": cmd_header, "set-passphrase": cmd_set_passphrase,
                "check-url": cmd_check_url, "restore": cmd_restore}[a.cmd](a) or 0
    except lockbox.LockError as e:
        note("lock: %s" % e)
        return 1
    except OSError as e:
        note("lock: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
