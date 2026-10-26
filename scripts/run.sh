#!/bin/sh
# The scheduled jobs: run.sh <cache|morning|live|grade|lab|futures>. Each exits at once outside its window on the
# New York clock, holds a lock so two runs never overlap, logs to trader/logs/<job>.log, and never places an order.
set -u
root="$(cd "$(dirname "$0")/.." && pwd)"
job="${1:-}"
[ -n "$job" ] || { echo "usage: run.sh <cache|morning|live|grade|lab|futures>"; exit 2; }
py=/usr/bin/python3
cd "$root" || exit 1
mkdir -p trader/logs
log="trader/logs/$job.log"
lock="trader/logs/.$job.lock"
if ! mkdir "$lock" 2>/dev/null; then echo "$(date) $job: already running" >> "$log"; exit 0; fi
trap 'rmdir "$lock" 2>/dev/null' EXIT INT TERM
# the New York clock, from dt.clock (no tz database needed)
read -r ny_day ny_hhmm ny_trading <<P
$($py -c "import sys; sys.path.insert(0,'$root'); from dt import clock; et=clock.to_eastern(clock.utc_now()); print(et.date().isoformat(), et.strftime('%H:%M'), 'yes' if clock.trading_day(et.date())[0] else 'no')")
P
echo "$(date) $job: New York $ny_day $ny_hhmm trading=$ny_trading" >> "$log"
inwin() { [ "$ny_hhmm" \> "$1" ] && [ "$ny_hhmm" \< "$2" ]; }
# after a job that published to trader/publish, seal the pages and the data into docs/data/bundle.enc (scripts/lock.py,
# the passphrase from the Keychain) and push it, so the GitHub Pages copy is current. Nothing goes up in the clear: a build
# that cannot find the passphrase or does not reopen fails closed and publishes nothing (never fatal to the job).
publish_docs() {
  $py scripts/lock.py build >> "$log" 2>&1 || { echo "$(date) $job: lock build failed; nothing published" >> "$log"; return 0; }
  git add docs/data/bundle.enc >/dev/null 2>&1 || return 0
  git diff --cached --quiet && return 0
  git -c user.name="daytrader" -c user.email="daytrader@localhost" commit -q -m "sealed dashboard: $job $ny_day $ny_hhmm ET" >> "$log" 2>&1 || return 0
  git push -q origin main >> "$log" 2>&1 || echo "$(date) $job: docs push failed (the local dashboard is still current)" >> "$log"
}
case "$job" in
  cache)
    [ "$ny_trading" = yes ] || { echo "not a trading day" >> "$log"; exit 0; }
    inwin 16:59 23:59 || { echo "outside 17:00-24:00" >> "$log"; exit 0; }
    $py scripts/fetch_cache.py --build-universe --max-age-hours 12 >> "$log" 2>&1 ;;
  futures)
    [ "$ny_trading" = yes ] || exit 0
    inwin 08:00 09:31 || { echo "outside 08:00-09:30" >> "$log"; exit 0; }
    $py trader-montecarlo/montecarlo.py futures --write >> "$log" 2>&1 ;;
  morning)
    [ "$ny_trading" = yes ] || { echo "not a trading day" >> "$log"; exit 0; }
    inwin 07:59 09:30 || { echo "outside 08:00-09:30" >> "$log"; exit 0; }
    $py trader-montecarlo/montecarlo.py futures --write >> "$log" 2>&1
    $py trader-desk/desk.py morning --write --publish >> "$log" 2>&1
    publish_docs ;;
  live)
    [ "$ny_trading" = yes ] || { echo "not a trading day" >> "$log"; exit 0; }
    inwin 09:49 15:31 || { echo "outside 09:50-15:30" >> "$log"; exit 0; }
    $py trader-regime/regime.py live --write --publish >> "$log" 2>&1
    $py trader-desk/desk.py live --write --publish >> "$log" 2>&1
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
    $py trader-montecarlo/montecarlo.py book --all-approved --publish >> "$log" 2>&1
    publish_docs ;;
  *) echo "unknown job $job" >> "$log"; exit 2 ;;
esac
echo "$(date) $job: done" >> "$log"
