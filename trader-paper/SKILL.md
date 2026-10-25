---
name: trader-paper
description: >-
  The paper broker. Fills one approved bracket order from free 5-minute bars so the whole
  loop runs end to end without an Alpaca key: the entry at the first completed bar after
  your recorded yes (its open plus half the round-trip cost), then the stop or the target as
  the real bars reach them, stop before the target inside a bar, and a flatten at your
  flat-by bar's close. Every fill is recorded through the upstream trader-broker tool
  (ticket.py result, ticket.py fill) so the portfolio, the broker log and trader-risk's
  daily limits see it. Use when the user says "paper trade it", "fill it on paper", "sync
  my paper positions", "what is my paper book", "simulate the fill", "no broker yet", or
  after /trader-broker records a yes and no brokerage connector is set up.
argument-hint: "[place --index N | sync | status | cancel --id ID]"
---

# trader-paper

Alpaca's paper account is the upstream's broker. Until it is connected, this is the stand-in:
a local book that fills the bracket trader-budget wrote and trader-risk passed, only after
the user's yes is already in the broker log, from the same free 5-minute bars everything
else here reads. It is a paper trade in the plainest sense. Every fill is simulated from
5-minute bars: a real fill can be worse, a stop can gap, a target can be missed by a cent.
The point is to run the whole chain and keep an honest record, not to pretend to a broker.
Most people who day trade lose money; a paper record is where you find out first.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/paper.py" place --index 1                   # fill the entry of an order you said yes to
python3 "${CLAUDE_SKILL_DIR}/paper.py" sync                              # stop, target or flat-by on the bars since
python3 "${CLAUDE_SKILL_DIR}/paper.py" status                            # the book, open and closed
python3 "${CLAUDE_SKILL_DIR}/paper.py" cancel --id paper-2026-10-19-1    # only when the user asks
```

Flags: `--now "2026-10-19 10:25"` (New York), `--offline FILE_OR_DIR` (saved Yahoo
charts), `--cache-root DIR` (a past day's bars from the store), `--cost-bps 4`, `--orders`,
`--state`, `--risk`, `--config`, `--portfolio`, `--book trader/paper/positions.json`, `--json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the bracket (trader-budget): symbol, qty, stop-loss, take-profit |
| `trader/state/broker.json` | the user's recorded yes for that exact order (ticket.py approve), then the placed and filled records this tool adds through ticket.py |
| `trader/state/risk.json` | trader-risk's PASS for that fingerprint on the order's day |
| `trader/config.json` | `paper` must be true; `flat_by`; `budget` for a fresh portfolio |
| Yahoo chart API, or the bar store | the day's 5-minute bars (completed bars only) |
| `trader/paper/positions.json` | the book: entry, stop, target, flat-by, status, exit, R, simulated P&L |
| `trader/portfolio.json` | kept by the upstream ticket.py as fills are recorded |

## How a fill works

1. **Three gates first.** The order exists and is a whole-share bracket buy; the config says
   `"paper": true`; the broker log holds the user's yes for this fingerprint (and no placed
   record yet); trader-risk's PASS is from the same day. Any gate fails: one plain line, no fill.
2. **The entry.** The first completed 5-minute bar that starts at or after the minute of the
   yes. A yes at 10:16 fills on the 10:20 bar's open, plus half the round-trip cost as
   slippage (4 basis points round trip by default). If that bar has not closed yet, it says
   "waiting for the 10:20 bar" and fills next time. The fill is recorded with
   `ticket.py result --status placed --broker-id paper-... --filled-qty --filled-price`.
3. **The exits.** `sync` walks every later completed bar: a low at or under the stop sells
   at the stop (less slippage), else a high at or over the target sells at the target; a
   bar that touches both is the stop (stop before the target inside a bar). A position
   still open at the flat-by bar (15:55) sells at that bar's close. Each exit is recorded
   with `ticket.py fill --side sell`, so trader-risk's daily loss count and the portfolio stay right.
4. **A past day.** When the yes was recorded on an earlier session, the bars come from the
   bar store for that day, so a replay or a grading run fills the same way.

## Steps

1. After `ticket.py approve` has recorded the yes and no brokerage connector is set up:
   `paper.py place --index N`. Read back the fill line as printed: bar, open, slippage, the
   stop, the target, the risk a share and the dollars at the stop.
2. While the market is open, `paper.py sync` every few minutes (a `/loop`), and at 15:55.
   Read back what closed and how.
3. `paper.py status` for the book. The simulated P&L is the one place a realised dollar
   figure belongs; it is labelled simulated on every line, and it is never a projection.
4. Hand the day to `/trader-scorecard` (the record keeps its own 95 % CI on the win rate)
   and `/trader-risk` (the broker log already carries the day's max losses).

## Example: the reel's order

Monday 2026-10-19. trader-budget wrote buy 8 TSLA, stop 358.32, target 370.04; trader-risk
passed it at 10:15; the user's yes was recorded at 10:16. `paper.py place --index 1 --now
"2026-10-19 10:25"`:

```
Placed paper-2026-10-19-1 on paper: TSLA 8 at 365.12, the 10:20 bar's open 365.05 plus 0.07 slippage
(4 bps round trip, half each way), simulated from 5-minute bars.
  Stop 358.32, target 370.04, risk 6.80 a share, $54.40 at the stop. Flat by 15:55 New York.
```

Neither side was hit; `sync --now "2026-10-19 16:05"` sold at the 15:50 bar's close, 367.16
after slippage: +0.30R, +$16.32 on 8 shares, simulated from 5-minute bars. The plan's risk
was 5.86 a share at the 364.18 close; the real fill made it 6.80. That difference is the
whole reason this tool exists.

## Rules

- Nothing is placed without the recorded yes; this tool reads the log, it never writes a yes.
- Every line that carries a price says "simulated from 5-minute bars".
- One fill a yes. A second `place` on the same order refuses.
- Stop before the target inside a bar, always. Slippage is always against you.

## What it never does

It never places an order with a broker, never approves anything, never fills an order the
user did not say yes to, never fills a short or an option, and never calls a simulated fill
a real one.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
