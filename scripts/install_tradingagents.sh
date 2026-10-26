#!/bin/sh
# Build the vendored TradingAgents' own virtual environment (vendor/TradingAgents/.venv) and install it there.
# It needs Python 3.11 or later: a python3.11+ on the PATH, or uv (which can fetch one). Nothing is installed into
# the system Python and nothing outside vendor/TradingAgents/.venv is written. usage: scripts/install_tradingagents.sh
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/vendor/TradingAgents"
venv="$src/.venv"
[ -f "$src/pyproject.toml" ] || { echo "vendor/TradingAgents is missing; run scripts/update_tradingagents.sh first"; exit 2; }
py=""
for c in python3.13 python3.12 python3.11; do
  if command -v "$c" >/dev/null 2>&1; then py="$(command -v "$c")"; break; fi
done
if [ -n "$py" ]; then
  echo "using $py"
  "$py" -m venv "$venv"
  "$venv/bin/python" -m pip install -q --upgrade pip
  "$venv/bin/python" -m pip install -q -e "$src[dev]"
elif command -v uv >/dev/null 2>&1; then
  echo "using uv (it fetches a Python 3.13 if none is installed)"
  uv venv --python 3.13 "$venv"
  uv pip install --python "$venv/bin/python" -e "$src[dev]"
else
  cat <<MSG
No Python 3.11+ and no uv on this Mac, so the vendored engine cannot be installed here.
Install one of:
  - Python 3.13 from https://www.python.org/downloads/macos/ (then re-run this script)
  - uv: see https://docs.astral.sh/uv/getting-started/installation/ (then re-run this script)
The desk's own research roles (trader-agents) do not need it: they run inside Claude Code with no key.
MSG
  exit 2
fi
"$venv/bin/python" -c "import tradingagents, sys; print('tradingagents', tradingagents.__version__, 'on', sys.version.split()[0])"
echo "installed; run the upstream tests with: $venv/bin/python -m pytest -q $src/tests"
echo "a provider key goes in your shell (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...) or $src/.env (gitignored); never in chat."
