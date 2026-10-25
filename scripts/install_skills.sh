#!/bin/sh
# Install every trader-* skill into ~/.claude/skills/ with plain names (/trader-scanner, /trader-desk ...).
# The six new skills import the shared dt/ library: a copy of dt/ goes inside each of their folders.
# usage: scripts/install_skills.sh [target-dir]   (default ~/.claude/skills)
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
target="${1:-$HOME/.claude/skills}"
mkdir -p "$target"
n=0
for d in "$root"/trader-*; do
  name="$(basename "$d")"
  [ -f "$d/SKILL.md" ] || continue
  rm -rf "$target/$name"
  cp -R "$d" "$target/$name"
  find "$target/$name" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  if grep -q "from dt import\|import dt\b\|from dt\." "$d"/*.py 2>/dev/null; then
    rm -rf "$target/$name/dt"; cp -R "$root/dt" "$target/$name/dt"
    find "$target/$name/dt" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  fi
  n=$((n+1))
done
echo "installed $n skills into $target"
ls "$target" | grep '^trader-' | tr '\n' ' '; echo
echo "Each skill reads and writes the trader/ folder of the directory Claude runs in; run them from $root (or any project with a trader/ folder)."
