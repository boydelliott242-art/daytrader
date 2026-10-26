# Vendored: TradingAgents v0.7.0

- Upstream: https://github.com/TauricResearch/TradingAgents (Tauric Research; Yijia Xiao, Edward Sun, Di Luo, Wei Wang;
  arXiv 2412.20138). License: Apache-2.0, see LICENSE in this folder. Copyright stays with the authors.
- Pinned at tag `v0.7.0`, commit `e8f5503e9b86d5c2710b0c76b1f686703968c9df` (merge of PR #1536), cloned 2026-10-25.
- Verbatim: nothing in this folder is edited by the desk. `scripts/update_tradingagents.sh` refreshes it from upstream.
- It needs Python 3.11 or later and a provider key (see its README). `scripts/install_tradingagents.sh` builds `.venv/`
  here (gitignored); `trader-agents/agents.py doctor` says what is present; `agents.py run --engine vendored` drives it.
- The desk's own research roles (`trader-agents/roles/`) are adapted from the prompts in `tradingagents/agents/` and
  say so at the top of each file.
