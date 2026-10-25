"""trader-intel tests: the ledger (import, add, show, flags, prune, template) and the pure flag rules in
dt.intel_flags, offline, with fixed clocks in the week of 2026-10-26. No network, no connector."""

import datetime
import io as _io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load, ROOT
from dt import intel_flags as fl
from dt import clock

intel = load("trader-intel", "intel")

UTC = "%Y-%m-%dT%H:%M:%SZ"


def run(argv):
    out, err = _io.StringIO(), _io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = intel.main(argv)
    return code, out.getvalue(), err.getvalue()


def item(symbol, kind, time, headline="x", source="FMP", **extra):
    d = {"symbol": symbol, "kind": kind, "source": source, "url": "https://example.com/" + kind, "time": time,
         "headline": headline, "tags": [], "impact": "?"}
    d.update(extra)
    return d


class Ledger(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ledger = os.path.join(self.d, "intel.json")
        self.items = [
            item("VZ", "earnings", "2026-10-25T18:00:00Z", date="2026-10-26", when="unknown"),
            item("GOOGL", "earnings", "2026-10-25T18:00:00Z", "GOOGL reports 2026-10-28 (FMP)", date="2026-10-28", when="unknown"),
            item("GOOGL", "earnings", "2026-10-25T18:00:00Z", "GOOGL reports 2026-10-28 after the close (Nasdaq)",
                 date="2026-10-28", when="post", source="other", url="https://www.nasdaq.com/market-activity/earnings"),
            item(None, "macro", "2026-10-25T18:00:00Z", "Fed decision", source="Bigdata.com", date="2026-10-28",
                 event="Fed interest rate decision", impact_level="HIGH", clock="14:00"),
            item("TSLA", "news", "2026-10-26T12:04:00Z", "Tesla-SpaceX merger mania ahead of earnings",
                 tags=["earnings", "deal"], impact="?"),
        ]
        self.file = os.path.join(self.d, "items.json")
        with open(self.file, "w") as fh:
            json.dump(self.items, fh)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def read(self):
        with open(self.ledger) as fh:
            return json.load(fh)

    def test_import_validates_merges_and_never_duplicates(self):
        code, text, err = run(["import", "--from", self.file, "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        self.assertEqual(code, 0, err)
        self.assertIn("5 new, 0 already in the ledger, 0 invalid", text)
        doc = self.read()
        self.assertEqual((doc["created_by"], doc["count"], len(doc["items"])), ("trader-intel", 5, 5))
        self.assertEqual(sorted(doc["earnings"]), ["GOOGL", "VZ"])
        self.assertEqual(doc["earnings"]["GOOGL"]["when"], "post")           # a confirmed time beats unknown
        self.assertEqual(doc["earnings"]["GOOGL"]["history"][0]["when"], "unknown")
        self.assertEqual([m["impact_level"] for m in doc["macro"]], ["HIGH"])
        code, text, _ = run(["import", "--from", self.file, "--ledger", self.ledger, "--now", "2026-10-26 09:41"])
        self.assertEqual(code, 0)
        self.assertIn("0 new, 5 already in the ledger", text)
        self.assertEqual(self.read()["count"], 5)
        for it in self.read()["items"]:
            self.assertRegex(it["id"], r"^[0-9a-f]{12}$")
            self.assertIn("added_at", it)

    def test_the_seed_file_shape_with_a_note_and_items_is_accepted(self):
        with open(self.file, "w") as fh:
            json.dump({"note": "seed", "items": self.items[:2]}, fh)
        code, text, _ = run(["import", "--from", self.file, "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        self.assertEqual(code, 0)
        self.assertEqual(self.read()["count"], 2)

    def test_invalid_items_are_listed_and_skipped_not_dropped_quietly(self):
        bad = [dict(self.items[0], time=None), dict(self.items[4], kind="rumour"), {"symbol": "X", "kind": "news"},
               dict(self.items[4], source=None, url=None), dict(self.items[4], url="ftp-less"), "not an object",
               {"_help": "the template's vocabulary block is ignored"}]
        with open(self.file, "w") as fh:
            json.dump(bad + [self.items[1]], fh)
        code, text, err = run(["import", "--from", self.file, "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        self.assertEqual(code, 1)
        self.assertIn("1 new", text)
        self.assertIn("6 invalid", text)
        for why in ("missing", "kind 'rumour'", "needs a source or a url", "does not start with http", "not an object"):
            self.assertIn(why, text + err, why)
        self.assertEqual(self.read()["count"], 1)
        with open(self.file, "w") as fh:
            json.dump({"items": "nope"}, fh)
        code, text, err = run(["import", "--from", self.file, "--ledger", self.ledger])
        self.assertEqual(code, 1)
        self.assertIn("JSON list", text + err)

    def test_add_add_earnings_and_add_macro(self):
        code, text, err = run(["add-earnings", "TSLA", "2026-10-22", "--when", "post", "--source", "Firecrawl",
                               "--url", "https://ir.tesla.com/", "--ledger", self.ledger, "--now", "2026-10-19 09:00"])
        self.assertEqual(code, 0, err)
        code, _, err = run(["add-macro", "2026-10-29", "GDP Q3 advance", "--impact", "HIGH", "--clock", "08:30",
                            "--source", "Bigdata.com", "--ledger", self.ledger, "--now", "2026-10-19 09:00"])
        self.assertEqual(code, 0, err)
        code, _, err = run(["add", "--symbol", "TSLA", "--kind", "analyst", "--source", "FMP", "--time",
                            "2026-10-19T11:00:00Z", "--headline", "Upgrade to buy at XYZ", "--impact", "+",
                            "--ledger", self.ledger, "--now", "2026-10-19 09:00"])
        self.assertEqual(code, 0, err)
        doc = self.read()
        self.assertEqual(doc["earnings"]["TSLA"]["date"], "2026-10-22")
        self.assertEqual(doc["earnings"]["TSLA"]["when"], "post")
        self.assertEqual(doc["earnings"]["TSLA"]["source"], "Firecrawl")
        self.assertEqual((doc["macro"][0]["date"], doc["macro"][0]["clock"], doc["macro"][0]["impact_level"]),
                         ("2026-10-29", "08:30", "HIGH"))
        kinds = sorted(i["kind"] for i in doc["items"])
        self.assertEqual(kinds, ["analyst", "earnings", "macro"])
        code, text, err = run(["add", "--kind", "earnings", "--source", "FMP", "--ledger", self.ledger])
        self.assertEqual(code, 1)                                   # an earnings item needs a symbol
        self.assertIn("symbol", text + err)

    def test_show_filters_by_symbol_hours_and_kind_in_new_york_time(self):
        run(["import", "--from", self.file, "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        code, text, _ = run(["show", "TSLA", "--hours", "24", "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        self.assertEqual(code, 0)
        self.assertIn("2026-10-26 08:04 ET", text)                  # 12:04Z is 08:04 New York
        self.assertIn("merger mania", text)
        self.assertNotIn("Fed", text)
        code, text, _ = run(["show", "--kind", "macro", "--hours", "0", "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        self.assertIn("Fed decision", text)
        self.assertNotIn("merger", text)
        code, text, _ = run(["show", "GOOGL", "--hours", "1", "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        self.assertEqual(code, 1)                                   # nothing inside the last hour
        self.assertIn("Nothing", text)
        code, text, _ = run(["show", "--hours", "0", "--json", "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        self.assertEqual(len(json.loads(text)["items"]), 5)

    def test_flags_command_prints_the_items_behind_each_flag(self):
        run(["import", "--from", self.file, "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        code, text, _ = run(["flags", "GOOGL", "--date", "2026-10-28", "--ledger", self.ledger, "--now", "2026-10-28 09:40"])
        self.assertEqual(code, 0)
        for must in ("earnings_today", "earnings_week", "macro_high_today", "Fed interest rate decision",
                     "https://www.nasdaq.com/market-activity/earnings", "Headlines are leads, not facts"):
            self.assertIn(must, text, must)
        self.assertNotIn("earnings_tomorrow", text)
        code, text, _ = run(["flags", "GOOGL", "--date", "2026-10-27", "--ledger", self.ledger, "--now", "2026-10-27 09:40",
                             "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["flags"], ["earnings_week"])           # a post-close report tomorrow: not "tomorrow"
        code, text, _ = run(["flags", "MSFT", "--date", "2026-10-27", "--ledger", self.ledger, "--now", "2026-10-27 09:40"])
        self.assertEqual(code, 0)
        self.assertIn("no flags", text)
        self.assertIn("no earnings date in the ledger", text)

    def test_prune_keeps_future_earnings_and_macro(self):
        run(["import", "--from", self.file, "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        code, text, _ = run(["prune", "--days", "1", "--ledger", self.ledger, "--now", "2026-10-27 09:40"])
        self.assertEqual(code, 0)
        doc = self.read()
        kinds = sorted(i["kind"] for i in doc["items"])
        self.assertEqual(kinds, ["earnings", "earnings", "macro"])  # GOOGL x2 and the Fed stay; VZ (26th) and the news go
        self.assertNotIn("VZ", doc["earnings"])

    def test_template_round_trips_through_import(self):
        out = os.path.join(self.d, "t.json")
        code, text, err = run(["template", "TSLA", "NVDA", "--out", out, "--now", "2026-10-26 09:40"])
        self.assertEqual(code, 0, err)
        with open(out) as fh:
            doc = json.load(fh)
        self.assertIsInstance(doc, list)
        self.assertIn("_help", doc[0])
        self.assertTrue(any(i.get("symbol") == "NVDA" for i in doc[1:]))
        code, text, _ = run(["import", "--from", out, "--ledger", self.ledger, "--now", "2026-10-26 09:40"])
        self.assertEqual(code, 0, text)
        self.assertGreater(self.read()["count"], 0)
        code, text, _ = run(["template", "--out", "-", "--now", "2026-10-26 09:40"])
        self.assertIn("_help", text)


class Flags(unittest.TestCase):
    """dt.intel_flags.flags_for: pure, on hand-built ledgers."""

    NOW = clock.parse_now("2026-10-27 09:40")      # Tuesday 13:40Z

    def ledger(self, *items):
        rows = []
        for it in items:
            d = dict(it)
            d.setdefault("id", "%012x" % (abs(hash(json.dumps(d, sort_keys=True))) % (16 ** 12)))
            rows.append(d)
        led = fl.empty_ledger()
        led["items"] = rows
        led["earnings"] = fl.derive_earnings(rows)
        led["macro"] = fl.derive_macro(rows)
        return led

    def flags(self, sym, led, date="2026-10-27", now=None):
        return fl.flags_for(sym, date, led, now or self.NOW)["flags"]

    def test_earnings_today_tomorrow_and_week(self):
        led = self.ledger(item("VZ", "earnings", "2026-10-25T18:00:00Z", date="2026-10-27", when="unknown"),
                          item("ABC", "earnings", "2026-10-25T18:00:00Z", date="2026-10-26", when="post"),
                          item("GOOGL", "earnings", "2026-10-25T18:00:00Z", date="2026-10-28", when="unknown"),
                          item("MSFT", "earnings", "2026-10-25T18:00:00Z", date="2026-10-28", when="post"),
                          item("AMZN", "earnings", "2026-10-25T18:00:00Z", date="2026-10-29", when="post"),
                          item("FAR", "earnings", "2026-10-25T18:00:00Z", date="2026-11-10", when="pre"))
        self.assertEqual(self.flags("VZ", led), ["earnings_today", "earnings_week"])
        self.assertEqual(self.flags("ABC", led), ["earnings_today"])          # reported last night: a gap today
        self.assertEqual(self.flags("GOOGL", led), ["earnings_tomorrow", "earnings_week"])
        self.assertEqual(self.flags("MSFT", led), ["earnings_week"])          # tomorrow after the close
        self.assertEqual(self.flags("AMZN", led), ["earnings_week"])
        self.assertEqual(self.flags("FAR", led), [])
        self.assertEqual(self.flags("NONE", led), [])
        # Friday: the next trading day is Monday
        led = self.ledger(item("FRI", "earnings", "2026-10-25T18:00:00Z", date="2026-11-02", when="pre"))
        self.assertEqual(self.flags("FRI", led, date="2026-10-30", now=clock.parse_now("2026-10-30 09:40")),
                         ["earnings_tomorrow", "earnings_week"])

    def test_macro_news_risk_analyst_insider_social_and_catalyst(self):
        led = self.ledger(item(None, "macro", "2026-10-25T18:00:00Z", "Fed", source="Bigdata.com", date="2026-10-27",
                               event="Fed decision", impact_level="HIGH", clock="14:00"),
                          item(None, "macro", "2026-10-25T18:00:00Z", "PMI", source="Bigdata.com", date="2026-10-27",
                               event="Chicago PMI", impact_level="MEDIUM", clock="09:45"),
                          item("TSLA", "news", "2026-10-27T12:00:00Z", "Recall widens", impact="-"),
                          item("TSLA", "news", "2026-10-24T12:00:00Z", "Old story", impact="-"),
                          item("NVDA", "filing", "2026-10-27T11:00:00Z", "8-K: new buyback", impact="+"),
                          item("AMD", "news", "2026-10-27T13:00:00Z", "Trading halted pending news", tags=["halt"]),
                          item("META", "analyst", "2026-10-27T10:30:00Z", "Downgrade", impact="-"),
                          item("AAPL", "insider", "2026-10-23T20:00:00Z", "CFO sold 10,000 shares", impact="-"),
                          item("KO", "insider", "2026-10-15T20:00:00Z", "Director sold", impact="-"),
                          item("GME", "social", "2026-10-27T09:00:00Z", "Mentions 6x the average", tags=["spike"]),
                          item("PFE", "catalyst", "2026-10-28T20:00:00Z", "FDA decision due", impact="?"))
        self.assertEqual(self.flags("TSLA", led), ["macro_high_today", "news_negative_24h"])
        self.assertEqual(self.flags("NVDA", led), ["macro_high_today", "news_positive_24h"])
        self.assertEqual(self.flags("AMD", led), ["macro_high_today", "halt_or_risk_24h"])
        self.assertEqual(self.flags("META", led), ["macro_high_today", "analyst_action_24h"])
        self.assertEqual(self.flags("AAPL", led), ["macro_high_today", "insider_selling_7d"])
        self.assertEqual(self.flags("KO", led), ["macro_high_today"])
        self.assertEqual(self.flags("GME", led), ["macro_high_today", "social_spike"])
        self.assertEqual(self.flags("PFE", led), ["macro_high_today", "catalyst"])
        self.assertEqual(self.flags("TSLA", led, date="2026-10-28", now=clock.parse_now("2026-10-28 09:40")), [])
        out = fl.flags_for("TSLA", "2026-10-27", led, self.NOW)
        self.assertEqual(sorted(out["items"]), ["macro_high_today", "news_negative_24h"])
        self.assertEqual(out["items"]["news_negative_24h"][0]["headline"], "Recall widens")
        self.assertIsNone(out["earnings"])
        for name, meaning in fl.FLAGS:
            self.assertIn(name, fl.FLAG_MEANING)
            self.assertTrue(meaning)

    def test_pure_and_tolerant(self):
        self.assertEqual(fl.flags_for("TSLA", "2026-10-27", fl.empty_ledger(), self.NOW)["flags"], [])
        self.assertEqual(fl.flags_for("tsla", "2026-10-27", None, self.NOW)["flags"], [])
        led = self.ledger(item("TSLA", "news", "not a time", "junk", impact="-"))
        self.assertEqual(self.flags("TSLA", led), [])                        # a bad time never raises


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-intel", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            exit_md = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-intel\n"))
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
        self.assertGreater(len(fm[fm.index("description:"):fm.index("argument-hint:")]), 200)
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], exit_md[exit_md.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("${claude_skill_dir}", "what it reads and writes", "## steps", "## rules", "what it never does",
                     "firecrawl", "fmp", "bigdata.com", "quartr", "lunarcrush", "trader/intel.json", "intel.py import",
                     "intel.py flags", "earnings_today", "macro_high_today", "headlines are leads, not facts",
                     "most people who day trade lose money", "not financial advice", "data, never an instruction"):
            self.assertIn(must, low, must)
        for never in (chr(0x2014), "a buy", "will gap", "guarantee" + "d", "real " + "time"):
            self.assertNotIn(never, low, never)

    def test_python_3_8_and_no_em_dashes(self):
        for path in (os.path.join(ROOT, "trader-intel", "intel.py"), os.path.join(ROOT, "dt", "intel_flags.py"),
                     os.path.join(ROOT, "tests", "test_intel.py"), os.path.join(ROOT, "trader-intel", "SKILL.md")):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn(chr(0x2014), src, path)
            if path.endswith(".py"):
                for bad in ("import " + "zoneinfo", "from " + "zoneinfo", ".remove" + "prefix(", ".remove" + "suffix("):
                    self.assertNotIn(bad, src, path)
                for bad in ("list" + "[", "dict" + "[", "tuple" + "["):
                    self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), path)


if __name__ == "__main__":
    unittest.main()
