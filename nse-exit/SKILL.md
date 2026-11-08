---
name: nse-exit
description: >-
  Exit Planner for NSE stocks. Sets your stop and your target before you enter,
  so you know when to get out. Reads the entry from nse-entry (or a price you
  give), puts the stop at the opening-range midpoint by default (or the range
  low, your price, a percent, or a multiple of the ATR), rounds it down to the
  NSE tick of ₹0.05, and sets the target at 2R (or 1R, or your price), rounded
  up to the tick. Refuses a stop at or above the entry, a target at or below
  it, or a stop too tight to survive the spread, and says why. Writes
  nse/plan.json for the Time Machine, the Budget Manager and Price Alerts, with
  prices in rupees and times in IST. Long side only, intraday. Use when the user
  says "where's my stop", "set my stop and target", "plan the exit", "when do I
  get out", "stop loss", "what's my R", "risk reward", or right after
  /nse-entry triggers, before /nse-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X] [--tick 0.05]"
---

# nse-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap, a circuit limit or a fast market can fill past it. Day trading loses most
people money, and a neat plan does not change that.

Everything is India Standard Time (IST, UTC+05:30, no daylight saving). The
Normal NSE sessions run Monday to Friday, 09:15 to 15:30 IST. The special
Muhurat session on Sunday 2026-11-08 ran 14:20 to 15:15 IST. Prices are in
rupees. The default Muhurat flat-by time was 15:10.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # nse/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 2840.55 --or-high 2825 --or-low 2764.85
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current nse/plan.json
```

Other flags: `--flat-by 15:15`, `--tick 0.05`, `--json`, `--now "2026-10-30
10:15"` (IST), `--entry PRICE` with a waiting entry.json to plan ahead,
`--setup level`, `--offline FILE` (a saved Yahoo chart JSON for an ATR stop).
Symbols are bare NSE symbols (RELIANCE, TCS, M&M); a trailing `.NS` is dropped.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/entry.json` | the trigger from nse-entry: symbol, entry price, time, bar, opening range |
| `nse/config.json` | `flat_by` (15:15), `tick` (0.05) and `min_reward_risk` (1.0: a note here, nse-risk enforces it) |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar, free and unofficial, can lag |
| `nse/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, flat by, tick. nse-timemachine, nse-budget and nse-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price (`₹` and commas are fine: `price:₹2,794.90`) |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Rounding to the tick.** The NSE quotes cash equities in steps of ₹0.05.
  The stop rounds AWAY from the entry to the tick (down, for a long). Risk =
  entry minus stop. Target = entry + R x risk, rounded UP to the tick. `2R`
  by default; `1R`, `1.5R` or `price:X` also work. Set `tick` in the config
  (or `--tick`) for a stock with a different tick size.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price or one tick, whichever is more:
  too tight to survive the spread, normal wiggle would stop you out. An
  entry.json that has not triggered (unless you give `--entry`). A short: this
  pack plans longs only.
- **Flat by.** 15:15 by default (or `flat_by` in the config). Out by then,
  win or lose, no overnight hold. Brokers auto square off MIS positions around
  15:15 to 15:25 and charge for it, so the plan beats them to it. The NSE has no
  scheduled half days; if it announces a shortened session the flat-by moves up
  with the close. An entry at or after the flat-by time is refused.
- **Only EQ series.** BE and BZ stocks are trade-to-trade and cannot be traded
  intraday, so there is nothing to plan.

## Steps

1. Run it right after `/nse-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/nse-timemachine` to replay the plan, then `/nse-budget`
   to size it to the max loss per trade. Offer `/nse-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: the reel

RELIANCE, Friday 2026-10-30 (synthetic test data, not a market record). The
entry is ₹2,840.55 at 10:00 AM (the 09:55 bar, bar 8) and the opening range is
₹2,764.85 to ₹2,825.00. `exit.py plan --stop or-mid --target 1R --write`:

```
Exit plan: RELIANCE long, Fri 2026-10-30, opening-range breakout on 5-minute bars
  Entry         ₹2,840.55 at 10:00 AM (bar 8)
  Stop          ₹2,794.90  opening-range midpoint: (₹2,825.00 + ₹2,764.85) / 2 = ₹2794.925, rounded down to the ₹0.05 tick
  Target        ₹2,886.20  1R (one times the risk)
  Risk a share  ₹45.65 (1.61% of the entry)
  Reward:risk   1.0
  Flat by       15:15 IST, win or lose: no overnight hold
Stop and target sit on the NSE tick of ₹0.05 (stop rounded down, target rounded up).
```

The default 2R target would be 2,840.55 + 2 x 45.65 = ₹2,931.85. nse-budget then
sizes it: a ₹1,000 max loss per trade buys floor(1000 / 45.65) = 21 shares.

A stop of `price:2840.40` is refused: it is ₹0.15 under the entry, less than
0.05% of the price (₹1.42), too tight to survive the spread.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only. This pack plans longs only: MIS short selling exists in
  India, but nse-exit does not plan it.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

Verified offline against fixtures; live endpoints not exercised at build time.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper mode by default (`"paper": true`): nothing is sent to a broker. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No derivatives, margin or leverage unless the config allows it and the user confirms. Short entries are unsupported and remain blocked even if `allow_short` is set to true.
- Broker logins and keys are entered by the user in the broker's own sign-in, never in the chat.
