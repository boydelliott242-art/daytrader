---
name: trader-nq
description: >-
  The NQ bench. Runs the desk's setups (opening-range breakouts, VWAP rules, gap and go, inside
  bars and the rest, 31 configurations) through the user's own fifteen-year Nasdaq-100 futures
  backtester: 1-minute E-mini NQ bars from October 2011, aggregated to 5-minute regular sessions,
  one market order at the signal bar, the engine's own fees, slippage and roll costs, back-adjusted
  prices. Writes the answer in the lab's shapes (win rate and expectancy with intervals, in and out
  of sample, by year, a rolling walk-forward by year, the engine's own net P&L and break-even
  slippage), fifteen years of overnight gaps and what the ORB did after them, and the overnight tags
  the lab merges into its regime slices. Use when the user says "run the setups on NQ", "backtest it
  on the Nasdaq futures", "fifteen years of futures", "what does NQ say overnight", "nq backtester",
  "run the orb on NQ", "is this rule real", or after /trader-lab to test the rules on long history.
argument-hint: "[check | setups [--configs approved] | overnight | session-tags | run STRATEGY | report]"
---

# trader-nq

The lab has sixty sessions of stock bars. The user's NQ backtester has 3,800 sessions of
1-minute Nasdaq-100 futures and an engine that charges what trading costs. This skill puts the
desk's rules through that engine. It is not a stock test: NQ is one instrument, the index itself.
A rule that holds up over fifteen years of NQ with fees and slippage has a record worth knowing;
a rule that does not has been told something the sixty sessions could not say. Either way the
answer is a record, not a prediction. Most people who day trade lose money, and the example ORB in
the backtester's own README makes the point: about six ticks a trade gross, and five ticks of
slippage would erase it.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives). The backtester
lives at `~/Claude/Projects/nq-backtester` (or `nq_backtester_path` in `trader/config.json`) with
its own venv; `nq_adapter.py` runs under that venv, `nq.py` is plain Python.

```bash
python3 "${CLAUDE_SKILL_DIR}/nq.py" check                                     # installed, usable, the data span
python3 "${CLAUDE_SKILL_DIR}/nq.py" setups --publish                          # all 31 configs, 2011 to 2026 -> trader/nq/
python3 "${CLAUDE_SKILL_DIR}/nq.py" setups --configs approved --start 2020-01-01
python3 "${CLAUDE_SKILL_DIR}/nq.py" overnight --publish                       # 15 years of gaps and the ORB after them
python3 "${CLAUDE_SKILL_DIR}/nq.py" session-tags --days 60                    # nq tags for the lab's sessions
python3 "${CLAUDE_SKILL_DIR}/nq.py" run orb --no-sensitivity                  # the backtester's own example strategy
python3 "${CLAUDE_SKILL_DIR}/nq.py" report --config "orb15|r=1" --by year
```

Flags: `--backtester DIR`, `--python PATH`, `--fees ninjatrader_free` (or ninjatrader_monthly,
ninjatrader_lifetime, ibkr_fixed, topstep, exchange_only, zero), `--slippage default|conservative|
optimistic|zero`, `--instrument NQ|MNQ`, `--capital 100000`, `--start`, `--end`, `--out-dir
trader/nq`, `--publish`, `--json`, `--quiet`.

The commands, in plain words: nq.py check, nq.py setups, nq.py overnight, nq.py session-tags, nq.py run STRATEGY
(the backtester's own runner) and nq.py report.

## What it reads and writes

| file | what |
| --- | --- |
| `<backtester>/data/nq_1m/NQ_1m_<year>.parquet`, `rolls.csv` | the user's 1-minute bars and contract rolls (Databento, CME Globex) |
| `dt/setups.py` | the same 12 setups and 31 configs the lab and the desk run |
| `trader/config.json` | the entry window and flat-by time (09:45 to 15:30 New York, flat 15:55 = 14:55 Chicago) |
| `trader/lab/edge.json` | with `--configs approved`: which configs to run |
| `trader/nq/trades.jsonl.gz` | one line per trade in the lab's shape plus the engine's exit reason, cost in dollars and a dt.sim cross-check |
| `trader/nq/edge.json` | the lab's table shape: per config all / in-sample / out-of-sample STATS, status by the same gate, by year, hour, weekday and overnight tag, a rolling walk-forward by year, the engine's stats and yearly table; `two_engines` (how often the minute engine and dt.sim agree); `configs_tested` |
| `trader/nq/overnight.json` | gap percentiles over 15 years, the ORB's record by gap bucket and by overnight-range bucket |
| `trader/nq/session_tags.json` | `nq_gap_up|down|flat` and `nq_wide|quiet|normal_overnight` per session; the lab merges them into its regime tags when present |
| `docs/data/nq.json` | with `--publish`: the tables for the dashboard |

## How the translation works

1. **Sessions.** The regular session, 08:30 to 15:00 Chicago (9:30 to 16:00 New York), is cut into
   78 five-minute buckets from the 1-minute bars (first open, highest high, lowest low, last close,
   summed volume). A session with a missing bucket (early closes, holidays, the known gaps) is skipped
   and counted. Prices are the engine's back-adjusted continuous series, so moves inside a contract
   are real and rolls do not fake a gap.
2. **Signals.** Every config runs on every session with the same context the lab uses: the prior
   session's close, the 14-day ATR, the entry window. The signal bar's last minute gets a market
   order for one contract with the plan's stop and target in points. The engine fills at the next
   minute's open plus its slippage, puts the stop first inside a bar, and is flat at 14:55 Chicago.
   `orb15_rs` needs SPY and never fires on NQ: it is listed as not applicable.
3. **R.** Measured against the plan's risk (entry minus stop), so a worse fill shows as a smaller R.
   `R_net` has the engine's fees and slippage inside it. `R_gross` does not.
4. **Two engines.** Every trade is also played by dt.sim on the 5-minute bars with the engine's
   dollars converted to basis points of the fill. The report prints how often the two agree on the
   outcome; a gap between them is a fill-model question, and it is printed, not hidden.
5. **The gate.** The same as the lab's: approved needs out-of-sample n at or above 100 and a 95 %
   expectancy lower bound above zero after costs. Out of sample is the last third of the sessions.

## Steps

1. `nq.py check`. If it refuses, the backtester or its venv is missing: say where it should be.
2. `nq.py setups --publish` (a few minutes the first time: the engine compiles). Read the table as
   printed: status, n, win % with its interval, expectancy with its interval, the engine's net
   dollars and break-even slippage ticks, the two-engines line, and "configs tested: 31".
3. `nq.py report --config ID --by year` when the user asks about one rule: the record by year and
   the rolling walk-forward (select on the prior three years, trade the next).
4. `nq.py overnight --publish` and `nq.py session-tags`, then `lab.py run` again so the lab's
   regime slices carry the overnight tags.
5. Say what it is: the rule's record on one instrument over fifteen years, not a forecast for any
   stock tomorrow, and not a reason to size up.

## Example: the first quarter of 2025

`nq.py setups --start 2025-01-01 --end 2025-03-31 --configs "orb15|r=1,orb15|r=2,vwap_reclaim|r=1,orb15_rs|r=1"`
over 60 full sessions, NinjaTrader free-plan fees and default slippage:

```
orb15|r=1          41 trades, win 59 % [43, 72], exp +0.228R [...], engine net $10,300, PF 1.45, break-even 25 extra ticks
orb15|r=2          41 trades, win 22 %, exp +0.028R, engine net -$4,053
vwap_reclaim|r=1   43 trades, win 35 %, exp -0.198R, engine net -$12,082
orb15_rs|r=1       not applicable on NQ (needs SPY)
Two engines agree on the outcome of 100 % of 125 trades.
```

Sixty sessions is a quarter; the status column is `rejected` for all three because the out-of-sample
third of a quarter is twenty sessions. The full fifteen-year run is what the status is for.

## Rules

- Every number comes from the engine's run; print the intervals, the engine's dollars and the
  break-even line as given.
- One instrument. Say it every time: a record on NQ is not a record on a stock.
- Fees and slippage are real and inside `R_net`; never quote the gross number alone.
- Never change the backtester's files; this skill only reads them.

## What it never does

It never places, sizes or checks an order, never trades futures or anything else, never edits the
backtester, and never calls fifteen years of history a prediction.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
