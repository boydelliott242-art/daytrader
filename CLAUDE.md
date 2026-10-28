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
- The site is locked. site/ holds the pages; docs/ holds only the lock screen, 404.html, .nojekyll and
  data/bundle.enc (scripts/lock.py build seals site/ + trader/publish/ into it). Never put a page or a .json under
  docs/. The passphrase is in the login Keychain (account daytrader, service daytrader-site-passphrase): never write it
  to a file, the repo, a log, memory or a reply, and never type it into the live github.io site (localhost:8765 only).
  Publish only through scripts/run.sh publish_docs or lock.py build + commit + push; a build that cannot find the
  passphrase publishes nothing.
- vendor/TradingAgents is Tauric Research's TradingAgents v0.7.0, verbatim (Apache-2.0, VENDORED.md pins the commit):
  never edited, never imported by the stdlib tools; its own venv (scripts/install_tradingagents.sh, Python 3.11+) is
  the one other pip exception, beside the NQ backtester's. The repo tests skip vendor/.
- The research desk (trader-agents) is Claude playing the roles: agents.py writes a prompt, Claude writes the JSON, the
  validator checks every claim against the evidence pack. Never fill a gap in the pack from memory; a rejected output
  is rewritten by the role, never patched with a number. The weights in trader-agents/weights.json are choices;
  agents.py weights-test is the only ground they stand on, and its verdict rides on every card and ranking.
- Sides stay separate: dt/shorts.py, |short config ids, a mirrored dt.sim, separate score vectors. Nothing places a
  short order (the upstream chain is long only; paper.py refuses one); a short is research until the config allows it.
- The 8:25 pick (trader-pick, dt/pick.py) is a count, not a prediction: the card prints the record of past picks (up AND
  down, with intervals) beside the base rates, and says "more likely to close 2 % ABOVE its open than below it" only when the live rule's
  out-of-sample pick closed up more often than down with separated Wilson intervals in BOTH halves (claim_of); otherwise
  that it may MOVE 2 % ("likely" only when the move rate's interval sits above one half). Never "most likely", and never print the model's in-sample bin rates or its summed move score
  (e_oc) as a name's odds or expected move. A new feature or rule joins the model only through scripts/pick_research.py:
  chosen on h1, confirmed on h2, leak check clean (the highest-ATR shadow in trader/pick/shadow.jsonl stays a shadow
  until it earns that). The 08:08 ET job (run.sh pick) fetches the calendars and the futures, scores, seals and pushes;
  never move it past 8:15 ET, never let the card claim more than the record in trader/pick/model.json supports, and keep
  ~/daytrader on main with nothing staged (publish_docs publishes only the bundle, only from main).
