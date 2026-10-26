"""The sealed site bundle: one file, docs/data/bundle.enc, that only the passphrase opens.

The lock screen (docs/index.html) is public and holds nothing: it fetches this file, derives a key from the
passphrase with PBKDF2-HMAC-SHA256 and opens it with AES-256-GCM in the browser's WebCrypto. A wrong passphrase
is not "compared" anywhere; it derives a wrong key and the tag check fails. There is nothing to bypass because
the pages and the numbers are not on the site in the clear.

File layout (CONTRACTS.md, "The lock"):
    bytes 0..8    magic  b"DTLOCK1\\0"
    bytes 8..12   PBKDF2 iterations, big-endian uint32
    bytes 12..28  salt, 16 random bytes (new every build)
    bytes 28..40  nonce, 12 random bytes (new every build)
    bytes 40..    AES-256-GCM ciphertext || 16-byte tag, with bytes 0..40 as the additional data
The plaintext is gzip of a JSON bundle: {"format", "built", "built_et", "pages": {"idea": html, "full": html},
"data": {"desk.json": {...}, ...}}.

The passphrase is never in the repo. It is read, in this order, from the environment (DAYTRADER_SITE_PASSPHRASE,
for tests and one-off builds), the macOS login Keychain (account "daytrader", service "daytrader-site-passphrase")
or a file trader/site.pass (gitignored, mode 600). Nothing here logs or prints it.
"""

import datetime
import gzip
import hashlib
import io
import json
import os
import secrets
import struct
import subprocess

from . import aesgcm

MAGIC = b"DTLOCK1\x00"
HEADER = 40
DEFAULT_ITERATIONS = 4000000       # 80 ms a guess in a 2026 desktop browser, 0.9 s to seal; four times the cost of the OWASP floor per guess
MIN_ITERATIONS = 600000           # OWASP's 2023 floor for PBKDF2-HMAC-SHA256
FORMAT = "daytrader-site/1"
KEYCHAIN_ACCOUNT = "daytrader"
KEYCHAIN_SERVICE = "daytrader-site-passphrase"
ENV = "DAYTRADER_SITE_PASSPHRASE"
PASS_FILE = os.path.join("trader", "site.pass")
PAGES = ("idea", "full")
DATA_FILES = ("desk.json", "edge.json", "regime.json", "scorecard.json", "largecap.json", "montecarlo.json", "nq.json")


class LockError(Exception):
    pass


# ------------------------------------------------------------------------------------------- the format
def derive_key(passphrase, salt, iterations):
    if not isinstance(passphrase, str) or not passphrase:
        raise LockError("the passphrase is empty")
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, 32)


def seal(passphrase, plaintext, iterations=DEFAULT_ITERATIONS, rng=secrets.token_bytes):
    """The sealed blob for plaintext bytes (gzipped inside)."""
    if iterations < MIN_ITERATIONS:
        raise LockError("iterations below %d" % MIN_ITERATIONS)
    salt, nonce = rng(16), rng(12)
    header = MAGIC + struct.pack(">I", iterations) + salt + nonce
    key = derive_key(passphrase, salt, iterations)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(plaintext)
    return header + aesgcm.encrypt(key, nonce, buf.getvalue(), header)


def parse_header(blob):
    """(iterations, salt, nonce) or LockError. Anyone can read the header; that is by design."""
    if len(blob) < HEADER + 16 or blob[:8] != MAGIC:
        raise LockError("not a DTLOCK1 bundle")
    iterations = struct.unpack(">I", blob[8:12])[0]
    return iterations, blob[12:28], blob[28:40]


def open_(passphrase, blob):
    """The plaintext bytes, or LockError (wrong passphrase and a changed file look the same, on purpose)."""
    iterations, salt, nonce = parse_header(blob)
    key = derive_key(passphrase, salt, iterations)
    try:
        packed = aesgcm.decrypt(key, nonce, blob[HEADER:], blob[:HEADER])
    except aesgcm.AuthenticationError:
        raise LockError("that passphrase does not open this bundle")
    return gzip.decompress(packed)


# ------------------------------------------------------------------------------------------- the bundle
def build_bundle(site_dir, publish_dir, now=None):
    """The plaintext JSON bundle: the two pages from site_dir and whatever data files publish_dir holds."""
    pages = {}
    for name in PAGES:
        path = os.path.join(site_dir, ("index" if name == "idea" else name) + ".html")
        if not os.path.exists(path):
            raise LockError("missing page %s" % path)
        with open(path, encoding="utf-8") as fh:
            pages[name] = fh.read()
    data = {}
    for name in DATA_FILES:
        path = os.path.join(publish_dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                try:
                    data[name] = json.load(fh)
                except ValueError as e:
                    raise LockError("%s is not valid JSON: %s" % (path, e))
    if not data:
        raise LockError("no data files in %s (run the tools with --publish first)" % publish_dir)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        from . import clock
        built_et = clock.to_eastern(now).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        built_et = now.strftime("%Y-%m-%d %H:%M UTC")
    return {"format": FORMAT, "built": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "built_et": built_et,
            "pages": pages, "data": data}


def bundle_bytes(bundle):
    return json.dumps(bundle, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_bundle(passphrase, blob):
    doc = json.loads(open_(passphrase, blob).decode("utf-8"))
    if doc.get("format") != FORMAT or "pages" not in doc or "data" not in doc:
        raise LockError("opened, but not a site bundle")
    return doc


# ------------------------------------------------------------------------------------------- the passphrase
def _keychain_read():
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    value = r.stdout.rstrip("\n")
    return value or None


def keychain_write(passphrase):
    """Store (or replace) the passphrase in the login Keychain. The value goes over a pipe, not the command line."""
    if not passphrase:
        raise LockError("refusing to store an empty passphrase")
    # -w with no value would prompt; the item is created with the password piped through stdin instead
    r = subprocess.run(["/usr/bin/security", "-i"], input="add-generic-password -a %s -s %s -U -w %s\n"
                       % (KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, _quote(passphrase)), capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise LockError("Keychain refused: %s" % (r.stderr.strip() or r.stdout.strip()))
    if _keychain_read() != passphrase:
        raise LockError("stored, but reading it back gave something else")


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_passphrase(root, env=None, keychain=True):
    """The passphrase from the environment, the Keychain or trader/site.pass; LockError when none is set."""
    env = os.environ if env is None else env
    value = env.get(ENV)
    if value:
        return value
    value = _keychain_read() if keychain else None
    if value:
        return value
    path = os.path.join(root, PASS_FILE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip("\r\n")
        if value:
            return value
    raise LockError("no passphrase: set it with scripts/lock.py set-passphrase (Keychain), %s, or %s" % (ENV, PASS_FILE))
