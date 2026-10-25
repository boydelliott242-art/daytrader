---
name: trader-desk
description: >-
  The desk. The advisor that runs the whole chain in front of the eleven upstream skills:
  before the open it builds the large-cap universe, reads the regime, lists the names that
  report today or tomorrow morning, runs the upstream daily scan and the news; after the first
  bar past the opening range it runs every setup the lab approved over today's bars, drops the
  earnings names, flags late triggers, ranks what is left by the lab's out-of-sample record
  (the conservative bound, in today's regime slice when it has a record there), replays the top
  candidates through the upstream time machine, prints the board and logs every candidate to
  the scorecard the moment it is shown. One candidate a symbol. It never sizes, checks or
  places anything: pick hands one candidate to the upstream exit planner, and the chain goes on
  from there with your yes. Use when the user says "what's on the desk", "morning plan", "the
  board", "what matched today", "run the desk", "pick TSLA", "grade today", "replay Friday",
  "what should I look at", or at 10:00, 10:15, 10:30 and 11:00 on any trading day.
argument-hint: "[morning | live [--at HH:MM] | replay --date D --at HH:MM | pick TICKER | grade]"
---

# trader-desk

The eleven upstream skills are one trade's tools, start to finish. The desk is the chair in
front of them. It does the morning work, watches the whole large-cap list at once when the
opening range closes, keeps only what the lab measured and approved, and shows the board with
the odds already on it: the lab's record for that setup, the regime slice that applies, the
time machine's replay of that exact plan. A candidate matched a rule with a measured record.
That is all a candidate is. It is not a prediction, and the desk never picks for you. Most
people who day trade lose money; a desk that shows the odds before the money does not change
that, it only makes the odds hard to ignore.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/desk.py" morning --write --publish            # before the open (08:30 ET job)
python3 "${CLAUDE_SKILL_DIR}/desk.py" live --write --publish               # after 9:50 ET; the 10:00 / 10:15 / 10:30 / 11:00 jobs
python3 "${CLAUDE_SKILL_DIR}/desk.py" live --at 10:15 --top 8 --write      # cut the clock at 10:15
python3 "${CLAUDE_SKILL_DIR}/desk.py" replay --date 2026-10-19 --at 10:15  # a past day from the bar store, no network
python3 "${CLAUDE_SKILL_DIR}/desk.py" pick TSLA --write                    # -> trader/entry.json and the upstream exit plan
python3 "${CLAUDE_SKILL_DIR}/desk.py" grade                                # after the close: scorecard grade + report
```

Flags: `--edge trader/lab/edge.json`, `--regime trader/regime.json`, `--symbols-file
trader/largecap.txt`, `--largecap trader/largecap.json`, `--cache-root trader/cache`,
`--exclusions FILE` (the morning desk.json, or a list of symbols), `--max-charts 300`,
`--top 8`, `--allow-earnings`, `--include-watch`, `--no-timemachine`, `--tm-until 15:55`,
`--now "2026-10-19 10:15"` (New York), `--write`, `--publish`, `--json`, `--quiet`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/largecap.txt`, `trader/largecap.json` | the universe (trader-largecap): `morning` rebuilds it when it is over 20 hours old |
| `trader/regime.json` | the regime tags (trader-regime): `morning` runs `regime.py daily --write` |
| Nasdaq earnings calendar | `morning`: today and the next session; a name reporting today (any time) or tomorrow pre-market is excluded |
| `trader/watchlist.json` | the upstream `scan.py daily` over the large caps (`morning`), then `news.py` for the top hits |
| `trader/lab/edge.json` | the lab's table: the configs with status `approved` run live (`--include-watch` adds `watch`, flagged); without the file every config runs and every candidate says `untested` |
| Yahoo spark + v8 charts, or the bar store | `live`: 5-minute closes for every symbol, then real highs and lows for the `--max-charts` most traded (PARTIAL when it stops short); `replay`: the store |
| `trader/desk.json` | the morning plan (`mode: morning`) or the board (`mode: live`): candidates with signal, lab block, time machine block, flags, score, rank |
| `trader/scorecard/calls.jsonl` | every shown candidate, appended once (id `date\|symbol\|config\|entry time`), the minute it is shown |
| `trader/entry.json`, `trader/plan.json` | `pick`: the chosen signal in the upstream trader-entry shape, then `exit.py plan --write` (the upstream trader-exit tool) |
| `docs/data/desk.json` | with `--publish`: the dashboard's copy |

## How the board is built

1. **The window.** Like the upstream scanner: a trading day, after the first bar past the
   15-minute range closes (9:50), before `no_entries_after` (15:30). Outside it, one plain line.
2. **The bars.** Completed 5-minute bars only, up to the cutoff. Today: spark closes for every
   name, then one chart with real highs and lows for the most traded names. A past day: the store.
3. **The rules.** Every approved config from `edge.json` runs on each chart with the same
   context the lab used (the entry window, the prior close, the daily ATR, SPY's bars for the
   relative-strength rule). One signal a config a chart: the first bar that confirmed it.
4. **The gates.** Names on the morning exclusions (reporting today, or tomorrow before the
   open) and the config's blocked list are dropped and listed. A trigger more than one bar old
   is flagged `late`: a late entry is a different trade. A config whose record in today's
   regime tag is negative with n at or above 50 is flagged `regime_against`.
5. **One candidate a symbol.** When several configs fire on one chart, the board shows the one
   with the higher score and lists the others under it as also fired.
6. **The score.** The lab's out-of-sample expectancy, its 95 % lower bound, after costs: the
   regime slice's when today's tag has n at or above 50 there, else the pooled block. Fresh
   candidates rank before late ones.
7. **The time machine.** The top `--top` fresh candidates are replayed through the upstream
   tool (10,000 replays, seed 19) on the stock's own last 60 days; the odds and the verdict line
   print as the tool printed them. On a replay the chart is built from the store, so no network.
8. **The log.** Every shown candidate goes to `calls.jsonl` at once, so the scorecard grades
   what was really on the board, not what looked good at the close.

## Steps

1. 08:30 ET: `desk.py morning --write --publish`. Read back the regime line, the exclusions
   (who reports today and tomorrow morning), the daily hits and the news flags.
2. 10:00 to 11:00 ET: `desk.py live --write --publish` (the schedule runs it four times).
   Read the board as printed: rank, symbol, the setup's label, entry / stop / target / R, the
   lab's n, win % with its interval, expectancy with its interval and status, the time machine
   odds and verdict verbatim, the flags. Say "configs tested: 31" with the table.
3. The user picks a name, or none. `desk.py pick TICKER --write` writes `trader/entry.json`
   and runs the upstream exit planner; then the chain is the upstream chain, in order:
   `timemachine.py run --write --until 15:55`, `size.py --write`, `check.py`, `ticket.py show`,
   and the user's yes. The desk never runs those for them.
4. 16:20 ET: `desk.py grade` runs `scorecard.py grade` and `report --publish`.

## Example: the reel, replayed

`desk.py replay --date 2026-10-19 --at 10:15` on the test store with the fixture edge table:
TSLA is on the board with the reel's exact signal, entry 364.18 at 10:15 AM (bar 8), stop
358.32, target 370.04, 1R, under the relative-strength breakout (`orb15_rs|r=1`, OOS n 150,
expectancy +0.20R [+0.06, +0.34]); the plain `orb15|r=1` fired on the same bar and is listed
under it. The time machine block repeats the reel: target first 29.4 %, stop first 25.5 %,
neither 45.1 %, "close to a coin flip". `pick TSLA --write` writes the entry and the upstream
plan: stop 358.32, target 370.04, flat by 15:55. At 10:50 the same signal is flagged late, 7
bars after its entry bar.

## Rules

- Repeat the board as printed; never promote a watch config or an untested one to approved.
- A candidate is not a recommendation. "Matched a rule with a measured record" is the sentence.
- Say the exclusions out loud: a name reporting tonight is on the list for a reason.
- Free Yahoo data is unofficial and can lag; the board says PARTIAL when it did not check every chart.

## What it never does

It never sizes, checks, approves or places an order (trader-budget, trader-risk and
trader-broker do, with your yes), never picks a candidate for you, never runs from a
background job into an order, and never calls a candidate a prediction.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
