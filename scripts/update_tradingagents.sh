#!/bin/sh
# Refresh vendor/TradingAgents from upstream at a tag (default: the latest release tag), verbatim, without its .git.
# usage: scripts/update_tradingagents.sh [tag]      Then update VENDORED.md's commit line and run the suite.
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
tag="${1:-}"
tmp="$(mktemp -d)"
git clone -q https://github.com/TauricResearch/TradingAgents.git "$tmp/TradingAgents"
cd "$tmp/TradingAgents"
[ -n "$tag" ] || tag="$(git describe --tags --abbrev=0)"
git checkout -q "$tag"
commit="$(git rev-parse HEAD)"
rm -rf .git
cd "$root"
keep="$root/vendor/TradingAgents/VENDORED.md"
cp "$keep" "$tmp/VENDORED.md"
rm -rf "$root/vendor/TradingAgents"
mv "$tmp/TradingAgents" "$root/vendor/TradingAgents"
cp "$tmp/VENDORED.md" "$keep"
rm -rf "$tmp"
echo "vendor/TradingAgents is now $tag ($commit); edit VENDORED.md's pinned line, then: python3 -m unittest discover -s tests"
