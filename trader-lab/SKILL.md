---
name: trader-lab
description: >-
  The lab. The edge study behind the desk: every setup in the library (opening-range
  breakouts, gap and go, VWAP reclaims and pullbacks, high-of-day and afternoon breakouts,
  inside bars, a VWAP mean reversion), 31 configurations in all, played over every large cap
  and every cached session of 5-minute bars, with a realistic next-open fill and 4 basis
  points of costs. Writes the trade list and the edge table: win rate and expectancy with
  confidence intervals, in sample, out of sample, by regime, hour, tier, sector and weekday,
  plus a rolling walk-forward. Use when the user says "run the lab", "what setups work",
  "backtest the setups", "edge table", "walk-forward", "which setup has an edge", "is this
  setup any good", "why did this fire", or before /trader-desk shows a board.
argument-hint: "[run | report [--config ID] [--by regime] | why TICKER DATE]"
---

# trader-lab

The lab measures. It takes the setup library in `dt/setups.py` (the same rules the desk
runs live), plays every configuration over every stored session of every large cap, charges
a realistic fill and costs, and writes down what happened. It never says a setup will work.
It says how often it did, with a confidence interval, and how often the same answer would
appear by luck when 31 configurations are tested at once. A setup "matched a rule"; the lab
tells you the record of that rule. Most people who day trade lose money, and a good-looking
table does not change that: it only tells you which rules have a record worth knowing before
the money.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/lab.py" run                                   # the whole store -> trader/lab/
python3 "${CLAUDE_SKILL_DIR}/lab.py" run --symbols TSLA,NVDA,AAPL           # a few names
python3 "${CLAUDE_SKILL_DIR}/lab.py" run --fill close --cost-bps 0          # the idealised plan fill, no costs
python3 "${CLAUDE_SKILL_DIR}/lab.py" run --publish                          # also docs/data/edge.json
python3 "${CLAUDE_SKILL_DIR}/lab.py" report                                 # the tables
python3 "${CLAUDE_SKILL_DIR}/lab.py" report --config "orb15|r=1" --by regime
python3 "${CLAUDE_SKILL_DIR}/lab.py" why TSLA 2026-10-19                    # every config's signal and outcome on one chart
```

Flags: `--cache-root DIR` (the bar store, default `trader/cache`), `--symbols-file`
(default `trader/largecap.txt`), `--largecap trader/largecap.json` (tiers and sectors),
`--sessions N` (the last N a symbol), `--fill next_open|close`, `--cost-bps 4`,
`--out-dir trader/lab`, `--workers K`, `--json`, `--quiet`. Everything from the cache; no
network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/cache/bars5m/<SYM>.json.gz` | full sessions of 5-minute bars (scripts/fetch_cache.py) |
| `trader/cache/daily/<SYM>.json.gz` | daily rows: the prior close, the 14-day ATR, the 20-day volume |
| `trader/cache/daily/SPY, _VIX, _VIX3M` | the regime tags for every session, from rows before it only |
| `trader/largecap.json` | tier and sector per symbol (trader-largecap); "unknown" when absent |
| `trader/config.json` | the entry window (09:45 to 15:30) and flat_by (15:55) |
| `trader/lab/trades.jsonl.gz` | one line per trade: the signal, the outcome, the context, the regime tags |
| `trader/lab/edge.json` | per config: all / in-sample / out-of-sample STATS, the slices, the rolling walk-forward, the status |
| `docs/data/edge.json` | with `--publish`: a trimmed copy for the dashboard |

## How it works

1. **The fill.** A bar that closes above a level is only known to have closed once it has
   closed. So the lab fills at the NEXT bar's open (`next_open`), not at the close that made
   the signal, and measures R against the plan's risk (entry minus stop). `--fill close` is
   the idealised number the upstream time machine prints; both are shown and the gap between
   them is part of the answer.
2. **Costs.** 4 basis points of the entry a round trip (half a spread each way plus a basis
   point of slippage, large caps only), taken from every trade in R. A gap through the stop
   counts as a stop at the stop price; a real fill can be worse.
3. **The split.** Sessions sorted; the first two thirds are in sample, the last third out of
   sample. The status a config earns (`approved`, `watch`, `rejected`) comes from the
   out-of-sample block only: approved needs n at or above 100 and a 95 % bootstrap confidence
   interval of the expectancy whose lower bound is above zero after costs.
4. **The walk-forward.** For each of the last 20 sessions, select the configs whose record
   over the 30 sessions before it clears the gate, trade them on that day (one trade a symbol,
   the config with the higher lower bound), roll. That equity curve is the honest expectation
   of "if I had run the desk live". The share of its profit that came from the best 20 % of
   days is printed next to it: when a few days carry the record, say so.
5. **Configs tested.** 31. Every table prints that number, because with 31 tries some
   record looks good by luck; the confidence interval and the out-of-sample block are the
   defence, not a promise.

## Steps

1. Keep the store fresh (`scripts/fetch_cache.py`, the 17:30 ET job), then `lab.py run`
   (a few minutes for 900 names). Say how many symbols and sessions it covered and how many
   it skipped.
2. `lab.py report`: read back the top of the table as printed, the status of each config,
   the configs-tested line and the rolling walk-forward line. Never round a confidence
   interval that crosses zero into an edge.
3. `lab.py report --config ID --by regime` when the user asks about one setup: the slices
   by regime tag, hour, tier, sector and weekday, each with its own n.
4. `lab.py why TICKER DATE` when the user asks why something fired or did not.
5. Hand off to `/trader-desk`, which reads `edge.json` and only shows approved configs.

## Example: the reel's chart

On the upstream TSLA fixture (59 full sessions before Monday 2026-10-19), `orb15|r=1` with
the plan fill and no costs reproduces the time machine's history line exactly: the rule fired
33 times, 13 hit the target, 15 the stop, 5 neither, average R -0.062. The 2026-10-19
signal itself is the reel's trade: entry 364.18 at 10:15 AM (bar 8), stop 358.32, target
370.04. With the realistic next-open fill and 4 basis points the same 33 trades average
-0.12R: the cost of being honest about the fill is about 0.06R a trade. On the three-symbol
fixture a run writes `trades.jsonl.gz` and `edge.json` in under a minute; every config in
the table shows n, win % with its Wilson interval, expectancy in R with its bootstrap
interval, profit factor and max drawdown.

## Rules

- Every number comes from the run. Repeat the tables as printed, with the intervals.
- "Approved" means the out-of-sample record cleared the gate. It is not a prediction and
  not a reason to take a trade.
- Say "configs tested: 31" whenever you quote a table.
- The lab never uses a bar before the signal bar closed, never mixes a session's own
  bars into its regime tags, and never looks at the test day when selecting for it.

## What it never does

It never places, sizes or checks an order, never picks a stock for today (that is the
desk, from the live bars), never prints a projected return, and never calls a rule a
winner.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
