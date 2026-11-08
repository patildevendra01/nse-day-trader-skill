import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("nse-broker", "ticket")
check = load("nse-risk", "check")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "nse-risk", "orders-reliance.json")
PLAN_PATH = os.path.join(FIX, "nse-budget", "plan-reliance.json")
NOW = "2026-10-30 09:40"          # India time (IST), a Friday
TODAY = "2026-10-30"
BATCH = ["RELIANCE", "TCS", "M&M", "BAJAJ-AUTO", "ITC"]
ENTRY_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
              "order_type": "MARKET", "quantity": 23, "product": "MIS", "validity": "DAY"}
STOP_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
             "quantity": 23, "product": "MIS", "validity": "DAY", "order_type": "SL-M", "trigger_price": 1411.55}
TARGET_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
               "quantity": 23, "product": "MIS", "validity": "DAY", "order_type": "LIMIT", "price": 1453.65}
RELIANCE_PLAIN = ("BUY 23 RELIANCE (NSE, MIS), market, good for today / then stop-loss: SELL 23 at trigger 1411.55 / "
                  "then take-profit: SELL 23 at limit 1453.65 / max loss about ₹484.15 if the stop fills at "
                  "1411.55 (it can fill worse in a fast market)")


def reliance(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=BATCH):
        return ticket.parse_yes(reply, order or reliance(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 23",
                  "yes buy 23 RELIANCE", "yes 1,411.55", "yes 1411.55 and 1453.65", "yes max loss 484.15",
                  "yes reliance"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it", "haan",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes TCS", "yes M&M", "yes BAJAJ-AUTO", "yes 24", "yes 2300", "yes but make it 20", "yes?", "no",
                  "not yes", "yes, wait", "yes if it is under 1430", "yes stop at 1411.55"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        sell = {"symbol": "ITC", "side": "sell", "qty": 30, "product": "CNC", "order_type": "market"}
        self.assertTrue(self.yes("yes sell 30 ITC", sell))
        self.assertFalse(self.yes("yes sell 31", sell))
        mm = {"symbol": "M&M", "side": "buy", "qty": 16, "product": "MIS", "order_type": "market"}
        self.assertTrue(self.yes("yes M&M", mm))
        self.assertFalse(self.yes("yes TCS", mm))
        self.assertTrue(self.yes("yes 16 M&M", mm, batch=["M&M", "TCS", "RELIANCE"]))

    def test_a_yes_covers_the_ticket_it_names(self):
        why = ticket.parse_yes("yes 23 RELIANCE", reliance(), BATCH)[1]
        self.assertTrue(why.startswith("explicit yes for BUY 23 RELIANCE (NSE, MIS)"))
        self.assertIn("then stop-loss", why)


class TicketShape(unittest.TestCase):
    def test_the_entry_call_is_kite_connect_shaped(self):
        self.assertEqual(ticket.tool_input(reliance()), ENTRY_CALL)
        self.assertEqual(ticket.entry_params(reliance()), ENTRY_CALL)

    def test_the_call_leaves_out_why_plan_and_the_legs(self):
        call = ticket.tool_input(reliance())
        for k in ("why", "plan", "stop_loss", "take_profit", "symbol", "qty", "order_class"):
            self.assertNotIn(k, call)

    def test_three_legs_in_order(self):
        legs = ticket.legs(reliance())
        self.assertEqual([r for r, _ in legs], ["entry", "stop-loss", "take-profit"])
        self.assertEqual([p for _, p in legs], [ENTRY_CALL, STOP_CALL, TARGET_CALL])
        self.assertEqual([r for r, _ in ticket.legs(reliance(stop_loss=None, take_profit=None))], ["entry"])

    def test_a_stop_limit_leg_sits_a_few_ticks_under_its_trigger(self):
        legs = dict(ticket.legs(reliance(), {"stop_type": "SL"}))
        s = legs["stop-loss"]
        self.assertEqual((s["order_type"], s["trigger_price"], s["price"]), ("SL", 1411.55, 1407.30))
        self.assertLess(s["price"], s["trigger_price"])
        self.assertTrue(check.on_tick(s["price"], 0.05))
        legs = dict(ticket.legs(reliance(), {"stop_type": "SL", "stop_limit_pct": 1}))
        self.assertEqual(legs["stop-loss"]["price"], 1397.40)       # 1411.55 x 0.99 = 1397.4345, down to the tick
        legs = dict(ticket.legs(reliance(), {"stop_type": "SL-M"}))
        self.assertNotIn("price", legs["stop-loss"])

    def test_limit_and_stop_entries(self):
        p = ticket.entry_params(reliance(order_type="limit", limit_price=1430.00))
        self.assertEqual((p["order_type"], p["price"]), ("LIMIT", 1430.0))
        self.assertNotIn("trigger_price", p)
        p = ticket.entry_params(reliance(order_type="sl", limit_price=1436.0, trigger_price=1435.0))
        self.assertEqual((p["order_type"], p["price"], p["trigger_price"]), ("SL", 1436.0, 1435.0))
        p = ticket.entry_params(reliance(order_type="SL_M", trigger_price=1435.0))
        self.assertEqual((p["order_type"], p["trigger_price"]), ("SL-M", 1435.0))
        self.assertNotIn("price", p)

    def test_a_short_entry_has_buy_legs(self):
        o = reliance(side="sell", stop_loss=1453.65, take_profit=1411.55)
        legs = dict(ticket.legs(o))
        self.assertEqual(legs["entry"]["transaction_type"], "SELL")
        self.assertEqual(legs["stop-loss"]["transaction_type"], "BUY")
        self.assertEqual(legs["take-profit"]["transaction_type"], "BUY")

    def test_symbols_are_sent_bare(self):
        p = ticket.entry_params(reliance(symbol="m&m.ns"))
        self.assertEqual(p["tradingsymbol"], "M&M")
        self.assertEqual(ticket.entry_params(reliance(symbol="BAJAJ-AUTO"))["tradingsymbol"], "BAJAJ-AUTO")

    def test_plain_english(self):
        self.assertEqual(ticket.plain(reliance()), RELIANCE_PLAIN)
        parts = ticket.plain_parts(reliance())
        self.assertEqual(len(parts), 4)
        self.assertEqual(ticket.plain_parts(reliance(stop_loss=None, take_profit=None, order_type="limit",
                                                     limit_price=1430.00, product="CNC", validity="IOC")),
                         ["BUY 23 RELIANCE (NSE, CNC), limit 1430.00, immediate or cancel"])
        self.assertEqual(ticket.plain_parts({"symbol": "ITC", "side": "sell", "qty": 30, "order_type": "sl",
                                             "trigger_price": 410.0, "limit_price": 409.0})[0],
                         "SELL 30 ITC (NSE, MIS), stop-limit, trigger 410.00, limit 409.00, good for today")

    def test_max_loss_and_money(self):
        self.assertEqual(ticket.max_loss(reliance()), (484.15, 1411.55))
        self.assertEqual(ticket.max_loss(reliance(order_type="limit", limit_price=1430.00, qty=27)), (498.15, 1411.55))
        o = reliance(stop_loss=None)
        self.assertEqual(ticket.max_loss(o)[1], 1411.55)          # the plan's stop still counts
        self.assertEqual(ticket.max_loss({"symbol": "X", "qty": 5}), (None, None))
        self.assertEqual(ticket._money(100000), "₹1,00,000")
        self.assertEqual(ticket._money(1234567.5), "₹12,34,567.50")
        self.assertEqual(ticket._money(484.15), "₹484.15")

    def test_the_india_clock(self):
        self.assertEqual(ticket.ist_iso(ticket.parse_now("2026-10-30 09:41")), "2026-10-30T09:41:00+05:30")
        self.assertEqual(ticket.parse_now("2026-10-30T04:11:00Z"), ticket.parse_now("2026-10-30 09:41"))
        self.assertEqual(ticket.to_ist(ticket.parse_now("2026-10-29T19:00:00Z")).date().isoformat(), TODAY)
        with self.assertRaises(ticket.TicketError):
            ticket.parse_now("later")


class ConnectorConfig(unittest.TestCase):
    def test_example_is_the_kite_mcp_server_with_no_secrets(self):
        with open(os.path.join(ROOT, "nse-broker", "claude_desktop_config.example.json")) as fh:
            raw = fh.read()
        server = json.loads(raw)["mcpServers"]["kite"]
        self.assertEqual((server["command"], server["args"]), ("npx", ["mcp-remote", "https://mcp.kite.trade/mcp"]))
        self.assertNotIn("env", server)
        for word in ("KEY", "SECRET", "TOKEN", "PASSWORD", "alpaca", "ALPACA"):
            self.assertNotIn(word, raw)

    def test_the_example_config_is_paper_zerodha(self):
        with open(CFG_PATH) as fh:
            c = json.load(fh)
        self.assertEqual((c["broker"], c["paper"], c["product"], c["tick"]), ("zerodha", True, "MIS", 0.05))
        self.assertEqual(ticket.broker_name(c), "Zerodha Kite")
        self.assertEqual(ticket.broker_name({"broker": "dhan"}), "Dhan")
        self.assertEqual(ticket.broker_name({"broker": "upstox"}), "Upstox")
        self.assertEqual(ticket.broker_name({"broker": "angelone"}), "Angel One")
        self.assertEqual(ticket.broker_name({"broker": "mybroker"}), "Mybroker")


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def set_cfg(self, **over):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_the_full_paper_flow(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER mode (nothing is sent to a broker), Zerodha Kite", text)
        for part in RELIANCE_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: nse-risk PASS", text)
        for call in (ENTRY_CALL, STOP_CALL, TARGET_CALL):
            for k, v in call.items():
                self.assertIn('"%s": %s' % (k, json.dumps(v)), text)
        self.assertIn("entry:", text)
        self.assertIn("stop-loss:", text)
        self.assertIn("take-profit:", text)
        self.assertIn("only AFTER the entry has filled", text)
        self.assertIn("do NOT cancel each other", text)
        self.assertIn("Paper mode: nothing is placed", text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertNotIn("place_stock_order", text)
        self.assertNotIn("Allow once", text)            # paper mode never reaches a broker card

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("Paper mode: do not call any broker tool", text)
        self.assertNotIn('"variety"', text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "paper-1",
                                      "--filled-qty", "23", "--filled-price", "1432.60", "--portfolio", self.pf,
                                      now="2026-10-30 09:42"))
        self.assertEqual(code, 0)
        self.assertIn("Now place the stop-loss and the take-profit", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc = st["orders"][0], st["orders"][1]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"]), ("yes", "entry", 484.15, TODAY))
        self.assertEqual(appr["at"], "2026-10-30T09:40:00+05:30")
        self.assertEqual((plc["broker_order_id"], plc["max_loss"], plc["kind"], plc["paper"]), ("paper-1", 484.15, "entry", True))
        self.assertEqual(plc["at"], "2026-10-30T09:42:00+05:30")
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 67050.2)
        self.assertEqual((pf["positions"][0]["ticker"], pf["positions"][0]["shares"]), ("RELIANCE", 23))

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss ₹484.15", text)

        # nse-risk now counts it: a second RELIANCE buy today is a duplicate, and the loss is used
        res = check.check([reliance()], {"budget": 100000}, state=st, now=check.parse_now("2026-10-30 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 484.15))

    def test_a_paper_risk_pass_cannot_be_reused_after_switching_to_live(self):
        self.assertEqual(self.run_risk(), 0)
        self.set_cfg(paper=False)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("paper/live mode changed since the risk check", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("paper/live mode changed since the risk check", err)
        self.assertFalse(os.path.exists(self.state))

    def test_a_risk_pass_cannot_be_reused_after_changing_risk_limits(self):
        self.assertEqual(self.run_risk(), 0)
        self.set_cfg(max_loss_per_trade_inr=100)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("risk settings changed since the risk check", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("risk settings changed since the risk check", err)
        self.assertFalse(os.path.exists(self.state))

    def test_the_live_flow_sends_the_entry_call_then_the_legs(self):
        self.set_cfg(paper=False)
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Zerodha Kite", text)
        self.assertIn("Allow once places it, Deny stops it", text)
        self.assertIn("read the", text)
        self.assertIn("connector's tool list", text)
        self.assertNotIn("Paper mode", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes 23 RELIANCE"))
        self.assertEqual(code, 0)
        self.assertIn("place the entry with your broker connector's order tool", text)
        self.assertEqual(json.loads(text[text.index("{"):text.rindex("}") + 1]), ENTRY_CALL)
        self.assertIn("After the entry fills, place the stop-loss and take-profit legs", text)
        self.assertFalse(self.state_doc()["orders"][0]["paper"])
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "2610300001234"))
        self.assertEqual(code, 0)
        self.assertIn("When it fills, record it: ticket.py fill --symbol RELIANCE --side buy", text)
        self.assertIn("cancel the other one", text)
        code, text, _ = run(["fill", "--symbol", "reliance.ns", "--side", "buy", "--qty", "23", "--price", "1432.65",
                             "--portfolio", self.pf, "--config", self.cfg, "--state", self.state, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("Recorded buy 23 RELIANCE at ₹1,432.65", text)
        with open(self.pf) as fh:
            self.assertEqual(json.load(fh)["positions"][0]["ticker"], "RELIANCE")

    def test_ticket_json(self):
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1", "--json"))
        self.assertEqual(code, 0)
        t = json.loads(text)
        self.assertTrue(t["ready"])
        self.assertEqual((t["broker"], t["paper"], t["has_legs"], t["max_loss"], t["stop"]),
                         ("zerodha", True, True, 484.15, 1411.55))
        self.assertEqual(t["tool_input"], ENTRY_CALL)
        self.assertEqual([l["role"] for l in t["legs"]], ["entry", "stop-loss", "take-profit"])
        self.assertEqual(t["fingerprint"], check.fingerprint(reliance()))
        self.assertEqual(t["plain"], RELIANCE_PLAIN)

    def test_flatten_by_the_close(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc",
                      "--filled-qty", "23", "--filled-price", "1432.60", "--portfolio", self.pf))
        late = "2026-10-30 15:05"
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 23 RELIANCE (NSE, MIS), market, good for today (flat by 15:15 IST)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("FIRST cancel the open stop-loss and take-profit orders", text)
        self.assertIn("unwanted short MIS", text)
        with open(self.orders) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["orders"][0]["qty"], 23)
        self.assertEqual(ticket.tool_input(doc["orders"][0]),
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                          "transaction_type": "SELL", "order_type": "MARKET", "quantity": 23, "product": "MIS",
                          "validity": "DAY"})
        self.assertEqual(self.run_risk(now=late), 0)       # after the 14:30 entry cutoff: an exit still passes
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertNotIn("then stop-loss", text)           # a flatten sell carries no legs
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 23 RELIANCE", now=late))[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "23",
                                   "--filled-price", "1450.00", "--portfolio", self.pf, now="2026-10-30 15:06"))
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual((pf["cash"], pf["positions"]), (100400.2, []))
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "reliance", "--qty", "23", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against your broker's positions", text)
        self.assertIn("(flat by 15:15 IST)", text)           # from the config when there is no plan
        for bad in ("0", "-2", "2.5", "many"):
            code, _, err = run(["flatten", "--symbol", "RELIANCE", "--qty", bad, "--orders", self.orders])
            self.assertEqual(code, 1, bad)
            self.assertIn("whole number of shares", err)

    def test_flatten_takes_the_qty_from_the_portfolio_and_says_so(self):
        with open(self.pf, "w") as fh:
            json.dump({"cash": 1000, "positions": [{"ticker": "TCS", "shares": 5, "lots": []}]}, fh)
        code, text, _ = run(["flatten", "--symbol", "TCS.NS", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("The qty comes from %s. Check it against your broker's positions" % self.pf, text)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = 24
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-29 10:15")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-30 09:20"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-30 09:20"))
        self.assertIn("BLOCKED by nse-risk: window: no new entries before 09:30 IST", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-30 09:20"))
        self.assertEqual(code, 1)

    def test_a_holiday_blocks_the_ticket(self):
        self.assertEqual(self.run_risk(now="2026-10-20 10:00"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-20 10:00"))
        self.assertIn("NSE holiday: Dussehra", text)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_other_brokers_get_the_same_neutral_parameters(self):
        self.set_cfg(broker="dhan", paper=False)
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Dhan", text)
        self.assertIn("Kite Connect names; map them to the connector's own fields", text)
        self.assertIn('"tradingsymbol": "RELIANCE"', text)
        self.assertNotIn("Interactive Brokers", text)

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"ticker": "ITC", "shares": 30, "lots": [
            {"id": "ITC-1", "date": "2025-01-02", "shares": 20, "cost": 8000.0},
            {"id": "ITC-2", "date": "2026-01-02", "shares": 10, "cost": 4200.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "ITC", "--side", "sell", "--qty", "25", "--price", "412.35",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 10308.75)
        lots = pf["positions"][0]["lots"]
        self.assertEqual([l["id"] for l in lots], ["ITC-2"])
        self.assertEqual(lots[0]["shares"], 5)
        self.assertEqual(lots[0]["cost"], 2100.0)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        self.assertIn("₹10,308.75", text)
        code, _, err = run(["fill", "--symbol", "ITC", "--side", "sell", "--qty", "50", "--price", "412",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["fill", "--symbol", "ITC", "--side", "buy", "--qty", "0", "--price", "412",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
