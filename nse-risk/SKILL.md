---
name: nse-risk
description: >-
  Risk Manager for NSE intraday trading. Checks every trade against your
  limits, in rupees, and stops anything over them before it can reach the
  broker. For an intraday trade: a stop is required, the loss at the stop must
  fit your max loss per trade (measured from the higher of the plan's entry
  and a fresh price), reward:risk must meet your minimum, the day's total risk
  must fit your daily loss limit, no more than your trades per day, and no new
  entries outside your entry window (IST) or when the market is closed (weekend,
  NSE holiday, outside 09:15 to 15:30). It also keeps the basics: blocked
  symbols, order size, position cap, cash buffer, margin, short sales,
  futures and options, trade-to-trade (-BE) stocks bought as MIS, prices off the
  0.05 tick, whole-share quantities, duplicates and a daily order limit.
  Getting out is never blocked by a day limit. Prints PASS or BLOCK with the
  reason and records the verdicts so nse-broker only shows an order that
  passed. Use when the user says "check my order", "is this safe", "risk
  check", "does this break my rules", "can I take this trade", "how much have
  I risked today", or before /nse-broker, always.
argument-hint: "[path to orders.json]"
---

# nse-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by nse-budget or nse-broker's flatten passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to the broker. Most people who trade intraday lose money. These
limits keep each loss and each day's losses small. They do not make a trade
win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # nse/ files; writes nse/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-30 09:40"  # pin the clock (India time, IST)
```

Verified offline against fixtures; live endpoints not exercised at build
time. This skill makes no network calls; it only reads the files below.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/orders.json` | the proposed orders (nse-budget, or nse-broker's flatten) |
| `nse/config.json` | the limits (nse-budget ships `config.example.json`) |
| `nse/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `nse/prices.json` | a fresh price raises the entry it measures risk from; also values holdings |
| `nse/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `nse/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

An order has: `symbol` (the bare NSE symbol: RELIANCE, M&M, BAJAJ-AUTO, never
RELIANCE.NS), `exchange` (NSE), `side`, `qty` (whole shares), `product` (`MIS`
intraday or `CNC` delivery), `order_type` (`market`, `limit`, `sl`, `sl-m`),
`limit_price`, `trigger_price`, `validity` (`DAY` or `IOC`), and for an
entry its `stop_loss` and `take_profit`.

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, reward:risk, blocked
list, size, duplicates) turns into a warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, exchange not NSE or BSE, side not buy or sell, a qty that is not a whole number above zero (no fractional or rupee-amount orders on NSE), a product other than MIS or CNC, an unknown order type or validity, a limit or sl order with no `limit_price`, an sl or sl-m order with no `trigger_price`, a price that is not a multiple of the tick (`tick`, default 0.05), a `stop_loss` without a `take_profit` or the other way round |
| asset | a futures or options symbol (like NIFTY26NOV24500CE) while `allow_options` is false; a symbol that is not a plain NSE equity symbol, including RELIANCE.NS (use RELIANCE); a trade-to-trade series (-BE, -BZ, -BL) bought as MIS, because those stocks cannot be traded intraday |
| short | any new short position (selling more than you hold). This pack's portfolio tracker does not support short positions, so `allow_short: true` cannot enable them |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need borrowed money (MIS leverage) while `allow_margin` is false |
| stop | (entries) no stop while `require_stop` is true (the order's `stop_loss`, or the plan stop); a stop on the wrong side of the entry |
| loss | (entries) qty x (entry minus stop) over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` or after `no_entries_after`, or the market is closed |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `nse/prices.json` (under 15 minutes old). A plan written at 09:35 cannot
  understate the risk at 10:55 after the price ran up. An older price is not
  used, and it says so.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as nse-broker logged it. Three full-size trades use up a Rs 1,500 day.
  It is the gross loss at the stop: brokerage, STT and other charges are not
  in it (nse-budget prints an estimate).
- **The window** is India time (IST, UTC+05:30, no daylight saving), worked
  out in the tool, never from the computer's clock zone. Normal sessions
  run 09:15 to 15:30. The 09:00 to 09:15 pre-open call auction is not a
  session for entries. The special Muhurat session on Sunday 2026-11-08
  ran 14:20 to 15:15. Weekends and NSE holidays are closed. The holiday list in this
  tool is the 2026 list (16 weekday closures, from nseindia.com, cross-checked
  against several brokers on 2026-10-31); NSE has no half-days. For any date
  outside 2026 it warns "the holiday list covers 2026 only: check
  nseindia.com", because the 2027 list was not out yet. Muhurat trading
  on Sunday 2026-11-08 used a fixed entry window of 14:40 to 15:00,
  regardless of configured entry-window values. Set both `no_entries_*` to null to switch the
  window off.
- **No pattern-day-trader rule** applies in India, so there is no `pdt_guard`
  and no day-trade count.
- **Prices on the tick**: NSE rejects a price that is not a multiple of the
  tick, so this check does too. Most stocks tick at 0.05; set `tick` to 0.01
  for one that does not.

Warnings do not block, but say them to the user: a LIVE account, no product
given (treated as MIS), a stop that lives only in the plan and not on the
order, a price too old to use, an unknown 2027 calendar.

## Steps

1. If the price may have moved since the plan, refresh it first
   (nse-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop, reward:risk, and how much of today's limits it uses. For a
   BLOCK, give the reason and the fix (fewer shares, a closer stop from
   nse-exit, wait for the window, or skip the trade). Never change a
   limit yourself.
4. If the order changes in any way, run the check again. nse-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: the reel

The RELIANCE order from nse-budget at 09:40 on Friday 2026-10-30, on a fresh
Rs 1,00,000 paper account with the example config:

```
PASS  #1 buy RELIANCE 23 shares, entry
          entry 1432.60 (the plan's entry), stop 1411.55, target 1453.65: risk 21.05 a share
          max loss ₹484.15 (limit ₹500), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss

1 passed, 0 blocked at 2026-10-30 09:40 IST. Cash after the passed orders: ₹67,050.20 (paper account).
```

One more share and it is blocked:

```
BLOCK #1 buy RELIANCE 24 shares, entry
          entry 1432.60 (the plan's entry), stop 1411.55, target 1453.65: risk 21.05 a share
          max loss ₹505.20 (limit ₹500), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss
        - loss: 24 shares x 21.05 risk a share = ₹505.20 at the stop, over your ₹500 max loss per trade
```

The same 23 shares checked at 10:55 with a fresh quote of 1440.00 in
`nse/prices.json` are blocked twice: 23 x 28.45 = Rs 654.35 at the stop, and
reward:risk 0.48. At 09:20 they are blocked by the window (no new entries
before 09:30), at 14:45 by the window (none after 14:30), and on Tuesday
2026-10-20 because NSE was closed for Dussehra. The 15:05 sell that closes the
position passes, even though it is after the 14:30 entry cutoff: it is an
exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and a
  stop can fill worse than its price in a fast market, or not at all at a circuit limit.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Differences from the US pack

Rupees with lakh grouping, whole-share NSE order fields (exchange, product,
order_type, validity) instead of Alpaca's, a 0.05 tick check, India time and
the NSE calendar, trade-to-trade series and derivatives checks, and no
pattern-day-trader rule. The cash-buffer rule no longer fires when
`allow_margin` is true and the user has explicitly allowed borrowing.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper mode by default (`"paper": true`): nothing is sent to a broker. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No derivatives, margin or leverage unless the config allows it and the user confirms. Short entries are unsupported and remain blocked even if `allow_short` is set to true.
- Broker logins and keys are entered by the user in the broker's own sign-in, never in the chat.
