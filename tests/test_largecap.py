"""trader-largecap tests. The screener fixture tests/fixtures/trader-largecap/screener.json is a cut of
Nasdaq's real answer (245 rows: the 150 largest caps, 40 mid-large, 35 under $10B, 20 without a usable
market cap, and every symbol with a slash); universe.txt lists most of the large ones but not BRK.A,
GOOG, GOOGL, LRCX, MNST or VLYPO, so the intersection is tested. Everything offline."""

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

largecap = load("trader-largecap", "largecap")

SCREENER = os.path.join(FIX, "trader-largecap", "screener.json")
UNIVERSE = os.path.join(FIX, "trader-largecap", "universe.txt")
NOW = "2026-10-25 09:00"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = largecap.main(argv)
    return code, out.getvalue(), err.getvalue()


def cap_of(row):
    try:
        return float(row["marketCap"])
    except (TypeError, ValueError):
        return None


def read_rows():
    with open(SCREENER, encoding="utf-8") as fh:
        return json.load(fh)


def read_universe():
    with open(UNIVERSE, encoding="utf-8") as fh:
        return [ln.split("|")[0] for ln in fh if ln.strip()]


def expected(floor=10e9, use_universe=True):
    """What the rule says the list is: cap >= floor, slash -> dot, in the universe file, largest first, one a symbol."""
    allowed = set(read_universe())
    seen, out = set(), []
    for r in sorted(read_rows(), key=lambda r: -(cap_of(r) or 0)):
        c = cap_of(r)
        sym = r["symbol"].replace("/", ".")
        if c is None or c < floor or (use_universe and sym not in allowed) or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


class Fixture(unittest.TestCase):
    def test_the_fixture_holds_what_the_tests_assume(self):
        rows = read_rows()
        self.assertGreaterEqual(len(rows), 240)
        self.assertTrue(all(set(("symbol", "name", "marketCap", "sector", "industry", "country")) <= set(r) for r in rows))
        self.assertIn("BRK/B", [r["symbol"] for r in rows])
        self.assertTrue(any(r["marketCap"] in ("", "0.00") for r in rows))
        self.assertTrue(any(cap_of(r) and cap_of(r) < 10e9 for r in rows))
        uni = read_universe()
        self.assertIn("BRK.B", uni)
        for missing in ("BRK.A", "GOOG", "GOOGL"):
            self.assertNotIn(missing, uni)
        self.assertEqual(len(expected()), 184)


class Build(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def build(self, *args):
        return run(["build", "--offline", SCREENER, "--universe", UNIVERSE, "--now", NOW] + list(args))

    def test_build_on_the_fixture_count_order_tiers(self):
        code, text, err = self.build("--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        syms = [s["symbol"] for s in doc["symbols"]]
        self.assertEqual(syms, expected())
        self.assertEqual(doc["count"], 184)
        self.assertEqual(doc["created_by"], "trader-largecap")
        self.assertEqual(doc["as_of"], "2026-10-25T13:00:00Z")
        self.assertEqual(doc["min_cap_usd"], 10e9)
        self.assertEqual(doc["universe_symbols"], 186)
        caps = [s["market_cap"] for s in doc["symbols"]]
        self.assertEqual(caps, sorted(caps, reverse=True))
        self.assertEqual(syms[0], "NVDA")
        # a '/' symbol is written with a dot, like the upstream lists spell it
        self.assertIn("BRK.B", syms)
        self.assertFalse(any("/" in s for s in syms))
        # a symbol the universe file does not list is dropped, however big
        for missing in ("BRK.A", "GOOG", "GOOGL", "LRCX"):
            self.assertNotIn(missing, syms)
        tiers = {s["symbol"]: s["tier"] for s in doc["symbols"]}
        self.assertEqual(tiers["NVDA"], "mega")
        self.assertEqual(tiers["BRK.B"], "mega")
        for s in doc["symbols"]:
            want = "mega" if s["market_cap"] >= 200e9 else "large" if s["market_cap"] >= 50e9 else "mid-large"
            self.assertEqual(s["tier"], want, s["symbol"])
        self.assertEqual(doc["counts"]["by_tier"], {"mega": 71, "large": 74, "mid-large": 39})
        self.assertEqual(doc["counts"]["rows"], 245)
        self.assertEqual(doc["counts"]["dropped_below_floor"], 55)
        self.assertEqual(doc["counts"]["dropped_not_in_universe"], 6)
        self.assertEqual(list(doc["counts"]["by_sector"].items())[0], ("Technology", 47))
        for k in ("symbol", "name", "market_cap", "tier", "sector", "industry", "country", "ipo_year"):
            self.assertIn(k, doc["symbols"][0], k)
        self.assertIsNone(doc["written"])
        self.assertFalse(os.path.exists(os.path.join("trader", "largecap.json")))

    def test_the_text_report(self):
        code, text, err = self.build()
        self.assertEqual(code, 0, err)
        for must in ("245 rows", "Floor $10B", "trader/universe.txt (186 symbols)", "Kept 184 large caps",
                     "71 mega (at or above $200B)", "74 large ($50B to $200B)", "39 mid-large ($10B to $50B)",
                     "Dropped 55 under the floor or without a market cap, 6 not in the universe file",
                     "Sectors: Technology 47", "Largest: NVDA $5.11T, AAPL $5.08T", "Nothing written",
                     "Most people who day trade lose money"):
            self.assertIn(must, text, must)
        self.assertNotIn("\u2014", text)

    def test_write_gives_the_two_files_in_the_upstream_shape(self):
        code, text, err = self.build("--write")
        self.assertEqual(code, 0, err)
        self.assertIn("Wrote trader/largecap.json and trader/largecap.txt (184 SYMBOL|Name lines).", text)
        with open(os.path.join("trader", "largecap.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        with open(os.path.join("trader", "largecap.txt"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 184)
        self.assertEqual(lines, ["%s|%s" % (s["symbol"], s["name"]) for s in doc["symbols"]])
        self.assertEqual(lines[0], "NVDA|NVIDIA Corporation Common Stock")
        self.assertTrue(all(re.match(r"^[A-Z]{1,5}(\.[A-Z])?\|.+$", ln) for ln in lines))
        self.assertEqual([doc[k] for k in ("created_by", "count", "min_cap_usd")], ["trader-largecap", 184, 10e9])
        # a second write with another path
        code, _, _ = self.build("--write", "--out", "x/l.json", "--txt", "x/l.txt")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists("x/l.json") and os.path.exists("x/l.txt"))

    def test_the_floor_from_the_flag_the_config_or_the_default(self):
        code, text, _ = self.build("--min-cap", "200e9", "--json")
        doc = json.loads(text)
        self.assertEqual([s["symbol"] for s in doc["symbols"]], expected(200e9))
        self.assertEqual(doc["counts"]["by_tier"], {"mega": 71})
        self.assertEqual(doc["floor_from"], "--min-cap")
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"largecap_min_cap_usd": 50e9}, fh)
        code, text, _ = self.build("--json")
        doc = json.loads(text)
        self.assertEqual(doc["min_cap_usd"], 50e9)
        self.assertEqual(doc["count"], 71 + 74)
        self.assertIn("config", doc["floor_from"])
        code, text, _ = self.build("--min-cap", "10e9", "--json")     # the flag wins over the config
        self.assertEqual(json.loads(text)["count"], 184)
        code, text, _ = self.build("--config", "nowhere.json", "--json")
        self.assertEqual(json.loads(text)["floor_from"], "the default")
        code, text, _ = self.build("--min-cap", "1e9", "--json")       # under $10B the tier is 'small'
        doc = json.loads(text)
        self.assertIn("small", doc["counts"]["by_tier"])
        self.assertEqual([s["symbol"] for s in doc["symbols"]], expected(1e9))

    def test_universe_none_takes_the_screener_alone(self):
        code, text, _ = self.build("--universe", "none", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(text)
        syms = [s["symbol"] for s in doc["symbols"]]
        self.assertEqual(syms, expected(use_universe=False))
        for sym in ("BRK.A", "GOOG", "GOOGL"):
            self.assertIn(sym, syms)
        self.assertEqual(doc["universe_rule"], "none (screener only)")
        self.assertNotIn("universe_symbols", doc)

    def test_the_api_document_shape_is_read_too(self):
        with open("api.json", "w") as fh:
            json.dump({"data": {"rows": read_rows(), "headers": {}}, "message": None, "status": {"rCode": 200}}, fh)
        code, text, _ = run(["build", "--offline", "api.json", "--universe", UNIVERSE, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["count"], 184)

    def test_refusals_are_one_plain_line(self):
        cases = [
            (["build", "--offline", SCREENER], "no trader/universe.txt: run scan.py universe first"),
            (["build", "--offline", "missing.json", "--universe", UNIVERSE], "no screener file"),
            (["build", "--offline", SCREENER, "--universe", UNIVERSE, "--min-cap", "0"], "above $0"),
            (["build", "--offline", SCREENER, "--universe", UNIVERSE, "--min-cap", "1e15"], "kept 0 of 245"),
            (["build", "--offline", SCREENER, "--universe", UNIVERSE, "--now", "yesterday"], "--now wants"),
        ]
        for argv, why in cases:
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(why, err, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertNotIn("Traceback", err)
        for empty in ([], {"data": {"rows": []}}, {"data": None}):
            with open("empty.json", "w") as fh:
                json.dump(empty, fh)
            code, text, err = run(["build", "--offline", "empty.json", "--universe", "none"])
            self.assertEqual(code, 1)
            self.assertIn("the screener has no rows", err)
        with open("bad.json", "w") as fh:
            fh.write("{not json")
        code, _, err = run(["build", "--offline", "bad.json", "--universe", "none"])
        self.assertEqual(code, 1)
        self.assertIn("could not read", err)
        with open(os.path.join("trader", "config.json") if os.path.isdir("trader") else "cfg.json", "w") as fh:
            json.dump({"largecap_min_cap_usd": "lots"}, fh)
        code, _, err = run(["build", "--offline", SCREENER, "--universe", UNIVERSE, "--config", "cfg.json"])
        self.assertEqual(code, 1)
        self.assertIn("not a dollar amount", err)


class Rows(unittest.TestCase):
    def test_boundary_slash_dedupe_and_no_cap(self):
        rows = [{"symbol": "AB/C", "name": "Ab Corp Class C", "marketCap": "10000000000.00", "sector": "Finance"},
                {"symbol": "LOW", "name": "Low Inc", "marketCap": "9999999999.99", "sector": "Finance"},
                {"symbol": "AB/C", "name": "Ab Corp Class C (again)", "marketCap": "10000000000.00"},
                {"symbol": "NOCAP", "name": "No Cap Inc", "marketCap": ""},
                {"symbol": "ZERO", "name": "Zero Inc", "marketCap": "0.00"},
                {"symbol": " big ", "name": "Big Inc", "marketCap": "$300,000,000,000"}]
        now = largecap.clock.parse_now(NOW)
        doc, counts = largecap.build(rows, 10e9, None, now)
        self.assertEqual([s["symbol"] for s in doc["symbols"]], ["BIG", "AB.C"])
        self.assertEqual([s["tier"] for s in doc["symbols"]], ["mega", "mid-large"])
        self.assertEqual(doc["count"], 2)
        self.assertEqual((counts["rows"], counts["at_or_above_floor"], counts["kept"]), (6, 3, 2))
        self.assertEqual(counts["dropped_below_floor"], 3)
        self.assertEqual(counts["dropped_not_in_universe"], 1)        # the duplicate
        self.assertEqual(doc["symbols"][1]["name"], "Ab Corp Class C")
        with self.assertRaises(largecap.LargecapError):
            largecap.build([], 10e9, None, now)
        with self.assertRaises(largecap.LargecapError):
            largecap.build(rows, 1e15, None, now)

    def test_money_words(self):
        self.assertEqual(largecap.money_short(5114261000000), "$5.11T")
        self.assertEqual(largecap.money_short(49024608422), "$49.0B")
        self.assertEqual(largecap.money_short(10e9), "$10B")
        self.assertEqual(largecap.money_short(9999999999.99), "$10B")
        self.assertEqual(largecap.money_short(850e6), "$850M")
        self.assertEqual(largecap.money_short(1234), "$1,234")
        self.assertEqual(largecap.money_short(None), "n/a")


class ShowAndCheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        code, _, err = run(["build", "--offline", SCREENER, "--universe", UNIVERSE, "--now", NOW, "--write"])
        self.assertEqual(code, 0, err)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_show_filters(self):
        code, text, _ = run(["show", "--tier", "mega", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual((doc["matched"], doc["shown"], doc["count"]), (71, 25, 184))
        self.assertTrue(all(s["tier"] == "mega" for s in doc["symbols"]))
        self.assertEqual([s["rank"] for s in doc["symbols"]][:3], [1, 2, 3])
        code, text, _ = run(["show", "--tier", "mid-large", "--top", "5", "--json"])
        doc = json.loads(text)
        self.assertEqual((doc["matched"], doc["shown"]), (39, 5))
        self.assertTrue(all(10e9 <= s["market_cap"] < 50e9 for s in doc["symbols"]))
        code, text, _ = run(["show", "--top", "0", "--json"])
        self.assertEqual(json.loads(text)["shown"], 184)
        code, text, _ = run(["show", "--sector", "finance", "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["matched"], 31)
        self.assertTrue(all(s["sector"] == "Finance" for s in doc["symbols"]))
        code, text, _ = run(["show", "--sector", "Finance", "--tier", "large", "--json"])
        doc = json.loads(text)
        self.assertTrue(all(s["sector"] == "Finance" and s["tier"] == "large" for s in doc["symbols"]))
        self.assertGreater(doc["matched"], 0)
        code, text, _ = run(["show", "--tier", "mega", "--top", "3"])
        self.assertEqual(code, 0)
        self.assertIn("71 of 184 match tier mega, showing 3", text)
        self.assertIn("rank  symbol  name", text)
        self.assertIn("NVDA", text)
        self.assertIn("$5.11T", text)
        self.assertIn("Most people who day trade lose money", text)
        code, text, _ = run(["show", "--sector", "Basket Weaving"])
        self.assertEqual(code, 1)
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("No symbol in trader/largecap.json matches sector Basket Weaving", text)

    def test_check_a_present_and_an_absent_symbol(self):
        code, text, _ = run(["check", "NVDA"])
        self.assertEqual(code, 0)
        for must in ("NVDA is in the large-cap list: #1 of 184", "$5.11T", "tier mega", "Sector Technology",
                     "industry Semiconductors", "country United States"):
            self.assertIn(must, text, must)
        code, text, _ = run(["check", "brk/b"])                      # any spelling, the dot form in the answer
        self.assertEqual(code, 0)
        self.assertIn("BRK.B is in the large-cap list", text)
        self.assertNotIn("Inc..", text)
        code, text, _ = run(["check", "tsla", "--json"])
        doc = json.loads(text)
        self.assertTrue(doc["in_list"])
        self.assertEqual((doc["symbol"], doc["tier"], doc["sector"]), ("TSLA", "mega", "Industrials"))
        self.assertIn("industry", doc)
        code, text, err = run(["check", "GOOG"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("GOOG is not in trader/largecap.json (184 symbols at or above $10B", text)
        code, text, _ = run(["check", "GOOG", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(text)["in_list"])

    def test_show_and_check_without_a_list(self):
        os.remove(os.path.join("trader", "largecap.json"))
        for argv in (["show"], ["check", "NVDA"]):
            code, text, err = run(argv)
            self.assertEqual(code, 1)
            self.assertIn("no trader/largecap.json yet: run largecap.py build --write first", err)
        with open(os.path.join("trader", "largecap.json"), "w") as fh:
            json.dump({"hello": 1}, fh)
        code, _, err = run(["show"])
        self.assertEqual(code, 1)
        self.assertIn("not a large-cap list", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-largecap", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            upstream = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-largecap\n"))
        self.assertGreater(len(text.splitlines()), 60)
        fm = text.split("---\n")[1]
        self.assertGreater(len(fm[fm.index("description:"):fm.index("argument-hint:")]), 200)
        self.assertEqual(text[text.index("## Safety"):], upstream[upstream.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("${claude_skill_dir}", "what it reads and writes", "## steps", "## rules", "what it never does",
                     "trader/universe.txt", "trader/largecap.json", "trader/largecap.txt", "mega", "mid-large",
                     "245 rows", "184", "$5.11t", "most people who day trade lose money", "scan.py universe",
                     "not financial advice"):
            self.assertIn(must, low, must)
        for never in ("\u2014", "real " + "time", "guarantee" + "d", "trader-" + "screener"):
            self.assertNotIn(never, low)

    def test_python_3_8_and_no_em_dashes_in_the_tool(self):
        folder = os.path.join(ROOT, "trader-largecap")
        for f in os.listdir(folder):
            if not f.endswith((".py", ".md")):
                continue
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("\u2014", src, f)
            if f.endswith(".py"):
                for bad in ("import " + "zoneinfo", "from " + "zoneinfo", ".remove" + "prefix(", ".remove" + "suffix("):
                    self.assertNotIn(bad, src, f)
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", f)
                for bad in ("list" + "[", "dict" + "[", "tuple" + "["):
                    self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), f)


if __name__ == "__main__":
    unittest.main()
