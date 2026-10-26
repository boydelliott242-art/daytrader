"""dt/catalysts.py: known, possible, none; nothing invented."""

import sys
import unittest

from _dt_fixtures import daily_rows, make_session, ROOT

sys.path.insert(0, ROOT)
from dt import catalysts  # noqa: E402

LEDGER = {
    "created_by": "trader-intel", "as_of": "2026-10-25T20:00:00Z",
    "items": [
        {"symbol": "VZ", "kind": "earnings", "source": "FMP", "url": "https://fmp.example/", "time": "2026-10-25T18:00:00Z",
         "headline": "VZ reports 2026-10-27 (pre)", "tags": ["earnings"], "impact": "?", "date": "2026-10-27", "when": "pre", "id": "e1"},
        {"symbol": "VZ", "kind": "analyst", "source": "FMP", "url": "https://fmp.example/a", "time": "2026-10-24T13:00:00Z",
         "headline": "Downgrade to hold at Example Bank", "tags": ["analyst"], "impact": "-", "id": "a1"},
        {"symbol": "VZ", "kind": "social", "source": "LunarCrush", "url": "https://lc.example/", "time": "2026-10-25T10:00:00Z",
         "headline": "mentions up 3x", "tags": ["spike"], "impact": "?", "id": "s1"},
        {"symbol": "VZ", "kind": "note", "source": "other", "time": "2026-10-01T10:00:00Z", "headline": "spectrum auction talk", "tags": [], "id": "n1"},
        {"symbol": "AAPL", "kind": "news", "source": "Yahoo", "url": "https://y.example/", "time": "2026-10-23T12:00:00Z",
         "headline": "Apple closes a deal", "tags": ["deal"], "impact": "+", "id": "d1"},
        {"kind": "macro", "source": "Bigdata.com", "url": "https://bd.example/", "time": "2026-10-20T00:00:00Z", "date": "2026-10-26",
         "clock": "08:30", "event": "GDP", "impact_level": "HIGH", "headline": "GDP 08:30 ET", "tags": ["macro"], "id": "m1"},
    ],
}


class Known(unittest.TestCase):
    def test_earnings_analyst_macro_and_the_possible_ones(self):
        c = catalysts.catalysts_for("VZ", "2026-10-26", LEDGER)
        self.assertFalse(c["none"])
        kinds = [e["kind"] for e in c["known"]]
        self.assertEqual(sorted(kinds), ["analyst", "earnings", "macro"])
        self.assertEqual(c["next_earnings"]["date"], "2026-10-27")
        self.assertEqual(c["next_earnings"]["sessions_ahead"], 1)
        self.assertEqual([e["direction"] for e in c["known"] if e["kind"] == "analyst"], ["down"])
        self.assertEqual(sorted(e["kind"] for e in c["possible"]), ["note", "social"])
        self.assertEqual(c["sources"], ["Bigdata.com", "FMP", "LunarCrush", "other"])
        self.assertTrue(all(e["source"] for e in c["known"]))
        self.assertIn("known:", catalysts.words(c))

    def test_the_window_and_the_date_matter(self):
        c = catalysts.catalysts_for("VZ", "2026-10-30", LEDGER)                   # the analyst call is a week old
        self.assertNotIn("analyst", [e["kind"] for e in c["known"]])
        self.assertIsNone(c["next_earnings"])                                      # earnings are past
        c = catalysts.catalysts_for("AAPL", "2026-10-26", LEDGER)
        self.assertEqual([e["kind"] for e in c["known"]], ["news", "macro"])        # the deal, dated; the market-wide print

    def test_bars_make_a_known_event_and_nothing_else_is_invented(self):
        rows = daily_rows(100.0, 30, 0.0, 1.0)
        rows[-1][5] = 4000000                                                      # 4x the 20-day average
        c = catalysts.catalysts_for("T", "2026-12-31", {}, rows)
        self.assertEqual([e["kind"] for e in c["known"]], ["volume"])
        self.assertEqual(c["sources"], ["bars"])
        sess = make_session(rows=[(106.0, 106.5, 105.5, 106.0, 1000)])
        c = catalysts.catalysts_for("T", "2026-12-31", None, daily_rows(100.0, 30, 0.0, 1.0), sess)
        self.assertEqual([(e["kind"], e["direction"]) for e in c["known"]], [("gap", "up")])
        empty = catalysts.catalysts_for("T", "2026-12-31", None, None, None)
        self.assertTrue(empty["none"])
        self.assertEqual(empty["known"] + empty["possible"], [])
        self.assertIn("no identified catalyst", catalysts.words(empty))


if __name__ == "__main__":
    unittest.main()
