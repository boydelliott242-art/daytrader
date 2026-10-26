# DAYTRADER: the Day Trader skill v2, plus a large-cap desk on top

Twenty Claude skills for day trading large caps, built on Jake Schincariol's
[Day Trader skill v2](https://github.com/Jakeschincariol/day-trader-skill) (MIT). The eleven upstream
skills are vendored here unchanged, with their 315 tests. Nine more sit on top: a measured universe,
the market regime, an edge study of every setup over every large cap, an advisor that runs the chain
and shows the odds before the money, a graded track record, a local paper broker, an intel ledger fed
by Claude's connectors, a 26,000-replay Monte Carlo, and the user's own fifteen-year Nasdaq-100 futures
backtester wired in. Plain Python 3.8+, nothing to pip install, free public data. Nothing trades
without your yes. Most people who day trade lose money; this desk shows you the odds first.

## The twenty

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

None of this is a prediction. It is what the rules did.

## Tests

```bash
python3 -m unittest discover -s tests          # the upstream 315 + the new suites, offline, on fixtures
```

## Fine print

The upstream README's fine print applies in full (see `UPSTREAM_README.md`): not financial advice; no
trade goes through until you say yes; paper first; the time machine is a simulation, not a prediction;
a stop is a plan, not a guarantee; the data is free and unofficial; most people who day trade lose money.

MIT. The eleven upstream skills by [Jake Schincariol](https://opusjake.ai); the desk on top built on
them, with their rules kept.
