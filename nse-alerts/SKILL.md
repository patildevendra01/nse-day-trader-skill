---
name: nse-alerts
description: >-
  Price Alerts for NSE stocks. Rings when a price, a stop or the opening bell
  hits. Turns the trade plan into three alerts in one go (the stop, the target
  and "flat by close" at your flat-by time, 15:15 IST by default), plus above /
  below / stop / target levels, percent moves from the previous close, any time
  of day in IST, and the 09:15 NSE opening bell, each ringing once so you are
  not spammed. Knows NSE hours and the 2026 trading holidays, works out IST
  itself (UTC+05:30, no daylight saving), fetches free delayed quotes (Yahoo
  chart JSON, symbol.NS) one ticker at a time, shows prices in rupees, and
  writes nse/prices.json for sizing and the risk check. Use when the user says
  "alert me when", "tell me if RELIANCE hits", "watch my stop", "watch my
  trade", "remind me to close by 3:15", "is the market open", "when does the
  market open", "get me a price", or right after /nse-exit writes a plan.
argument-hint: "[symbol] [price]"
---

# nse-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. With a bracket order the stop-loss and the
target already sit at the broker and do the work; the alerts tell the user
what happened and when it is time to be flat.

Everything is India Standard Time (IST, UTC+05:30, no daylight saving) and
rupees. The NSE trades Monday to Friday, 09:15 to 15:30 IST. From 09:00 to
09:15 is the pre-open call auction: no continuous trading, so the bell rings
at 09:15.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by close from nse/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 2794.90
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 2886.20
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:15 --label "flat by close" --before-close
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # -> nse/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-30 15:15"` pins the clock (IST); `--offline DIR` reads saved
`yahoo-<SYMBOL>.json` files instead of the network. Symbols are bare NSE
symbols (RELIANCE, TCS, M&M, BAJAJ-AUTO); the tool asks Yahoo for `SYMBOL.NS`
and URL-encodes it, so `M&M` becomes `M%26M.NS`. A trailing `.NS` is dropped.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/plan.json` | the trade from nse-exit, for `add --from-plan` |
| `nse/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `nse/state/alerts.json` | which rules rang and when, so each rings once |
| `nse/prices.json` | the latest quotes, merged in, for nse-budget and nse-risk |
| `nse/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:15") on a trading day | once per trading day |
| `opening_bell` | the NSE is open | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. A `time` rule
with `"before_close": true` moves earlier by the same amount the close moves
if the NSE ever announces a shortened session (none is scheduled), so "flat by
close" at 15:15 would ring 3 hours earlier if the close came at 12:30.

## Quotes

- `yahoo` is the only source: `query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS`,
  an unofficial free JSON endpoint, one request per symbol, half a second
  apart. On a 429 it waits and tries once more; after that it tries `query1`.
  It identifies itself as `nse-day-trader-skill/1.0`. Stooq has no NSE data
  and was dropped.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `nse/prices.json` keeps its quote time, and
  nse-risk only uses one under 15 minutes old.
- If Yahoo is down, read the price from the broker connector and write it into
  `nse/prices.json` as `{"as_of": "...Z", "prices": {"RELIANCE": 2858.5}}`.

## The NSE clock

Regular session 09:15 to 15:30 IST, closed on weekends and on NSE trading
holidays. The 2026 weekday closures built in: Jan 15, Jan 26, Mar 3, Mar 26,
Mar 31, Apr 3, Apr 14, May 1, May 28, Jun 26, Sep 14, Oct 2, Oct 20
(Dussehra), Nov 10, Nov 24 and Dec 25. Special sessions such as the Diwali
Muhurat trading hour (Sunday 2026-11-08) fall on a weekend, so the tool says
closed: check nseindia.com for that one. The NSE has no scheduled early closes.

For any other year the tool says the calendar is unknown: check nseindia.com.
The 2027 list was not out when this was built.

## Steps

1. Right after nse-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop or target alert means the
   bracket order has probably filled: check the position and record the fill
   with nse-broker. "Flat by close" means close anything still open, because
   brokers auto square off MIS positions around 15:15 to 15:25 and charge for
   it: nse-broker `flatten`, then nse-risk, then the user's yes. Never act
   for them.

## Example: the reel

RELIANCE on Friday 2026-10-30 (synthetic test data, not a market record):
entry ₹2,840.55 at 10:00 AM, stop ₹2,794.90, target ₹2,886.20, flat by 15:15.
`add --from-plan` writes `reliance-plan-stop`, `reliance-plan-target` and
`reliance-flat-by` for that day. A `check` at 10:37 reads the 10:37 quote,
₹2,858.50, between the stop and the target:

```
NSE OPEN (2026-10-30 10:37 IST, regular session).
Nothing hit. 3 rule(s) watched today, RELIANCE ₹2,858.50.
```

At 3:15 PM:

```
ALERT Flat by close: it is 15:15 IST (set for 15:15), the market closes at 15:30. If RELIANCE is still open, close it:
nse-broker flatten --symbol RELIANCE builds the sell ticket, then nse-risk and your yes. Brokers auto square off MIS
positions around 15:15 to 15:25 and charge for it.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 09:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

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
