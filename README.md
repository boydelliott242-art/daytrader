# DAYTRADER: the Day Trader skill v2, plus a large-cap desk on top

Twenty-two Claude skills for day trading large caps, built on Jake Schincariol's
[Day Trader skill v2](https://github.com/Jakeschincariol/day-trader-skill) (MIT). The eleven upstream
skills are vendored here unchanged, with their 315 tests. Eleven more sit on top: a measured universe,
the market regime, an edge study of every setup over every large cap, an advisor that runs the chain
and shows the odds before the money, a graded track record, a local paper broker, an intel ledger fed
by Claude's connectors, a 26,000-replay Monte Carlo, the user's own fifteen-year Nasdaq-100 futures
backtester wired in, and a research desk built on Tauric Research's TradingAgents (vendored whole, Apache-2.0):
the roles run by Claude over an evidence pack, a short side with its own setups and risk, and rankings over the universe. Plain Python 3.8+, nothing to pip install, free public data. Nothing trades
without your yes. Most people who day trade lose money; this desk shows you the odds first.

## The twenty-two

| command | job |
| --- | --- |
| `/trader-scanner` ... `/trader-pelosi` | the eleven upstream skills, unchanged: scan, news, entry, exit, time machine, risk, budget, broker, alerts, analyst, Pelosi |
| `/trader-largecap` | the universe: US common stocks and ADRs at or above $10B, from Nasdaq's screener, under the upstream rule |
| `/trader-regime` | the weather: SPY vs its averages, VIX, breadth, sectors, the opening range, the futures' overnight |
| `/trader-lab` | the edge study: 12 setups, 31 configs, every large cap, every cached session, next-open fill, costs, walk-forward |
| `/trader-desk` | the advisor: morning plan, the live board at 10:00 to 11:00, pick, grade; one candidate a symbol, with the odds on it |
| `/trader-scorecard` | the record: every candidate logged when shown, graded on the real bars after the close, late calls kept apart |
| `/trader-paper` | the paper broker: fills an approved bracket from 5-minute bars through the upstream broker log, no Alpaca key needed |
| `/trader-intel` | the intel ledger: what Claude found through Firecrawl, FMP, Bigdata.com, Quartr and LunarCrush, as flags on the board |
| `/trader-montecarlo` | 26,000 replays of a plan, the trade book resampled at your sizing rules, the NQ and ES overnight read |
| `/trader-nq` | the desk's setups on fifteen years of NQ 1-minute bars with real fees and slippage, through your own backtester |
| `/trader-agents` | the research desk: the TradingAgents roles (six analysts, bull and bear in rounds, a contrarian, the research manager, the trader, three risk debaters, a code-fed risk analyst, the portfolio manager), run by Claude over one evidence pack per symbol, validated claim by claim, scored, written as a card; the code-only rankings over the universe; the memory of settled cards |
| `/trader-pick` | the 8:25 pick: one large cap a morning from what is known by 8:05 ET, the name with the best measured odds of closing 2 % above its open, posted by 8:25 with the record's odds up AND down beside the base rates and its own graded record |
| `scripts/lock.py` | not a skill: the lock. Seals the pages and the data into `docs/data/bundle.enc`; the live site is a passphrase screen |

**Live:** https://boydelliott242-art.github.io/daytrader/ is a lock screen. Behind the passphrase: the one-idea
page (big words: today's best candidate with the odds on it, or "Not yet" before 10:00 ET and "Nothing" on a day no rule
matched) and, one link away, every table. GitHub Pages from `docs/`; the scheduled jobs seal fresh data into
`docs/data/bundle.enc` after each run. The code is at https://github.com/boydelliott242-art/daytrader.

## The lock

The site is public hosting, so it is encrypted rather than "protected": `scripts/lock.py build` seals both pages and
every published data file into one bundle with AES-256-GCM under a key derived from the passphrase (PBKDF2-HMAC-SHA256,
four million rounds, fresh salt and nonce every build). The lock screen holds nothing: it fetches the bundle, derives
the key in the browser's own WebCrypto and opens it in memory. A wrong passphrase derives a wrong key and the
authentication tag fails; there is no stored password and no check to bypass. Nothing is kept: it locks on every
reload, on the Lock link, when the tab is hidden, when it comes back from the back-forward cache, and after ten idle
minutes. The page's Content-Security-Policy stops it talking to anywhere but its own origin.

- The passphrase lives in the macOS login Keychain (`scripts/lock.py set-passphrase`), or `DAYTRADER_SITE_PASSPHRASE`,
  or `trader/site.pass` (gitignored). It is never written to the repo, printed or logged. Change it, rebuild, push:
  `scripts/lock.py set-passphrase && scripts/lock.py build && git commit -am "resealed" && git push`.
- `scripts/lock.py verify` opens the local bundle; `scripts/lock.py check-url https://boydelliott242-art.github.io/daytrader/`
  proves the live site serves the same bytes, that they open, and that no page or data file is served in the clear.
- Honest limits. The bundle is public, so anyone can download it and guess offline at full speed, forever; nothing on a
  static site can rate-limit that. Each guess costs four million SHA-256 rounds (about a second on a laptop, far less on a
  cracking rig), so the passphrase is the whole defence: a word with digits after it falls to a pattern attack in hours to
  days; four or five random words do not fall. The cipher is not the weak point. Anyone you give the passphrase to can read
  everything. A compromised Mac (where the Keychain is) is a compromised site. The lock is JavaScript served by GitHub over
  HTTPS and is exactly as trustworthy as that. Data published before the lock (2026-10-25) stays in the git history.

## Install

```bash
git clone <this repo> ~/daytrader
~/daytrader/scripts/install_skills.sh          # every trader-* skill -> ~/.claude/skills/, with a dt/ copy where needed
cd ~/daytrader && cp trader-budget/config.example.json trader/config.json   # then put your name and email in sec_user_agent
python3 trader-scanner/scan.py universe && python3 trader-largecap/largecap.py build --write
python3 scripts/fetch_cache.py                  # ~900 large caps x 60 sessions of 5-minute bars (about 30 minutes, resumable)
python3 trader-lab/lab.py run --publish         # the edge study (a few minutes)
python3 scripts/lock.py set-passphrase           # once: the site passphrase into the Keychain
python3 scripts/lock.py build                   # seal site/ + trader/publish/ into docs/data/bundle.enc
```

The project lives at `~/daytrader` with a symlink on the Desktop: launchd jobs cannot read the Desktop
folder. `scripts/install_launchd.sh` installs the weekday schedule (cache 17:30 ET, futures 08:05,
morning 08:30, the board at 10:00, 10:15, 10:30 and 11:00, grading 16:20, the lab Saturday 09:00);
`scripts/uninstall_launchd.sh` removes it. `scripts/start.command` opens the dashboard at
http://localhost:8765. Nothing scheduled can place an order: nobody is there to say yes.

## One trade, start to finish

```
morning   desk.py morning            universe, regime, who reports today / tomorrow, the upstream daily scan, the news
intel     Claude + connectors        intel.py import; intel.py flags TICKER            (what we know, with sources)
10:15     desk.py live               every approved setup on every large cap, ranked by its out-of-sample record,
                                     the time machine on the top candidates, logged to the scorecard as shown
pick      desk.py pick TICKER        -> trader/entry.json, then the upstream exit.py plan --write
odds      timemachine.py run         the upstream 10,000 replays      montecarlo.py plan     26,000 replays
size      size.py --write            the upstream budget manager: shares from your max loss
check     check.py                   the upstream risk manager: PASS or BLOCK
send      ticket.py show / approve   the upstream broker: your yes, word for word, then Alpaca paper (or paper.py place)
close     alerts.py / paper.py sync  stop, target, flat by 15:55
grade     desk.py grade              the scorecard: what the board really did
```

Every skill reads and writes the `trader/` folder of the project you run it in, in the upstream's
shapes. `CONTRACTS.md` is the spec for the new layer.

## The 8:25 pick

Every trading morning at 8:08 ET (a retry at 8:18) the desk scores every liquid large cap from what is known by 8:05: two
years of daily rows (yesterday's move, volume, range position, momentum, ATR, RSI, realised volatility, each stock's own
habit of 2 % days), the market's trend and VIX, the Nasdaq futures gap at 8:05 from the user's NQ minute bars, and Nasdaq's
earnings calendars. Left out, in the backtest and live alike: names reporting today, names reacting to a report since the
previous session (Nasdaq drops report times for past dates, so the history cannot tell a report after the close from one
before the open, and a Saturday report counts for Monday), special situations (a name that moved 20 % or more the day
before: a crash, a deal, a split the store missed), half days, and names a session behind in the price store, each listed
on the card. The record counts a name only while it was a large cap (its estimated cap at the prior close of $10B or
more), and names that left the list keep their history. The pick is the name with the best measured odds of closing 2 %
or more above its open. It is sealed into the site and on the front page in big words by about 8:15, with:

- the record of past picks as its odds, closing 2 % up AND closing 2 % down, each with its interval, beside the base rates;
- the plain reasons, the pre-market print at the last 5-minute bar before the run (shown, never scored), the next five names;
- its out-of-sample record half by half, and its live record, graded every evening on the real open, high, low and close.

**What two years say (walk-forward, refit every 20 sessions, 327 out-of-sample mornings, 3 rules tested; the model of
2026-10-28; intervals are Wilson for the pick and resample whole mornings for the base rate):**

| | first half (Jul 2025 to Mar 2026) | second half (Mar to Oct 2026) |
| --- | --- | --- |
| the pick closed 2 % or more above its open | 31.9 % [25, 39] | 28.1 % [22, 35] |
| the pick closed 2 % or more below its open | 35.6 % [29, 43] | 36.6 % [30, 44] |
| the pick closed between 2 % and 5 % above its open | 23.3 % [17, 30] | 15.8 % [11, 22] |
| the pick traded 2 % above its open at some point | 57.1 % [49, 64] | 56.1 % [48, 64] |
| the pick traded 2 % below its open at some point | 68.1 % [61, 75] | 62.8 % [55, 70] |
| any large cap: closed 2 % up / 2 % down | 9.7 % [8.6, 11.0] / 9.3 % [8.1, 10.6] | 11.5 % [10.4, 12.7] / 11.8 % [11.0, 12.8] |
| any large cap: closed 2 to 5 % up | 8.7 % [7.7, 9.8] | 10.0 % [9.1, 10.9] |
| any large cap: traded 2 % above its open | 19.5 % [17.9, 21.4] | 24.6 % [23.1, 26.1] |
| the most volatile liquid name (highest ATR): closed 2 % up / down | 33.7 % [27, 41] / 36.8 % [30, 44] | 34.8 % [28, 42] / 28.7 % [22, 36] |
| the most volatile liquid name: traded 2 % above its open | 69.9 % [62, 76] | 66.5 % [59, 73] |

On the last 40 picks with 5-minute bars, +2 % came first 20 times and -2 % first 19 times (once neither). So the pick
finds a stock likely to **move** 2 % (about three times the base rate), the direction is not predictable from what is
known at 8:05, and simply taking the most volatile liquid name does about as well. The card says exactly that: "likely to
MOVE 2 %" (the move rate's interval sits above one half), with the record's odds both ways, until a rule's pick closes up
more often than down with separated intervals in both halves. Then, and only then, it would say "more likely to close 2 %
ABOVE its open than below it". It never says "most likely".

**The hunt for direction (2026-10-27).** Ten agents in parallel each tested one family of directional evidence through
the same harness (`scripts/pick_research.py`: every variant chosen on the first half, judged on the second, an automatic
look-ahead check on every module, independent verifiers re-running anything that claimed to hold): intraday persistence,
short-term reversal, post-earnings drift, relative strength to the sector, volume flow, calendar effects, the futures gap
times beta, price levels, each stock's own asymmetry, volatility regimes. The desk also tested the morning's own gap:
over two years, news-free gap-downs of 3 % or more bounced +0.40 % and +0.33 % from the open in the two halves (intervals
clear of zero), yet in the most recent 60 mornings, measured with real 8:05 pre-market prints (which track the opening gap
with a correlation of 0.81), the same names closed -0.12 % with 23 % up and 26 % down. An edge that holds for a year and
then fades is not one to post as a promise. The pre-market history now grows every morning, so the question can be asked
again with more of it.

None of the ten held. Four families claimed both halves; independent verifiers refuted all four (moving the cut points by
20 % broke three, and the fourth sat at the median of its own within-date shuffles). Post-earnings drift passed the
harness's rule, but its own 20 shuffles showed the gain was noise. Nothing joined the model.

**The rule hunt and the audit (2026-10-27 night).** Two researchers working apart tested six ranking rules each for the
literal target (a close 2 to 5 % up; a touch of +2 %): the live rule, the highest ATR, the stock's own habit of 2 % days,
scorers trained on the band and on the touch, and one more each. Chosen on the first half, judged on the second, none beat
the live rule by more than the noise. The most volatile liquid name touched +2 % more often in both halves, but the second
half's margin was +8.5 points [-1.2, +18.8], so it is tracked forward as a shadow (`trader/pick/shadow.jsonl`, graded every
evening, never posted as the pick) until fresh mornings say otherwise. An adversarial audit (six lenses, every finding put to
independent skeptics) found 39 defects. The ones that mattered are fixed:
- the card's "expected" move (an uncalibrated sum that averaged +1.8 % while the picks averaged -0.2 %) and the model's
  in-sample bin odds (shared by a dozen or more names a morning) are gone from the card;
- a futures file without a Nasdaq read no longer crashes the morning;
- a job killed mid-run no longer leaves a lock that stops it forever;
- a re-run can no longer post a different stock than the one the record grades;
- the reaction rule live now matches the history;
- the base rate's intervals now resample whole mornings;
- names count only while they were large caps;
- a failed morning posts a dated card saying so;
- a stale store or a half day posts no pick;
- publishing happens only from main.

```bash
python3 trader-pick/pick.py train --workers 12 --rule up --publish     # the walk-forward and the live model
python3 trader-pick/pick.py morning --at 08:05 --write --publish        # what the 08:08 ET job runs (it passes its own clock time)
python3 trader-pick/pick.py report                                       # every rule's record by half, the lifts, the live record
```

## The research desk (TradingAgents, integrated)

`vendor/TradingAgents/` is TradingAgents v0.7.0 by Tauric Research (arXiv 2412.20138), every file, verbatim, with its
license; `scripts/install_tradingagents.sh` builds its own venv (Python 3.11+) and its 100 unit tests pass there; with a
provider key, `agents.py run SYM --engine vendored` runs the upstream graph itself and adds its reports to the pack.
The desk's own roles need no key: `trader-agents/roles/*.md` are the upstream prompts adapted (the market, fundamentals,
news and sentiment analysts, the bull and bear researchers, the research manager, the trader, the aggressive,
conservative and neutral risk debaters, the portfolio manager) plus a momentum analyst, a regime analyst, a contrarian
and a code-fed risk analyst. Claude plays each role inside Claude Code:

```
agents.py prepare NVDA --date D --at 10:15 --depth standard   # the evidence pack (every item with an id) and the first prompts
  ... read each prompt, write its JSON ...                     # one claim per sentence; DATA, SIGNAL and STAT claims cite ids
agents.py next NVDA --date D                                  # the validator: wrong numbers, missing ids, high confidence with a
                                                              #   source missing, levels off a cited signal: rejected, sentence by sentence
agents.py decide NVDA --date D --write --publish              # the scores, the card (NO TRADE is a result), logged to memory
agents.py rank --write --publish                              # top momentum longs, short candidates, high conviction, risk-adjusted,
                                                              #   contrarian, most dangerous shorts, watchlist; empty lists say NO TRADE
agents.py settle; agents.py reflect ID                        # settled on the real bars, then a lesson the next pack carries
```

The short side is its own library (`dt/shorts.py`: 19 bearish configs, ids ending in `|short`), played mirrored by the
simulator, measured by the same lab gate, shown on its own board, scored by its own weight vector and judged by its own
risk block (squeeze risk unknown unless a source says otherwise, the unbounded loss named on every card). Nothing places
a short order: the upstream chain is long only and the paper broker refuses one.

Every weight in `trader-agents/weights.json` is a choice with a reason beside it, and `agents.py weights-test` is the
only ground it stands on. The first run (14,344 OOS trades of the three watch configs) said the momentum score does NOT
separate them: the bottom quintile made +0.066R [+0.018, +0.111] and the top lost 0.034R [-0.079, +0.007]. That verdict
rides on every card and ranking until a run says otherwise.

## What the numbers are, and are not

- **The lab** plays each setup on every stored session with a fill at the NEXT bar's open (you only
  know a bar closed above a level once it closed) and 4 basis points of costs, and measures R against
  the plan's risk. It reports the win rate with a Wilson interval and the expectancy with a bootstrap
  interval, in sample and out of sample, by regime, hour, tier, sector and weekday, and a rolling
  walk-forward (select on the 30 sessions before each day, trade the next). A config is `approved` only
  when the out-of-sample expectancy's 95 % lower bound is above zero after costs with n at or above 100.
  Every table prints `configs tested: 31`: with 31 tries something looks good by luck.
- **The desk** only shows approved configs, flags late triggers and names that report, ranks by the
  conservative bound, and logs every candidate the minute it is shown. A candidate "matched a rule with
  a measured record". It is not a prediction.
- **The scorecard** grades every shown call on the real bars; late calls stay out of the headline;
  twenty calls say little and the report says so.
- **NQ** is one instrument. Fifteen years of setups on it, with the engine's fees and slippage, test
  the rules, not any stock. The example ORB there made about 6 ticks a trade gross and 4.7 ticks of
  slippage would wipe it out; that is the honest shape of intraday edges.
- Free Yahoo data is unofficial and can lag. A stop is a plan, not a guarantee. Paper first.

## What the numbers said on 2026-10-25

These are the first real runs, printed here so nobody has to take the tools' word for it.

- **The lab, 901 large caps, 60 sessions (2026-07-31 to 2026-10-23), next-open fill, 4 bps.** No config
  cleared the gate. The best out of sample was `vwap_reclaim|r=2` at +0.011R [-0.010, +0.033] after costs
  (status `watch`); every opening-range variant was negative, the plain `orb15|r=1` at -0.086R
  [-0.104, -0.068] over 9,330 out-of-sample trades. Configs tested: 31. Over this window the twelve
  rules, traded mechanically across every large cap, had no edge after realistic fills and costs.
- **The Monte Carlo book on that watch config at the sizing rules** ($50 a trade, 3 trades a day, $150
  daily limit): the first three signals of each day are not the pooled average; a 20-day window ended
  red in 95 % of 26,000 resamples, median -19.8R. Pooled statistics across 900 names cannot be traded as
  "the first three that fire".
- **The NQ bench, 3,729 sessions (2011 to 2026), NinjaTrader free-plan fees and default slippage.** One
  config cleared the gate out of sample: `orb15|r=1|stop=or_low`, n 872, win rate 40 % [37, 43],
  expectancy +0.072R [+0.014, +0.128], profit factor 1.19, about $113,000 net on one contract over fifteen
  years, and 4.3 extra ticks of slippage a side would erase it. Fourteen more opening-range variants sit at
  `watch`; the VWAP, inside-bar and mean-reversion rules were rejected. The minute engine and the desk's
  own simulator agreed on 98.5 % of 46,107 trade outcomes.
- **The overnight, fifteen years of NQ:** the plain `orb15|r=1` made +0.107R a trade [+0.005, +0.211]
  after an overnight gap up of more than half a percent and +0.069R [-0.047, +0.186] after a gap down of
  that size, and lost 0.21R a trade after a flat night (gap inside a tenth of a percent). The night
  matters more than the rule.

- **The short side, the same lab, the same gate (2026-10-25, 1,005,765 trades over both libraries).** No bearish config
  cleared the gate. Two sit at `watch`: `gap_fade|r=1|short` (n 794, +0.050R [-0.005, +0.105], win 33 % [29.8, 36.3]) and
  `gap_fade|r=2|short`; every opening-range breakdown was negative, the plain `orb15_down|r=1|short` at -0.075R
  [-0.094, -0.057] over 9,398 trades. Configs tested: 50.
- **The feature study (the OOS trades of the three watch configs):** on the long side only the moving-average structure
  and the realised volatility separated the trades; on the short side the 5 and 20-day moves, relative strength, RSI,
  the MACD histogram, up-day persistence and beta did (lower beta better). Fourteen pairs of features were redundant
  (Spearman above 0.8): the momentum family is one fact, not nine.
- **The robustness pass:** every watch config is FRAGILE. `vwap_reclaim|r=2` loses in both regime classes the window
  held (range -0.084R, strong_bull -0.091R), its one neighbour is negative and its validation third is -0.115R; the gap
  fades hold the three-way split (train +0.071R, validation +0.000R, test +0.052R) but are positive in one regime class of
  two (strong_bull +0.092R, range -0.002R). A rule that works in one regime is a rule that works in one regime.

None of this is a prediction. It is what the rules did.

## Tests

```bash
python3 -m unittest discover -s tests          # the upstream 315 + the new suites (650 in all), offline, on fixtures
vendor/TradingAgents/.venv/bin/python -m pytest -q vendor/TradingAgents/tests   # the upstream TradingAgents suite, in its venv
```

## Fine print

The upstream README's fine print applies in full (see `UPSTREAM_README.md`): not financial advice; no
trade goes through until you say yes; paper first; the time machine is a simulation, not a prediction;
a stop is a plan, not a guarantee; the data is free and unofficial; most people who day trade lose money.

MIT. The eleven upstream skills by [Jake Schincariol](https://opusjake.ai); the desk on top built on
them, with their rules kept.
