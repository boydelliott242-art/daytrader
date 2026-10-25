#!/bin/sh
# Remove the DAYTRADER LaunchAgents.
dir="$HOME/Library/LaunchAgents"
for f in "$dir"/com.daytrader.*.plist; do
  [ -e "$f" ] || continue
  label="$(basename "$f" .plist)"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$f"; echo "removed $label"
done
