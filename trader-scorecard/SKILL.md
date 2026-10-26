---
name: trader-scorecard
description: >-
  The scorecard. An append-only track record of every candidate the desk showed, graded
  against the real bars after the close: logged the moment it was shown, so nothing is
  added in hindsight; a call logged late is marked late and kept out of the headline. Prints
  the live record with Wilson intervals on the win rate and a bootstrap interval on the
  expectancy, by setup and by week, the equity curve in R and in dollars at your max loss a
  trade, and the gap between the lab's out-of-sample expectation and what happened. Use
  when the user says "how is the desk doing", "track record", "grade yesterday", "did the
  calls work", "what did the board do", "scorecard", "live record vs the lab", or after the
  close on any day the desk ran.
argument-hint: "[log --from-desk | grade [--date D] | report [--publish] | show ID]"
---

# trader-scorecard

A desk that only remembers its wins is not a desk. This skill writes down every candidate
the board showed, at the minute it showed it, and after the close it plays each one forward
on the real 5-minute bars, the same way the lab does (next-open fill, stop before target
inside a bar, flat by 15:55, 4 basis points), and keeps the result next to the call. The
headline numbers come with their intervals, because twenty calls cannot tell you much, and
the report says so in words when the sample is small. A call that was logged after its entry
bar had already closed by more than a bar is marked late: it was not knowable in time, and it
stays out of the headline. Most people who day trade lose money; a scorecard is how you
find out whether you are one of them before the account does.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scorecard.py" log --from-desk                 # the board in trader/desk.json -> calls.jsonl (desk.py live does this too)
python3 "${CLAUDE_SKILL_DIR}/scorecard.py" grade                           # every ungraded call whose session is complete
python3 "${CLAUDE_SKILL_DIR}/scorecard.py" grade --date 2026-10-19 --cache-root trader/cache
python3 "${CLAUDE_SKILL_DIR}/scorecard.py" report                          # the live record
python3 "${CLAUDE_SKILL_DIR}/scorecard.py" report --since 2026-10-01 --publish   # also trader/publish/scorecard.json
python3 "${CLAUDE_SKILL_DIR}/scorecard.py" show "2026-10-19|TSLA|orb15|r=1|10:15 AM"
```

Flags: `--dir trader/scorecard`, `--desk trader/desk.json`, `--cache-root DIR`, `--offline
DIR` (saved Yahoo charts, for tests), `--cost-bps 4`, `--config trader/config.json`
(`max_loss_per_trade_usd`, the dollars one R is worth), `--now`, `--json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/desk.json` | the board (trader-desk): one call per candidate, with its signal, the lab's expectation and the time machine's odds |
| `trader/scorecard/calls.jsonl` | append-only: `{id, logged_at, date, as_of, symbol, setup, config_id, rank, signal, lab_oos_exp_R, tm_p_target}`; the id is `date\|symbol\|config_id\|entry_time`, never written twice |
| `trader/scorecard/outcomes.jsonl` | one line per graded call: outcome, exit, R for the `next_open` fill (the headline) and the plan fill, `late` |
| `trader/cache/bars5m/<SYM>.json.gz` | the real session; a missing day is fetched from Yahoo (`5d`, `5m`) |
| `trader/publish/scorecard.json` | with `--publish`: calls, graded, late, STATS, by setup, by week, the equity curves, the lab expectation |

## The rules of the record

- **Logged when shown.** `desk.py live` appends every candidate as it prints the board;
  `log --from-desk` does the same by hand and never duplicates an id.
- **Graded on the real bars.** The signal's entry bar k, stop and target are replayed with
  `dt.sim.play`: fill at the next bar's open, stop before target inside a bar, out at the
  flat-by bar's close, 4 basis points. Both fills are kept; `next_open` is the headline.
- **Late is late.** `logged_at` more than five minutes after the entry bar's close marks
  the call late. Late calls are reported apart and never in the headline.
- **Intervals always.** Win rate with its Wilson interval, expectancy with a bootstrap
  interval (2,000 draws, seed 19). Under 30 graded calls the report says the sample is
  small, in words.
- **Dollars at your rule.** One R is `max_loss_per_trade_usd` (50 by default): the equity
  curve in dollars is what the sizing rule would have done, not a forecast.
- **The gap.** The mean of the lab's out-of-sample expectancy over the graded calls, next
  to the realised mean R. When the live record runs under the lab, that is the number to
  look at, not the win rate.

## Steps

1. During the session nothing to do: the desk logs. After the close (the 16:20 ET job, or
   by hand): `scorecard.py grade`, then `scorecard.py report --publish`.
2. Read back: calls, graded, pending, late; the headline win rate and expectancy with their
   intervals; by setup; the last week; the gap to the lab. Say "n is small" when it says so.
3. `show ID` when the user asks about one call: the signal, when it was logged, what
   happened, both fills.
4. Never edit a call or an outcome. A wrong call is a graded loss, not a deleted line.

## Example: the reel's call

The board of Monday 2026-10-19 at 10:15 AM showed TSLA, `orb15|r=1`: entry 364.18 at
10:15 AM (bar 8), stop 358.32, target 370.04. Graded on the fixture bars after the close:

```
2026-10-19|TSLA|orb15|r=1|10:15 AM  logged 10:15:40 ET, graded 16:40 ET
  next-open fill 364.21: close at 3:55 PM, 367.23, +0.49R net (+0.51R gross)
  plan fill      364.18: close at 3:55 PM, 367.23, +0.50R net (+0.52R gross)
```

Neither side hit; the trade was flat at 15:55 with +0.49R in hand. A JPM VWAP reclaim shown
on the same board stopped out at 10:40 for -1.07R. Two calls are two calls: the report's
Wilson interval on a 1-for-2 record runs from about 9 % to 91 %, which is the honest width.

## Rules

- Every number comes from the files. The report prints intervals with every rate; repeat them.
- A record is not a prediction of the next call. Say so when the user reads it as one.
- Never drop a call, never regrade with a kinder fill, never count a late call in the headline.

## What it never does

It never places, sizes or checks an order, never picks a candidate, never changes a call
after it was logged, and never prints a projected return.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
