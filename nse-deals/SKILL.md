---
name: nse-deals
description: >-
  Deal Tracker. Watches NSE bulk deals and block deals (the days when a fund
  or a big client moves more than 0.5 % of a company, or trades a negotiated
  block), alerts once per new deal row, and flags deals in the stocks you
  watch or hold and by clients you name, such as "Rakesh Jhunjhunwala" or
  "Vanguard". Each row: date, symbol, client, buy or sell, quantity, average
  price and value in crore rupees. Optionally adds insider and promoter
  trades (SEBI PIT disclosures) for your tickers. Use when the user says "any
  bulk deals", "block deals today", "what is Jhunjhunwala buying", "are funds
  buying RELIANCE", "promoter buying", "insider trades", "track big
  investors", "alert me on bulk deals", or "run the deals skill".
argument-hint: "[--watch client names] [--tickers SYMBOLS] [--insiders]"
---

# nse-deals

Every trading day NSE publishes two lists: **bulk deals** (a client trades
more than 0.5 % of a company's shares in a day) and **block deals** (a
negotiated trade in the special window). Both name the client. This skill
reads them, prints only what is new since the last check, and puts the deals
that touch you first.

It is fast, not early. The files are posted after the close, so a deal is
always a fact about the day before you read it. There is no inside
information here: only public disclosures, read quickly. Say that every time.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/deals.py"                                  # new bulk + block deals
python3 "${CLAUDE_SKILL_DIR}/deals.py" --watch "Rakesh Jhunjhunwala" Vanguard
python3 "${CLAUDE_SKILL_DIR}/deals.py" --tickers RELIANCE TCS --all     # only these, seen or not
python3 "${CLAUDE_SKILL_DIR}/deals.py" --insiders --write               # add promoter / insider trades, save nse/deals.json
```

## What it reads and writes

- Reads `https://nsearchives.nseindia.com/content/equities/bulk.csv` and
  `block.csv` (static files for the latest session).
- With `--insiders`, reads
  `https://www.nseindia.com/api/corporates-pit?index=equities&symbol=<SYMBOL>`
  for each of your tickers. NSE wants a browser User-Agent and the cookie from
  its home page, so the tool visits the home page first. NSE often refuses
  cloud, VPN and data-centre addresses: when it does, the tool says so once,
  skips the rest of the insider lookups and the deal files still run.
- Reads `nse/watchlist.json`, `nse/plan.json` and `nse/portfolio.json` for
  "your" tickers.
- Writes `nse/state/deals.json` (deals already shown, kept 30 days, so each
  deal alerts once) and, with `--write`, `nse/deals.json`.
- Verified offline against fixtures; live endpoints not exercised at build
  time. If a file's layout has changed the tool prints a plain note instead of
  guessing.

## The rules

- **Quote the file.** Every row is from NSE's own list. Say which list (bulk,
  block or insider) and the date.
- **Clients are names, not intentions.** A fund buying is not a forecast. A
  deal can be a hedge, an index rebalance or an arbitrage leg. Never say
  "smart money".
- **Say both sides.** Brokers and funds often appear on both sides of a
  stock on the same day. Show the sell next to the buy.
- **Value in crore.** Quantity times price, in crore rupees (1 crore = 10
  million), with Indian digit grouping.
- **Insider rows are disclosures.** Promoter and director trades reach NSE
  days after the trade; the date shown is the trade date, not the filing.
- **BIG** means 100 crore or more in one row. It is a size flag, not advice.

## Steps

1. Run the tool. Show the "on your list" rows first (YOURS = a ticker you
   watch or hold, WATCHED = a client you named), then the rest.
2. For a stock on your list, offer `/nse-news` for the headline behind it and
   `/nse-entry` only if a setup already exists.
3. To watch a client, pass `--watch` with the name (case and punctuation do
   not matter). Ask the user which names; never invent a list of "good"
   investors.
4. If the tool says NSE refused, say so plainly: bulk and block files may
   still have worked. Do not retry in a loop.

## What it never does

- Never places an order and never turns a deal into a signal.
- Never scrapes behind a login or reads anything outside NSE's public files.
- Never calls a deal "insider information": it is a public disclosure.

## Differences from the US pack

This replaces the Pelosi tracker (US Congress STOCK Act filings). India has
no equivalent for politicians; bulk, block and promoter disclosures are the
public record of who is moving big size.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper mode by default (`"paper": true`): nothing is sent to a broker. Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No derivatives, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker logins and keys are entered by the user in the broker's own sign-in, never in the chat.
