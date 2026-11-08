# The Day Trader skill for India

11 Claude skills for intraday trading on the NSE: scan the Nifty 500 for setups, track news and exchange announcements, time every entry and exit, and replay every outcome before you risk a rupee.

Free, MIT, no signup, no API key. The tools are plain Python that read free
public data: Yahoo Finance charts, NSE's archive lists and corporate
announcements. Everything is in rupees (₹), India time (IST) and NSE rules.

This is an India port of Jake Schincariol's day-trader-skill (the US
version, built for Nasdaq and NYSE). The method and the three order gates are
his; the market data, calendar, currency, costs and broker are rebuilt for NSE.

One scans hundreds of charts for setups. Another tracks news and NSE
announcements on every stock you watch. One watches a setup bar by bar and
tells you the moment to get in, and another sets your stop and your target
before you do. And the craziest one is the Time Machine: before you risk a
rupee, it replays the rest of the session 10,000 times from the stock's own
recent bars, plus every past day the same setup fired, and tells you the
honest odds.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

## Verified how, and how far

Every tool was tested **offline against synthetic fixtures** shaped like the
real files and feeds (Yahoo chart JSON, NSE archive CSVs, NSE announcement
JSON). **The live endpoints were not exercised when this was built**, because
the build sandbox could not reach them. Expect to fix small things on first
live use, and treat the example numbers below as fixture output, not market
data. NSE's website APIs often refuse cloud, VPN and data-centre addresses;
the tools say so once and carry on with what still works.

## Install

Copy the skill folders into your Claude skills folder:

```bash
cp -r nse-day-trader-skill/nse-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. As a plugin, point Claude Code at this folder (it has a
`.claude-plugin/plugin.json`); installed as a plugin the skills are namespaced
as `/nse-day-trader-skill:nse-scanner` and so on. No Claude Code at all? Paste
any single `SKILL.md` at the top of a chat and it runs as a mode. You lose
the Python tools, but the method works.

The tools need Python 3.8 or newer. Nothing to pip install. No keys, no
email, no sign-up.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/nse-scanner` | Chart Scanner | Scans the Nifty 500 (or Nifty 50, or every EQ stock) for setups: opening-range breakouts, flags, oversold bounces. |
| `/nse-news` | News Monitor | Tracks Yahoo headlines and NSE announcements on every stock you watch and flags what moves it. |
| `/nse-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/nse-exit` | Exit Planner | Sets your stop and your target before you enter, so you know when to get out. |
| `/nse-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions plus every past time the setup fired. |
| `/nse-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/nse-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade, with an estimate of intraday costs. |
| `/nse-broker` | Order Executor | Sends approved orders to your broker (Zerodha Kite as the worked example), paper trading by default, never without your yes. |
| `/nse-alerts` | Price Alerts | Rings when a price, a stop or the 09:15 opening bell hits, and flags the flat-by time. |
| `/nse-analyst` | Earnings Analyst | Reads quarterly results (YoY and QoQ, in ₹ crore) and sums up what changed. |
| `/nse-deals` | Deal Tracker | Reads NSE bulk and block deals (and promoter trades) and flags the stocks and investors you follow. |

## The India rules it encodes

- **Hours:** regular sessions have pre-open auction 09:00 to 09:15 (no entries)
  and continuous trading 09:15 to 15:30 IST (75 five-minute bars). The special
  Muhurat session on 2026-11-08 ran 14:20 to 15:15 IST; its 15-minute opening
  range ended at 14:35, new entries were limited to 14:40–15:00, and positions
  were required to be flat by 15:10. IST is UTC+05:30, with no daylight saving.
- **Calendar:** 2026 NSE weekday holidays are built in. The special Diwali
  Muhurat session on Sunday 2026-11-08 is explicitly supported by the scanner,
  entry, exit, risk, alert and Time Machine tools. The Time Machine reads the
  14:20–15:15 Muhurat plan session and compares it with matching afternoon bars
  from prior full regular sessions. This is an afternoon analogue, not a pool
  of historical Muhurat sessions. The 2027 holiday list is not built in; check
  the official NSE calendar before using the tools for 2027.
- **Series:** EQ only. BE and BZ are trade-to-trade (no intraday).
- **Symbols:** bare NSE symbols (RELIANCE, M&M, BAJAJ-AUTO); Yahoo gets
  `SYMBOL.NS` with `&` URL-encoded.
- **Orders:** MIS (intraday, auto squared off) or CNC (delivery), whole
  shares, long only, tick size ₹0.05 (a stop rounds down, a target rounds up).
  Bracket orders are discontinued at most brokers, so `/nse-broker` makes an
  entry ticket plus a separate stop-loss and take-profit ticket and tells you
  to cancel the other leg when one fills.
- **Square-off:** regular-session default flat-by is 15:15 IST. The 2026
  Muhurat session uses a stricter 15:10 flat-by time. Brokers often auto
  square-off MIS between 15:15 and 15:25 and may charge for it.
- **No PDT rule** applies in India.
- **Costs** (brokerage, STT, exchange charges, SEBI fee, stamp duty, GST) are
  printed as an **estimate**. Check your broker's contract note.

## How to use it

Everything lands in an `nse/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by the session-specific cutoff                     (while it is open)
news / analyst / deals                                                        (on a schedule)
```

An example, from the offline fixtures: RELIANCE, Friday 30 October 2026.
Prices are synthetic.

1. **`/nse-scanner`** at 10:15 IST: of the fixture's 43 usable charts, 32 had
   closed above their 15-minute opening range (09:15 to 09:30).
2. **`/nse-news`**: RELIANCE up 3.15 % at 10:52, an NSE "Financial Results"
   announcement flagged `!` from the evening before, all in IST. A day trade
   that is out before 15:15 is not exposed to the next gap.
3. **`/nse-entry`** watched RELIANCE bar by bar: waiting while the bar is
   still forming, then triggered on the first 5-minute close above the range
   high, ₹1,455.50 at 10:15.
4. **`/nse-exit`** set the plan: stop at the range midpoint, ₹1,451.00 (₹4.50 a
   share at risk), target 2R, ₹1,464.50, flat by 15:15.
5. **`/nse-timemachine`** replayed the rest of the day 10,000 times (seed 19):
   target first 32.3 %, stop first 64.5 %, still open at the close 3.1 %. The
   same rule fired 26 times on the 39 past sessions: 6 target, 13 stop, 7
   neither. Verdict: close to break-even. That is the point of looking before
   you risk a rupee.
6. **`/nse-budget`** with ₹1,00,000 and a ₹500 max loss per trade: 500 / 4.50
   allows 111 shares, but the ₹50,000 max order binds, so **34 shares**
   (₹49,487). Max loss at the stop ₹153; estimated costs about ₹52.
7. **`/nse-risk`** checks it against your limits: PASS or BLOCK, with the reason.
8. **`/nse-broker`** shows the tickets in plain words. You type yes for that
   exact order. Claude calls your broker connector (Zerodha's Kite MCP as the
   worked example), and Claude's own "Allow once / Deny" card asks again.
9. **`/nse-alerts`** rings on the stop, the target and at the session's
   flat-by time (15:15 regular session; 15:10 on the 2026 Muhurat session).

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 nse-scanner/scan.py universe                                # Nifty 500, EQ series only
python3 nse-scanner/scan.py daily                                   # pre-market setups
python3 nse-scanner/scan.py orb --at 10:15                          # opening-range breakouts so far
python3 nse-news/news.py --tickers RELIANCE --hours 12              # headlines, NSE announcements, today's move
python3 nse-entry/entry.py check RELIANCE --setup orb               # waiting / triggered / expired
python3 nse-exit/exit.py plan --stop or-mid --target 2R --write     # -> nse/plan.json
python3 nse-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 nse-budget/size.py --write                                  # -> nse/orders.json
python3 nse-risk/check.py                                           # PASS / BLOCK per order
python3 nse-broker/ticket.py show                                   # the tickets, READY or not
python3 nse-alerts/alerts.py add --from-plan                        # stop, target, session-specific flat-by
python3 nse-alerts/alerts.py check                                  # rings what hit, once
python3 nse-analyst/results.py RELIANCE                             # quarters, YoY, QoQ, EPS
python3 nse-deals/deals.py --watch "Vanguard"                       # new bulk and block deals
```

```bash
cd tests && python3 -m unittest discover -s . -v
```

The tests run offline on synthetic fixtures in the real shapes. They check
the algorithms against independent recomputations (a brute-force opening-range
scan, a Decimal replay of the history) rather than against their own output.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Studies, SEBI's among them, have found that most retail intraday traders
lose money. You can lose money. Decide for yourself, and talk to a
SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `/nse-broker` can lead to
an order, and only after `/nse-risk` passes that exact order and you type
yes for it in the chat. Set your broker tool to "Needs approval" in Claude,
never "Always allow", so Claude's own "Allow once / Deny" card is a second
check. No options, margin, short selling or leverage unless you turn them on.
The pack is long-only; MIS short selling exists but is not done here.

**Paper first.** `"paper": true` is the default in `config.example.json`.
Broker keys and tokens live in environment variables, never in the chat. The
broker tool names are not hard-coded: `/nse-broker` renders Kite Connect's
`place_order` parameters and tells you to map them to whatever your connector
exposes. Check the connector's own tool list.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: a results announcement, a circuit, a headline.

**A stop is a plan, not a guarantee.** In a fast market, across a gap or at a
circuit limit, a stop order can fill well past its price or not at all. The
max loss shown is the loss at the stop price.

**The data is free and unofficial.** Yahoo Finance and NSE's website APIs
are not official feeds for this use: they can lag, change or block you. The
tools read 5-minute bars, not every tick. Check anything that matters
against your broker before you act on it.

**Taxes.** Intraday equity gains are speculative business income in India,
and brokerage, STT and charges apply. Nothing here is tax advice.

## Files

```
nse-scanner/     SKILL.md, scan.py
nse-news/        SKILL.md, news.py
nse-entry/       SKILL.md, entry.py
nse-exit/        SKILL.md, exit.py
nse-timemachine/ SKILL.md, timemachine.py
nse-risk/        SKILL.md, check.py
nse-budget/      SKILL.md, size.py, config.example.json, targets.example.json
nse-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
nse-alerts/      SKILL.md, alerts.py, alerts.example.json
nse-analyst/     SKILL.md, results.py, metrics.example.json
nse-deals/       SKILL.md, deals.py
tests/           unittest suite + offline fixtures
```

MIT. The original US pack is by Jake Schincariol; this port keeps his license.
