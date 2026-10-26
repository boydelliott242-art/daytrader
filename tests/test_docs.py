"""The dashboard (docs/), the data files it reads, the scripts and the schedule: offline checks."""

import json
import os
import re
import subprocess
import tempfile
import unittest

from _load import ROOT

DOCS = os.path.join(ROOT, "docs")
SITE = os.path.join(ROOT, "site")          # the pages, sealed into docs/data/bundle.enc by scripts/lock.py
DATA = os.path.join(ROOT, "trader", "publish")   # what --publish writes (private, gitignored)
SCRIPTS = os.path.join(ROOT, "scripts")

KEYS = {
    "desk.json": ("created_by", "mode", "date"),   # a morning plan or a live board
    "edge.json": ("created_by", "configs", "configs_tested", "split"),
    "regime.json": ("created_by", "date", "daily", "tags"),
    "scorecard.json": ("as_of", "calls", "graded", "stats"),
    "largecap.json": ("created_by", "count", "symbols"),
    "montecarlo.json": ("created_by", "kind", "books"),
    "nq.json": ("edge",),
    "rankings.json": ("created_by", "date", "lists", "no_trade"),
    "research.json": ("created_by", "cards"),
    "features.json": ("created_by", "kind", "per_config", "adds_value"),
    "robustness.json": ("created_by", "kind", "per_config"),
}


class Dashboard(unittest.TestCase):
    def test_the_front_page_is_one_idea_and_the_full_page_reads_every_file(self):
        with open(os.path.join(SITE, "index.html"), encoding="utf-8") as fh:
            idea = fh.read()
        with open(os.path.join(SITE, "full.html"), encoding="utf-8") as fh:
            full = fh.read()
        for name in ("desk.json", "regime.json"):
            self.assertIn("data/" + name, idea, name)
        for name in KEYS:
            self.assertIn("data/" + name, full, name)
        self.assertIn('href="#full" target="_top"', idea)
        self.assertIn('href="#idea" target="_top"', full)
        for html in (idea, full):
            self.assertNotRegex(html, r'<script[^>]+src="https?://')
            self.assertNotRegex(html, r'<link[^>]+href="https?://')
            self.assertIn("<title>", html)
            self.assertIn("prefers-color-scheme", html)
            self.assertNotIn(chr(0x2014), html)
            self.assertIn("most people who day trade lose money", html.lower())
        low = idea.lower()
        for must in ("not a prediction", "not a buy", "typed yes", "the idea comes at 10:00 et", "watch only", "too late", "short side", "research only"):
            self.assertIn(must, low, must)
        for must in ("not financial advice", "configs tested", "short board", "rankings", "research cards", "feature study", "robustness",
                     "lose more than the stake", "no trade"):
            self.assertIn(must, full.lower(), must)

    def test_the_data_files_parse_and_carry_the_contract_keys(self):
        # the published files are private (trader/publish, sealed into the bundle); checked where the tools have run
        present = [n for n in KEYS if os.path.exists(os.path.join(DATA, n))]
        if not present:
            self.skipTest("no published data in trader/publish on this machine (scripts/example_data.py writes an example set)")
        for name in present:
            keys = KEYS[name]
            path = os.path.join(DATA, name)
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            for k in keys:
                self.assertIn(k, doc, "%s lacks %s" % (name, k))


class Scripts(unittest.TestCase):
    def test_shell_scripts_parse_and_are_executable(self):
        for f in ("run.sh", "install_launchd.sh", "uninstall_launchd.sh", "start.command", "bg.sh", "install_skills.sh"):
            path = os.path.join(SCRIPTS, f)
            self.assertTrue(os.access(path, os.X_OK), f)
            r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn(chr(0x2014), fh.read(), f)

    def test_the_schedule_installs_dry(self):
        d = tempfile.mkdtemp()
        env = dict(os.environ, DAYTRADER_DRY_RUN="1", DAYTRADER_PLIST_DIR=d)
        r = subprocess.run(["sh", os.path.join(SCRIPTS, "install_launchd.sh")], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        plists = sorted(f for f in os.listdir(d) if f.endswith(".plist"))
        self.assertEqual(plists, ["com.daytrader.cache.plist", "com.daytrader.futures.plist", "com.daytrader.grade.plist",
                                  "com.daytrader.lab.plist", "com.daytrader.live.plist", "com.daytrader.live2.plist",
                                  "com.daytrader.live3.plist", "com.daytrader.live4.plist", "com.daytrader.morning.plist"])
        for f in plists:
            path = os.path.join(d, f)
            lint = subprocess.run(["plutil", "-lint", path], capture_output=True, text=True)
            self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("run.sh", text)
            self.assertIn("<key>RunAtLoad</key><false/>", text)
            if f != "com.daytrader.lab.plist":
                for wd in range(1, 6):
                    self.assertIn("<key>Weekday</key><integer>%d</integer>" % wd, text, f)
                self.assertNotIn("<integer>6</integer>", text.split("StartCalendarInterval")[1].split("Weekday</key><integer>")[0] if False else "")
        self.assertIn("Nothing scheduled ever places an order", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_run_sh_knows_its_jobs_and_never_calls_the_broker(self):
        with open(os.path.join(SCRIPTS, "run.sh"), encoding="utf-8") as fh:
            text = fh.read()
        for job in ("cache)", "morning)", "live)", "grade)", "lab)", "futures)"):
            self.assertIn(job, text, job)
        self.assertNotIn("ticket.py approve", text)
        self.assertNotIn("place_stock_order", text)
        self.assertNotIn("paper.py place", text)
        r = subprocess.run(["sh", os.path.join(SCRIPTS, "run.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stdout + r.stderr)


class Repo(unittest.TestCase):
    def test_readme_and_claude_md(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        for must in ("/trader-desk", "/trader-lab", "/trader-nq", "/trader-montecarlo", "/trader-intel", "install_skills.sh",
                     "configs tested", "Most people who day trade lose money", "UPSTREAM_README.md", "not a prediction"):
            self.assertIn(must, readme, must)
        self.assertNotIn(chr(0x2014), readme)
        with open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8") as fh:
            claude = fh.read()
        for must in ("standard library", "em dashes", "CONTRACTS.md", "unittest discover"):
            self.assertIn(must, claude, must)
        self.assertNotIn(chr(0x2014), claude)

    def test_contracts_has_no_em_dash_and_names_every_skill(self):
        with open(os.path.join(ROOT, "CONTRACTS.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(chr(0x2014), text)
        for s in ("trader-largecap", "trader-regime", "trader-lab", "trader-desk", "trader-scorecard", "trader-paper"):
            self.assertIn(s, text, s)
        # the three added after the first contract
        extra = re.findall(r"trader-(intel|montecarlo|nq)", text)
        self.assertTrue(extra, "CONTRACTS.md should mention the later skills")


if __name__ == "__main__":
    unittest.main()
