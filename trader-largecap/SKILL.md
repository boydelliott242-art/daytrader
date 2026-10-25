---
name: trader-largecap
description: >-
  The large-cap universe. Builds the list the desk and the lab work from: every US-listed
  common stock, ordinary share or ADR with a market cap at or above $10 billion, from
  Nasdaq's screener (market cap, sector, industry, country), kept only when the upstream
  trader-scanner universe rule also keeps it. Writes trader/largecap.json (names, caps,
  tiers, sectors) and trader/largecap.txt (the SYMBOL|Name list scan.py reads). Use when the
  user says "build the universe", "which large caps", "is X a large cap", "what tier is
  NVDA", "show me the mega caps", "refresh the list", "sector breakdown", or before the
  first /trader-lab run or /trader-desk morning.
argument-hint: "[build [--min-cap 10e9] | show [--tier mega] [--sector NAME] | check TICKER]"
---

# trader-largecap

The desk trades large caps only: the names with the depth to fill a bracket near its price
and the data to measure a setup on. This skill draws that line. It reads Nasdaq's screener
(every US-listed stock with its market cap, sector and industry), keeps a row only when its
cap clears the floor AND the upstream common-stock rule keeps the symbol (so the same list
`scan.py universe` built decides what counts as a stock), and writes the list in two shapes:
JSON for the lab and the desk, and plain `SYMBOL|Name` lines for `scan.py --watchlist`. A
list is a list. Being on it means the lab measures the name; it says nothing about the name
today. Most people who day trade lose money, and a tidy universe does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/largecap.py" build --write                 # Nasdaq screener -> trader/largecap.json + .txt
python3 "${CLAUDE_SKILL_DIR}/largecap.py" build --min-cap 50e9 --json   # a higher floor
python3 "${CLAUDE_SKILL_DIR}/largecap.py" show --tier mega --top 20
python3 "${CLAUDE_SKILL_DIR}/largecap.py" show --sector Technology
python3 "${CLAUDE_SKILL_DIR}/largecap.py" check NVDA
```

Flags: `--universe trader/universe.txt` (the upstream list; `none` takes the screener
alone), `--offline screener.json` (a saved screener answer, for tests), `--config
trader/config.json` (`largecap_min_cap_usd`), `--largecap PATH` for show and check, `--now`.

## What it reads and writes

| file | what |
| --- | --- |
| Nasdaq screener | `api.nasdaq.com/api/screener/stocks?download=true`: symbol, name, last sale, volume, market cap, sector, industry, country, IPO year |
| `trader/universe.txt` | the upstream scanner's list (`scan.py universe`): the common-stock / ADR rule; run it first |
| `trader/config.json` | `largecap_min_cap_usd` (default 10e9) |
| `trader/largecap.json` | `{created_by, as_of, min_cap_usd, count, symbols: [{symbol, name, market_cap, tier, sector, industry, country, ipo_year}]}`, largest first |
| `trader/largecap.txt` | one `SYMBOL\|Name` a line, the shape `scan.py daily --watchlist` and `scripts/fetch_cache.py` read |

## The rule

1. A row needs a market cap. Rows without one (indexes, odd listings) are dropped and counted.
2. The cap must be at or above the floor: $10 billion by default.
3. The symbol, with Nasdaq's `/` written as `.` (BRK/B becomes BRK.B), must be in
   `trader/universe.txt` unless `--universe none`. That file is the upstream rule: no ETFs,
   warrants, units, preferreds or notes.
4. Tiers: `mega` at or above $200B, `large` $50B to $200B, `mid-large` $10B to $50B
   (`small` under $10B only appears with a lower floor).
5. One row a symbol, sorted by cap, largest first.

## Steps

1. Once a week, or when a name is missing: `scan.py universe` (upstream), then `largecap.py
   build --write`. Read back the counts as printed: rows, kept, dropped under the floor,
   dropped as not in the universe, and the tiers.
2. `show` to answer "which mega caps" or "which energy names"; `check TICKER` to answer
   "is it on the list" with its cap, tier, sector and industry.
3. Then `scripts/fetch_cache.py` so the bar store covers the new list, and `/trader-lab`.

## Example: the test fixture

The fixture is a cut of the real screener from 2026-10-25: 245 rows (the 150 largest caps,
40 mid-large names, 35 under the floor, a few odd rows and every symbol with a slash), with
a universe file of 186 symbols that leaves out BRK.A and five others.

```
largecap.py build --offline screener.json --universe universe.txt
Nasdaq screener: 245 rows. Floor $10B. Universe rule: trader/universe.txt (186 symbols).
Kept 184 large caps: 71 mega (at or above $200B), 74 large ($50B to $200B), 39 mid-large ($10B to $50B).
Dropped 55 under the floor or without a market cap, 6 not in the universe file.
  #1 NVDA  NVIDIA Corporation Common Stock  $5.11T  mega  Technology / Semiconductors
```

`check NVDA` says "#1 of 184, $5.11T, tier mega"; `check brk/b` finds BRK.B in the dot
spelling.

## Rules

- The list is as of the screener's answer; say the time. Caps move every day.
- Being on the list is not a signal. "Matched a rule" is for the scanner; "in the list" is all this says.
- Keep the upstream universe rule in the loop unless the user asks for the screener alone, and say so.

## What it never does

It never places, sizes or checks an order, never scans a chart, never picks a stock, and
never calls a company cheap, expensive or a buy.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
