---
name: trader-montecarlo
description: >-
  Every trade backtested 26,000 times, three ways. plan: the upstream time machine on the
  plan from trader-exit with 26,000 replays (and the reel's own 10,000 beside it, so you see
  how little the dice move). book: the lab's trade book for a setup, resampled 26,000 times by
  session at your own sizing rules (max loss a trade, trades a day, the daily loss limit): the
  spread of a 20-day month, the drawdown you should expect, the odds a month ends red. futures:
  the overnight read from Nasdaq-100 and S&P 500 futures, the gap and the range before the
  open, with tags the regime and the desk use. Use when the user says "backtest this 26,000
  times", "run the monte carlo", "how bad can a month get", "risk of ruin", "what are the
  odds", "what do the futures say", "overnight gap", "resample the book", or after
  /trader-exit writes a plan and before /trader-budget sizes it.
argument-hint: "[plan | book --config ID | book --all-approved | futures]"
---

# trader-montecarlo

The upstream time machine replays the rest of the day 10,000 times. This skill does three
things with that idea. It runs the same plan 26,000 times and prints the reel's 10,000-replay
numbers next to it, so the user sees that the dice are not the problem: at 26,000 replays the
interval on "target first" is under a point wide, and the rest of the error is the model, not
the randomness. It takes a setup's whole trade book from the lab and resamples the sessions
26,000 times at the user's own sizing rules, so a 20-day month has a distribution instead of
one number. And before the open it reads the futures, because the first thing a day trader
needs to know is what the night did. A replay is a simulation built from the past, not a prediction and never a
promise. Resampling 20 real sessions cannot show what they never did. Most people who day
trade lose money; a distribution of outcomes is how you find out what that means in dollars
before it happens.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/montecarlo.py" plan --until 15:55 --write          # trader/plan.json, 26,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/montecarlo.py" plan --replays 50000 --seed 7
python3 "${CLAUDE_SKILL_DIR}/montecarlo.py" book --config "orb15|r=1" --write     # the lab's book, resampled at your rules
python3 "${CLAUDE_SKILL_DIR}/montecarlo.py" book --all-approved --publish         # every approved config, one line each
python3 "${CLAUDE_SKILL_DIR}/montecarlo.py" futures --write                       # NQ and ES overnight -> trader/futures.json
```

Flags: `--replays`, `--resamples`, `--seed 19`, `--days 20`, `--horizon-days 60`, `--fill
next_open|close`, `--all-sessions` (not just out of sample), `--include-watch`, `--edge
trader/lab/edge.json`, `--trades trader/lab/trades.jsonl.gz`, `--config-file trader/config.json`,
`--offline` (a saved chart for plan, a folder for futures), `--at HH:MM`, `--now`, `--json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: `plan` hands it to the upstream `timemachine.py run --replays N` |
| `trader/lab/trades.jsonl.gz`, `trader/lab/edge.json` | the lab's trade book and its split (out of sample by default) and `configs_tested` |
| `trader/config.json` | `max_loss_per_trade_usd` (one R in dollars), `max_trades_per_day`, `max_daily_loss_usd`, `budget` |
| Yahoo chart API | `futures`: NQ=F and ES=F, 5 days of 5-minute bars and 60 daily bars, free and unofficial |
| `trader/montecarlo.json` | with `--write`: the plan run (`kind: plan`) or the book run (`kind: book`) |
| `trader/futures.json` | with `--write`: `{date, cutoff, nq: {last, prev_settle, gap_pct, overnight_high, overnight_low, range_vs_20d, tags}, es: {...}, tags}`; trader-regime reads it when it is for the same day |
| `docs/data/montecarlo.json` | with `--publish`: the book run, for the dashboard |

## How it works

1. **plan.** The upstream tool runs unchanged with `--replays 26000 --seed 19`; its odds, its
   history line and its verdict print as it printed them, plus a 95 % Wilson interval on each
   replay count. Then it runs once more at 10,000 replays and seed 19, the reel's own numbers.
2. **book.** The config's trades (out of sample unless `--all-sessions`) are grouped by session.
   Each session is run once through the sizing rules, in entry order: at most
   `max_trades_per_day`, and no new trade once the trades already placed could lose
   `max_daily_loss_usd` at their stops (the upstream risk rule: every entry counts its full
   max loss). Then 26,000 windows of `--days` sessions are drawn with replacement (a drawn day
   brings its whole trade list, so a bad morning stays a bad morning). Per window: the total in
   R and in dollars at one R = `max_loss_per_trade_usd`, the max drawdown, the worst day, the
   trade count, whether a daily-limit day was inside it. The report prints the percentiles, the
   share of windows that end red, the odds of a drawdown over 10 %, 25 % and 50 % of the budget,
   and the expectancy a trade with its band, for the window and for the longer horizon.
3. **futures.** The prior settle is the last 5-minute bar at or before 4:00 PM New York of the
   previous trading day. The overnight window runs from there to the cutoff, capped at 9:30.
   The gap is the last overnight close against the settle; the range is its high minus low
   against the average daily range of the last 20 sessions. Tags at half a percent
   (`nq_gap_up`, `nq_gap_down`, `nq_gap_flat`) and at 1.3x / 0.7x (`nq_wide_overnight`,
   `nq_quiet_overnight`, `nq_normal_overnight`), the same for ES.

## Steps

1. After `/trader-exit` writes the plan: `montecarlo.py plan --until 15:55 --write`. Read the
   odds with their intervals, the history line and the verdict verbatim, then the reel's
   numbers and the sentence about the dice. Hand off to `/trader-budget`.
2. After `/trader-lab` runs: `montecarlo.py book --all-approved --publish`, then `book --config
   ID` for the one the user asks about. Read the window's percentiles, the share of windows
   that end red and the drawdown odds, and the caveat line as printed. Say "configs tested: 31".
3. Before the open: `montecarlo.py futures --write`. Read the two lines. `/trader-regime live`
   folds the tags into the day's regime when the file is for today.

## Example: the reel's plan

TSLA, Monday 2026-10-19, entry 364.18 at 10:15 AM, stop 358.32, target 370.04 (1R), on the
upstream's own 60-day chart, seed 19:

```
  target first   29.5 %   95 % CI 29.0 to 30.1 % (26,000 replays)
  stop first     25.4 %   95 % CI 24.9 to 25.9 % (26,000 replays)
  neither        45.1 %   95 % CI 44.5 to 45.7 % (26,000 replays)
History: the same rule on 59 real sessions fired 33 times: 13 target, 15 stop, 5 neither. Average R -0.062.
Close to a coin flip: target first 29.5 % vs stop first 25.4 %.
The reel's own run, 10,000 replays, seed 19: target first 29.4 %, stop first 25.5 %, neither 45.1 %.
```

Twenty-six thousand replays moved the answer a tenth of a point. The coin flip is the model's
answer, not the dice's. Friday 2026-10-23 before the open, from the saved charts: NQ -0.30 %
overnight against Thursday's 4:00 PM settle 29,146.50, an overnight range 0.51x its 20-day
average, tags `nq_gap_flat nq_quiet_overnight`.

## Rules

- Every number comes from the run; print the intervals and the percentiles as given.
- The reel's 10,000 replays and the 26,000 replays differ by dice only; say so, once.
- A resampled month is the spread the real sessions allow, not a forecast of next month.
- Costs are already inside the lab's R; the sizing rules here only decide which trades count.

## What it never does

It never places, sizes or checks an order, never changes the plan, never promises a fill, and
never calls a replay or a resample a prediction.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
