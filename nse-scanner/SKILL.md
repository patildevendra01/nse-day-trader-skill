---
name: nse-scanner
description: >-
  Chart Scanner for NSE stocks. Scans hundreds of charts for setups:
  opening-range breakouts, flags, oversold bounces. Builds the universe from
  NSE's own lists (Nifty 500 by default, or Nifty 50, or every EQ-series
  stock), keeps only the EQ series that can trade intraday, runs a
  pre-market scan of daily closes (20-day closing-high breakout, bull flag,
  reclaimed the 20-day average, oversold, oversold bounce) and an intraday
  opening-range breakout scan on 5-minute bars, then writes
  nse/watchlist.json for nse-news and nse-entry. Free Yahoo data, bar by
  bar, India time. Use when the user says "scan the market", "scan Nifty",
  "find me setups", "what is breaking out", "opening range breakouts",
  "scan for bull flags", "oversold stocks", "pre-market scan", "build my
  watchlist", "what should I look at today", or at the start of a trading
  day before /nse-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# nse-scanner

The Chart Scanner is the first step of a day trade. It reads hundreds of free
charts and lists the ones that match a rule. That is all it does. A scan is a
list of charts that match a rule, not a list of stocks to buy, and a setup on
a chart is not a prediction. Most people who day trade lose money; the
scanner does not change that. It only saves you from flipping through 500
charts by hand.

Verified offline against fixtures; live endpoints not exercised at build
time. NSE's archive files and Yahoo's endpoints are free and unofficial, and
NSE may refuse cloud or VPN addresses.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                          # Nifty 500 -> nse/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --universe nifty50       # or all-eq
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                             # before 09:15 -> nse/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,M\&M # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                               # after 09:35 IST -> nse/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                    # cut the clock at 10:15 IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist nse/watchlist.json --json
```

Flags: `--min-price 20` (rupees) and `--min-volume` (daily 1,000,000, orb 0),
`--min-turnover-cr` (price times volume, in crore rupees), `--or-minutes 15`,
`--max-confirm 200` (orb stage 2), `--now "2026-10-30 10:15"` (India time) to
pin the clock, `--offline PATH` to read saved files, `--no-write`, `--show N`,
`--json`. Exit code 0 when the scan found something, 1 when it found nothing
or could not run (the reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `nse/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `nse/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. nse-news and nse-entry read `hits[].symbol` |
| `nse/config.json` | read by `orb`: `no_entries_after` (default 14:30) |

## The universe

`universe` downloads one of NSE's static lists from nsearchives.nseindia.com
and keeps a row when:

1. The series is EQ. BE and BZ are trade-to-trade: no intraday, so they are
   dropped and counted.
2. It is not an ETF or a fund.
3. It has a symbol that builds a Yahoo ticker (`SYMBOL.NS`, with `&` encoded
   as `%26`, so M&M becomes `M%26M.NS`; `BAJAJ-AUTO` keeps its hyphen).

Lists: `nifty500` (`ind_nifty500list.csv`, the default), `nifty50`
(`ind_nifty50list.csv`) and `all-eq` (`EQUITY_L.csv`, every listed company).
Header spellings and a leading byte-order mark are tolerated. Nifty 500 is the
default because day trading wants liquidity; the long tail of `all-eq` is
mostly thin.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so Nifty 500
takes about 25 requests with the polite 0.3 s pause. Liquidity comes from the
spark meta: last price at least `--min-price` (default ₹20) and last
session's volume at least `--min-volume`. Run it before the open or after the
close: during the session the volume is the day's so far, and today's daily
bar is left out until 15:30.

The scanner requires each chart's latest completed daily candle to match the latest completed NSE session; stale charts are skipped and counted. The setups are computed from daily closes only (spark has no highs or lows):

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Daily results also carry a `classification`: `momentum_watch` when at least one directional setup is present (20-day breakout, bull flag, MA20 reclaim or oversold bounce), otherwise `oversold_only_watch`. An oversold-only chart is context for further review, never a long-entry signal.

Hits are listed most traded first (price times volume). A flat 14-period RSI is treated as neutral (50), not overbought. A chart with an old latest candle is excluded rather than being presented as a current setup.

## Offline evaluation of daily setups

Use the separate evaluator with saved Yahoo v8 daily OHLC charts (not spark closes). The chart bundle must include Yahoo's `adjclose` array; charts without complete adjusted-close data are rejected:

```bash
python3 "${CLAUDE_SKILL_DIR}/evaluate.py" --offline /path/to/daily-chart-bundle.json --json
python3 "${CLAUDE_SKILL_DIR}/evaluate.py" --offline /path/to/chart-folder --symbols RELIANCE,TCS --round-trip-cost-pct 0.30
python3 "${CLAUDE_SKILL_DIR}/evaluate.py" --offline /path/to/chart-folder --universe-history /path/to/nse-membership.csv --json
```

The evaluator identifies each daily setup using data available at that signal-day close, enters at the next available daily bar's adjusted open, and measures the adjusted close after 1, 3 and 5 held sessions. It scales each OHLC bar by that day's adjusted-close / raw-close factor, so entry, exit and excursion calculations share Yahoo's split- and dividend-adjusted price basis. Missing or incomplete adjusted-close arrays are rejected and listed in the report. If the newest input candle belongs to today's still-open NSE session, it is excluded so a partial candle cannot create a signal or outcome. It reports win rate, average/median return, best/worst return, and maximum favourable/adverse price excursion for each observed setup. The JSON output includes every observation for independent review. Before evaluation, the tool checks that timestamp and OHLC/adjusted-close arrays align, timestamps are valid and strictly chronological, values are finite and positive, and each bar's high/low contains its open and close. Symbols with structural or OHLC errors are rejected and listed. It also flags gaps of four or more calendar days as *possible* missing sessions; these are warnings, not proof, because exchange holidays are not fully represented for every historical year. Verify every flagged gap against an authoritative exchange calendar and source data. A clean check still cannot prove that no sessions are missing. Use `--round-trip-cost-pct` to supply an estimated all-in round-trip cost as a percentage of entry value (for example, `0.30` means 0.30%). The evaluator keeps gross returns unchanged and also reports net returns after subtracting this assumption, including net win rate and net averages. The default is `0.0`, which means no cost deduction; it is not a realistic cost estimate.

This is a research diagnostic, not a trading strategy or a prediction. It does not fetch or calculate your broker-specific brokerage, statutory taxes, bid-ask spread, market impact or liquidity constraints; net returns subtract only the user-supplied all-in round-trip cost assumption, which must be independently estimated for the broker, order type and date; it can have survivorship bias if the input universe contains only today's listed stocks. Yahoo's adjusted-close history is a minimum safeguard, not independent verification of every corporate action. Check split, bonus, dividend, rights-issue, merger and demerger coverage against official exchange/company records before drawing conclusions; unresolved events or bad source data must keep the affected symbol out of the evaluation. Use a multi-year point-in-time universe to reduce survivorship bias. The optional --universe-history CSV applies membership to each signal date; it requires columns symbol,effective_from,effective_to, uses inclusive ISO dates (YYYY-MM-DD), and treats a blank end date as still a member. Example rows: RELIANCE,2020-01-01, or ABC,2018-06-01,2022-09-30. Symbols absent from the file are excluded. The evaluator also prints a membership-data quality check: loaded chart symbols missing from the membership file, requested historical members missing chart data, and membership intervals outside the available chart date range. A clean check means only that no such detectable gap was found; it does not prove the CSV or chart universe is complete or accurate. This filter cannot recover delisted stocks whose charts are missing or prove that all historical constituents are present. Do not call a backtest survivorship-bias-free merely because this option was used. It evaluates daily setups only, not intraday ORB. Never change entry, exit or risk rules based only on a small sample.

## The opening-range breakout scan (during the session)

Normal NSE sessions run 09:15 to 15:30 IST after the 09:00–09:15
pre-open call auction. On Muhurat day (Sunday 2026-11-08), the special
continuous session runs 14:20 to 15:15 IST. The opening range is the first
`--or-minutes` (15) of the session: 09:15, 09:20 and 09:25 normally, or
14:20, 14:25 and 14:30 on Muhurat day. A hit is
the first completed 5-minute bar to CLOSE above the range high, at that
bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first, and
   prints PARTIAL with the count it skipped. `--max-confirm 0` checks them all.

A chart is usable only when every 5-minute bar from the session open to the
cutoff is there; a missing bar means a halt or a thin stock. Null closes are dropped, a
bar off the 5-minute grid (the live tick) is dropped, and a bar counts only
once it is complete (start + 5 minutes). Before the opening range completes (09:35 normally, 14:35 on Muhurat
day), on a closed day, after the close, or after `no_entries_after`, it
prints one plain line and writes nothing. Muhurat scans use a 15:00 cutoff.
If Yahoo supplies no usable 5-minute bars for the 14:20–15:15 session, the
scan fails closed and reports that the feed could not support a conclusion.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 480 of 500 charts (20 failed
  or had no data)". Never round a partial scan up to "the whole market".
- The holiday calendar covers 2026 (nseindia.com list). For 2027 the tools say
  the holiday list is not known and tell you to check nseindia.com.
- Everything is long-only here. Tick size is 0.05.

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 09:35 IST (10:15 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/nse-news SYMBOL` (what is moving
   it), then `/nse-entry SYMBOL` (the entry bar by bar). Never pick for them.

## Example (offline fixtures, Friday 2026-10-30)

```
python3 scan.py orb --offline wall5m-2026-10-30.json.gz --now "2026-10-30 10:15" --min-price 0
Opening-range breakout scan, Fri 2026-10-30 10:15 IST: 15-minute range (09:15 to 09:30), 5-minute bars, Yahoo.
Fetched 45 of 46 charts (1 failed or had no data); 43 usable; 39 candidates, 39 confirmed with real highs and lows.
32 of 43 charts closed above their opening-range high by 10:15:
  09:35  HINDUNILVR      ₹2,402.25  range 2,394.90 to 2,400.30  +0.08% over
  09:35  IOC               ₹160.30  range 159.80 to 160.25  +0.03% over
  ... and 30 more in the watchlist (--show N for more).
```

```
python3 scan.py daily --offline daily-spark-2026-10-30.json --now "2026-10-30 08:30"
Daily scan, Fri 2026-10-30 08:30 IST, closes through 2026-10-29 (Yahoo spark, daily closes only).
14 charts matched a setup: 5 20-day closing-high breakout, 2 bull flag, 3 reclaimed the 20-day average, 3 oversold (RSI under 30), 1 oversold bounce.
  LT              ₹3,716.10  RSI  70.8  20-day closing-high breakout
```

The fixtures are synthetic, so the names and levels show the format, not the
market. The user picks one symbol and Claude hands it to `/nse-news SYMBOL`,
then `/nse-entry SYMBOL`.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Differences from the US pack

NSE lists (Nifty 500 default) instead of Nasdaq Trader's directories, EQ
series only, ₹20 price floor, IST with a normal 09:15 open and 15:30 close, plus the 2026-11-08
Muhurat session from 14:20 to 15:15, no early-close days, bare symbols with a `.NS` suffix for Yahoo.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
