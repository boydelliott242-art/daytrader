# TradingAgents reconnaissance (vendor/TradingAgents at v0.7.0, e8f5503, cloned 2026-10-25)

Working notes for the integration. Kept short on purpose: what the upstream does, what we take, what we leave.

## What it is

Apache-2.0. Python >= 3.11, LangGraph + LangChain, 183 Python files, 12 MB with assets, ~100 test files (pytest).
`TradingAgentsGraph(config).propagate(ticker, date)` -> (final_state, rating). CLI `tradingagents --ticker --date`.
Needs an LLM provider key (OpenAI default, or Anthropic/Google/xAI/DeepSeek/Qwen/GLM/MiniMax/OpenRouter/Mistral/
Kimi/Groq/NVIDIA/Bedrock/Ollama/any OpenAI-compatible) or a ChatGPT Plus/Pro sign-in. THIS MAC: no python 3.11+,
no uv, no docker, no API keys in the environment. So the vendored graph cannot run here as-is; the port runs.

## The graph (graph/setup.py)

START -> [Market Analyst | Sentiment Analyst | News Analyst | Fundamentals Analyst | Memory Log] in parallel
-> Bull Researcher <-> Bear Researcher (2 * max_debate_rounds turns, default 1 round)
-> Research Manager (deep model, structured ResearchPlan: recommendation Buy/Overweight/Hold/Underweight/Sell,
   rationale, strategic_actions)
-> Trader (structured TraderProposal: action Buy/Hold/Sell, reasoning, entry_price, stop_loss, position_sizing;
   grounded in the market report's price structure)
-> Aggressive -> Conservative -> Neutral risk debaters (3 * max_risk_discuss_rounds turns)
-> Portfolio Manager (deep model, structured PortfolioDecision: rating 5-tier, executive_summary,
   investment_thesis, price_target, time_horizon) -> END
Each analyst is its own subgraph with tools and max_tool_rounds (20), then a WRAP_UP turn. State is
AgentState (messages, company_of_interest, trade_date, the four reports, investment_debate_state,
investment_plan, trader_investment_plan, risk_debate_state, final_trade_decision, final_rating,
past_context, memory_note, portfolio_context, instrument_context, asset_type).

## The agents and their prompts (agents/)

- Market Analyst: pick up to 8 complementary indicators from a fixed list (close_50_sma, close_200_sma,
  close_10_ema, macd, macds, macdh, rsi, boll, boll_ub, boll_lb, atr, vwma); call get_stock_data first, then
  get_indicators, then get_verified_market_snapshot (the source of truth for exact price claims); no claims of
  historical validation without tool-backed dates and prices; markdown table at the end.
- Fundamentals Analyst: get_fundamentals, balance sheet, cash flow, income statement, insider transactions
  (SEC EDGAR as filed, point in time; Yahoo otherwise).
- News Analyst: get_news (ticker), get_global_news (macro queries), get_macro_indicators (FRED aliases: cpi,
  core_pce, unemployment, fed_funds_rate, 10y_treasury, yield_curve, real_gdp, vix ...), get_prediction_markets
  (Polymarket).
- Sentiment Analyst: pre-fetched Yahoo headlines + StockTwits (bullish/bearish tags) + Reddit; structured
  SentimentReport (band 6 tiers, score 0-10, confidence low/medium/high, narrative); explicit data-limit rules;
  "past sentiment is not predictive". Optional Jev screening of posts (TypeSafe key).
- Bull / Bear Researchers: conversational debate; "opponent has not spoken yet" marker instead of an empty
  argument (stops fabricated rebuttals, #1176); report_or_absent markers for missing analyst reports.
- Research Manager: judge the debate; conflict alone is not a reason to Hold; Hold only when balanced or thin.
- Trader: absolute price levels only, never percentages (#1288); NO_EXTERNAL_TOOLS.
- Risk debaters: aggressive / conservative / neutral, each rebutting the other two.
- Portfolio Manager: reads the lessons (memory), gives the rating; rating parsed with a strict regex when
  structured output is unavailable; REVIEW sentinel when no rating can be read (never a silent Hold, #1170).
- rating.py: 5-tier scale, extract_rating/parse_rating/run_rating/is_review.
- context.py: deterministic instrument identity (Yahoo profile) injected into every prompt (#814), language
  instruction, portfolio context ("not provided" is not a flat book).

## Memory, settlement, backtest (memory/, backtest.py)

- Memory log: append-only markdown, one entry per (ticker, date) with the decision and rating, settled later
  with the realised return and alpha vs the regional benchmark (SPY for US) over holding_period_days (5),
  entered at the FIRST CLOSE AFTER the decision date (a run reads news after the close); a one-paragraph
  reflection per settled decision; the Portfolio Manager reads same-ticker decisions + cross-ticker lessons,
  filtered point in time for historical runs (#1251). Idempotent per ticker+date.
- Backtest: run_backtest(tickers, dates grid, config) -> cells scored on realised alpha grouped by rating;
  resumable by run_id; its own memory log.

## Data layer (dataflows/)

- router.route_to_vendor with per-category vendor chains (core_stock_apis yfinance|alpha_vantage,
  technical_indicators, fundamental_data sec_edgar,yfinance, news_data, macro_data fred, prediction_markets
  polymarket); vendor_unavailable / no_data_available markers.
- date_window.as_of / as_of_window: every dated tool is clamped to the trade date (no look-ahead).
- yahoo/snapshot.build_verified_market_snapshot: latest row on or before the date + indicators + recent closes.
- sec_edgar: company facts as filed (point in time, restatements read as first reported).
- reddit, stocktwits (recent only, not point in time), polymarket, fred, alpha_vantage.

## Reporting

- reporting.write_report_tree + report_html (one HTML page); full_states_log_<date>.json per run with
  run_settings (provider, models, analysts, rounds, vendors; never keys or endpoints).

## What this desk already has that maps onto it

| TradingAgents | DAYTRADER |
| --- | --- |
| Market Analyst (daily indicators) | dt/setups (12 intraday rules), dt/regime, trader-regime, the lab's OOS stats |
| Fundamentals Analyst (EDGAR) | trader-analyst (EDGAR XBRL, 8-K) |
| News Analyst | trader-news (Yahoo headlines, 8-K), trader-intel (Firecrawl, FMP, Bigdata, Quartr) |
| Sentiment Analyst | trader-intel social items (LunarCrush); no StockTwits/Reddit |
| Bull/Bear debate, managers, trader, risk debate | nothing yet: this is the port |
| Memory log + settlement | trader-scorecard (every shown call graded on the real bars) |
| Backtest grid | trader-lab (31 configs, IS/OOS, walk-forward), trader-nq, trader-montecarlo |
| Verified snapshot (source of truth) | the evidence pack (new): one JSON of everything known, with ids |
| REVIEW sentinel, report_or_absent, opponent-not-spoken | the validators (new) |

## What we take, adapt, leave

Take verbatim (vendored, attributed): the whole repository under vendor/TradingAgents, runnable in its own
venv when a Python 3.11+ and a provider key exist (scripts/install_tradingagents.sh; trader-agents --engine
vendored). Adapt into stdlib + Claude-as-the-agents: the ten roles, the debate order, the structured schemas,
the rating scale, the evidence-first rules, the memory/settlement idea (our scorecard already grades on bars;
we add the reflection), the point-in-time clamp (the evidence pack is built as of a date). Leave: LangGraph,
pydantic, yfinance (we have our own bar store), the ChatGPT auth, i18n, Docker, the CLI's questionary prompts.

## Status after the build (2026-10-26 00:30 ET)

Done: vendor/TradingAgents (verbatim, venv on ~/.local/bin/python3.13, 100 upstream tests green); the contract
(CONTRACTS.md "The research desk"); dt/shorts.py + mirrored sim + both sides in the lab, the desk, the scorecard;
dt/features.py, dt/regimes.py (tags on every lab session), dt/catalysts.py, dt/risk.py, dt/evidence.py, dt/scoring.py,
dt/featurestudy.py, dt/agents.py; trader-agents (agents.py, roles/, weights.json, vendored_driver.py, SKILL.md);
lab.py features / robustness; rankings; cards; memory; the site panels; the sealed bundle carries 11 data files;
run.sh ranks after each live board and runs the study after the lab. 650 tests.

Honest numbers: no short config approved (two gap fades at watch); the momentum score runs the wrong way across
the three watch configs' OOS trades (weights-test), so the weights are a choice, said on every card.

Open: a real research run by Claude at each depth as it is used (the loop is exercised by scripted outputs in the
tests and one hand-made AAPL run); the vendored engine needs a provider key to produce anything; short interest,
borrow and crowding have no source yet (every card says unknown); StockTwits and Reddit are not in the ledger.
