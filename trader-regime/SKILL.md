---
name: trader-regime
description: >-
  Market Regime. The context around every setup, before and during the
  session: SPY against its 20- and 50-day averages (trend up, down or flat),
  the VIX and its bucket, VIX against VIX3M (backwardation), how wide
  yesterday was against the 20-day ATR; then, live at any cutoff after 9:50,
  the SPY and QQQ gap, where they sit against the 15-minute opening range and
  VWAP, the 11 sector ETFs ranked since the open, and breadth across the
  large-cap list from one spark pass. Writes trader/regime.json for the desk
  and the lab, with the same tags the lab uses for every past session (no
  look-ahead). Use when the user says "what's the regime", "market context",
  "how is the market today", "is SPY above VWAP", "is the VIX in
  backwardation", "sector leaders", "market breadth", "what kind of day is
  it", "regime tags", or before /trader-desk builds the morning plan or the
  live board.
argument-hint: "[daily [--date YYYY-MM-DD] | live [--at HH:MM] | tags DATE] [--write] [--publish]"
---

# trader-regime

The regime is the weather report around a setup. An opening-range breakout
in a quiet, trending tape and the same breakout on a gap-down day with the
VIX in backwardation are not the same trade, and the lab's tables say so
by slicing every setup's record by regime tag. This skill computes those
tags for the session ahead from the daily stores, and the intraday read at
a cutoff from live 5-minute bars. It describes what the market has done;
it never says what it will do. A regime tag is context, not a forecast and
never a buy. Day trading loses most people money; knowing the regime does
not change that, it only tells you which slice of the record applies.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/regime.py" daily                        # the next session, from the stores
python3 "${CLAUDE_SKILL_DIR}/regime.py" daily --write --publish      # save trader/regime.json and docs/data/regime.json
python3 "${CLAUDE_SKILL_DIR}/regime.py" daily --date 2026-10-23      # a past session, rows before it only
python3 "${CLAUDE_SKILL_DIR}/regime.py" live --at 10:30 --write      # the intraday read at a cutoff (Yahoo)
python3 "${CLAUDE_SKILL_DIR}/regime.py" live --at 10:30 --from-cache --now "2026-10-20 10:30"   # replay from the bar store
python3 "${CLAUDE_SKILL_DIR}/regime.py" tags 2026-10-23              # the tags the lab uses for a session date
```

Other flags: `--cache-root DIR` (the bar store, default `trader/cache`),
`--symbols-file trader/largecap.txt` (the breadth list), `--max-symbols N`,
`--offline DIR` (saved Yahoo answers: `<SYM>.json` charts and `spark.json`),
`--now "2026-10-23 10:30"` (New York time) to pin the clock, `--json`,
`--out PATH` (where `--write` puts the regime), `--quiet`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/cache/daily/SPY.json.gz`, `_VIX.json.gz`, `_VIX3M.json.gz` | the daily stores from `scripts/fetch_cache.py`: closes, highs, lows; only rows dated BEFORE the session count |
| `trader/cache/bars5m/<SYM>.json.gz` | with `--from-cache`: a stored session's 5-minute bars, cut at the cutoff, so a past day replays with no network |
| `trader/largecap.txt` | the large-cap list from trader-largecap, one `SYMBOL\|Name` a line: the breadth universe |
| Yahoo chart API | `live`: one v8 chart (`range=1d&interval=5m`, or `5d` for a day this week) each for SPY, QQQ and XLK XLF XLE XLV XLY XLP XLI XLU XLB XLRE XLC, 0.5 s apart; free, unofficial, can lag |
| Yahoo spark API | `live`: ONE pass over the large-cap list, 20 symbols a request, 5-minute closes only (no opens, no volume) |
| `trader/regime.json` | with `--write`: `{created_by, as_of, date, daily, live, tags}` (CONTRACTS.md). `live` merges into the daily block already there for the same date; `daily` keeps a live read already there for the same date |
| `docs/data/regime.json` | with `--publish`: the same document, for the dashboard's regime strip |

## How it works

1. **The daily block** for a session date uses daily rows dated strictly
   before it, so the block for Friday is what was knowable Thursday night.
   `trend` is up when the SPY close is above its 20-day average and the
   20-day is above the 50-day, down when both are the other way, flat
   otherwise. `ret5d_pct` is the close against the close five sessions
   back. The VIX close falls in one of four buckets: `<15`, `15-20`,
   `20-30`, `>30`. `backwardation` is true when the VIX closed above VIX3M
   (near-term fear above the 3-month). `prev_range_atr` is the prior
   session's SPY high minus low over its 20-day ATR: `prev_quiet` under
   0.7, `prev_wide` over 1.3, `prev_normal` between.
2. **The tags** are the daily block as words: `trend_up`, `vix_15_20`,
   `vix_backwardation`, `prev_quiet`, and so on. `dt.regime.session_tags`
   gives the lab the same list for every stored session, computed the same
   way, so the lab's `by_regime` slices and the desk's gate speak one
   language. Day type after the fact (`trend_day`) is a hindsight label the
   lab keeps apart; it is never a tag here.
3. **The live read** takes completed 5-minute bars only: a bar counts once
   its start plus five minutes is at or before the cutoff. The gap is the
   9:30 open against the prior close from the daily store (the chart's own
   `chartPreviousClose` when the store does not hold the session before).
   The 15-minute opening range is the high and low of the 9:30, 9:35 and
   9:40 bars; `above`, `inside` or `below` is the last completed close
   against it. VWAP is cumulative on typical price. Sector ETFs are ranked
   by their move since their own 9:30 open, with the move against SPY next
   to it.
4. **Breadth** comes from one spark pass, which carries closes only. So
   `breadth_above_open_pct` is the share of the list whose last completed
   close is above its 9:30 bar's close (not its open), and
   `breadth_up_pct` the share whose last close is above Yahoo's
   `chartPreviousClose`; a symbol without one is skipped and counted. VWAP
   breadth is not possible from closes, and the tool says so.
5. **Refused, in one plain line:** no SPY daily store; a date with no rows
   before it; a weekend or NYSE holiday; a live cutoff before 9:50 (the
   first bar after the 15-minute range closes at 9:50, so before that the
   range is still forming); `--at` later than now or after the close; a day
   more than six days back without `--from-cache` or `--offline`.

## Steps

1. Keep the stores fresh: `scripts/fetch_cache.py` (the 17:30 ET job) so
   the daily block has last night's close. The tool warns when the store
   does not hold the session right before the date.
2. Before the open, run `regime.py daily --write --publish`. Read back the
   trend line, the VIX line, the prior-day line and the tags, as printed.
3. After 9:50, run `regime.py live --at HH:MM --write --publish` (the desk
   runs it at 10:00, 10:15, 10:30 and 11:00). Read back SPY and QQQ (gap,
   range, VWAP), the breadth shares, the sector table, and the live labels.
4. Say which lab slice applies: the tags are the keys of the lab's
   `by_regime` table. A setup whose record in today's tags is thin or
   negative is a setup to leave alone today, not a setup to flip.
5. Say once that the regime is context and not a forecast: a quiet tape
   can turn wide at 10:05, and a tag never made a trade work.
6. Hand off to `/trader-desk` for the board. Nothing here picks a stock.

## Example: Friday 2026-10-23 on the test fixture

`regime.py daily --date 2026-10-23 --cache-root tests/fixtures/dt-cache`
(rows through Thursday 2026-10-22):

```
Regime for Fri 2026-10-23, from daily closes through Thu 2026-10-22 (SPY, ^VIX, ^VIX3M stores in tests/fixtures/dt-cache).
  SPY 763.96 below its 20-day 770.7, the 20-day above the 50-day 768.2: trend flat (mixed). 5-day return -1.01 %.
  VIX 19.03 (15-20). VIX3M 20.12: no backwardation.
  Prior session Thu 2026-10-22: SPY range 6.54, 0.91 x its 20-day ATR 7.16: normal.
  Tags: trend_flat vix_15_20 prev_normal
```

`regime.py live --at 10:30 --now "2026-10-23 10:30" --offline tests/fixtures/trader-regime
--cache-root tests/fixtures/dt-cache --symbols-file tests/fixtures/trader-regime/largecap.txt`:

```
Live read, Fri 2026-10-23 at 10:30 AM ET: 12 completed 5-minute bars, the last closing at 10:30 AM, from offline files.
  SPY 759.65: gap -0.53 % from the prior close 763.96 (daily store), inside its 15-minute opening range (759.14 to 760.77), -0.06 % vs VWAP 760.13, -0.03 % since the 9:30 open.
  QQQ 704.87: gap -0.34 % from the prior close 704.47 (daily store), above its 15-minute opening range (701.16 to 704.50), +0.14 % vs VWAP 703.90, +0.39 % since the 9:30 open.
  SPY range so far 758.68 to 761.32: 0.37 x its 20-day ATR.
  Breadth, 40 of 40 large caps with bars (spark closes only): 52.5 % above their 9:30 close, 35.0 % up on the day against the prior close.
  Sectors since the open (move %, and against SPY):
    XLE  Energy                   +1.30  (+1.33)
    XLK  Technology               +0.66  (+0.69)
    XLC  Communication Services   +0.23  (+0.26)
    XLF  Financials               -0.50  (-0.47)
    ...
    XLP  Consumer Staples         -1.18  (-1.15)
  Daily context (through 2026-10-22): trend flat, VIX 19.03 (15-20), prior day normal. Tags: trend_flat vix_15_20 prev_normal
  Live labels: spy_gap_down spy_or_inside spy_below_vwap breadth_mixed
```

The read: a gap down of half a percent that has not followed through (SPY
back inside its range, a hair under VWAP), a tech and energy tape while
staples and materials lag, two of three large caps above their 9:30 close
but only a third up on the day. The lab's `trend_flat` and `vix_15_20`
slices are the record that applies to any setup that matched a rule this
morning. None of it says where SPY closes.

## Rules

- Every number comes from the run. Repeat the lines as printed; never
  round a mixed tape into a trend.
- Only rows before the session feed the daily block. Never recompute a
  past session's tags with later data: that is look-ahead, and the lab's
  slices would be fiction.
- The live read uses completed bars only. A bar still in progress at the
  cutoff never counts, so the read at 10:30 is the same whenever it is run.
- Breadth is from closes only; say so when you say the number.
- A tag is a slice of the record, never a signal. "Matched a rule in a
  trend_up tape" is a fact; "the tape says go" is not something this skill
  can say.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, never
  tick data and never real time.

## What it never does

It never picks a stock, never scans for setups, never places or sizes an
order, never predicts a direction and never prints a P&L. It never uses a
bar that has not closed and never lets a later close into an earlier day's
tags.
