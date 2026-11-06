---
name: nse-budget
description: >-
  Budget Manager for NSE intraday trading. Sizes every position to your budget
  and your max loss per trade, in rupees. Reads the trade plan from nse-exit
  (entry, stop, target), finds the whole shares that fit inside four limits
  (max loss per trade, position cap, max order, cash after your buffer), says
  which limit decided it, and writes ONE order (entry with a stop-loss and a
  take-profit leg) to nse/orders.json. Prices are whole ticks (0.05), the stop
  rounds down and the target up. It prints an estimate of the round-trip cost
  (brokerage, STT, exchange charges, SEBI fee, stamp duty, GST). A stop too
  far away for your max loss is a plain refusal, not a smaller order. Nothing
  is placed. Use when the user says "how many shares", "size this trade",
  "how much should I buy", "I have Rs 1 lakh", "I only want to risk Rs 500 a
  trade", "set my rules", "keep 10% in cash", "intraday position size", or
  after /nse-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# nse-budget

The Budget Manager turns a plan ("buy RELIANCE at 1432.60, stop 1411.55")
into an exact order ("buy 23 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. It
writes a proposal only. nse-risk checks it and nse-broker asks the user yes
or no. Intraday trading loses most people money. Sizing to a max loss keeps
each loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                        # nse/plan.json + nse/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                # write nse/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write        # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1430.00 --json # a limit order at 1430.00
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=40,TCS=60 --prices nse/prices.json --config split.json   # weights mode, below
```

`--now "2026-10-30 09:40"` pins the clock (India time, IST). A plan for
another day is refused: make a new one with nse-exit. Amounts print with
Indian digit grouping (the rupee sign, then 1,00,000 for one lakh).

Verified offline against fixtures; live endpoints not exercised at build
time. This skill makes no network calls at all, so that only matters for
the prices file it reads.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/config.json` | the rules (start from `config.example.json` in this folder) |
| `nse/plan.json` | the trade: bare NSE symbol, entry, stop, target, tick, setup, flat-by time (nse-exit writes it) |
| `nse/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (nse-alerts writes it) |
| `nse/portfolio.json` | optional: its cash caps the size (nse-broker keeps it) |
| `nse/orders.json` | the one proposed order it writes with `--write` |

## Step 1: set the rules once

If `nse/config.json` does not exist, copy `config.example.json` into
`nse/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for, in rupees. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop (default 500).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (Rs 50,000) and the smallest order (Rs 500).
- The day limits nse-risk enforces: **max_daily_loss_inr** (1,500), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:30") and **flat_by** ("15:15"), all India time.
- **tick**: the price step, 0.05 for most NSE stocks. Use 0.01 for a stock that trades in
  paise, and say so in the config.
- **product**: `MIS` (intraday, squared off by the broker if you forget) or `CNC` (delivery).
- **stop_type**: `SL-M` (stop-market, default) or `SL` (stop-limit); nse-broker uses it.
- **costs** (optional): override any rate in the cost estimate, see below.
- **blocked**: symbols they never want to trade.
- Keep **paper** true until they have watched it work. Paper mode means nothing is sent
  to a broker, because no Indian broker offers a free paper-trading order API. nse-broker
  explains it.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only (the lot size of cash equity is 1). The size is the
smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | `max_loss_per_trade_inr` / risk a share (entry minus stop) |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `nse/portfolio.json` |

Prices are exact: all the arithmetic is in integer paise, and prices are
whole ticks. The stop is rounded DOWN to a tick (a little more room), the
target UP, the entry to the nearest tick.

The entry it sizes at: the limit price with `--limit`. For a market order, the
plan's entry, or the price in `nse/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the symbol is blocked, or the plan is a short. Say
the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees
and the max loss at the stop. Show the cost estimate too. Say plainly that a
stop is an order, not a promise: in a fast market, or when a stock hits its
circuit limit, it can fill below the stop or not fill at all. Then hand over
to `/nse-risk`.

### The cost estimate

Intraday round trip, per share and in total, at the target and at the stop.
Default rates (percent of turnover, all overridable in the `costs` block of
the config): brokerage the smaller of Rs 20 or 0.03% per executed order,
STT 0.025% on the sell side, exchange transaction charge 0.00297% on both
sides, SEBI fee 0.0001%, stamp duty 0.003% on the buy side, GST 18% on
brokerage plus exchange charge plus SEBI fee. It is an estimate: rates
change and brokers differ, so always say "check your broker's contract
note". The size is NOT cut to pay for costs: the max loss cap is the gross
loss at the stop, and the report shows the net figure beside it. On a small
Rs 500 loss the costs are a real part of it.

## Example: the reel

RELIANCE on Friday 2026-10-30. Opening-range breakout: entry 1432.60 at
09:35, stop 1411.55 (the opening-range midpoint), target 1453.65 (1R). Risk
21.05 a share. The example config: Rs 1,00,000 budget, 10% cash buffer, Rs 500
max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | Rs 500 / 21.05 | **23 (decides it)** |
| position cap | 75% of Rs 1,00,000 = Rs 75,000 / 1432.60 | 52 |
| max order | Rs 50,000 / 1432.60 | 34 |
| cash | Rs 1,00,000 minus Rs 10,000 = Rs 90,000 / 1432.60 | 62 |

23 shares = Rs 32,949.80. Max loss at the stop: 23 x 21.05 = Rs 484.15. The
estimated costs are about Rs 35 either way, so the net loss at the stop is
near Rs 519 and the net gain at the target near Rs 449. It writes:

```json
{"symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": 23, "product": "MIS",
 "order_type": "market", "validity": "DAY", "stop_loss": 1411.55, "take_profit": 1453.65,
 "why": "opening-range breakout on RELIANCE: 23 shares, bound by your Rs 500 max loss per trade (23 x 21.05 = Rs 484.15 at the stop)",
 "plan": {"entry": 1432.6, "stop": 1411.55, "target": 1453.65, "risk_per_share": 21.05, "max_loss": 484.15,
          "setup": "orb", "entry_time": "09:35", "flat_by": "15:15"}}
```

`stop_loss` and `take_profit` are the two exit legs. Most Indian brokers have
discontinued intraday bracket orders, so nse-broker turns this one order into
an entry ticket plus a separate stop-loss ticket and take-profit ticket.
`why` and `plan` are notes for nse-risk and nse-broker and are never sent
anywhere. A wider stop means fewer shares: with the stop at 1300.00 one share
risks 132.60, so the max loss allows only 3 shares (3 x 132.60 = Rs 397.80), and
with the target unchanged reward:risk is 0.16, so it is refused anyway.
With the max loss cut to Rs 10, even the 1411.55 stop is too far away (21.05
a share): no order. If a fresh quote of 1440.00 arrives before the order is
placed, the same plan sizes to 17 shares at 28.45 a share, and reward:risk falls
to 0.48: refused, because the move it was waiting for has mostly happened.

## Weights mode (kept from v1)

`--weights RELIANCE=22.04,TCS=10.86` or `--targets FILE`
(`targets.example.json` here, example weights only) splits a budget across a
list by weight: the cash buffer comes off the top, weights are renormalised
to 100, any stock over `max_position_pct` is pinned at the cap and its excess
shared out, slices under the minimum order are dropped, and the result is
whole shares, because NSE has no fractional shares. So it always needs
prices (`--prices nse/prices.json`, or nse-alerts `alerts.py quote ... --write`).
Orders come out as delivery (`CNC`) buys by default (`weights_product` in the
config). These are plain buys with no stop: nse-risk blocks them while
`require_stop` is true and counts each one against `max_trades_per_day`. Use
a separate config (`--config`) for a long-term split.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.
- MIS gives leverage, but this pack sizes to cash by default. `allow_margin` is off.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes options,
futures, margin or short positions.

## Differences from the US pack

Rupees and lakh grouping instead of dollars. Whole ticks of 0.05. India time
(IST, no daylight saving). No pattern-day-trader rule, so no `pdt_guard`. No
fractional or rupee-amount orders. A cost estimate. Order fields follow the
NSE shape (exchange, product, order_type, validity), not Alpaca's.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper mode by default (`"paper": true`): nothing is sent to a broker. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No derivatives, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker logins and keys are entered by the user in the broker's own sign-in, never in the chat.
