# CONTRACTS: the large-cap day-trading desk built on the Day Trader skill v2

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
range (above/inside/below), SPY vs VWAP, breadth = the share of the large-cap
list whose last close is above its 9:30 open, and the share up on the day against the
prior close (spark closes carry no volume, so VWAP breadth is not possible), sector ETF ranking by move since
the open. Writes `trader/regime.json`:

```
{"created_by": "trader-regime", "as_of": "...", "date": "YYYY-MM-DD",
 "daily": {"spy_close", "spy_sma20", "spy_sma50", "trend", "ret5d_pct", "vix", "vix_bucket", "vix3m", "backwardation", "prev_range_atr"},
 "live": {"at", "spy_gap_pct", "spy_or": "above|inside|below", "spy_vs_vwap_pct", "breadth_above_open_pct", "breadth_up_pct", "sectors": [{"etf", "move_pct"}]} | null,
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
  the day is a news day : the user can override with `--allow-earnings`),
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

## The dashboard (`site/`) and the lock (`docs/`)

The pages live in `site/`: `index.html` (one idea in big words) and `full.html`
(every table). They read `trader/publish/{desk,edge,regime,scorecard,largecap,
montecarlo,nq}.json`, written by the skills (`--publish`), and behind the lock
they read the same documents from memory (`window.__DT`, set by the lock
screen). Cool neutral monochrome, one signal colour, dark and light. Panels:
today's board, the regime strip, the edge table with CIs and `configs_tested`,
the rolling walk-forward curve, the live track record vs the lab expectation,
the fine print (verbatim upstream fine print).

`docs/` is what GitHub Pages serves, and it holds nothing in the clear:
`index.html` (the lock screen), `404.html`, `.nojekyll` and
`data/bundle.enc`. `scripts/lock.py build` seals the two pages and the
published data into the bundle:

    bytes 0..8    magic  "DTLOCK1\0"
    bytes 8..12   PBKDF2-HMAC-SHA256 iterations, big-endian uint32 (4,000,000; floor 600,000)
    bytes 12..28  salt, 16 random bytes, new every build
    bytes 28..40  nonce, 12 random bytes, new every build
    bytes 40..    AES-256-GCM ciphertext || 16-byte tag; the 40 header bytes are the additional data
    plaintext     gzip of {"format": "daytrader-site/1", "built", "built_et", "pages": {"idea", "full"}, "data": {...}}

`dt/aesgcm.py` is AES-GCM in the standard library (FIPS 197 and the GCM
specification's test cases in `tests/test_lock.py`); `dt/lockbox.py` is the
format and the passphrase lookup. The lock screen derives the key in the
browser's WebCrypto and opens the bundle there; a wrong passphrase derives a
wrong key and the tag fails, so there is no check to bypass. Nothing is kept
in the browser: the bundle is held in memory only, and the screen locks on
reload, on `#lock`, when the tab is hidden, when the page comes back from the
back-forward cache, and after ten idle minutes. The passphrase is read from
`DAYTRADER_SITE_PASSPHRASE`, the login Keychain (account `daytrader`, service
`daytrader-site-passphrase`) or `trader/site.pass`, and is never written,
printed or logged; a build that cannot find it, or whose bundle does not
reopen, publishes nothing. `scripts/run.sh publish_docs` builds the bundle and
pushes only `docs/data/bundle.enc`. `scripts/lock.py check-url` proves the
live site serves the same bundle and nothing in the clear. The bundle is
public, so guesses run offline at full speed: the passphrase is the whole
defence, and four or five random words is the honest floor.
`scripts/start.command` serves `docs/` at http://localhost:8765 (the lock
screen; the only place the passphrase is ever typed by a script or a test).

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

## Added after the first build: intel, montecarlo, nq

| folder | job |
| --- | --- |
| `trader-intel/` | the connector-fed ledger `trader/intel.json` (items with source, url, UTC time; derived earnings and macro maps) and `dt/intel_flags.py` (`flags_for(symbol, date, ledger, now)`: earnings_today, earnings_tomorrow, earnings_week, macro_high_today, news_negative_24h, news_positive_24h, halt_or_risk_24h, analyst_action_24h, insider_selling_7d, social_spike, catalyst). The desk adds them to each candidate's flags as `intel:<flag>`. The Python never calls a connector: Claude does, and writes the items to a file. |
| `trader-montecarlo/` | `plan`: the upstream time machine at 26,000 replays with Wilson intervals and the reel's 10,000 beside it, `trader/montecarlo.json`; `book`: the lab's trade book resampled 26,000 times by session at the sizing rules (max loss a trade, trades a day, the daily loss limit), window and horizon percentiles, drawdown odds; `futures`: NQ=F and ES=F overnight gap and range with tags `nq_gap_up|down|flat`, `nq_wide|quiet|normal_overnight` (and `es_*`), `trader/futures.json`, which `regime.py live` folds into the day's tags when it is for the same date. |
| `trader-nq/` | `nq.py` (stdlib) drives `nq_adapter.py` under the user's NQ backtester venv (`~/Claude/Projects/nq-backtester`, or `nq_backtester_path` in the config): the desk's setups on 15 years of 1-minute NQ bars aggregated to 5-minute RTH sessions, one market order at the signal bar's last minute (the engine fills at the next minute's open with its slippage and fees), results in the lab's shapes in `trader/nq/edge.json` and `trades.jsonl.gz` (status by the same gate, by year, a rolling walk-forward by year, the engine's own stats, a two-engine agreement rate against dt.sim); `overnight.json` (15 years of gaps and the ORB after them); `session_tags.json` (the nq tags per session, which the lab merges into its regime tags when present). `orb15_rs` is not applicable on NQ (it needs SPY). |

The desk shows the watch list, every candidate flagged `watch_only`, when the lab approved no config at all,
and says so; an empty lab table is an honest board, not a broken one.

## The research desk: the TradingAgents integration (added 2026-10-25)

Everything below ADDS to the desk. No existing file shape, flag, command or number changes; the long-side
lab results stay bit-identical (tests pin them). Attribution: `vendor/TradingAgents/` is Tauric Research's
TradingAgents v0.7.0 (Apache-2.0, arXiv 2412.20138), vendored whole and unchanged with its LICENSE; the roles,
the debate order, the rating scale and the evidence-first rules below are adapted from it and say so.

### Folders

| folder | job |
| --- | --- |
| `vendor/TradingAgents/` | the upstream repository, verbatim, pinned in `vendor/TradingAgents/VENDORED.md`. Runs in its own venv (Python 3.11+, `scripts/install_tradingagents.sh`) with a provider key; `trader-agents/agents.py run --engine vendored` drives it and imports its reports. Never edited; never imported by the stdlib tools. |
| `trader-agents/` | the research desk: `agents.py` (prepare, validate, decide, card, rank, settle, reflect, run), `roles/*.md` (one prompt file per role, adapted from upstream, with the output contract), `weights.json` (the scoring weights, documented), `SKILL.md`. |
| `dt/features.py` | the momentum and structure features from daily and 5-minute bars, as of a date (no look-ahead). |
| `dt/shorts.py` | the bearish setup library (same shape as `dt/setups.py`, `side: "short"`). |
| `dt/regimes.py` | the seven regime classes on top of the existing tags. |
| `dt/catalysts.py` | KNOWN / POSSIBLE / NONE from the intel ledger, the earnings calendar and the bars. |
| `dt/risk.py` | the risk block per candidate and side, and the do-not-trade verdicts. |
| `dt/evidence.py` | the evidence pack: one JSON of everything known about a symbol as of a date, every item with an id. |
| `dt/scoring.py` | the scores (0 to 100) and the long/short/confidence formulas from `weights.json`. |
| `trader/research/<date>/<SYM>/` | one run: `evidence.json`, `prompts/<role>.md`, `outputs/<role>.json`, `card.json`, `card.md`. |
| `trader/research/memory.jsonl` | the settled cards and their reflections (the upstream memory log, in our shape). |
| `trader/rankings.json`, `trader/research/latest.json` | what the site shows (`--publish` copies to `trader/publish/`). |

### Sides

Every signal, trade, config and stat carries `side`: `"long"` (the existing library, unchanged, default when
absent) or `"short"` (`dt/shorts.py`). A short signal has `entry` (the bar's close), `stop` ABOVE the entry
(rounded UP to the cent), `target` BELOW (rounded down), `risk_per_share = stop - entry`, the same
`MIN_RISK_FRAC` floor. `dt/sim.play` reads `signal.get("side")`: for a short the stop is hit when `h >= stop`
(checked first), the target when `l <= target`, R is `(entry_fill - exit) / risk`, `mae_R` from the worst HIGH,
`mfe_R` from the best LOW; costs are the same bps plus `borrow_bps_day` (default 0, an ASSUMPTION the report
names) for the bars held. Short config ids end in `|short` so no id collides with a long one. The lab plays
both libraries and reports them in separate tables (`edge.json` gains `"sides": {"long": [...ids], "short":
[...ids]}`; the gate is the same; `configs_tested` counts both). The desk runs a long board and a short board,
never merges them, and ranks each by its own side's OOS record. Nothing places a short order: the upstream
risk manager blocks short sales unless the user's config allows them, and `paper.py place` refuses a short
unless `allow_short` is true. A short candidate is research until then.

Bearish setups (first bar in the window whose CLOSE confirms, like the long side): `orb15_down` /
`orb30_down` (close below the range low; stop at the range midpoint or high), `vwap_reject` (3+ bars under
VWAP, a bar closes above it, the next closes back below: stop above that bar's high), `failed_breakout`
(a close above the range high, then a close back below the range high: stop above the day high),
`gap_fade` (gap up 1%+, a red first bar, a close below the first bar's low by 10:30), `lod_break` (low of
day broken after a tight consolidation, after 11:00), `pm_breakdown` (2:00 to 3:15, a close below the day
low). Target multiples 1 / 1.5 / 2 R as on the long side. 19 configs (6 orb15_down, 3 orb30_down, 2 each of the rest).

### Features (`dt/features.py`)

`features_for(sym, date, daily_rows, sessions=None, spy_rows=None, sector_rows=None)` -> dict, every value
from rows strictly before `date` (a session's own bars only through `features_intraday(bars, k)` for the live
board). Keys: `roc_5, roc_20, roc_63, roc_126, roc_252` (percent), `rs_spy_20, rs_spy_63, rs_sector_20`
(relative return, percent), `ma_structure` (`"up"|"down"|"mixed"`: close vs SMA20 vs SMA50 vs SMA200),
`sma20_slope_pct, sma50_slope_pct` (10-day), `adx_14, rsi_14, macd, macd_signal, macd_hist, atr_14,
atr_pct, realized_vol_20` (annualised percent), `relvol_20` (yesterday's volume over the 20-day average),
`vol_accel` (5-day over 20-day average volume), `updown_vol_ratio_20`, `gap_pct` (open vs prior close, for the
session when bars are given), `dist_52w_high_pct, dist_52w_low_pct`, `breakout_20d` (close above the 20-day
high), `breakdown_20d`, `up_days_20` (fraction), `trend_r2_63` (R-squared of log price on time),
`weekly_roc_4, weekly_roc_13, weekly_structure`, `vol_adj_mom_63` (roc_63 over realized vol),
`adv_usd_20`, `beta_spy_63`, `mtf_agreement` (`"agree_up"|"agree_down"|"short_term_up_long_term_down"|
"short_term_down_long_term_up"|"mixed"`). Anything that cannot be computed is `None`, never a guess, and
`features["missing"]` lists the keys.

The lab's feature study (`lab.py features`): for every config with 30+ OOS trades, bucket its OOS trades by
each feature's terciles (computed over that config's trades) and print expectancy with intervals per
tercile; `adds_value` when the top and bottom tercile intervals do not overlap and the middle sits between;
a Spearman correlation matrix over the trade set with every pair above 0.8 named as redundant. Written to
`trader/lab/features.json`. The study is the only evidence a feature is allowed to carry weight.

### Regime classes (`dt/regimes.py`)

`classify(daily_context, breadth=None, drawdown_pct=None)` -> one of `strong_bull, weak_bull, range,
high_vol, bear, crisis, transitional` with the rule that fired, from rows strictly before the date:
`crisis` when VIX >= 30 or the 20-day drawdown from the 52-week high exceeds 15 percent and the trend is down;
`high_vol` when VIX >= 25 or realised vol is above its 80th percentile; `strong_bull` when the trend is up,
SMA50 is rising and breadth (share of large caps above their SMA50, when known) is above 60 percent;
`weak_bull` when the trend is up otherwise; `bear` when the trend is down; `transitional` when the SMA20 crossed
the SMA50 within the last 10 sessions; `range` otherwise. `regime.py` adds `"class"` and the `regime_<class>`
tag; the lab slices by it; the desk's confidence adjusts: a short in `strong_bull` and a long in `bear` or
`crisis` carry the flag `regime_against` and their confidence falls one grade.

### Catalysts (`dt/catalysts.py`)

`catalysts_for(sym, date, ledger, earnings_map, daily_rows)` -> `{"known": [...], "possible": [...],
"none": bool}`; a KNOWN catalyst has a date and a source url (earnings from the calendar, a dated filing,
a HIGH-impact macro print, an unusual price or volume event measured on the bars: relvol >= 3 or a gap of
5 percent or more); a POSSIBLE one is an undated mention in the ledger. Nothing is invented: no ledger, no
calendar, no bars means `none: true`, and the card says "no identified catalyst".

### Risk (`dt/risk.py`)

`risk_block(sym, side, date, features, signal, catalysts, regime_class, intel)` -> `{"atr_pct",
"adv_usd", "liquidity": "deep|ok|thin", "gap_risk_pct_95" (the 95th percentile overnight gap over 250
sessions), "event_risk": {...earnings within 5 sessions, macro HIGH today}, "beta_spy", "sector",
"regime_risk", "squeeze_risk": "unknown"|"low"|"high", "short_interest": value|None, "days_to_cover":
value|None, "crowding": "unknown", "borrow": "unknown", "invalidation": the stop and the level,
"asymmetric_event": bool, "verdict": "ok"|"caution"|"do_not_trade", "reasons": [...]}`. The verdict is
`do_not_trade` for a short when: the stock is up 20 percent or more in 5 sessions on 3x relative volume
(parabolic), or earnings fall within the next session, or ADV is under 5 million dollars, or
`short_interest` is known and above 20 percent of the float, or the regime is `strong_bull` AND the setup's
OOS record in that regime is not positive; for a long when: earnings fall within the next session, the
regime is `crisis`, or ADV is under 5 million dollars. `caution` when squeeze risk is unknown for a short,
the gap risk exceeds twice the stop distance, or the MTF read disagrees. Every "unknown" is printed as
unknown; the reasons name which data was unavailable.

### The evidence pack (`dt/evidence.py`)

`build(sym, date, root, now, depth)` -> `trader/research/<date>/<SYM>/evidence.json`:

```
{"format": "evidence/1", "symbol", "date", "as_of", "built", "depth": "fast|standard|deep|maximum",
 "identity": {"name", "sector", "industry", "tier", "market_cap"},
 "snapshot": {"last_close", "last_session": {o,h,l,c,v,date}, "sma20", "sma50", "sma200", "rsi_14", "atr_14",
              "atr_pct", "avg_vol_20", "relvol", "high_52w", "low_52w", "gap_pct", "intraday": {...} | null},
 "features": {... dt.features ...},
 "regime": {"date", "class", "rule", "tags", "daily": {...}, "live": {...} | null},
 "setups": {"long": [{config_id, signal, lab: {status, oos, regime_slice}}], "short": [...]},
 "lab": {"approved": [...], "watch": [...], "configs_tested": n, "sides": {...}},
 "scorecard": {"symbol": STATS, "overall": STATS, "recent": [...]},
 "catalysts": {...}, "risk": {"long": {...}, "short": {...}},
 "fundamentals": {... trader-analyst's EDGAR summary when cached ...} | null,
 "news": [... trader-news items for the symbol ...], "intel": [... ledger items ...],
 "social": [...], "macro": [...], "futures": {...} | null, "montecarlo": {...} | null,
 "lessons": [{"id", "date", "symbol", "rating", "outcome", "reflection"}],
 "items": [{"id": "E1", "kind": "DATA|SIGNAL|STAT|NEWS|FILING|EVENT|SOCIAL|MACRO|REGIME|RECORD|LESSON",
            "source": "...", "as_of": "...", "text": "...", "value": {...}}],
 "missing": ["short_interest", "borrow_fee", "fundamentals", ...]}
```

Every section above is also flattened into `items`, each with an id, so a role can cite `E17`. Items dated
after `date` are never included (point in time, as the upstream clamps every tool to the trade date).
`missing` names every source that was not available, by name, so a role can say so instead of guessing.

### The roles (`trader-agents/roles/*.md`, `outputs/<role>.json`)

The order, adapted from the upstream graph, with the two roles the pasted brief adds (momentum, regime) and
the contrarian:

```
analysts   market, momentum, fundamentals, news, sentiment, regime        (independent; any order)
research   bull_1, bear_1 [, bull_2, bear_2, bull_3, bear_3], contrarian   (each reads the prior arguments)
manager    research_manager                                                (the judge: a plan)
trader     trader                                                          (the proposal, absolute levels)
risk       risk_aggressive, risk_conservative, risk_neutral [, round 2, 3], risk_analyst (code-fed)
decision   portfolio_manager                                               (the rating, with the lessons)
```

Depth: `fast` = market, momentum, regime, bull_1, bear_1, research_manager, risk_analyst, portfolio_manager;
`standard` = every analyst, 1 debate round, 1 risk round; `deep` = 2 rounds each plus the contrarian;
`maximum` = 3 rounds each, the contrarian, the time machine and the Monte Carlo on the proposal, and the
intel ledger refreshed first (8, 15, 21 and 26 outputs). Every role's output is JSON:

```
{"role": "bull", "round": 1, "symbol", "date", "stance": <role enum>, "confidence": "low|medium|high",
 "claims": [{"text": "...", "kind": "DATA|SIGNAL|INTERPRETATION|ASSUMPTION|UNCERTAINTY",
             "evidence": ["E3", "E7"]}],
 "rebuttals": [{"to": "bear_1", "text": "...", "evidence": [...]}],      (research and risk roles)
 "scores": {... role-specific 0 to 10 ...}, "missing": ["..."], "summary": "..."}
```

Role enums: analysts `stance in bullish|mildly_bullish|neutral|mixed|mildly_bearish|bearish` (the upstream
sentiment bands, used by every analyst); researchers the same plus `strength 1..5`; research_manager
`recommendation in buy|overweight|hold|underweight|sell` (the upstream five tiers) plus `side_preference in
long|short|none`, `plan`; trader `action in long|short|hold`, `entry`, `stop`, `target` (absolute prices,
never a percentage, grounded in a cited SIGNAL item or `null`), `size_note`; risk roles `stance in
take|reduce|avoid` with `adjustments`; portfolio_manager `rating` (five tiers), `side`, `horizon in
intraday|swing`, `thesis`, `invalidation`, `key_risks`, `no_trade: bool`.

The validator (`agents.py validate`) rejects an output when: a required key is missing or an enum is wrong;
a claim of kind DATA, SIGNAL or STAT cites no evidence id or cites one not in the pack; a claim quotes a
number that appears in no cited item's text or value (digits compared after rounding to 2 places);
confidence is `high` while `missing` (the pack's or the role's) names a core source for that role
(fundamentals for the fundamentals analyst, news for news, social for sentiment, short_interest for a
short risk read); the trader gives a level that is not within 2 percent of a cited SIGNAL's level; a
researcher in round 2+ has no rebuttal to the opponent's last round; the portfolio manager's rating
disagrees in direction with the trader's action without a claim that says why. A rejected output is
rewritten by the role, never patched by code. `REVIEW` (the upstream sentinel) is the card's rating when
the final output never validates: a card nobody can read is not a Hold.

### Scores (`dt/scoring.py`, `trader-agents/weights.json`)

All 0 to 100, computed by code from the pack and the validated outputs, every input named:
`momentum` (features: roc, rs, structure, slopes, adx, persistence, vol-adjusted, breakout; each term only
when the feature study says it adds value, else weight 0 and the card says so), `technical` (the setup's
signal quality: lab OOS lower bound, time of day, volume confirmation, MTF agreement), `fundamental`
(the analyst's stance and the EDGAR deltas when present, else `null`), `sentiment` (the analyst's score),
`catalyst` (known 100 / possible 50 / none 0, signed by direction), `regime` (fit of the side to the class),
`risk` (100 is the safest), `liquidity`, `long_opportunity`, `short_opportunity`, `confidence`.
`long_opportunity` and `short_opportunity` have SEPARATE weight vectors in `weights.json` (default weights
documented with their reason; never the negation of the other); `confidence` = evidence completeness (share
of core sources present) x agreement (the share of roles whose stance points the card's way) x the lab's
record grade (approved 1.0, watch 0.6, untested 0.3), capped at `medium` when the regime is against or a
core source is missing. `agents.py weights-test` scores every OOS trade of the lab with the code-only
terms (the agent terms are not available historically, and the card says so) and prints expectancy by
score quintile with intervals: the only ground a weight is allowed to stand on.

### Rankings (`agents.py rank`, `trader/rankings.json`)

Over the universe, from the code-only layers (features, setups, lab, risk, catalysts; the agents run on the
top names only): `top_momentum_longs`, `top_short_candidates`, `top_high_conviction`, `top_risk_adjusted`,
`top_contrarian` (MTF disagreement with the regime on the candidate's side), `most_dangerous_shorts`
(high short score with a `do_not_trade` verdict), `watchlist`, each a list of `{symbol, side, score,
config_id, verdict, reasons}`; `"no_trade": true` with the reason when a list has nothing that clears the
bar. Rankings carry `configs_tested` and the lab's dates like every table.

### The card (`card.json`, `card.md`)

Sections in this order, each line tagged DATA / SIGNAL / INTERPRETATION / ASSUMPTION / UNCERTAINTY and
citing its evidence ids: TICKER, PRICE / DATA SNAPSHOT, MARKET REGIME, MOMENTUM, TECHNICAL, FUNDAMENTAL,
NEWS / SENTIMENT, CATALYSTS, BULL CASE, BEAR CASE, CONTRARIAN CASE, RISK, LONG SCORE, SHORT SCORE,
CONFIDENCE, BEST SETUP (or NO TRADE), THESIS, INVALIDATION, KEY RISKS, EVIDENCE (the cited items). A card is
logged to `trader/research/memory.jsonl` the minute it is written (`pending`), like a scorecard call.

### Memory (`agents.py settle`, `agents.py reflect`)

A card with horizon `intraday` is settled by the scorecard's grade of its setup (the real bars); a card with
horizon `swing` is settled at the first close after its date over 5 sessions (the upstream rule, #1518),
raw return and alpha against SPY from the daily store, `resolved` at the last bar's date. `reflect` is a
role: 2 to 4 sentences from the outcome, stored verbatim; `prepare` puts the last 5 same-symbol and 3
cross-symbol settled cards whose `resolved` date is on or before the run date into the pack as LESSON items
(point in time, #1251).

### The vendored engine (`agents.py run --engine vendored`)

`scripts/install_tradingagents.sh` makes `vendor/TradingAgents/.venv` with Python 3.11+ when one exists (or
`uv` can fetch one) and `pip install -e vendor/TradingAgents`; `agents.py doctor` says what is present:
the Python, the venv, the provider keys by NAME only (never a value), `~/.tradingagents` and its memory log.
`run --engine vendored` calls `trader-agents/vendored_driver.py` under that venv: `TradingAgentsGraph(
config).propagate(sym, date)`, then writes the four reports, the debate histories, the plan, the proposal,
the risk histories and the decision into `outputs/vendored.json` and adds them to the pack as items
(`source: "tradingagents v0.7.0"`), so the desk's roles can cite them. Without the Python or a key it
prints the blocker and exits 2; nothing is faked.

### Site

`site/full.html` gains panels: the short board, the rankings, the latest cards (rating, side, confidence,
verdict, the thesis line) and the feature study; `data/rankings.json` and `data/research.json` join the
sealed bundle (`dt/lockbox.DATA_FILES`). The one-idea page keeps the long idea and adds one line for the
day's top short candidate when its verdict is `ok`, with the same words: matched a rule, not a prediction.

### Words

A card "matched rules with a measured record and was read by the desk's roles". A score is a number from
named inputs, not a probability. "Unknown" is printed as unknown. NO TRADE is a result. Most people who
day trade lose money; shorting adds the way to lose more than the stake, and the card says so on every
short.

## Export for the unified research layer (2026-10-25)

`python3 scripts/export_unified.py --to <path>` copies every JSON under `trader/publish/` into one bundle
(`{"source": "daytrader", "exported_at", "publish_dir", "files": {name: contents}, "skipped": [...]}`), written
atomically to the path given. Read-only, standard library, no network. The unified layer at
`~/Desktop/trading-unified` reads it on its own schedule; nothing in this repo depends on it.

## The morning pick (`trader-pick/`, `dt/pick.py`; added 2026-10-27)

One large cap a trading morning, posted to the site by 8:25 ET, ranked from what is known by 8:05 ET, with its measured
record printed beside the base rates. A pick is a count, not a prediction, and never a buy.

**Target.** The session's own open-to-close move (what a buyer at the open gets): UP = the close at least 2 percent
above the open, DN = at least 2 percent below; the 2 to 5 percent band, the touch of +2 and -2 percent (high and low
against the open) and close-to-close are reported beside it.

**Rows.** For every name a large-cap list has held (`trader/largecap.json` and the dated copies the evening cache job
keeps in `trader/largecap_history/`) and every session in the daily store with 25 sessions before it: features from rows
strictly before the session (`symbol_table`: ATR as a share of price, yesterday's move, relative volume, close location
and gap, the 5 and 20-day moves, the distance to the 20-day high and low, RSI 14, 20-day realised volatility, the stock's
own share of UP and DN sessions over 60), the market's context from rows strictly before the session (`market_table`:
SPY's 1 and 5-day moves and trend, the VIX, the weekday) and the NQ futures gap at 8:05 ET (`nq.py session-tags --at
07:05`, minute bars from the user's backtester; live from `trader/futures.json`). Left out of training, scoring and the
live pick alike (`earnings_session`, `special_of`, `HALF_DAYS`, `universe_caps`):

- an earnings session: the name reports on the session (Nasdaq's calendar, `scripts/fetch_earnings_history.py` ->
  `trader/cache/earnings/D.json`, every calendar day including weekends and holidays), or it reported on the session
  before or on a day with no session in between (a Saturday report). The report time plays no part: Nasdaq drops the
  times of past dates, so the history cannot tell a report after the close from one before the open, and the rule the
  record measured is the one the live pick applies;
- a special situation: the name moved 20 percent or more close to close on the session before (news, a deal, a crash,
  or a split the store has not adjusted for), a day its history does not describe;
- a half day (a 1:00 p.m. close): the live pick posts no pick on one;
- a session before the name was a large cap: its estimated cap at the prior close (its cap at the newest list that holds
  it x the close then / the price at that list) under $10B. A name that left the list keeps its history for the
  sessions it qualified. (Share counts are taken as constant; names that left before the lists were kept cannot be
  restored.)

These rules were set before they were measured and are not tuned; the training output counts what each left out.

**Scorers.** Every feature is bucketed at fixed cut points (`CUTS`, `CATEGORICAL`; `na` is its own bucket). From the bucket
counts: the up lift log(rate_up(bucket) / base_up), the down lift likewise, and the expected-move deviation, all smoothed
toward the base with 20 pseudo-rows. A name's score is the sum over its buckets; the calibrated odds are the observed rate
of the training score's bin (20 equal bins on a 1-in-10 sample of the training rows). No weights to tune. The calibrated
odds are in-sample and every name in a bin shares them, so the card never prints them as a name's odds; the summed
expected-move score (`e_oc`) is not calibrated and ranks only the research rule `ret`: it is never printed as an expected
move.

**Rules.** `up` ranks by the up odds, ties by the raw score (floor: 1.5 x the base rate); `net` by up odds minus down odds
(floor: 2 points); `ret` by the expected-move score (floor: +0.10). The liquid universe is 20-day average dollar volume of
20 million or more. A morning whose top name misses the floor is NO PICK; the record counts how often that happened (it
has not happened yet), and the card's words say so.

**Walk-forward.** After 120 training sessions the model is refit every 20 on everything before and scores the next 20; a
session's rows join the counts only after it is scored. The record per rule: the pick and the top five, split into the two
halves of the out-of-sample window (`h1`, `h2`), beside the base rate of every liquid name and the highest-ATR name each
morning, and every rate carries an interval: the UP and DN rates, the move either way, the 2 to 5 percent band, the touch
of +2 and -2 percent, and the mean open-to-close. One record a morning: Wilson intervals and a bootstrap of the mean. Several
records a morning (the base rate, the top five): whole mornings are resampled, because the names of one morning share the
market's move. Anything chosen on `h1` (a rule, a feature) is believed only if `h2` agrees.

**The claim.** The card says "likely to close 2 percent ABOVE its open" only when the live rule's pick closed UP more often
than DN with separated Wilson intervals in both halves (`claim_of`); otherwise "likely to MOVE 2 percent from its open",
with the move rate of past picks, then "they closed 2 percent down more often than up" when the down interval sits above
the up interval, or "up or down, the record cannot tell" (`direction_kind`). Never "most likely": the most volatile
liquid name moved at least as often. The card's odds are the record of past picks with their intervals, beside the base
rates and the highest-ATR name.

**The morning's own rules.** A name whose store is not through the previous session is left out and counted; if SPY is
behind, or more than 5 percent of the names are, the store is stale and the card is NO PICK. A futures file without a
Nasdaq read is no gap, not a crash. A card written after 9:25 ET on its date is posted marked late and never logged. A
second run on a date keeps the pick already logged (the card and the record always name the same stock); `--replace`
posts another and marks the first superseded, never graded. A morning that fails posts a dated NO PICK card with the
reason, so the site never says "Not yet" all day. The shadow: the most volatile eligible liquid name, logged to
`trader/pick/shadow.jsonl` and graded each evening, the one lead the rule research left (it touched +2 percent more often
than the pick in both halves, inside the noise in the second); never the pick, and it joins nothing except through
`scripts/pick_research.py`.

**Research.** `scripts/pick_research.py` evaluates an extra feature module (`CUTS`, `compute(sym, rows, ctx)`) against the
cached base rows and runs `dt.pick.leak_check` (recompute sampled dates with the rows cut there; any difference is a LEAK).
`pick.py train --extra MODULE` makes a confirmed module part of the live model.

**Files.** `trader/pick/model.json` (spec, lifts, calibration, rule, claim, the universe rule, the walk-forward record),
`trader/pick.json` (today's card: the pick, the record's odds, the reasons, the pre-market print at the last bar that ended
by the cutoff, shown and never scored, the next nine, the direction, every rule's record, the live record, the shadow, and
what was left out this morning: who reports today, who reacts to a report since the previous session, each special
situation with its reason, the names a session behind), `trader/pick/record.jsonl` (every pick logged pending the morning
it was made, graded on the real open, high, low and close by the evening cache job), `trader/pick/shadow.jsonl`,
`trader/publish/pick.json` (sealed into the site's bundle; before the first morning it carries the record),
`trader/largecap_history/D.json` (each evening's list).

**Calendar files.** A refresh never makes a file worse: an empty answer does not replace a file that has rows, and a
report time on disk survives a newer answer without one. On the morning, a calendar with no rows for a trading day is an
answer that failed: the intel ledger stands in, and the card says so.

**Schedule.** `pick` at 08:08 ET and `pick2` at 08:18 ET (`scripts/run.sh pick`): the calendars from the previous session
to today, a fresh futures read unless today's file has a Nasdaq gap, `pick.py morning --write --publish`, seal and push,
then the pre-market history for research. A retry does nothing when today's pick is on origin, and only seals and pushes
when today's card exists but did not land. Every job runs under `caffeinate` (the Mac stays awake while it runs), waits up
to a minute for the network, holds a lock that names its pid (a lock whose pid is gone or that is over four hours old is
broken and logged), and publishes only from `main` and only the bundle. The 17:30 cache job grades the pick and the shadow
and refreshes the calendar; the Saturday lab job retrains.

**Words.** Most people who day trade lose money. A stock that moves 2 percent one morning in three does not move two
mornings in three, and the ones that move up are matched by the ones that move down until a record says otherwise.
