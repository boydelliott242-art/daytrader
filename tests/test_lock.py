"""The site lock: AES-GCM against the published known answers, the sealed bundle, the lock script, the public docs/."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from _load import ROOT

sys.path.insert(0, ROOT)
from dt import aesgcm, lockbox  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
SITE = os.path.join(ROOT, "site")
LOCK = os.path.join(ROOT, "scripts", "lock.py")
PASS = "fixture passphrase, never the real one"
FAST = 600000   # the floor, so the tests stay quick


def h(s):
    return bytes.fromhex(s)


class AESKnownAnswers(unittest.TestCase):
    def test_fips_197_appendix_c(self):
        pt = h("00112233445566778899aabbccddeeff")
        for klen, ct in ((16, "69c4e0d86a7b0430d8cdb78070b4c55a"), (24, "dda97ca4864cdfe06eaf70a0ec0d7191"),
                         (32, "8ea2b7ca516745bfeafc49904b496089")):
            self.assertEqual(aesgcm.aes_encrypt_block(bytes(range(klen)), pt).hex(), ct, "AES-%d" % (klen * 8))

    def test_gcm_specification_test_cases(self):
        def gcm(key, iv, p, a=b""):
            out = aesgcm.encrypt(key, iv, p, a)
            return out[:-16].hex(), out[-16:].hex()
        self.assertEqual(gcm(bytes(16), bytes(12), b""), ("", "58e2fccefa7e3061367f1d57a4e7455a"))                       # TC 1
        self.assertEqual(gcm(bytes(16), bytes(12), bytes(16)), ("0388dace60b6a392f328c2b971b2fe78", "ab6e47d42cec13bdf53a67b21257bddf"))
        k = h("feffe9928665731c6d6a8f9467308308")
        iv = h("cafebabefacedbaddecaf888")
        p = h("d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a721c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b391aafd255")
        self.assertEqual(gcm(k, iv, p), ("42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091473f5985",
                                         "4d5c2af327cd64a62cf35abd2ba6fab4"))                                                    # TC 3
        aad = h("feedfacedeadbeeffeedfacedeadbeefabaddad2")
        self.assertEqual(gcm(k, iv, p[:60], aad), ("42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091",
                                                   "5bc94fbc3221a5db94fae95ae7121a47"))                                          # TC 4
        self.assertEqual(gcm(bytes(32), bytes(12), b""), ("", "530f8afbc74536b9a963b4f1c4cb738b"))                       # TC 13
        self.assertEqual(gcm(bytes(32), bytes(12), bytes(16)), ("cea7403d4d606b6e074ec5d3baf39d18", "d0d1c8a799996bf0265b98b5d48ab919"))

    def test_round_trip_and_every_tamper_is_caught(self):
        key, nonce, aad = bytes(range(32)), bytes(range(12)), b"header bytes"
        data = bytes(range(256)) * 40 + b"tail"
        ct = aesgcm.encrypt(key, nonce, data, aad)
        self.assertEqual(len(ct), len(data) + 16)
        self.assertEqual(aesgcm.decrypt(key, nonce, ct, aad), data)
        for where in (0, 777, len(ct) - 1):
            bad = bytearray(ct)
            bad[where] ^= 0x01
            with self.assertRaises(aesgcm.AuthenticationError):
                aesgcm.decrypt(key, nonce, bytes(bad), aad)
        with self.assertRaises(aesgcm.AuthenticationError):
            aesgcm.decrypt(key, nonce, ct, b"other aad")
        with self.assertRaises(aesgcm.AuthenticationError):
            aesgcm.decrypt(bytes(32), nonce, ct, aad)
        with self.assertRaises(aesgcm.AuthenticationError):
            aesgcm.decrypt(key, nonce, ct[:10], aad)
        with self.assertRaises(ValueError):
            aesgcm.encrypt(key, bytes(16), data)
        with self.assertRaises(ValueError):
            aesgcm.encrypt(bytes(20), nonce, data)
        self.assertEqual(aesgcm.decrypt(key, nonce, aesgcm.encrypt(key, nonce, b"", aad), aad), b"")


class SealedBundle(unittest.TestCase):
    def test_seal_open_header_and_the_wrong_passphrase(self):
        blob = lockbox.seal(PASS, b'{"hello": "desk"}' * 100, FAST)
        self.assertEqual(blob[:8], lockbox.MAGIC)
        iterations, salt, nonce = lockbox.parse_header(blob)
        self.assertEqual(iterations, FAST)
        self.assertEqual((len(salt), len(nonce)), (16, 12))
        self.assertNotEqual(salt, bytes(16))
        self.assertEqual(lockbox.open_(PASS, blob), b'{"hello": "desk"}' * 100)
        for wrong in (PASS + " ", PASS.upper(), "password", " " + PASS):
            with self.assertRaises(lockbox.LockError):
                lockbox.open_(wrong, blob)
        bad = bytearray(blob)
        bad[lockbox.HEADER + 5] ^= 0x80
        with self.assertRaises(lockbox.LockError):
            lockbox.open_(PASS, bytes(bad))
        bad = bytearray(blob)
        bad[9] ^= 0x01                                   # the iteration count is covered by the tag too
        with self.assertRaises(lockbox.LockError):
            lockbox.open_(PASS, bytes(bad))
        with self.assertRaises(lockbox.LockError):
            lockbox.parse_header(b"not a bundle at all, far too short")
        with self.assertRaises(lockbox.LockError):
            lockbox.seal(PASS, b"x", FAST - 1)
        with self.assertRaises(lockbox.LockError):
            lockbox.seal("", b"x", FAST)
        self.assertNotEqual(lockbox.seal(PASS, b"same", FAST), lockbox.seal(PASS, b"same", FAST))   # fresh salt and nonce

    def test_the_bundle_carries_both_pages_and_the_data(self):
        d = tempfile.mkdtemp()
        site, pub = os.path.join(d, "site"), os.path.join(d, "publish")
        os.makedirs(site)
        os.makedirs(pub)
        with open(os.path.join(site, "index.html"), "w") as fh:
            fh.write("<html><head></head><body>idea</body></html>")
        with open(os.path.join(site, "full.html"), "w") as fh:
            fh.write("<html><head></head><body>full</body></html>")
        with self.assertRaises(lockbox.LockError):
            lockbox.build_bundle(site, pub)                                   # no data yet
        with open(os.path.join(pub, "desk.json"), "w") as fh:
            json.dump({"created_by": "trader-desk", "mode": "morning", "date": "2026-10-26"}, fh)
        with open(os.path.join(pub, "regime.json"), "w") as fh:
            json.dump({"created_by": "trader-regime", "date": "2026-10-26"}, fh)
        with open(os.path.join(pub, "notes.json"), "w") as fh:
            json.dump({"never": "published"}, fh)                              # not one of the seven: left out
        bundle = lockbox.build_bundle(site, pub)
        self.assertEqual(bundle["format"], lockbox.FORMAT)
        self.assertEqual(sorted(bundle["pages"]), ["full", "idea"])
        self.assertEqual(sorted(bundle["data"]), ["desk.json", "regime.json"])
        self.assertRegex(bundle["built_et"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} ET")
        blob = lockbox.seal(PASS, lockbox.bundle_bytes(bundle), FAST)
        back = lockbox.load_bundle(PASS, blob)
        self.assertEqual(back["pages"]["idea"], "<html><head></head><body>idea</body></html>")
        self.assertEqual(back["data"]["desk.json"]["mode"], "morning")
        with self.assertRaises(lockbox.LockError):
            lockbox.load_bundle(PASS, lockbox.seal(PASS, b'{"format": "something else"}', FAST))

    def test_the_passphrase_comes_from_the_environment_then_the_file_and_never_from_the_repo(self):
        d = tempfile.mkdtemp()
        self.assertEqual(lockbox.read_passphrase(d, {lockbox.ENV: "from env"}, keychain=False), "from env")
        with self.assertRaises(lockbox.LockError):
            lockbox.read_passphrase(d, {}, keychain=False)
        os.makedirs(os.path.join(d, "trader"))
        with open(os.path.join(d, lockbox.PASS_FILE), "w") as fh:
            fh.write("from file\n")
        self.assertEqual(lockbox.read_passphrase(d, {}, keychain=False), "from file")
        self.assertEqual(lockbox.read_passphrase(d, {lockbox.ENV: "env wins"}, keychain=False), "env wins")
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")
        self.assertNotIn(lockbox.PASS_FILE, tracked)
        self.assertFalse([t for t in tracked if t.startswith("trader/publish/")], "trader/publish is private")
        self.assertFalse([t for t in tracked if t.startswith("docs/") and t.endswith(".json")], "no plain data under docs/")
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            ignore = fh.read()
        for line in ("trader/publish/", "trader/site.pass", "docs/data/*.json"):
            self.assertIn(line, ignore)


class LockScript(unittest.TestCase):
    def run_lock(self, args, env_pass=PASS):
        env = dict(os.environ)
        env.pop(lockbox.ENV, None)
        if env_pass is not None:
            env[lockbox.ENV] = env_pass
        return subprocess.run([sys.executable, LOCK] + args, capture_output=True, text=True, env=env, cwd=ROOT)

    def test_build_verify_header_restore_and_the_wrong_passphrase(self):
        d = tempfile.mkdtemp()
        pub, out = os.path.join(d, "publish"), os.path.join(d, "out", "bundle.enc")
        os.makedirs(pub)
        with open(os.path.join(pub, "desk.json"), "w") as fh:
            json.dump({"created_by": "trader-desk", "mode": "morning", "date": "2026-10-26", "candidates": []}, fh)
        r = self.run_lock(["build", "--site", SITE, "--publish-dir", pub, "--out", out, "--iterations", str(FAST)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Wrote", r.stdout)
        self.assertNotIn(PASS, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(out))
        self.assertFalse(os.path.exists(out + ".tmp"))
        r = self.run_lock(["verify", "--out", out])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Opens.", r.stdout)
        self.assertIn("desk.json", r.stdout)
        r = self.run_lock(["header", "--out", out], env_pass=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["iterations"], FAST)
        r = self.run_lock(["verify", "--out", out], env_pass="not it")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not open", r.stderr)
        r = self.run_lock(["restore", "--out", out, "--dir", os.path.join(d, "plain")])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(d, "plain", "index.html")))
        self.assertTrue(os.path.exists(os.path.join(d, "plain", "full.html")))
        self.assertTrue(os.path.exists(os.path.join(d, "plain", "data", "desk.json")))
        r = self.run_lock(["build", "--site", SITE, "--publish-dir", pub, "--out", out, "--iterations", "1000"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("iterations", r.stderr)
        r = self.run_lock([], env_pass=None)
        self.assertEqual(r.returncode, 2)


class PublicDocs(unittest.TestCase):
    def test_docs_holds_only_the_lock_screen_the_404_and_the_sealed_bundle(self):
        found = []
        for base, dirs, files in os.walk(DOCS):
            for f in files:
                found.append(os.path.relpath(os.path.join(base, f), DOCS))
        self.assertEqual(sorted(found), [".nojekyll", "404.html", os.path.join("data", "bundle.enc"), "index.html"])

    def test_the_real_bundle_is_sealed_hard_and_does_not_open_to_the_obvious(self):
        with open(os.path.join(DOCS, "data", "bundle.enc"), "rb") as fh:
            blob = fh.read()
        iterations, salt, nonce = lockbox.parse_header(blob)
        self.assertGreaterEqual(iterations, lockbox.MIN_ITERATIONS)
        self.assertNotEqual(salt, bytes(16))
        self.assertNotEqual(nonce, bytes(12))
        for guess in ("password", "daytrader"):
            with self.assertRaises(lockbox.LockError):
                lockbox.open_(guess, blob[:lockbox.HEADER] + blob[lockbox.HEADER:lockbox.HEADER + 64] + blob[-16:])
        self.assertNotIn(b"created_by", blob)
        self.assertNotIn(b"<html", blob)

    def test_the_lock_screen_keeps_nothing_and_loads_nothing_from_elsewhere(self):
        with open(os.path.join(DOCS, "index.html"), encoding="utf-8") as fh:
            shell = fh.read()
        for must in ("Content-Security-Policy", "connect-src 'self'", "form-action 'none'", "base-uri 'none'", 'name="robots" content="noindex',
                     "DTLOCK1", "PBKDF2", "AES-GCM", "DecompressionStream", "visibilitychange", "pageshow", "prefers-color-scheme",
                     'type="password"', 'autocomplete="current-password"', "most people who day trade lose money"):
            self.assertTrue(must in shell or must in shell.lower(), must)
        for never in ("localStorage", "sessionStorage", "document.cookie", "indexedDB", "https://", "http://"):
            self.assertNotIn(never, shell, never)
        self.assertNotRegex(shell, r'<script[^>]+src=')
        self.assertNotIn(chr(0x2014), shell)
        self.assertNotIn("<!--", shell.split("<script>", 1)[1])   # a comment opener inside the script would break the parser
        with open(os.path.join(DOCS, "404.html"), encoding="utf-8") as fh:
            nf = fh.read()
        self.assertIn('url=/daytrader/', nf)
        self.assertIn("noindex", nf)
        self.assertNotIn(chr(0x2014), nf)

    def test_the_pages_read_the_decrypted_data_and_link_through_the_top_window(self):
        for name in ("index.html", "full.html"):
            with open(os.path.join(SITE, name), encoding="utf-8") as fh:
                html = fh.read()
            self.assertIn("window.__DT", html, name)
            self.assertIn('href="#lock" target="_top"', html, name)
            self.assertNotIn('href="full.html"', html, name)
            self.assertNotIn('href="index.html"', html, name)
        with open(os.path.join(ROOT, "scripts", "run.sh"), encoding="utf-8") as fh:
            run = fh.read()
        self.assertIn("lock.py build", run)
        self.assertIn("git add docs/data/bundle.enc", run)
        self.assertNotIn("git add docs/data ", run)
        self.assertNotIn("git add docs/data\n", run)
        self.assertIn("nothing published", run)


if __name__ == "__main__":
    unittest.main()
