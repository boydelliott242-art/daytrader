---
name: trader-pick
description: >-
  The 8:25 pick. One large cap a morning, the one whose evidence by 8:05 ET carried the best measured odds of closing
  2 percent or more above its open, posted to the site by 8:25 with the record of past picks (up AND down, with
  intervals), the base rates and the live record beside it. A naive-Bayes scorer over bucketed features from the daily
  store (yesterday's move, volume, range position, momentum, ATR, RSI, realised volatility, the stock's own habit of 2
  percent days, the market's trend and VIX, the Nasdaq futures gap at 8:05 ET from the NQ bench), refit as it walks
  forward so every scored morning was trained only on the mornings before it, over every name while it was a large cap.
  NO PICK is a result. Use when the user says "the morning pick", "which stock jumps today", "the 8:25 pick", "most
  likely to be up 2 percent", "train the pick", "grade the picks", or "how is the pick doing".
argument-hint: "[train [--rule up|net|ret] [--extra MODULE] [--no-pit] | morning [--at 08:05] --write --publish [--replace] | grade --write | report]"
---

# trader-pick

A pick is a count, not a prediction. The base rate of a large cap closing at least 2 percent above its open is about
one day in nine; the pick is the name whose morning evidence has historically done best, and the record of past picks
is printed beside that base rate every day, up AND down. It is not a buy. `${CLAUDE_SKILL_DIR}` is this skill's folder;
run from the project.

```bash
python3 "${CLAUDE_SKILL_DIR}/pick.py" train --workers 12 --rule up         # the walk-forward over every large cap and session
python3 "${CLAUDE_SKILL_DIR}/pick.py" morning --at 08:05 --write --publish  # today's pick (the 08:08 ET job runs this with its own clock time)
python3 scripts/pick_research.py --base-cache C.pickle --extra my_feature.py   # does a feature add direction in both halves?
python3 "${CLAUDE_SKILL_DIR}/pick.py" grade --write --publish              # settle past picks and the shadow from the store
python3 "${CLAUDE_SKILL_DIR}/pick.py" report                               # the out-of-sample record, the lifts, the live record
```

## How the pick is made

1. **The rows.** For every name a large-cap list has held (`trader/largecap.json` and the dated copies in
   `trader/largecap_history/`) and every session in the daily store, counted only while the name's estimated cap at
   the prior close was $10B or more, the features are computed from the rows strictly before the session: ATR as a
   share of price, yesterday's move and its relative volume and where it closed in its range, the 5 and 20-day moves,
   the distance to the 20-day high and low, RSI, yesterday's gap, 20-day realised volatility, and the stock's own share
   of 2 percent up and down days over the last 60 sessions. The market's context rides along: SPY's 1 and 5-day moves
   and trend, the VIX, the day of the week, and the NQ futures gap as it stood at 8:05 ET. The outcome is the session's
   own close against its open: UP at +2 percent or more, DOWN at -2 percent or worse. Left out, as live: earnings
   sessions (the report day, and the session after any report, a Saturday report included: Nasdaq drops report times
   for past dates, so the time plays no part), special situations (a 20 percent move the session before), half days.
2. **Three scorers, three rules.** Each feature is bucketed at fixed cut points; from the bucket counts come the up
   odds, the down odds (calibrated, in-sample, shared by every name in a bin) and a summed move score that is not
   calibrated and ranks only the research rule `ret`. The `up` rule takes the best up odds, ties by the raw score (it
   finds names that MOVE: volatile names go both ways), `net` takes up odds minus down odds, `ret` the move score.
3. **The walk-forward.** Starting after 120 sessions, the model is refit every 20 sessions on everything before and
   scores the next 20. The record per rule, split into the two halves of the out-of-sample window, sits beside the
   base rate of every liquid name and the naive pick of the highest-ATR name each morning, every rate with an
   interval (Wilson for one pick a morning; whole mornings resampled for the base rate and the top five).
4. **The claim.** The card says "likely to close 2 percent ABOVE its open" only when the live rule's pick closed up
   more often than down, with separated intervals, in BOTH halves. Otherwise it says "likely to MOVE 2 percent" with
   the move rate of past picks, and says whether the record leans down or cannot tell. Never "most likely": the most
   volatile liquid name moved at least as often. The odds on the card are the record of past picks, not the model's
   bin rates.
5. **The morning.** `morning` reads the store (the job first catches up every name not through the previous session;
   one still behind is left out; a store with SPY or most names behind is NO PICK; a half day's pick is shown and not
   counted), the futures read (a missing Nasdaq value is no gap, not a crash), the calendars from the
   previous session to today (an empty calendar on a trading day is a failed answer, not a quiet day), scores every
   name, fetches the pre-market price of the top ten from Yahoo (the last 5-minute bar that ended by the run, shown and
   never scored), writes `trader/pick.json`, logs the pick pending in `trader/pick/record.jsonl` and the shadow in
   `trader/pick/shadow.jsonl`, and with `--publish` copies the card for the site. A second run on the same day keeps the
   logged pick (`--replace` posts another and marks the first superseded); a card written after 9:25 ET is posted marked
   late and never counted; a morning that fails posts a dated NO PICK card with the reason. The 08:08 ET job seals and
   pushes it (retries at 08:18, 08:40 and 09:05 post or push again if it did not land; the 09:19 pre-open run keeps the
   logged pick and refreshes its pre-market print): on the front page in big words by about 8:15, refreshed five minutes
   before the open.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/cache/daily/*.json.gz` | the daily rows (the 17:30 cache job keeps them current) |
| `trader/largecap.json`, `trader/largecap_history/D.json` | the lists: who counts, and each name's cap for the point-in-time rule |
| `trader/nq/session_tags_0805.json` | the NQ gap at 8:05 ET per session, from the bench (falls back to `session_tags.json`, then na) |
| `trader/futures.json` | this morning's NQ and ES read from the futures job |
| `trader/cache/earnings/D.json` | Nasdaq's calendar, every calendar day: who reports today, who reacts to a report since the previous session |
| `trader/intel.json` | the fallback for today's reporters when the calendar for the date is missing or empty |
| `trader/pick/model.json` | the lifts, the base rate, the calibration bins, the universe rule, the walk-forward record |
| `trader/pick/record.jsonl` | every pick the morning it was made (pending), then graded on the real open and close |
| `trader/pick/shadow.jsonl` | the highest-ATR eligible name each morning, graded the same way, never the pick |
| `trader/pick.json`, `trader/publish/pick.json` | today's card: the pick, the record's odds, the base rates, the reasons, the next nine, what was left out |

## Rules

- The base rate is printed with every pick, and the live record with its interval. A record under 30 picks says so.
- NO PICK when nothing clears the floor; the record says how often that happened (it has not yet), so a no-pick
  morning points at the inputs.
- The pre-market price is information, not a score: the model has no pre-market history and says so.
- A stale store (the cache job did not finish) is NO PICK, not a pick on the wrong morning's history.
- A pick that hit is a count of one. The picks that made the record also made the losses the record prints.
- The shadow is a lead under test, not a second pick; it joins the model only through `scripts/pick_research.py`.

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
