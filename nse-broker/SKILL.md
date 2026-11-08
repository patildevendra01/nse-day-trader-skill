---
name: nse-broker
description: >-
  Order Executor for NSE intraday trading. Turns approved orders into tickets
  for your Indian broker, paper mode by default, never without your yes. Shows
  each order as a plain ticket: the entry, then a separate stop-loss order and a
  take-profit order (most Indian brokers have discontinued intraday bracket
  orders), with the max loss at the stop and the exact Kite Connect
  place_order parameters for each leg. Records your yes word for word, then
  Claude's own Allow once / Deny card asks again. Builds the sell ticket that
  closes an intraday trade by your flat-by time (after cancelling the open
  stop and target so they cannot sell the shares twice), records fills, and
  logs each trade's max loss so nse-risk can hold the daily limit. Covers
  setting up Zerodha's Kite MCP server (and the others) in Claude Desktop with
  order tools on "Needs approval". Use when the user says "place the order",
  "buy it", "execute", "send it to Zerodha", "close my position", "flatten",
  "square off", "get me out", "connect Zerodha", "set up paper trading", or
  after /nse-risk passes.
argument-hint: "[order number]"
---

# nse-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every ticket:

1. **nse-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one ticket** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the order tool is set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 23 --filled-price 1432.60
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 23 --price 1453.65
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --qty 23
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, builds the flatten ticket, and keeps the log in
`nse/state/broker.json`. `--now "2026-10-30 09:40"` pins the clock (India time,
IST). Verified offline against fixtures; live endpoints and the broker
connectors were not exercised at build time.

## Paper mode and live mode

`"paper": true` (the default in `nse/config.json`) is **paper mode: nothing is
sent to any broker**, because no Indian broker offers a free paper-trading
order API the way Alpaca did. In paper mode you get the same tickets, the
same nse-risk gate and the same typed yes, but `approve` tells Claude not to
call any broker tool. Record a simulated fill instead:
`ticket.py result --index N --status placed --filled-qty Q --filled-price P`
(use the last traded price). That updates `nse/portfolio.json` and the log, so
nse-risk's daily limits keep working. `"paper": false` is the user's own
decision, in their own config: only then does Claude send approved legs through
the broker connector, one at a time, each behind its own Allow once.

## One order, three tickets

nse-budget writes ONE order with a `stop_loss` and a `take_profit`. Most
Indian brokers have discontinued intraday bracket orders. The normal
`ticket.py show` prints the entry parameter set only. After the broker confirms
the actual filled quantity, `ticket.py exits --index N --filled-qty Q` prints
the two exit parameter sets only (names from the Kite Connect API, so map them
to whatever your connector calls them):

- **entry**: BUY, `exchange` NSE, `tradingsymbol` (the bare symbol), `order_type`
  MARKET or LIMIT, `quantity`, `product` MIS (intraday) or CNC (delivery),
  `validity` DAY, plus `price` for a limit.
- **stop-loss**: SELL the same quantity, `order_type` SL-M with `trigger_price`
  (or SL with a `price` a few ticks under the trigger, if the config says
  `"stop_type": "SL"`; check which one your broker accepts for equity).
- **take-profit**: SELL the same quantity, `order_type` LIMIT at the target `price`.

One yes covers the entry and its planned protection, because they are one
decision. The exit tickets are generated only after the fill quantity is known.
But:

- **The normal `show` command prints the entry only.** It withholds exit tickets
  until an entry fill is recorded with `result --status placed --filled-qty Q
  --filled-price P`. If the order was first recorded as placed before its fill
  was known, run that same result command again when the first broker-confirmed
  fill arrives; this attaches the fill to the approved order. Use
  `reconcile-fill` only for later cumulative partial-fill updates. The exits
  command requires its quantity to exactly match that recorded fill; a manually
  typed or larger quantity is rejected. After
  recording the fill, check the broker's current net position and run
  `ticket.py exits --index N --filled-qty Q`. A partial fill must use the
  actual filled quantity, not the originally requested quantity. If the broker
  position changes after recording, do not generate exits from an old fill
  record; reconcile the fill record first. A sell placed first or for too many
  shares could open a short MIS position.
- **The two exit orders do not cancel each other.** When one fills, cancel
  the other yourself (a leftover limit sell could later sell shares you no
  longer hold, which is a short sale).
- Each leg still goes through Claude's own Allow once card in live mode.
- All prices are whole ticks (0.05), and the numbers go as numbers, not text.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/orders.json` | the proposed orders (nse-budget writes it; `flatten` replaces it) |
| `nse/state/risk.json` | nse-risk's verdicts; only a PASS from today is READY |
| `nse/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss (IST stamps) |
| `nse/portfolio.json` | cash and shares, updated by `fill`, `result --filled-qty`, and `reconcile-fill`; order-linked fills carry idempotency keys so retrying after a partial file-write failure does not apply the same share delta twice. Before a sale, the tool verifies that position shares match valid lot records and blocks the sale if they do not |
| `nse/plan.json` | the flat-by time for `flatten` |

## Setup A: Zerodha Kite MCP (Claude Desktop)

Zerodha publishes the Kite MCP server: github.com/zerodha/kite-mcp-server. Its
README gives a hosted endpoint, `https://mcp.kite.trade/mcp`, reached with
`mcp-remote`, and says the hosted version needs no installation and no API
keys: you sign in to Kite through the link its `login` tool produces, in your
own browser, never in the chat. Checked against that README on 2026-11-02.

1. The user needs a Zerodha account and Node.js (it provides `npx`).
2. In Claude Desktop: Settings > Developer > Edit Config. That opens
   `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
   Windows: `%APPDATA%\Claude\`). The user adds this block (also in
   `claude_desktop_config.example.json` here):

```json
{
  "mcpServers": {
    "kite": {
      "command": "npx",
      "args": ["mcp-remote", "https://mcp.kite.trade/mcp"]
    }
  }
}
```

3. Restart Claude Desktop, then ask Claude to log in to Kite. It opens an
   authorization link; the user signs in there. **Passwords, PINs, TOTP codes
   and API keys never go in the chat.** If the user pastes one anyway, tell
   them to delete that message and change it.
4. **Check what the connector actually exposes.** The README lists order tools
   (`place_order`, `modify_order`, `cancel_order`, and GTT tools) but also says
   the hosted version "excludes potentially destructive trading operations for
   security" without saying which. Read the connector's tool list in Claude. If
   there is no `place_order`, stay in paper mode or place the legs by hand in
   the Kite app from the tickets; do not guess a tool name. The README says that
   for other operations you generate your own API keys and run the server
   locally, with the keys in environment variables on the user's computer.
5. **Set the tool permissions.** Customize > Connectors (Settings > Connectors
   in older versions), open kite, and for each tool pick Always allow, Needs
   approval or Blocked:
   - `place_order`: **Needs approval**. Never Always allow.
   - `modify_order`, `cancel_order`: Needs approval.
   - the GTT tools (`place_gtt_order`, `modify_gtt_order`, `delete_gtt_order`):
     **Blocked** unless the user has a reason and says so (a GTT can fire later,
     with nobody watching).
   - read-only tools (`get_orders`, `get_trades`, `get_order_history`, the
     holdings, positions and quote lookups): Always allow is fine.
6. Check it: ask Claude for the account's positions (read only) and confirm in
   the Kite app that it is the right account.

In Claude Code instead of Claude Desktop, keep the order tools on ask in
`.claude/settings.json`: `"permissions": {"ask": ["mcp__kite__place_order"]}`,
and never put an order tool in `allow`.

## Setup B: Dhan, Upstox, Angel One and others

Several Indian brokers publish an MCP server or an API. The ticket's
parameters are Kite Connect-shaped on purpose and are the same for all of them
(`"broker": "dhan"`, `"upstox"`, `"angelone"` or `"other"` only changes the
name on the ticket). For any of them: use the broker's own, current
documentation to install its connector; sign in in the broker's own page, not
in the chat; read the connector's tool list and map the ticket's fields to
its fields (do not guess names); set every order, modify and cancel tool to
**Needs approval** and anything that fires later or acts on everything at
once to **Blocked**; and check whether it has a paper or sandbox mode (some
brokers offer a sandbox for their API; if so, that is worth using before real
money). If the connector has no order tool, stay in paper mode.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/nse-risk`.
2. Show ONE ticket and ask: "Approve this ticket? Reply yes or no." One ticket
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", a yes that names a different stock
   or amount, or one that contains a doubt like "yes stop at 1411"). Ask again.
   Never turn a reply into a yes yourself.
4. Paper mode: record the simulated fill (see above) and go to the legs. Live
   mode: only after "Recorded your yes", send the entry with the broker's
   order tool and exactly the entry parameters the ticket printed. Claude's card
   appears: Allow once or Deny.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <id> --filled-qty Q --filled-price P`, or `--status denied`, or
   `--status failed` with `--note`. A failed order is not retried without a new
   check and a new yes. After a fill is recorded, the daily loss is recalculated from the broker-confirmed
   cumulative average fill price, filled quantity and stop; later partial fills update it.
6. Do NOT use exit quantities from the original ticket. Record the actual fill
   with `result --status placed --filled-qty Q --filled-price P`. The exits
   command rejects requests unless Q exactly matches that recorded fill. Cancel
   any unfilled remainder of the entry order first, then verify the broker's
   current net position matches Q. If another entry partial fill arrives, use
   `ticket.py reconcile-fill --index N --filled-qty TOTAL --filled-price AVERAGE`
   with the broker's cumulative filled quantity and cumulative average price; it
   applies only the new share delta. Never use it for exit fills or a reduced
   position. If the net position changes for another reason, stop and reconcile
   manually before generating exits. Never place
   exits for more shares than the broker currently holds. Each live exit still
   needs its own Allow once.

## Steps: while it is open, and out by the close

- nse-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`). Your broker also auto-squares-off MIS positions
  late in the session (around 15:15 to 15:25, and it may charge a fee): be flat
  before that.
- When the stop-loss or the take-profit fills, record it:
  `ticket.py fill --symbol RELIANCE --side sell --qty 23 --price 1411.55 --fill-id BROKER_TRADE_ID`, then
  cancel the other leg.
- For manual fill records, pass the broker's unique trade/execution ID with
  `--fill-id`. Repeating the same command with the same ID and details is safe:
  it will not add the shares or cash twice. The portfolio also stores the fill
  details against each ID, so a retry after an interrupted log write rejects
  changed details instead of attaching the old ID to a different trade. A
  different broker fill must use its own ID, even if its quantity and price
  happen to match. If your broker does not
  show a trade ID, do not invent one; omit `--fill-id` and take extra care not
  to repeat the command.

- By `flat_by` (15:15 IST by default), anything still open gets closed:
  1. Check the shares actually held with the broker's positions lookup (read only).
  2. `ticket.py flatten --symbol RELIANCE --qty 23` writes a market sell for
     those shares to `nse/orders.json`.
  3. `/nse-risk`. A sell that only closes what the user holds is never blocked by
     the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above.
  5. **First cancel the open stop-loss and take-profit orders** for those shares
     (the orders lookup shows them; the cancel tool is on Needs approval). If they
     stay open beside the flatten sell, one can fill after it and sell the same
     shares again, which opens an unwanted short MIS position. Then send the
     flatten sell, then `result`.

## Example: the reel

```
ORDER 1 of 1, PAPER mode (nothing is sent to a broker), Zerodha Kite
  BUY 23 RELIANCE (NSE, MIS), market, good for today
  then stop-loss: SELL 23 at trigger 1411.55
  then take-profit: SELL 23 at limit 1453.65
  max loss about ₹484.15 if the stop fills at 1411.55 (it can fill worse in a fast market)
  why: opening-range breakout on RELIANCE: 23 shares, bound by your ₹500 max loss per trade (23 x 21.05 = ₹484.15 at the stop)
  READY: nse-risk PASS, waiting for your yes
  entry:       {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
                "order_type": "MARKET", "quantity": 23, "product": "MIS", "validity": "DAY"}
  stop-loss:   {... "transaction_type": "SELL", "quantity": 23, "order_type": "SL-M", "trigger_price": 1411.55}
  take-profit: {... "transaction_type": "SELL", "quantity": 23, "order_type": "LIMIT", "price": 1453.65}
```

(The real ticket prints each leg in full.) The user replies "yes". In paper
mode Claude records the simulated fill. In live mode it calls the broker's
order tool for the entry; the card reads "Claude wants to use Place order" with
those parameters and two buttons, Allow once and Deny. The `why` and the plan
stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two tickets on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats passwords, PINs, TOTP codes or API keys.
- Never switches `"paper"` to false. The user does that themselves.
- Never prints executable exit legs with the original requested quantity. Exit legs are generated separately using the broker-confirmed filled quantity; never places exits for more shares than the current position, and never leaves old exit orders open beside a flatten sell.
- Never uses GTT orders, bulk cancel or close-everything tools on its own.
- Never promises a fill price: a market order and a stop fill at whatever the market gives, and a stock at a circuit limit may not fill at all.

## Differences from the US pack

Zerodha Kite (and other Indian brokers) instead of Alpaca and Interactive
Brokers. Paper mode means nothing is sent, instead of a broker-side paper
account. Three separate tickets instead of one bracket order. Kite Connect
parameters (exchange, tradingsymbol, product MIS/CNC, validity) instead of
`place_stock_order`. India time, rupees, whole shares only.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper mode by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except nse-broker, and only when all three hold: nse-risk
  passed that exact order today, the user typed an explicit yes for that one ticket in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No derivatives, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker logins and keys are entered by the user in the broker's own sign-in or in their own config file, never in the chat.

Sources: [zerodha/kite-mcp-server README](https://github.com/zerodha/kite-mcp-server).
