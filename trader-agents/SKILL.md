---
name: trader-agents
description: >-
  The research desk. The TradingAgents roles (Tauric Research, Apache-2.0, vendored whole under vendor/TradingAgents),
  adapted and run by Claude inside Claude Code with no key: six analysts (market, momentum, fundamentals, news,
  sentiment, regime), a bull and a bear researcher in rounds, a contrarian, the research manager, the trader, three
  risk debaters, a code-fed risk analyst and the portfolio manager, all over one evidence pack per symbol and date
  (every item with an id), validated claim by claim against that pack, scored, and written as a card with a NO TRADE
  that counts. Also the code-only rankings over the universe (top momentum longs, short candidates, high conviction,
  risk-adjusted, contrarian, most dangerous shorts, watchlist), the memory of settled cards with reflections, the
  weights test on the lab's own trades, and the vendored engine when a Python 3.11 venv and a provider key exist.
  Use when the user says "research NVDA", "run the agents on TSLA", "bull and bear case", "what does the desk think",
  "rank the universe", "top shorts", "most dangerous shorts", "decision card", "settle the cards", "reflect", or
  "run tradingagents".
argument-hint: "[prepare SYM --date D --at 10:15 --depth fast|standard|deep|maximum | next | validate | decide --write | rank | settle | reflect ID]"
---

# trader-agents

The desk's roles are adapted from TradingAgents v0.7.0 (`vendor/TradingAgents`, verbatim, Apache-2.0; the roles
in `roles/*.md` say so at the top). You are the model: `agents.py` writes each role's prompt from the evidence pack and
the earlier roles' outputs, you write the role's JSON, the validator checks every claim against the pack, and when the
plan is complete the card is computed by code. Nothing here calls a model or the network, and nothing places, sizes or
approves an order. `${CLAUDE_SKILL_DIR}` is this skill's folder; run from the project folder (the one with `trader/`).

```bash
python3 "${CLAUDE_SKILL_DIR}/agents.py" prepare NVDA --date 2026-10-27 --at 10:15 --depth standard   # the pack and the first prompts
python3 "${CLAUDE_SKILL_DIR}/agents.py" next NVDA --date 2026-10-27        # after writing outputs: validate, prompt the next roles
python3 "${CLAUDE_SKILL_DIR}/agents.py" decide NVDA --date 2026-10-27 --write --publish   # the card, logged to memory
python3 "${CLAUDE_SKILL_DIR}/agents.py" rank --at 10:15 --top 10 --write --publish        # the universe, code only
python3 "${CLAUDE_SKILL_DIR}/agents.py" settle                               # pending cards -> settled, from the real bars
python3 "${CLAUDE_SKILL_DIR}/agents.py" reflect "2026-10-27|NVDA|long"       # the reflection prompt; --apply stores it
python3 "${CLAUDE_SKILL_DIR}/agents.py" weights-test                         # the momentum score by quintile on the lab's OOS trades
python3 "${CLAUDE_SKILL_DIR}/agents.py" doctor                               # python 3.11+, the venv, keys by name
python3 "${CLAUDE_SKILL_DIR}/agents.py" run NVDA --date 2026-10-27 --engine vendored   # the upstream graph itself, when it can run
```

## The loop (you, step by step)

1. `prepare SYM --date D --at HH:MM --depth X`. It builds `trader/research/D/SYM/evidence.json` from the stores and
   the working files (bars, features, the regime class, the fired rules with the lab's record, the scorecard, the
   intel ledger, the earnings calendar, the metrics file, the futures, the Monte Carlo, the lessons) and writes the
   analysts' prompts under `prompts/`. Read what is missing: it is printed, and it is not yours to fill.
2. For each prompt listed as ready: read it in full, then write the JSON it asks for to the output path. One claim
   per sentence; every DATA, SIGNAL or STAT claim cites ids; quote numbers as the items write them; name what is
   missing; confidence high only when every source is there.
3. `next SYM --date D`. Rejected outputs are listed with the reason, sentence by sentence: rewrite the role's
   answer (never patch it with a number from memory) and run `next` again. New prompts appear as their needs are met:
   bull after the analysts, bear after the bull, the manager after the debate, the trader after the manager, the risk
   debaters after the trader, the portfolio manager last.
4. `decide SYM --date D --write`. The scores (code only, from the weights file), the confidence, the best setup, the
   trader's levels, the thesis and the invalidation, the risk verdicts, the evidence cited: `card.json` and
   `card.md`, logged to `trader/research/memory.jsonl` as pending. A risk verdict of do_not_trade on the chosen side
   turns the card into NO TRADE whatever the rating said, and the card says why. `--publish` refreshes
   `trader/publish/research.json` for the site.
5. Later: `settle` (an intraday card settles from the scorecard's grade of its setup; a swing card at the first close
   after its date over five sessions, alpha against SPY), then `reflect ID`, write `outputs/reflect.json`, and
   `reflect ID --apply`. The next `prepare` on that symbol carries the lesson as a LESSON item.

Depth: `fast` (market, momentum, regime, one debate round, the risk analyst, the manager and the portfolio manager:
8 outputs), `standard` (every analyst, one round each: 15), `deep` (two rounds, the contrarian: 21), `maximum`
(three rounds, the contrarian, and first `trader-intel` refreshed, `montecarlo.py plan` on the proposal: 26).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/research/D/SYM/evidence.json` | the pack: identity, snapshot, features, regime, the fired rules per side with the lab's record, the record, catalysts, risk per side, fundamentals, news, intel, social, macro, futures, Monte Carlo, lessons, items with ids, missing |
| `prompts/<step>.md`, `outputs/<step>.json` | one per role and round (bull_1, bear_1, risk_aggressive_2 ...) |
| `card.json`, `card.md` | the decision card: TICKER, SNAPSHOT, REGIME, MOMENTUM, TECHNICAL, FUNDAMENTAL, NEWS / SENTIMENT, CATALYSTS, BULL, BEAR, CONTRARIAN, RISK, LONG SCORE, SHORT SCORE, CONFIDENCE, BEST SETUP or NO TRADE, THESIS, INVALIDATION, KEY RISKS, EVIDENCE |
| `trader/research/memory.jsonl` | every card the minute it is written (pending), then settled with the outcome and the reflection |
| `trader/rankings.json`, `trader/research/latest.json` | the rankings and the latest card per symbol; `--publish` copies both to `trader/publish/` for the site |
| `trader/lab/weights_test.json` | the momentum score by quintile over the lab's OOS trades |
| `weights.json` | every weight with its reason; the long and short vectors are separate on purpose |

## Rules

- The validator is the editor. It rejects a claim that quotes a number no cited item carries, a DATA claim with no
  id, an id not in the pack, high confidence with a source missing, a trader level not within 2 percent of a cited
  SIGNAL, a researcher in round 2 with no rebuttal, a portfolio manager whose rating points the other way from the
  trader without saying why. A rejected output is rewritten by the role.
- The pack is the world. No fundamental, headline, short-interest figure, borrow rate or event from memory. What is
  missing is written as missing. The card prints the missing list.
- Separate sides. A short is never the negation of a long: its own setups, its own lab record, its own risk block
  (squeeze, borrow, the unbounded loss), its own score vector. "Do not short this even though the bearish score is
  high" is a valid and expected card.
- NO TRADE is a result. A hold, a REVIEW (no readable rating), a do_not_trade verdict on the chosen side, or nothing
  that cleared the bar all end in NO TRADE, printed with the reason.
- Rankings are code only. The roles run on the names a person picks from them, at the depth they choose.
- The vendored engine is the upstream's own graph; its reports enter the pack as INTERPRETATION items (a model's
  words, never DATA). Without a Python 3.11 venv and a provider key it prints the blocker and runs nothing.
- Every number comes from a file; intervals travel with rates; "approved" means the gate was cleared, nothing more.
  Most people who day trade lose money; a short can lose more than the stake.

## What it never does

It never places, sizes, checks or approves an order, never calls a broker, never calls a model or the network,
never fills a gap in the evidence from memory, and never prints a projected return.

## Safety

- Education and tooling, not financial advice. A pass is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk passed that exact
  order today, the user typed an explicit yes for that one order in the chat, and the user clicked Allow once on
  Claude's own card because the order tool is set to "Needs approval" (Customize > Connectors > Tool permissions).
  Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
