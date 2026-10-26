#!/usr/bin/env python3
"""Run the vendored TradingAgents graph under its own venv and write its reports for the desk's roles to cite.

    <vendor/TradingAgents/.venv/bin/python> trader-agents/vendored_driver.py SYM DATE OUT.json [--analysts market,news,...]
        [--debate-rounds 1] [--risk-rounds 1] [--provider openai] [--quick MODEL] [--deep MODEL]

Needs Python 3.11+ (the venv scripts/install_tradingagents.sh builds) and a provider key in the environment or in
vendor/TradingAgents/.env; never a key on the command line. Writes the four analyst reports, the debate histories, the
research plan, the trader's proposal, the risk histories, the decision and the rating as one JSON, plus the upstream
report tree under trader/research/<date>/<SYM>/vendored/. Exit 2 with a plain reason when it cannot run; nothing is faked.
"""

import argparse
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("date")
    ap.add_argument("out")
    ap.add_argument("--analysts", default="market,social,news,fundamentals")
    ap.add_argument("--debate-rounds", type=int, default=1)
    ap.add_argument("--risk-rounds", type=int, default=1)
    ap.add_argument("--provider")
    ap.add_argument("--quick")
    ap.add_argument("--deep")
    ap.add_argument("--reports-dir")
    a = ap.parse_args()
    if sys.version_info < (3, 11):
        print("vendored TradingAgents needs Python 3.11+; this is %s. Run scripts/install_tradingagents.sh." % sys.version.split()[0], file=sys.stderr)
        return 2
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError as e:
        print("tradingagents is not importable here (%s): run scripts/install_tradingagents.sh and use its venv's python." % e, file=sys.stderr)
        return 2
    config = DEFAULT_CONFIG.copy()
    if a.provider:
        config["llm_provider"] = a.provider
    if a.quick:
        config["quick_think_llm"] = a.quick
    if a.deep:
        config["deep_think_llm"] = a.deep
    config["max_debate_rounds"] = a.debate_rounds
    config["max_risk_discuss_rounds"] = a.risk_rounds
    try:
        graph = TradingAgentsGraph(selected_analysts=[x.strip() for x in a.analysts.split(",") if x.strip()], config=config)
        state, rating = graph.propagate(a.symbol, a.date)
    except Exception as e:  # a missing key, a vendor outage: say it plainly
        print("TradingAgents could not run: %s: %s" % (type(e).__name__, str(e)[:400]), file=sys.stderr)
        return 2
    inv = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}
    out = {"created_by": "trader-agents vendored_driver", "engine": "tradingagents %s" % getattr(__import__("tradingagents"), "__version__", "?"),
           "symbol": a.symbol, "date": a.date, "rating": rating, "settings": graph.run_settings(),
           "reports": {"market": state.get("market_report"), "sentiment": state.get("sentiment_report"), "news": state.get("news_report"),
                       "fundamentals": state.get("fundamentals_report")},
           "debate": {"bull": inv.get("bull_history"), "bear": inv.get("bear_history"), "history": inv.get("history")},
           "research_plan": state.get("investment_plan"), "trader_proposal": state.get("trader_investment_plan"),
           "risk": {"aggressive": risk.get("aggressive_history"), "conservative": risk.get("conservative_history"), "neutral": risk.get("neutral_history")},
           "decision": state.get("final_trade_decision"), "memory_note": state.get("memory_note")}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    if a.reports_dir:
        try:
            graph.save_reports(state, a.symbol, a.reports_dir)
        except Exception as e:
            print("report tree not written: %s" % e, file=sys.stderr)
    print("wrote %s (rating %s)" % (a.out, rating))
    return 0


if __name__ == "__main__":
    sys.exit(main())
