---
name: nse-analyst
description: >-
  The Earnings Analyst for Indian listed companies. Reads a company's
  quarterly results (Yahoo Finance's fundamentals feed, or the numbers you
  paste from the filing on NSE or BSE) and sums up what changed: revenue,
  operating and net margins, net profit and diluted EPS in rupees crore,
  quarter by quarter, each against the same quarter a year earlier (YoY) and
  the quarter before (QoQ), with the last four quarters and Indian fiscal
  labels (Q2 FY27 = July to September 2026). Reads an earnings call
  transcript when you paste one. Use when the user says "how did Reliance's
  results look", "analyze the results", "Q2 results", "what changed this
  quarter", "is revenue growing", "compare margins", "sum up the concall",
  "does it report this week", or before trading a stock around its results.
argument-hint: "[symbols]"
---

# nse-analyst

Results move Indian stocks more than almost anything else on the calendar,
and the press release is written to put the best number first. This skill
lines up the reported numbers quarter by quarter and says plainly what
changed. For a day trader it answers two questions before a trade: what did
the last result say, and is the next one inside this trade?

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE
python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE TCS "M&M" --metrics nse/metrics.json
python3 "${CLAUDE_SKILL_DIR}/results.py" --file tcs-results.csv --symbol TCS
python3 "${CLAUDE_SKILL_DIR}/results.py" --help
```

Verified offline against fixtures; live endpoints not exercised at build
time. Yahoo's fundamentals feed is free, unofficial, often carries only the
last four or five quarters, and can fail or lag: when it does, use `--file`.

## What it reads and writes

- Reads Yahoo Finance's fundamentals-timeseries for `<SYMBOL>.NS`
  (quarterlyTotalRevenue, quarterlyOperatingIncome, quarterlyNetIncome,
  quarterlyDilutedEPS, quarterlyGrossProfit). Money is shown in rupees crore
  (1 crore = 10 million); EPS in rupees.
- `--file` reads a JSON or CSV of results you copied from the company's
  filing on NSE or BSE (columns `quarter_end`, `revenue_cr`,
  `gross_profit_cr`, `operating_income_cr`, `net_income_cr`, `eps_diluted`;
  commas, blank cells and (negative) brackets are fine). Filed numbers beat
  Yahoo's: prefer this route when the user can paste them.
- Writes `nse/metrics.json` with `--metrics`: one row per company (last-four-
  quarter revenue and net profit in crore, EPS, latest margins and growth).
  `metrics.example.json` in this folder is a synthetic example, not real data.
- Earnings calls: the tool does not fetch transcripts. If the user pastes one
  (or the company's investor page has it), read it and sum up what
  management said changed: guidance, margins, demand, order book, anything
  they declined to answer.
- The next results date is not in this data. Look for the board-meeting
  intimation in `/nse-news` (NSE announcements) or on the company's investor
  page, and say where it came from.

## The rules

- **Filed numbers first.** Say which numbers came from Yahoo and which from
  the filing. Open the filing before quoting a number as fact.
- **YoY and QoQ both matter.** Growth against the same quarter a year earlier
  removes seasonality; quarter on quarter shows momentum. The tool prints
  both. Many Indian businesses are seasonal (festive demand in Q3, monsoon in
  Q2), so lead with YoY.
- **Fiscal quarters.** The Indian fiscal year runs April to March: Q1 is
  April to June, Q4 is January to March, and FY27 ends 31 March 2027.
- **Say what is missing.** When the data has no quarter from a year earlier,
  YoY is blank and the tool says so. Banks, NBFCs and many other companies
  have no gross profit: that column is blank, not zero.
- **Standalone or consolidated.** Yahoo mixes them for some companies; the
  filing says which. Say so when the numbers look off.
- **Flag one-offs.** When net profit is above operating profit, other income,
  exceptional gains or tax items are inside it. The tool says so.

## Steps

1. Run it for the companies in question (or the symbols on
   `nse/watchlist.json`). Use `--quarters 8` for two years, `--json` for
   everything machine-readable.
2. If Yahoo has too little, ask the user to paste the quarterly table from the
   exchange filing and run `--file`.
3. Write the summary: one line on the quarter (revenue, net profit, EPS and
   both growth rates), one on margins, one on anything unusual, and the
   source. Plain words, real numbers, no adjectives the numbers do not support.
4. If the next result falls inside the planned trade (an overnight hold, or a
   result before the open), say it before `/nse-exit` sets the stop: a stop
   does not protect against a gap, the stock can open past it.

## Example (synthetic fixture, not real figures)

`results.py RELIANCE --offline tests/fixtures/nse-analyst`:

```text
quarter   ended          revenue      YoY      QoQ net profit      YoY      QoQ  op mgn net mgn      EPS
Q2 FY27   2026-09-30      27,900   +10.7%    +4.1%      2,120   +14.6%    +3.4%   17.2%    7.6%    31.30
Q1 FY27   2026-06-30      26,800    +9.4%    +3.9%      2,050   +15.2%    +7.9%   17.2%    7.6%    30.30
```

The summary: "Q2 FY27 revenue was Rs 27,900 crore, up 10.7 % on a year ago and
4.1 % on the quarter. Net profit rose 14.6 % to Rs 2,120 crore; operating
margin held at 17.2 %. No gross profit is reported, so gross margin is blank."

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the data does not have, or estimates a quarter it
  cannot derive.

## Differences from the US pack

Yahoo and pasted filings instead of SEC EDGAR XBRL, rupees crore instead of
US$ millions, Indian fiscal quarters, YoY and QoQ side by side.

## Safety

Education and tooling, not financial advice. This skill only reads filings.
Nothing places an order except `/nse-broker`, after `/nse-risk` passes
and the user types an explicit yes for that order.
