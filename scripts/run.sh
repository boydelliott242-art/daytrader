#!/bin/sh
# The scheduled jobs: run.sh <cache|morning|live|grade|lab|futures|pick>. Each exits at once outside its window on the
# New York clock, holds a lock so two runs never overlap, logs to trader/logs/<job>.log, and never places an order.
set -u
root="$(cd "$(dirname "$0")/.." && pwd)"
job="${1:-}"
[ -n "$job" ] || { echo "usage: run.sh <cache|morning|live|grade|lab|futures|pick>"; exit 2; }
# keep the Mac awake while a job runs (idle sleep always, system sleep on AC): a job that started in a dark wake would
# otherwise run in slices between sleeps and post after its window
if [ -z "${DT_AWAKE:-}" ] && [ -x /usr/bin/caffeinate ]; then
  DT_AWAKE=1 exec /usr/bin/caffeinate -i -s /bin/sh "$0" "$@"
fi
py=/usr/bin/python3
cd "$root" || exit 1
mkdir -p trader/logs
log="trader/logs/$job.log"
notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"DAYTRADER $job\"" 2>/dev/null; }
# the lock holds the run's pid: a run whose pid is gone (a restart, a kill, a crash) or that is over four hours old left it
# behind, and the next run breaks it instead of skipping forever
lock="trader/logs/.$job.lock"
if ! mkdir "$lock" 2>/dev/null; then
  old=$(cat "$lock/pid" 2>/dev/null || true)
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null && [ -z "$(find "$lock" -maxdepth 0 -mmin +240 2>/dev/null)" ]; then
    echo "$(date) $job: already running (pid $old)" >> "$log"; exit 0
  fi
  if [ -z "$old" ] && [ -n "$(find "$lock" -maxdepth 0 -mmin -1 2>/dev/null)" ]; then
    echo "$(date) $job: another run took the lock a moment ago" >> "$log"; exit 0
  fi
  echo "$(date) $job: breaking a stale lock (pid ${old:-unknown} is gone, or the lock is over four hours old)" >> "$log"
  rm -rf "$lock"
  mkdir "$lock" 2>/dev/null || { echo "$(date) $job: could not take the lock" >> "$log"; exit 0; }
fi
echo $$ > "$lock/pid"
trap 'rm -rf "$lock" 2>/dev/null' EXIT
trap 'rm -rf "$lock" 2>/dev/null; exit 143' INT TERM
# the New York clock, from dt.clock (no tz database needed)
read -r ny_day ny_hhmm ny_trading <<P
$($py -c "import sys; sys.path.insert(0,'$root'); from dt import clock; et=clock.to_eastern(clock.utc_now()); print(et.date().isoformat(), et.strftime('%H:%M'), 'yes' if clock.trading_day(et.date())[0] else 'no')")
P
if [ -z "${ny_day:-}" ] || [ -z "${ny_trading:-}" ]; then
  echo "$(date) $job: the New York clock could not be read (python or dt.clock failed); nothing ran" >> "$log"
  notify "the job did not run: the clock could not be read"
  exit 1
fi
echo "$(date) $job: New York $ny_day $ny_hhmm trading=$ny_trading" >> "$log"
inwin() { [ "$ny_hhmm" \> "$1" ] && [ "$ny_hhmm" \< "$2" ]; }
# after a wake the network can lag: wait up to a minute for Yahoo's and GitHub's names to resolve
wait_net() {
  for i in 1 2 3 4 5 6 7; do
    $py -c "import socket; socket.getaddrinfo('query2.finance.yahoo.com', 443); socket.getaddrinfo('github.com', 443)" 2>/dev/null && return 0
    sleep 10
  done
  echo "$(date) $job: the network did not come up in a minute; going on anyway" >> "$log"
}
# after a job that published to trader/publish, seal the pages and the data into docs/data/bundle.enc (scripts/lock.py,
# the passphrase from the Keychain) and push it, so the GitHub Pages copy is current. Nothing goes up in the clear: a build
# that cannot find the passphrase or does not reopen fails closed and publishes nothing (never fatal to the job). Only the
# bundle is committed, and only on main: a checkout left on another branch publishes nothing and says so.
publish_docs() {
  branch=$(git symbolic-ref --short -q HEAD 2>/dev/null || echo "a detached HEAD")
  if [ "$branch" != main ]; then
    echo "$(date) $job: the checkout is on $branch, not main; nothing published" >> "$log"
    notify "the site was not updated: the repo is on $branch, not main"
    return 0
  fi
  $py scripts/lock.py build >> "$log" 2>&1 || { echo "$(date) $job: lock build failed; nothing published" >> "$log"; notify "the site was not updated: the lock build failed"; return 0; }
  git add docs/data/bundle.enc >/dev/null 2>&1 || { echo "$(date) $job: git add failed; nothing published" >> "$log"; notify "the site was not updated: git add failed"; return 0; }
  git diff --cached --quiet -- docs/data/bundle.enc && return 0
  git -c user.name="daytrader" -c user.email="daytrader@localhost" commit -q -m "sealed dashboard: $job $ny_day $ny_hhmm ET" -- docs/data/bundle.enc >> "$log" 2>&1 \
    || { echo "$(date) $job: commit failed; nothing published" >> "$log"; notify "the site was not updated: the commit failed"; return 0; }
  git push -q origin main >> "$log" 2>&1 || { echo "$(date) $job: docs push failed (the local dashboard is still current)" >> "$log"; notify "the site push failed: see trader/logs/$job.log"; }
}
case "$job" in
  cache)
    [ "$ny_trading" = yes ] || { echo "not a trading day" >> "$log"; exit 0; }
    inwin 16:59 23:59 || { echo "outside 17:00-24:00" >> "$log"; exit 0; }
    wait_net
    $py scripts/fetch_cache.py --build-universe --max-age-hours 12 >> "$log" 2>&1
    # the day's session is in the store now: grade the morning's pick, refresh the earnings calendar ahead, publish
    $py trader-pick/pick.py grade --write --publish --quiet >> "$log" 2>&1
    $py scripts/fetch_earnings_history.py --refresh-last 11 --threads 2 --quiet >> "$log" 2>&1
    $py scripts/fetch_premarket.py --range 5d --max-age-hours 0 --threads 4 --quiet >> "$log" 2>&1
    publish_docs ;;
  futures)
    [ "$ny_trading" = yes ] || exit 0
    inwin 08:00 09:31 || { echo "outside 08:00-09:30" >> "$log"; exit 0; }
    wait_net
    $py trader-montecarlo/montecarlo.py futures --write >> "$log" 2>&1 ;;
  pick)
    # the 8:25 pick (08:08 ET, a retry at 08:18): the earnings calendars from the previous session to today, the futures
    # read at 08:05, the store through yesterday's close from the 17:30 cache job; sealed and pushed, live about a minute later
    [ "$ny_trading" = yes ] || { echo "not a trading day" >> "$log"; exit 0; }
    inwin 07:59 09:25 || { echo "outside 08:00-09:25" >> "$log"; exit 0; }
    # a failed or preview card is not a posted pick: the retry runs the morning again for it
    posted=$($py -c "import json; d = json.load(open('trader/publish/pick.json')); print('redo' if d.get('failed') or d.get('preview') else d.get('date'))" 2>/dev/null || echo none)
    pushed=$(git log -1 --format=%h --grep="pick $ny_day" origin/main 2>/dev/null || true)
    if [ "$posted" = "$ny_day" ] && [ -n "$pushed" ]; then
      echo "$(date) $job: today's pick is already posted" >> "$log"; exit 0
    fi
    wait_net
    if [ "$posted" = "$ny_day" ]; then
      # today's card was written but did not reach origin: push it again, never pick again
      echo "$(date) $job: today's card exists but is not on origin; publishing it again" >> "$log"
      publish_docs
      echo "$(date) $job: done" >> "$log"; exit 0
    fi
    days=$($py -c "import sys, datetime; sys.path.insert(0, '$root'); from dt import intel_flags; d = datetime.date.fromisoformat('$ny_day'); p = intel_flags.prev_trading_day(d); print(','.join((p + datetime.timedelta(days=k)).isoformat() for k in range((d - p).days + 1)))")
    $py scripts/fetch_earnings_history.py --dates "$days" --refresh-last 10 --threads 2 --quiet >> "$log" 2>&1
    $py -c "import json, sys; f = json.load(open('trader/futures.json')); sys.exit(0 if f.get('date') == '$ny_day' and (f.get('nq') or {}).get('gap_pct') is not None else 1)" 2>/dev/null \
      || $py trader-montecarlo/montecarlo.py futures --write >> "$log" 2>&1
    $py trader-pick/pick.py morning --at "$ny_hhmm" --write --publish --quiet >> "$log" 2>&1 \
      || notify "the 8:25 pick failed; see trader/logs/pick.log"
    publish_docs
    # the pre-market history for research, after the card is out (the pick fetches its own quotes for its top ten)
    $py scripts/fetch_premarket.py --range 1d --max-age-hours 0 --threads 6 --quiet >> "$log" 2>&1 ;;
  morning)
    [ "$ny_trading" = yes ] || { echo "not a trading day" >> "$log"; exit 0; }
    inwin 07:59 09:30 || { echo "outside 08:00-09:30" >> "$log"; exit 0; }
    wait_net
    $py trader-montecarlo/montecarlo.py futures --write >> "$log" 2>&1
    $py trader-desk/desk.py morning --write --publish >> "$log" 2>&1
    publish_docs ;;
  live)
    [ "$ny_trading" = yes ] || { echo "not a trading day" >> "$log"; exit 0; }
    inwin 09:49 15:31 || { echo "outside 09:50-15:30" >> "$log"; exit 0; }
    wait_net
    $py trader-regime/regime.py live --write --publish >> "$log" 2>&1
    $py trader-desk/desk.py live --write --publish >> "$log" 2>&1
    $py trader-agents/agents.py rank --at "$ny_hhmm" --write --publish --quiet >> "$log" 2>&1
    n=$($py -c "import json; d=json.load(open('trader/desk.json')); print(len([c for c in d.get('candidates',[]) if 'late' not in c.get('flags',[])]))" 2>/dev/null || echo 0)
    /usr/bin/osascript -e "display notification \"$n fresh candidates on the board at $ny_hhmm ET (nothing placed)\" with title \"DAYTRADER desk\"" 2>/dev/null
    publish_docs ;;
  grade)
    [ "$ny_trading" = yes ] || exit 0
    inwin 16:04 23:59 || { echo "outside 16:05-24:00" >> "$log"; exit 0; }
    $py trader-desk/desk.py grade >> "$log" 2>&1
    $py trader-scorecard/scorecard.py report --publish >> "$log" 2>&1
    publish_docs ;;
  lab)
    $py trader-lab/lab.py run --publish >> "$log" 2>&1
    $py trader-pick/pick.py train --publish --quiet >> "$log" 2>&1
    $py trader-lab/lab.py features --publish >> "$log" 2>&1
    $py trader-lab/lab.py robustness --publish >> "$log" 2>&1
    $py trader-agents/agents.py weights-test --write >> "$log" 2>&1
    $py trader-montecarlo/montecarlo.py book --all-approved --publish >> "$log" 2>&1
    publish_docs ;;
  *) echo "unknown job $job" >> "$log"; exit 2 ;;
esac
echo "$(date) $job: done" >> "$log"
