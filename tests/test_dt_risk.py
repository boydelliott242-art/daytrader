"""dt/risk.py: the block, the verdicts, the unknowns said out loud."""

import sys
import unittest

from _dt_fixtures import daily_rows, ROOT

sys.path.insert(0, ROOT)
from dt import risk  # noqa: E402

F_OK = {"adv_usd_20": 5e8, "atr_pct": 1.5, "roc_5": 2.0, "relvol_20": 1.1, "mtf_agreement": "agree_up", "beta_spy_63": 1.1,
        "realized_vol_20": 20.0}
SIG_L = {"entry": 100.0, "stop": 99.0, "target": 102.0, "level": 99.5, "stop_rule": "or_mid"}
SIG_S = {"entry": 100.0, "stop": 101.0, "target": 98.0, "level": 100.5, "stop_rule": "or_mid", "side": "short"}
NO_CAT = {"known": [], "possible": [], "none": True, "next_earnings": None}


def cat_earnings(sessions_ahead):
    return {"known": [{"kind": "earnings", "date": "2026-10-27", "text": "reports"}], "possible": [], "none": False,
            "next_earnings": {"date": "2026-10-27", "sessions_ahead": sessions_ahead}}


class Verdicts(unittest.TestCase):
    def test_a_plain_long_is_ok_and_its_unknowns_are_named(self):
        b = risk.risk_block("T", "long", "2026-10-26", F_OK, SIG_L, NO_CAT, "weak_bull")
        self.assertEqual((b["verdict"], b["liquidity"], b["regime_fit"]), ("ok", "deep", "for"))
        self.assertEqual(b["invalidation"]["stop_distance_pct"], 1.0)
        self.assertIn("short_interest", b["unknown"])
        self.assertTrue(any(r.startswith("unknown:") for r in b["reasons"]))
        self.assertIsNone(b["gap_risk_pct_95"])
        self.assertGreater(risk.score(b), 60)

    def test_a_short_without_short_interest_is_a_caution_and_says_the_stake_is_not_the_limit(self):
        b = risk.risk_block("T", "short", "2026-10-26", F_OK, SIG_S, NO_CAT, "range")
        self.assertEqual((b["verdict"], b["squeeze_risk"]), ("caution", "unknown"))
        self.assertTrue(any("squeeze" in r for r in b["reasons"]))
        self.assertTrue(any("lose more than the stake" in r for r in b["reasons"]))
        self.assertLess(risk.score(b), risk.score(risk.risk_block("T", "long", "2026-10-26", F_OK, SIG_L, NO_CAT, "range")))

    def test_do_not_short_a_parabolic_name_or_a_crowded_one_or_into_a_strong_bull(self):
        para = dict(F_OK, roc_5=25.0, relvol_20=4.0)
        b = risk.risk_block("T", "short", "2026-10-26", para, SIG_S, NO_CAT, "range")
        self.assertEqual(b["verdict"], "do_not_trade")
        self.assertTrue(b["parabolic"])
        si = [{"kind": "short_interest", "source": "FMP", "value": {"short_pct_float": 28.0, "days_to_cover": 6.0}}]
        b = risk.risk_block("T", "short", "2026-10-26", F_OK, SIG_S, NO_CAT, "range", si)
        self.assertEqual((b["verdict"], b["squeeze_risk"], b["short_interest_pct"]), ("do_not_trade", "high", 28.0))
        self.assertNotIn("short_interest", b["unknown"])
        b = risk.risk_block("T", "short", "2026-10-26", F_OK, SIG_S, NO_CAT, "strong_bull")
        self.assertEqual(b["verdict"], "do_not_trade")
        b = risk.risk_block("T", "short", "2026-10-26", F_OK, SIG_S, NO_CAT, "strong_bull", regime_slice={"stats": {"exp_R": 0.1, "n": 60}})
        self.assertNotEqual(b["verdict"], "do_not_trade")               # a positive record in that regime lifts the bar
        low = [{"kind": "short_interest", "source": "FMP", "value": {"short_pct_float": 2.0, "days_to_cover": 1.0}}]
        b = risk.risk_block("T", "short", "2026-10-26", F_OK, SIG_S, NO_CAT, "range", low)
        self.assertEqual((b["verdict"], b["squeeze_risk"]), ("ok", "low"))

    def test_events_thin_names_and_crisis(self):
        b = risk.risk_block("T", "long", "2026-10-26", F_OK, SIG_L, cat_earnings(1), "weak_bull")
        self.assertEqual(b["verdict"], "do_not_trade")
        self.assertTrue(b["asymmetric_event"])
        b = risk.risk_block("T", "long", "2026-10-26", F_OK, SIG_L, cat_earnings(4), "weak_bull")
        self.assertEqual(b["verdict"], "caution")
        self.assertTrue(b["event_risk"]["earnings_within_5"])
        b = risk.risk_block("T", "long", "2026-10-26", dict(F_OK, adv_usd_20=2e6), SIG_L, NO_CAT, "weak_bull")
        self.assertEqual((b["verdict"], b["liquidity"]), ("do_not_trade", "thin"))
        b = risk.risk_block("T", "long", "2026-10-26", F_OK, SIG_L, NO_CAT, "crisis")
        self.assertEqual((b["verdict"], b["regime_fit"]), ("do_not_trade", "against"))
        b = risk.risk_block("T", "long", "2026-10-26", dict(F_OK, mtf_agreement="short_term_up_long_term_down"), SIG_L, NO_CAT, "range")
        self.assertEqual(b["verdict"], "caution")

    def test_gap_risk_from_the_rows_and_the_stop_that_cannot_hold(self):
        rows = daily_rows(100.0, 300, 0.0, 1.0)
        for i in range(1, len(rows)):
            rows[i][1] = rows[i - 1][4] * (1.03 if i % 10 == 0 else 1.001)      # a 3 % gap every tenth day
        g = risk.gap_risk(rows, "2027-12-31")
        self.assertAlmostEqual(g, 3.0, 1)
        self.assertIsNone(risk.gap_risk(rows[:10], "2027-12-31"))
        b = risk.risk_block("T", "long", "2027-12-31", F_OK, SIG_L, NO_CAT, "range", None, rows)
        self.assertEqual(b["verdict"], "caution")
        self.assertTrue(any("overnight gap" in r for r in b["reasons"]))
        self.assertEqual(b["gap_risk_pct_95"], g)

    def test_the_score_ladder(self):
        self.assertIsNone(risk.score(None))
        ok = risk.risk_block("T", "long", "2026-10-26", F_OK, SIG_L, NO_CAT, "weak_bull")
        bad = risk.risk_block("T", "long", "2026-10-26", dict(F_OK, adv_usd_20=1e6, atr_pct=6.0), SIG_L, cat_earnings(1), "crisis")
        self.assertGreater(risk.score(ok), risk.score(bad))
        self.assertGreaterEqual(risk.score(bad), 0)


if __name__ == "__main__":
    unittest.main()
