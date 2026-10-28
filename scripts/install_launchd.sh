#!/bin/sh
# Install the schedule as LaunchAgents (weekdays, local time computed from the New York times).
# DAYTRADER_DRY_RUN=1 writes the plists to $DAYTRADER_PLIST_DIR (or a temp dir) and skips launchctl.
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
dry="${DAYTRADER_DRY_RUN:-}"
dir="${DAYTRADER_PLIST_DIR:-$HOME/Library/LaunchAgents}"
[ -n "$dry" ] && dir="${DAYTRADER_PLIST_DIR:-$(mktemp -d)}"
mkdir -p "$dir" "$root/trader/logs"
# hours difference: local clock minus New York, from the two UTC offsets right now
shift_h=$(/usr/bin/python3 -c "
import sys, time; sys.path.insert(0, '$root'); from dt import clock
ny = clock.eastern_offset(clock.utc_now()); local = time.localtime().tm_gmtoff // 3600; print(local - ny)")
jobs="cache:17:30 futures:08:05 pick:08:08 pick2:08:18 morning:08:30 live:10:00 live2:10:15 live3:10:30 live4:11:00 grade:16:20"
for spec in $jobs; do
  name="${spec%%:*}"; hm="${spec#*:}"; h="${hm%%:*}"; m="${hm#*:}"
  job="$(echo "$name" | sed 's/[0-9]*$//')"
  lh=$(( (10#$h + shift_h + 24) % 24 ))
  label="com.daytrader.$name"
  plist="$dir/$label.plist"
  cal=""
  for wd in 1 2 3 4 5; do cal="$cal<dict><key>Weekday</key><integer>$wd</integer><key>Hour</key><integer>$lh</integer><key>Minute</key><integer>$((10#$m))</integer></dict>"; done
  cat > "$plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array><string>/bin/sh</string><string>$root/scripts/run.sh</string><string>$job</string></array>
<key>WorkingDirectory</key><string>$root</string>
<key>StartCalendarInterval</key><array>$cal</array>
<key>StandardOutPath</key><string>$root/trader/logs/launchd-$name.log</string>
<key>StandardErrorPath</key><string>$root/trader/logs/launchd-$name.log</string>
<key>RunAtLoad</key><false/>
</dict></plist>
P
  plutil -lint "$plist" >/dev/null
  printf "%-22s %s ET -> %02d:%s local, weekdays  %s\n" "$label" "$hm" "$lh" "$m" "$plist"
  if [ -z "$dry" ]; then
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
  fi
done
# the lab: Saturday 09:00 ET
lh=$(( (9 + shift_h + 24) % 24 )); label="com.daytrader.lab"; plist="$dir/$label.plist"
cat > "$plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array><string>/bin/sh</string><string>$root/scripts/run.sh</string><string>lab</string></array>
<key>WorkingDirectory</key><string>$root</string>
<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>$lh</integer><key>Minute</key><integer>0</integer></dict>
<key>StandardOutPath</key><string>$root/trader/logs/launchd-lab.log</string>
<key>StandardErrorPath</key><string>$root/trader/logs/launchd-lab.log</string>
<key>RunAtLoad</key><false/>
</dict></plist>
P
plutil -lint "$plist" >/dev/null
printf "%-22s Saturday 09:00 ET -> %02d:00 local  %s\n" "$label" "$lh" "$plist"
if [ -z "$dry" ]; then launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true; launchctl bootstrap "gui/$(id -u)" "$plist"; fi
echo "Nothing scheduled ever places an order: nobody is there to say yes."
