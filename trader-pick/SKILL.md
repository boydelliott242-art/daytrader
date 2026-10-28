---
name: trader-pick
description: >-
  The 8:25 pick. One large cap a morning, the one whose evidence by 8:05 ET carried the best measured odds of closing
  2 percent or more above its open, posted to the site by 8:25 with its own out-of-sample hit rate, the base rate
  and the live record beside it. A naive-Bayes scorer over bucketed features from the daily store (yesterday's move,
  volume, range position, momentum, ATR, RSI, realised volatility, the stock's own habit of 2 percent days, the
  market's trend and VIX, the Nasdaq futures gap at 8:05 ET from the NQ bench), refit as it walks forward so every
  scored morning was trained only on the mornings before it. NO PICK is a result. Use when the user says "the morning
  pick", "which stock jumps today", "the 8:25 pick", "most likely to be up 2 percent", "train the pick", "grade the
  picks", or "how is the pick doing".
argument-hint: "[train [--rule up|net|ret] [--extra MODULE] | morning [--at 08:05] --write --publish | grade --write | report]"
---

# trader-pick

A pick is a count, not a prediction. The base rate of a large cap closing at least 2 percent above its open is about
one day in eight; the pick is the name whose morning evidence has historically done best, and its record is printed
beside that base rate every day. It is not a buy. `${CLAUDE_SKILL_DIR}` is this skill's folder; run from the project.

```bash
python3 "${CLAUDE_SKILL_DIR}/pick.py" train --workers 14 --rule up         # the walk-forward over every large cap and session
python3 "${CLAUDE_SKILL_DIR}/pick.py" morning --at 08:05 --write --publish  # today's pick (the 08:08 ET job runs this)
python3 scripts/pick_research.py --base-cache C.pickle --extra my_feature.py   # does a feature add direction in both halves?
python3 "${CLAUDE_SKILL_DIR}/pick.py" grade --write --publish              # settle past picks from the store
python3 "${CLAUDE_SKILL_DIR}/pick.py" report                               # the out-of-sample record, the lifts, the live record
```

## How the pick is made

1. **The rows.** For every symbol in `trader/largecap.json` and every session in the daily store (about 500), the
   features are computed from the rows strictly before the session: ATR as a share of price, yesterday's move and
   its relative volume and where it closed in its range, the 5 and 20-day moves, the distance to the 20-day high and
   low, RSI, yesterday's gap, 20-day realised volatility, and the stock's own share of 2 percent up and down days over
   the last 60 sessions. The market's context rides along: SPY's 1 and 5-day moves and trend, the VIX, the day of the
   week, and the NQ futures gap as it stood at 8:05 ET (from `nq.py session-tags --at 07:05`, the bench's minute bars).
   The outcome is the session's own close against its open: UP at +2 percent or more, DOWN at -2 percent or worse.
   Left out, as live: earnings sessions (the report day, and the session after a report made after the close or with
   no time given, from Nasdaq's calendar, `scripts/fetch_earnings_history.py`) and special situations (a name that
   moved 20 percent or more the session before: a crash, a deal, a split the store missed).
2. **Three scorers, three rules.** Each feature is bucketed at fixed cut points; from the bucket counts come the up
   odds, the down odds and the expected open-to-close move, calibrated on the training rows. No weights to tune: the
   counts are the model. The `up` rule takes the best up odds (the name most likely to MOVE: volatile names go both
   ways), `net` takes up odds minus down odds, `ret` the best expected move.
3. **The walk-forward.** Starting after 120 sessions, the model is refit every 20 sessions on everything before and
   scores the next 20. The record per rule, split into the two halves of the out-of-sample window, sits beside the
   base rates of every liquid name and the naive pick of simply taking the highest-ATR name each morning.
4. **The claim.** The card says "most likely to close 2 percent ABOVE its open" only when the live rule's pick closed up
   more often than down, with separated intervals, in BOTH halves. Otherwise it says "most likely to MOVE 2 percent",
   prints both odds side by side, and says the direction has no measured edge.
5. **The morning.** `morning` reads the store (through yesterday's close: the 17:30 cache job must have run), today's
   futures read, today's and yesterday's earnings calendars (names reporting today, and names reacting to a report
   after yesterday's close, are left out, as are special situations, each listed with its reason), scores every name,
   fetches the pre-market price of the top ten from Yahoo (shown, never scored: the model saw none of them), writes
   `trader/pick.json`, logs the
   pick pending in `trader/pick/record.jsonl`, and with `--publish` copies the card for the site. The 08:08 ET job seals
   and pushes it (a retry at 08:18 if it did not land): on the front page in big words by about 8:15.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/cache/daily/*.json.gz` | the daily rows (the 17:30 cache job keeps them current) |
| `trader/nq/session_tags_0805.json` | the NQ gap at 8:05 ET per session, from the bench (falls back to `session_tags.json`, then na) |
| `trader/futures.json` | this morning's NQ and ES read from the 08:05 futures job |
| `trader/cache/earnings/D.json` | Nasdaq's calendar: a name reporting today, or reacting to yesterday's report after the close, is left out |
| `trader/intel.json` | the fallback for today's reporters when there is no calendar file for the date |
| `trader/pick/model.json` | the lifts, the base rate, the calibration bins, the walk-forward record |
| `trader/pick/record.jsonl` | every pick the morning it was made (pending), then graded on the real open and close |
| `trader/pick.json`, `trader/publish/pick.json` | today's card: the pick, the odds, the base rate, the reasons, the next nine, the records |

## Rules

- The base rate is printed with every pick, and the live record with its interval. A record under 30 picks says so.
- NO PICK when nothing clears the floor. A morning with no pick is a normal morning, not a broken one.
- The pre-market price is information, not a score: the model has no pre-market history and says so.
- A stale store (the cache job did not run) is flagged on the card; the pick still runs on what it has.
- A pick that hit is a count of one. The picks that made the record also made the losses the record prints.
- Earnings days are in the backtest (no calendar reaches back) and out of the live pick (the calendar is live).

## What it never does

It never places, sizes, checks or approves an order, never reads the pre-market into the score, and never prints a
projected return. Most people who day trade lose money; a 2 percent day that happens one time in five is a 2 percent
day that does not happen four times in five.

## Safety

- Education and tooling, not financial advice. A pass is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk passed that exact
  order today, the user typed an explicit yes for that one order in the chat, and the user clicked Allow once on
  Claude's own card because the order tool is set to "Needs approval" (Customize > Connectors > Tool permissions).
  Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
