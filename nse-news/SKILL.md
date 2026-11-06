---
name: nse-news
description: >-
  News Monitor. Tracks breaking news on every NSE stock you watch and flags
  what moves it: Yahoo Finance headlines per symbol, each company's NSE
  corporate announcements (financial results, board meetings, director
  changes, acquisitions, buy-backs, insolvency, credit ratings) and how far
  each stock is from yesterday's close. Shows only what is new since the
  last check, newest first, in India time (IST), tags each item (earnings,
  analyst, deal, capital, regulatory, macro...) and never repeats one. Use
  when the user says "any news on RELIANCE", "why is this moving", "what's
  the news on my watchlist", "morning briefing", "pre-market news", "monitor
  the news", "did anything break", "check the announcements", or after
  /nse-scanner finds setups.
argument-hint: "[symbols]"
---

# nse-news

A chart can look perfect and still be about to gap on a results date, a board
meeting or a regulatory order. Before a setup becomes a trade, this skill
reads the two feeds that carry those first, for every stock on the watchlist,
and tells you only what is new. It also prints how far each stock has moved
today, so a headline and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                         # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS "M&M" --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-filings --json     # headlines only
```

Symbols are bare NSE symbols (RELIANCE, M&M, BAJAJ-AUTO). Times are IST
(UTC+05:30, no daylight saving).

Verified offline against fixtures; live endpoints not exercised at build
time. NSE often refuses cloud, VPN and data-centre addresses: when it does the
tool says so once and the headlines still run.

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `nse/watchlist.json` | the scanner's hits (nse-scanner) |
| `nse/plan.json` | the trade you are planning (nse-exit) |
| `nse/portfolio.json` | what you hold (nse-broker records fills there) |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN` |
| Yahoo Finance chart endpoint | the last price against yesterday's close (unofficial, can lag) |
| NSE corporate announcements | `https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE`, after the cookie handshake NSE needs (visit nseindia.com first, browser User-Agent). Best effort. `--no-filings` skips it |
| `nse/state/news.json` | what has been shown (kept 30 days) |

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the announcement and read it. Quote the source
  and link it. Never summarise a paywalled story you could not read: say so.
- **Announcements beat headlines.** If an NSE announcement and a headline
  disagree, the announcement is what the company told the exchange.
- **Say what it touches.** Every item lists the tickers it touches and its
  tags: earnings, analyst, legal, deal, capital, people, risk, halt,
  regulatory, macro. Announcements marked `!` carry something important:
  financial results, outcome of a board meeting, change in directors or key
  managerial personnel, an acquisition or merger, a scheme of arrangement,
  insolvency or default, fraud, delisting or suspension, an open offer, a
  buy-back, fund raising, a credit rating, an auditor change or a resignation.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` announcement, anything tagged
   earnings, halt, legal, regulatory or risk, and every MOVING symbol first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are today: an overnight gap can jump past any stop").
4. If a story changes the picture, say so before `/nse-entry` and
   `/nse-exit`: a results date inside the trade, a board meeting, a halt.
   Never act on a headline alone.

During the session: `/loop 15m /nse-news`, or a scheduled task each morning
before the 09:15 open (the pre-open call auction runs 09:00 to 09:15).

## Example (offline fixture, Friday 2026-10-30, 11:00 IST)

```text
RELIANCE    ₹1,494.60 +3.15% vs yesterday's close ₹1,448.90 at 10:52 IST  MOVING
  2026-10-30 07:40 IST  RELIANCE    Yahoo Finance Reliance Industries shares in focus ahead of quarterly results
      tags: earnings
! 2026-10-29 17:42 IST  RELIANCE    NSE           Reliance Industries Limited: Financial Results
      tags: earnings
  2026-10-29 17:00 IST  RELIANCE    Yahoo Finance Reliance board to consider dividend and buyback proposal
      tags: capital
```

The brief: "Results came out after the close yesterday, and the stock is up
3.15 % at 10:52. Today's trade is in and out before 15:15. No other
announcement." When a symbol has no news, say so rather than padding.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per symbol, a pause between
  calls, and it stops asking NSE after the first refusal.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/nse-broker`, after
`/nse-risk` passes and the user types an explicit yes for that order.
