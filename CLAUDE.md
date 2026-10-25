# DAYTRADER: rules for working in this repo

- The eleven upstream skills (trader-scanner ... trader-pelosi), their tests and fixtures are vendored from
  github.com/Jakeschincariol/day-trader-skill (MIT) and are never edited. UPSTREAM_README.md is their README, verbatim.
- Everything new is Python 3.8+ standard library only (the one exception: trader-nq/nq_adapter.py runs under the
  NQ backtester's own venv with pandas). No pip installs in this repo. No 3.9+ syntax (tests/test_repo.py checks).
- No em dashes (U+2014) in any text file: the upstream repo test fails on one.
- CONTRACTS.md is the spec for the new layer: file shapes, rules, words. Change the contract before the code.
- Honest words: "matched a rule", never "a buy"; print intervals with every rate; "configs tested" with every table;
  nothing trades without the user's typed yes (the upstream three gates). Most people who day trade lose money.
- Run the full suite before a commit: python3 -m unittest discover -s tests (the upstream 315 must stay green).
- The project lives at ~/daytrader with a Desktop symlink: launchd jobs cannot read the Desktop folder. Long jobs
  (the cache refresh, the lab) run through scripts/bg.sh so they survive a Claude session restart.
- trader/config.json holds the SEC user agent (an email): never commit it. trader/ is the working folder and is
  gitignored except the examples.
- Install the skills with scripts/install_skills.sh (it copies dt/ into each new skill folder).
