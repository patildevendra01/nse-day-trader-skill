---
name: nse-entry
description: >-
  Entry Timer for NSE stocks. Watches a setup bar by bar and tells you the
  moment to get in. Reads free Yahoo 1-minute or 5-minute bars for NSE
  symbols (RELIANCE, TCS, M&M, BAJAJ-AUTO), builds the opening range (the
  first 15 minutes of the 09:15 IST open by default) and calls the entry on
  the first completed bar that CLOSES above the range high, or above a level
  you give it (like a 20-day high from the scanner), with an optional volume
  check. Says waiting and how far, triggered with the time and the price in
  rupees, expired, or not open yet, and writes nse/entry.json for /nse-exit.
  Long side only, intraday. It never places an order. Use when the user says
  "tell me when to get in", "watch RELIANCE", "has it broken out", "did it
  break the opening range", "Nifty stock entry signal", "time my entry",
  "check my watchlist", or after /nse-scanner and /nse-news, before /nse-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# nse-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that.

Everything is India Standard Time (IST, UTC+05:30, no daylight saving). The
NSE trades Monday to Friday, 09:15 to 15:30 IST. From 09:00 to 09:15 is the
pre-open call auction: no continuous trading and no bars, so the clock for the
opening range starts at 09:15.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE                # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write        # also save nse/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --setup level --level 2912.50
python3 "${CLAUDE_SKILL_DIR}/entry.py" check TCS --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch M\&M --until 14:30     # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                     # every hit in nse/watchlist.json
```

Symbols are bare NSE symbols (RELIANCE, TCS, M&M, BAJAJ-AUTO). In a shell,
quote or escape the ampersand: `"M&M"`. The tool asks Yahoo for `SYMBOL.NS`
and URL-encodes it (`M%26M.NS`). A trailing `.NS` is accepted and dropped.

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-10-30 10:00"` (IST) to replay a day. `watch`
with `--now` or `--offline` runs on a simulated clock and replays the day bar
by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `nse/config.json` | `no_entries_before` (09:30) and `no_entries_after` (14:30), IST. Optional |
| Yahoo chart API | today's bars, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `nse/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the
  9:15, 9:20 and 9:25 five-minute bars, or fifteen 1-minute bars. The level
  to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/nse-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 09:55 bar closes at 10:00 AM).
  The price is its close, in rupees as Yahoo reports it.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 10:34:45, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. The 9:15 bar carries the
  open, so 1.0 is already strict.
- **Time limits.** No entries before 09:30 or after 14:30 (config). That
  leaves about 45 minutes before the 15:15 flat-by, because brokers auto
  square off MIS positions around 15:15 to 15:25 and charge for it. The NSE has
  no scheduled half days; if it ever announces a shortened session the cutoff
  moves up with the close.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 09:15: not open yet (pre-open).
  Before 09:30: the range is still forming. Weekends and NSE holidays: the
  market is closed. The 2026 holiday list is built in. The official 2027 list
  is not out yet, so for 2027 the tool says the calendar is unknown: check
  nseindia.com.
- **Not for every stock.** Only EQ-series stocks trade intraday. Stocks in
  the BE and BZ series are trade-to-trade (delivery only), so skip them.
  Stocks under surveillance measures or at their circuit limit can behave
  differently: check the exchange before trading one.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/nse-scanner`. Check `/nse-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:30` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `nse/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/nse-exit` to set the stop and the target BEFORE anything
   is bought.

## Example

RELIANCE, Friday 2026-10-30, 5-minute bars (synthetic test data, not a market
record). The opening range is 2,764.85 to 2,825.00. At 09:57 the 09:55 bar is
still open, so it waits:

```
WAITING  last close ₹2,821.10 (9:50 bar), ₹3.90 under the range high ₹2,825.00 (-0.14%).
The 9:55 bar is still forming (₹2,840.55 so far, closes 10:00 AM); it does not count until it closes.
```

At 10:00 the bar has closed:

```
RELIANCE  Fri 2026-10-30 10:00 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 9:15-9:30: high ₹2,825.00, low ₹2,764.85 (3 of 3 bars).
TRIGGERED 10:00 AM at ₹2,840.55: the 9:55 bar (bar 8) closed above the range high ₹2,825.00 (+0.55%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /nse-exit (exit.py plan --from nse/entry.json).
```

With `--min-volume-x 1` the 09:55 bar is too thin (0.60x the range's volume)
and the trigger moves to 10:20 AM at ₹2,852.85 (the 10:15 bar, 7.98x).

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. This pack plans longs only: MIS short selling exists in
  India, but nse-entry does not time it. If the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/nse-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/nse-broker` from a background
watch: nobody is there to say yes.

Verified offline against fixtures; live endpoints not exercised at build time.

## Differences from the US pack

IST instead of US Eastern time, no daylight saving, 09:15 open and 15:30
close, bare NSE symbols with `.NS` added for Yahoo, rupee prices with Indian
digit grouping, an earlier last-entry time (14:30) because of the MIS
square-off, and the NSE 2026 holiday list instead of the US exchange one.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper mode by default (`"paper": true`): nothing is sent to a broker. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No derivatives, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker logins and keys are entered by the user in the broker's own sign-in, never in the chat.
