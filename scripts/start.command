#!/bin/sh
# Double-click: serve the dashboard (docs/) at http://localhost:8765 and open it.
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root/docs" || exit 1
( sleep 1; open "http://localhost:8765" ) &
echo "DAYTRADER dashboard at http://localhost:8765  (Ctrl+C to stop)"
exec /usr/bin/python3 -m http.server 8765
