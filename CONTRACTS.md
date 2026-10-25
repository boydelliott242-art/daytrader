# CONTRACTS — the large-cap day-trading desk built on the Day Trader skill v2

The eleven upstream skills (`trader-scanner` … `trader-pelosi`) are vendored
UNCHANGED from github.com/Jakeschincariol/day-trader-skill (MIT, commit 3d0f4ef).
Their file contracts (`trader/watchlist.json`, `entry.json`, `plan.json`,
`orders.json`, `state/*.json`) are the law; everything new reads and writes
those shapes and never edits an upstream file.

Everything new is plain Python 3.8+ standard library (nothing to pip install),
lives in `dt/` (shared library) and six new skill folders, and keeps the
upstream tone: honest numbers, no predictions, nothing trades without the
user's yes.

## Folders

| folder | job |
| --- | --- |
| `dt/` | shared library: clock, Yahoo/Nasdaq fetch + cache, bars, setups, stats |
| `trader-largecap/` | the large-cap universe (Nasdaq screener market caps + sectors) |
| `trader-regime/` | market context: SPY/QQQ/VIX/sector ETFs, breadth, day type |
| `trader-lab/` | the edge study: every setup × every large cap × every cached session, walk-forward, after costs |
| `trader-desk/` | the advisor: morning plan, live candidate board, grading; drives the upstream chain |
| `trader-scorecard/` | append-only track record of every candidate the desk showed, graded against real bars |
| `trader-paper/` | local paper broker: fills brackets from Yahoo bars so the loop runs without Alpaca |
| `docs/` | static dashboard (`index.html` + `data/*.json`), served locally by `scripts/start.command` |
| `scripts/` | `fetch_cache.py` (bar store), `bg.sh` (launchd one-off jobs), `install_launchd.sh` (schedule) |
| `tests/` | upstream suite (unchanged) + `test_dt_*.py` + one file per new skill, all offline on fixtures |

## Time and calendar (`dt/clock.py`)

Mirrors the upstream tools exactly: New York time worked out from UTC with the
US DST rule, NYSE holidays and 1:00 p.m. early closes for 2026 and 2027, the
5-minute grid (slot 0 = 9:30, slot 77 = 15:55; an early close has 42 slots),
`parse_now()` accepting `"2026-10-19 10:15"` (New York) or `...Z`.

## The bar store (`trader/cache/`)

`scripts/fetch_cache.py` keeps, per symbol:

- `trader/cache/bars5m/<SYM>.json.gz`:
  `{"symbol", "interval": "5m", "source": "yahoo", "updated_at", "sessions": {"YYYY-MM-DD": [[t, o, h, l, c, v], …]}}`
  Only COMPLETE regular sessions (every slot 9:30–15:55 present, 78 rows, or 42
  on an early close); a session with a missing bar is dropped and listed in
  `trader/cache/bars5m/_dropped.json`. Rows are on the 5-minute grid, `t` is the
  bar's start in UTC epoch seconds, `v` is an int. Today's partial session is
  never stored.
- `trader/cache/daily/<SYM>.json.gz`: `{"symbol", "interval": "1d", "rows": [["YYYY-MM-DD", o, h, l, c, v], …]}`
  up to two years, regular session only (Yahoo's `1d` with adjusted closes not
  used; raw closes).
- `trader/cache/meta.json`: `{"built_at", "symbols": N, "sessions_min", "sessions_max", "first_date", "last_date"}`.

The fetcher is resumable (a symbol fetched in the last 20 hours is skipped),
polite (0.5 s between requests, query2 then query1, one retry after a 429),
and merges new sessions into an existing store so history grows past Yahoo's
60-day window over time.

Context symbols always in the store: `SPY QQQ IWM DIA ^VIX ^VIX3M XLK XLF XLE XLV XLY XLP XLI XLU XLB XLRE XLC`.

## The universe (`trader-largecap/largecap.py`)

`largecap.py build` reads Nasdaq's screener (`api.nasdaq.com/api/screener/stocks?download=true`,
with the Origin/Referer headers it wants), keeps rows whose market cap is at
least `largecap_min_cap_usd` (config, default 10e9) AND whose symbol is in the
upstream `trader/universe.txt` (so the same common-stock/ADR rule applies;
run `scan.py universe` first), and writes:

- `trader/largecap.json`: `{"created_by", "as_of", "min_cap_usd", "count", "symbols": [{"symbol", "name", "market_cap", "sector", "industry", "country", "ipo_year"}]}`
  sorted by market cap, largest first.
- `trader/largecap.txt`: one `SYMBOL|Name` a line (the shape `scan.py --watchlist` reads).

Cap tiers used everywhere: `mega` ≥ 200e9, `large` 50e9–200e9, `mid-large` 10e9–50e9.

## Bars in memory (`dt/bars.py`)

A session is a list of bars, each `{"t", "o", "h", "l", "c", "v"}`, index = slot.
Helpers: `load_store(sym)`, `sessions_of(store)` (sorted (date, bars)),
`opening_range(bars, n)`, `vwap_series(bars)` (cumulative typical-price VWAP),
`atr(bars, n)`, `rel_volume(bars, k, ref_sessions)`, `session_from_yahoo(series, day)`
(today's partial session from a live v8 chart, completed bars only, same rules
as the upstream scanner), `prev_close(daily_rows, date)`.

## Setups (`dt/setups.py`)

A setup is a pure function over one session (plus context) that returns at
most ONE signal per session (the first time its rule fires, like the upstream
entry timer), or `None`:

```
signal = {"setup": "orb15", "params": {...}, "k": 7,          # entry bar index (slot); the bar CLOSES at slot k+1's time
          "entry": 364.18,                                      # that bar's close (the plan price, like trader-entry)
          "stop": 358.32, "target": 370.04, "target_r": 1.0,
          "stop_rule": "or_mid", "entry_time": "10:15 AM",
          "opening_range": {"high":..., "low":..., "minutes": 15} | None,
          "level": float | None}
```

`ctx` carries what the rule may know AT bar k without looking ahead:
`{"prev_close", "daily_atr", "avg_vol20", "spy": session bars of SPY that day (or None), "spy_prev_close"}`.
A setup must never read `bars[k+1:]`.

Registry `SETUPS` = `{name: {"fn", "label", "grid": [param dicts], "side": "buy"}}`. The v1 library:

| name | the rule (5-minute bars, long side) | stop | default target |
| --- | --- | --- | --- |
| `orb15` | first bar after the 15-minute opening range to CLOSE above its high (the upstream rule) | or_mid | 1R / 1.5R / 2R |
| `orb30` | same with a 30-minute range | or_mid | same |
| `orb15_vol` | orb15 AND the trigger bar's volume ≥ 1.0× the range bars' average | or_mid | same |
| `orb15_rs` | orb15 AND the stock's move since the open beats SPY's by ≥ 0.3 % (relative strength) | or_mid | same |
| `orb15_gap` | orb15 AND the open gapped up ≥ 0.5 % from the prior close | or_mid | same |
| `gap_go` | gap up ≥ 1 %, the 9:30 bar closes green, entry = first later bar (by 10:30) to close above the 9:30 bar's high | 9:30 bar low | 1R / 2R |
| `vwap_reclaim` | after 10:00, price has closed below VWAP for ≥ 3 bars, then a bar closes above VWAP | min low of the last 3 bars | 1R / 2R |
| `vwap_pullback` | after 10:00, price > VWAP and VWAP rising, a bar's low touches VWAP (≤ VWAP + 0.1 × ATR) and the bar closes above VWAP | that bar's low | 1R / 2R |
| `hod_break` | after 11:00, ≥ 6 bars whose range stays inside 0.6 × ATR under the day high, then a bar closes above the day high | consolidation low | 1R / 2R |
| `pm_break` | 14:00–15:15, a bar closes above the day high (afternoon breakout) | min low of the last 6 bars | 1R / 2R |
| `vwap_revert` | after 10:30, close ≤ VWAP − 2 × stdev(5m returns so far) × price and the bar closes green | bar low − 0.5 × ATR | VWAP (R varies) |
| `inside_break` | after 10:00, an inside bar (inside the bar before), entry = next bar closes above the inside bar's high | inside bar's low | 1R / 2R |

Stops round AWAY from the entry to the cent; targets round half-up (upstream
`trader-exit` math). A signal whose risk is under 0.05 % of the price is
dropped (too tight to survive the spread, the upstream rule). Entries only
between `no_entries_before` (09:45) and `no_entries_after` (15:30); a signal at
or after `flat_by` (15:55) never exists.

## Outcomes and costs (`dt/sim.py`)

`play(bars, signal, fill="next_open", until=flat_by_slot, cost_bps=4)` walks
bars k+1 … until, stop checked before target inside a bar (a bar that touches
both counts as the stop, the upstream rule), and returns:

```
{"fill": "next_open"|"close", "entry_fill": float, "outcome": "target"|"stop"|"close",
 "exit_slot": int, "exit_price": float, "R_gross": float, "R_net": float,
 "mae_R": float, "mfe_R": float, "bars_held": int}
```

`fill="close"` enters at the signal bar's close (the plan price, the
idealised upstream number). `fill="next_open"` enters at the NEXT bar's open
(you only know a bar closed above the level once it has closed; this is the
realistic fill). The lab reports both and the desk shows both; the desk's
ranking uses `next_open`. `R` is measured against the PLAN's risk (entry
minus stop) so a worse fill shows up as a smaller R, exactly as it would in
the account. Costs: `cost_bps` round trip (default 4 basis points of the
entry: half a spread each way plus a basis point of slippage; large caps
only) subtracted from every trade, win or lose, in R. A gap through the stop
counts as a stop at the stop price here; a real fill can be worse, and the
report says so.

## Regime (`trader-regime/regime.py`)

`regime.py daily [--date D]` from the stores: SPY close vs 20/50-day SMA
(`trend`: up/down/flat), 5-day SPY return, VIX close and bucket (`<15`,
`15-20`, `20-30`, `>30`), VIX vs VIX3M (backwardation flag), prior-day SPY
range vs its 20-day ATR. `regime.py live --at HH:MM` adds the intraday read
at a cutoff, from live Yahoo bars: SPY gap %, SPY vs its 15-minute opening
range (above/inside/below), SPY vs VWAP, breadth = share of the large-cap
list above VWAP (from the live spark closes), sector ETF ranking by move since
the open. Writes `trader/regime.json`:

```
{"created_by": "trader-regime", "as_of": "...", "date": "YYYY-MM-DD",
 "daily": {"spy_close", "spy_sma20", "spy_sma50", "trend", "ret5d_pct", "vix", "vix_bucket", "vix3m", "backwardation", "prev_range_atr"},
 "live": {"at", "spy_gap_pct", "spy_or": "above|inside|below", "spy_vs_vwap_pct", "breadth_above_vwap_pct", "sectors": [{"etf", "move_pct"}]} | null,
 "tags": ["trend_up", "vix_15_20", ...]}
```

`dt/regime.py` exposes `session_tags(date, stores)` so the lab tags every past
session with the SAME daily tags, computed only from data available before
that session's open (no look-ahead). Day type after the fact (`trend_day` if
|close − open| ≥ 0.6 × range) is a hindsight label: it appears in the lab's
breakdown as `hindsight.day_type` and is never a live filter.

## The lab (`trader-lab/lab.py`)

`lab.py run [--symbols …] [--sessions N] [--fill next_open|close] [--cost-bps 4] [--workers K]`
plays every setup config over every large cap and every cached session and
writes `trader/lab/trades.jsonl.gz` (one line per trade: the signal, the
outcome, the context, the session's regime tags) and `trader/lab/edge.json`:

```
{"created_by": "trader-lab", "as_of", "fill", "cost_bps", "symbols", "sessions": {"first", "last", "n"},
 "configs_tested": N,
 "split": {"is": {"first", "last", "n"}, "oos": {"first", "last", "n"}},    # chronological 2/3 : 1/3
 "configs": [{"id": "orb15|r=1.5", "setup", "params", "label",
              "all": STATS, "is": STATS, "oos": STATS,
              "by_regime": {tag: STATS}, "by_hour": {"10": STATS, …}, "by_tier": {...}, "by_sector": {...}, "by_dow": {...},
              "status": "approved" | "watch" | "rejected"}],
 "rolling": {"window": 30, "test_days": 20, "selected_per_day": [...], "equity_R": [...], "stats": STATS},
 "note": "..."}
STATS = {"n", "target", "stop", "close", "green", "win_rate", "win_ci95": [lo, hi],   # Wilson
         "exp_R", "exp_ci95": [lo, hi],                                             # bootstrap, 2000 draws, seed 19
         "pf", "avg_win_R", "avg_loss_R", "max_dd_R", "median_R", "n_symbols", "n_sessions"}
```

Status rule (the desk's gate): `approved` when OOS n ≥ 100 AND the OOS
expectancy's 95 % CI lower bound > 0 after costs; `watch` when OOS n ≥ 30 and
OOS exp_R > 0 but the CI crosses zero; else `rejected`. `configs_tested` is
printed next to every table: with dozens of configs, something looks good by
luck. The rolling walk-forward (select on the previous 30 sessions, trade the
approved set on the next session, roll) is the honest expectation for "if I
had run the desk live over the last 20 sessions".

`lab.py report [--config ID]` prints the tables; `lab.py why SYM DATE` prints
every signal and outcome on one chart. Everything from the cache; no network.

## The desk (`trader-desk/desk.py`)

- `desk.py morning` (before the open): `largecap.py build` if stale, `regime.py daily`,
  Nasdaq earnings calendar for today and the next session → `exclusions`
  (a stock reporting today pre-market, after the close, or tomorrow pre-market
  is out: a day trade is not exposed to the report, but the open is a gap and
  the day is a news day — the user can override with `--allow-earnings`),
  the upstream `scan.py daily --watchlist trader/largecap.txt`, then `news.py`
  for the daily hits. Writes `trader/desk.json` (`mode: "morning"`).
- `desk.py live [--at HH:MM] [--top N]` (after the first bar past the range):
  fetches today's bars for the large-cap list the upstream way (spark closes
  for all, v8 chart for candidates), runs every `approved` (and `watch`, marked)
  config's rule on each chart at the cutoff, drops exclusions, applies the
  regime gate (a config whose stats in today's regime tag are negative with
  n ≥ 50 is marked `regime_against`), ranks by the lab's OOS `exp_ci95[0]`
  (the conservative bound; regime slice when n ≥ 50, else pooled), runs the
  upstream time machine on the top N (same seed 19), and writes
  `trader/desk.json`:

```
{"created_by": "trader-desk", "mode": "live", "as_of": "YYYY-MM-DD HH:MM ET", "regime": {...},
 "universe": N, "scanned": N, "excluded": {"earnings": [...], "blocked": [...]},
 "candidates": [{"rank", "symbol", "name", "sector", "tier", "setup", "config_id", "label",
                 "signal": {entry, stop, target, target_r, entry_time, k, opening_range, stop_rule},
                 "lab": {"status", "oos": STATS, "regime_slice": {tag, STATS} | null, "fill_note"},
                 "timemachine": {"p_target", "p_stop", "p_neither", "p_green", "avg_R", "history": {...}, "verdict", "seed"} | null,
                 "flags": ["late" (trigger more than one bar old), "earnings_week", "regime_against", "watch_only", "moving"],
                 "score": float}],
 "note": "Candidates matched a rule with a measured record. None is a prediction. Most people who day trade lose money."}
```
  and prints the board. Every candidate is logged to the scorecard the moment
  it is shown (`scorecard.py log --from-desk`).
- `desk.py pick SYM [--target 1.5R]` writes `trader/entry.json` (the signal,
  as `trader-entry` would) and runs the upstream `exit.py plan --write` so
  `trader/plan.json` is the upstream shape; then prints the next commands:
  `timemachine.py run --write`, `size.py --write`, `check.py`, `ticket.py show`.
  It never sizes, checks or places anything itself.
- `desk.py grade [--date D]` after the close: plays every logged candidate on
  the real bars (next-open fill and plan fill) and hands the outcomes to the
  scorecard.
- `desk.py replay --date D --at HH:MM` runs `live` on a past day from the cache (no network) so the whole board can be reproduced.

The desk NEVER places, sizes or approves an order. The chain after `pick` is
the upstream chain, unchanged: time machine → budget → risk → broker, and the
user's yes.

## The scorecard (`trader-scorecard/scorecard.py`)

`trader/scorecard/calls.jsonl` is append-only: one line per candidate shown
(`{"id", "logged_at", "date", "as_of", "symbol", "setup", "config_id", "rank", "signal", "lab_oos_exp_R", "tm_p_target"}`)
and `trader/scorecard/outcomes.jsonl` one line per graded call
(`{"id", "graded_at", "fill", "outcome", "R_net", "exit_slot", "exit_price", "late": bool}`).
A call logged after its entry bar closed by more than one bar is marked
`late` and excluded from the headline numbers (it was not knowable in time).
`scorecard.py report` prints the live record: calls, graded, win rate with
CI, expectancy, by setup, by week, the equity curve in R and in dollars at
the config's `max_loss_per_trade_usd`, and the gap between the lab's OOS
expectation and the live record. Writes `docs/data/scorecard.json`.

## The paper broker (`trader-paper/paper.py`)

For a bracket order in `trader/orders.json` that trader-risk PASSED and the
user approved through `ticket.py approve` (recorded in `trader/state/broker.json`),
`paper.py place --index N` simulates the entry fill at the next completed
5-minute bar's open (Yahoo, plus `cost_bps`/2 slippage), records it through
the upstream `ticket.py result --status placed --filled-qty --filled-price`,
and stores the open bracket in `trader/paper/positions.json`.
`paper.py sync` walks each open position's later bars: stop before target
inside a bar, fills recorded through `ticket.py fill`, and a position still
open at `flat_by` is sold at that bar's close. `paper.py status` shows the
book. It never runs without the user's yes already recorded, and it is
explicit on every line that fills are simulated from 5-minute bars.

## The dashboard (`docs/`)

Static, offline-capable, reads `docs/data/{desk,edge,regime,scorecard,largecap}.json`
written by the skills (`--publish` on desk/lab/regime/scorecard copies there).
Cool neutral monochrome, one signal colour, dark and light. Panels: today's
board, the regime strip, the edge table with CIs and `configs_tested`, the
rolling walk-forward curve, the live track record vs the lab expectation,
the fine print (verbatim upstream fine print). `scripts/start.command` serves
`docs/` at http://localhost:8765.

## Schedule (`scripts/install_launchd.sh`)

LaunchAgents, local time, weekdays: `cache` 17:30 ET (merge the day's bars),
`morning` 08:30 ET, `live` 10:00 / 10:15 / 10:30 / 11:00 ET, `grade` 16:20 ET,
`lab` Saturday 09:00 ET. Each job runs `scripts/run.sh <job>` which exits at
once outside its window and writes `trader/logs/<job>.log`. A macOS
notification on a new board (`osascript`). Nothing scheduled ever places an
order (nobody is there to say yes).

## Tests

Offline, on fixtures cut from the real cache (a dozen symbols × 60 sessions,
gz), `python3 -m unittest discover -s tests -v`. Every new module has a test
file; every setup has a hand-built session that fires it and one that does
not; `dt/sim.py` has stop-before-target and gap cases; the lab's stats have
known-answer tests (Wilson, bootstrap seed 19); the desk has a replay test
that reproduces a board from fixtures; the scorecard refuses a late call.

## Words

Say "matched a rule", never "a buy". Numbers come from the run; CIs are
printed with every rate. "Approved" means the OOS record cleared the gate,
not that the trade wins. Most people who day trade lose money; the desk does
not change that, it only shows the odds before the money.
