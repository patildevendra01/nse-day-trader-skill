import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("nse-budget", "size")

CFG = os.path.join(ROOT, "nse-budget", "config.example.json")
PLAN = os.path.join(FIX, "nse-budget", "plan-reliance.json")
TARGETS = os.path.join(ROOT, "nse-budget", "targets.example.json")
TOP5 = os.path.join(FIX, "nse-budget", "example-top5.json")
ORDERS_FIXTURE = os.path.join(FIX, "nse-risk", "orders-reliance.json")
NOW = "2026-10-30 09:40"          # India time (IST), a Friday
TODAY = "2026-10-30"
PRICES = {"RELIANCE": 1432.60, "HDFCBANK": 1012.40, "TCS": 3120.40, "INFY": 1488.10, "ITC": 412.35}
# the contract's config, key for key
CONTRACT_CFG = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 50000, "min_order_inr": 500,
    "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3, "max_orders_per_day": 10,
    "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:15", "tick": 0.05,
    "product": "MIS", "stop_type": "SL-M", "broker": "zerodha", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False,
}
# rules for the weights mode tests
W = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_inr": 50000,
     "min_order_inr": 500, "paper": True, "blocked": []}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def wcfg(**over):
    c = dict(W)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def rows(path):
    with open(path) as fh:
        return size.parse_weights(json.load(fh))


def amounts(result):
    return {p["ticker"]: p["inr"] for p in result["positions"]}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-10-30T04:05:00Z"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"RELIANCE": price},
            "detail": {"RELIANCE": {"prev_close": 1418.30, "time": quote_time, "source": "yahoo"}}}


class Rupees(unittest.TestCase):
    def test_indian_digit_grouping(self):
        self.assertEqual(size.inr(0), "₹0")
        self.assertEqual(size.inr(999), "₹999")
        self.assertEqual(size.inr(1000), "₹1,000")
        self.assertEqual(size.inr(100000), "₹1,00,000")
        self.assertEqual(size.inr(48708.4), "₹48,708.40")
        self.assertEqual(size.inr(1234567.5), "₹12,34,567.50")
        self.assertEqual(size.inr(12345678), "₹1,23,45,678")
        self.assertEqual(size.inr(-1500), "-₹1,500")
        self.assertEqual(size.inr(19.999), "₹20")

    def test_tick_rounding_is_exact_paise(self):
        self.assertEqual(size.round_tick(1411.57, 0.05, "down"), 1411.55)
        self.assertEqual(size.round_tick(1411.55, 0.05, "down"), 1411.55)
        self.assertEqual(size.round_tick(1453.62, 0.05, "up"), 1453.65)
        self.assertEqual(size.round_tick(1453.65, 0.05, "up"), 1453.65)
        self.assertEqual(size.round_tick(1432.62, 0.05), 1432.60)
        self.assertEqual(size.round_tick(1432.625, 0.05), 1432.65)
        self.assertEqual(size.round_tick(1432.62, 0.01), 1432.62)
        self.assertEqual(size.round_tick(2845.05, 0.05, "down"), 2845.05)
        self.assertEqual(size.round_tick(2845.05, 0.05, "up"), 2845.05)

    def test_bare_symbols_keep_ampersand_and_hyphen(self):
        self.assertEqual(size.bare("reliance.ns"), "RELIANCE")
        self.assertEqual(size.bare(" M&M.NS "), "M&M")
        self.assertEqual(size.bare("BAJAJ-AUTO"), "BAJAJ-AUTO")
        self.assertEqual(size.bare("TCS.BO"), "TCS")

    def test_shares_are_floored_in_exact_paise(self):
        self.assertEqual(size.shares_for(500, 21.05), 23)
        self.assertEqual(size.shares_for(21.05 * 23, 21.05), 23)
        self.assertEqual(size.shares_for(0, 21.05), 0)

    def test_cost_estimate(self):
        c = size.estimate_costs(23, 1432.60, 1453.65)
        buy, sell = 23 * 1432.60, 23 * 1453.65
        brokerage = min(20, buy * 0.0003) + min(20, sell * 0.0003)
        stt = sell * 0.00025
        exch = (buy + sell) * 0.0000297
        sebi = (buy + sell) * 0.000001
        stamp = buy * 0.00003
        gst = (brokerage + exch + sebi) * 0.18
        self.assertAlmostEqual(c["brokerage"], brokerage, delta=0.01)
        self.assertAlmostEqual(c["stt"], stt, delta=0.01)
        self.assertAlmostEqual(c["total"], brokerage + stt + exch + sebi + stamp + gst, delta=0.03)
        self.assertAlmostEqual(c["per_share"], c["total"] / 23, delta=0.001)
        # the flat brokerage cap bites on a big order, and the cost model is overridable
        big = size.estimate_costs(1000, 1500, 1510)
        self.assertEqual(big["brokerage"], 40.0)
        free = size.estimate_costs(23, 1432.60, 1453.65, {"brokerage_flat": 0})
        self.assertEqual(free["brokerage"], 0.0)


class TheReel(unittest.TestCase):
    """Rs 1,00,000, 10% buffer, Rs 500 max loss, RELIANCE risk 21.05 -> 23 shares, Rs 32,949.80, max loss Rs 484.15."""

    def test_config_example_is_the_contract(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)

    def test_reliance_is_23_shares_bound_by_the_max_loss(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual((s["symbol"], s["exchange"], s["qty"], s["bound_by"]), ("RELIANCE", "NSE", 23, "max loss"))
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1432.60, 1411.55, 1453.65))
        self.assertEqual((s["risk_per_share"], s["reward_risk"], s["tick"]), (21.05, 1.0, 0.05))
        self.assertEqual((s["cost"], s["max_loss"]), (32949.80, 484.15))
        self.assertEqual({l["name"]: l["shares"] for l in s["limits"]},
                         {"max loss": 23, "position cap": 52, "max order": 34, "cash": 62})
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["max loss"])
        self.assertIsNone(s["refusal"])
        self.assertEqual(s["product"], "MIS")
        self.assertGreater(s["costs"]["at_target"]["total"], 0)
        self.assertGreater(s["costs"]["at_stop"]["total"], 0)

    def test_the_order(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        o = size.bracket_order(s)
        self.assertEqual({k: o[k] for k in ("symbol", "exchange", "side", "qty", "product", "order_type", "validity",
                                           "stop_loss", "take_profit")},
                         {"symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": 23, "product": "MIS",
                          "order_type": "market", "validity": "DAY", "stop_loss": 1411.55, "take_profit": 1453.65})
        self.assertNotIn("limit_price", o)
        for gone in ("order_class", "notional", "type", "time_in_force", "stop_loss_stop_price"):
            self.assertNotIn(gone, o)
        self.assertIsInstance(o["qty"], int)
        self.assertEqual(o["plan"]["max_loss"], 484.15)
        self.assertEqual(o["plan"]["flat_by"], "15:15")
        self.assertIn("₹500 max loss per trade", o["why"])
        self.assertIn("23 x 21.05 = ₹484.15", o["why"])

    def test_the_fixture_orders_file_is_what_it_writes(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        with open(ORDERS_FIXTURE) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["orders"], [size.bracket_order(s)])
        self.assertEqual(doc["created_by"], "nse-budget")

    def test_a_limit_order(self):
        s = size.size_trade(plan(), cfg(), limit=1430.00, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["qty"]), (1430.0, "limit", 27))   # 500 / 18.45
        o = size.bracket_order(s)
        self.assertEqual((o["order_type"], o["limit_price"]), ("limit", 1430.0))

    def test_cli_writes_one_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "orders.json")
            code, text, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--write", out,
                                   "--portfolio", os.path.join(d, "none")])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("Size: 23 shares of RELIANCE = ₹32,949.80 at 1432.60. "
                          "Max loss at the stop: 23 x 21.05 = ₹484.15.", text)
            self.assertIn("max loss      ₹500 max loss / 21.05 risk a share", text)
            self.assertIn("<- binds", text)
            self.assertIn("Estimated intraday costs", text)
            self.assertIn("check your broker's contract note", text)
            self.assertIn("buy 23 RELIANCE on NSE, MIS, market, day, stop-loss 1411.55, take-profit 1453.65", text)
            self.assertIn("Flat by 15:15 IST", text)
            self.assertIn("not financial advice", text)
            self.assertIn("Wrote 1 proposed order", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual((doc["created_by"], doc["status"], doc["paper"]), ("nse-budget", "proposed", True))
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["qty"], 23)
            self.assertIn("RELIANCE orb 2026-10-30", doc["source"])

    def test_cli_json_and_limit(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--json", "--limit",
                                   "--portfolio", os.path.join(d, "none")])
            self.assertEqual(code, 0, err)
            doc = json.loads(text)
            self.assertEqual(doc["sizing"]["order_type"], "limit")
            self.assertEqual(doc["orders"][0]["limit_price"], 1432.6)


class PlanRules(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=10), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("one share risks ₹21.05", s["refusal"])
        self.assertIn("more than your ₹10 max loss per trade", s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        self.assertIsNone(s["costs"])
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "orders.json")
            with open(os.path.join(d, "cfg.json"), "w") as fh:
                json.dump(cfg(max_loss_per_trade_inr=10), fh)
            code, text, err = run(["--plan", PLAN, "--config", os.path.join(d, "cfg.json"), "--now", NOW,
                                   "--write", out, "--portfolio", os.path.join(d, "none")])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_each_limit_can_bind(self):
        big = dict(max_loss_per_trade_inr=100000)
        self.assertEqual(size.size_trade(plan(), cfg(**big), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(max_order_inr=0, **big), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (52, "position cap"))
        s = size.size_trade(plan(), cfg(max_order_inr=0, max_position_pct=0, **big), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (62, "cash"))

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(plan(), cfg(), price=1440.00, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"], s["qty"]), (1440.0, "price", 28.45, 17))
        s = size.size_trade(plan(), cfg(), price=1420.00, today=TODAY)     # lower: the plan's entry stands
        self.assertEqual((s["entry"], s["entry_from"], s["qty"]), (1432.6, "plan", 23))

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            none = os.path.join(d, "none")
            pp = os.path.join(d, "prices.json")
            cp = os.path.join(d, "cfg.json")
            with open(cp, "w") as fh:
                json.dump(cfg(min_reward_risk=0), fh)    # at 1440 the 1453.65 target is only 0.48R: not the point here
            with open(pp, "w") as fh:
                json.dump(prices_doc(1440.00, "2026-10-30T04:05:00Z"), fh)    # 09:35 IST, 5 minutes old
            code, text, err = run(["--plan", PLAN, "--config", cp, "--now", NOW, "--prices", pp, "--portfolio", none])
            self.assertEqual(code, 0, err)
            self.assertIn("RELIANCE last 1440.00 (as of 09:35 IST) is above the plan's entry 1432.60", text)
            self.assertIn("Sized at 1440.00 (the latest price", text)
            with open(pp, "w") as fh:
                json.dump(prices_doc(1440.00, "2026-10-30T03:30:00Z"), fh)    # 09:00 IST, 40 minutes old
            code, text, err = run(["--plan", PLAN, "--config", cp, "--now", NOW, "--prices", pp, "--portfolio", none])
            self.assertEqual(code, 0, err)
            self.assertIn("not used (older than 15 minutes)", text)
            self.assertIn("from 2026-10-30 09:00 IST", text)
            self.assertIn("Sized at 1432.60 (the plan's entry)", text)

    def test_the_portfolio_cash_caps_the_size(self):
        s = size.size_trade(plan(), cfg(), cash=20000, today=TODAY)       # 20,000 - 10,000 buffer = 10,000
        self.assertEqual((s["qty"], s["bound_by"]), (6, "cash"))
        self.assertIn("cash ₹20,000 in nse/portfolio.json minus the ₹10,000 buffer", s["limits"][-1]["how"])
        s = size.size_trade(plan(), cfg(), cash=500000, today=TODAY)      # plenty of cash changes nothing
        self.assertEqual(s["qty"], 23)

    def test_prices_snap_to_the_tick(self):
        s = size.size_trade(plan(entry=1432.62, stop=1411.57, target=1453.62), cfg(), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1432.60, 1411.55, 1453.65))
        s = size.size_trade(plan(entry=1432.62, stop=1411.57, target=1453.62), cfg(tick=0.01), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1432.62, 1411.57, 1453.62))
        # the plan's own tick is not trusted over yours: the config's tick decides
        self.assertEqual(size.tick_of({"tick": "0.1"}), 0.1)
        self.assertEqual(size.tick_of({}), 0.05)

    def test_symbols_are_bare_nse_symbols(self):
        s = size.size_trade(plan(symbol="reliance.ns"), cfg(), today=TODAY)
        self.assertEqual(s["symbol"], "RELIANCE")
        s = size.size_trade(plan(symbol="M&M", entry=3100, stop=3090, target=3120), cfg(), today=TODAY)
        self.assertEqual((s["symbol"], s["qty"], s["bound_by"]), ("M&M", 16, "max order"))   # 50,000 / 3,100
        self.assertEqual(size.bracket_order(s)["symbol"], "M&M")
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(blocked=["reliance.ns"]), today=TODAY)

    def test_bad_plans(self):
        bad = [
            (plan(side="sell"), "long (buy) intraday trades only"),
            (plan(symbol=""), "no symbol"),
            (plan(stop=1440.0), "stop 1440.00 is not below the entry"),
            (plan(target=1430.0), "target 1430.00 is not above the entry"),
            (plan(date="2026-10-29"), "plan is for 2026-10-29 and today is 2026-10-30 in India"),
            (plan(target=None), "positive target"),
            (plan(stop=0), "positive stop"),
        ]
        for p, msg in bad:
            with self.assertRaises(size.SizeError) as cm:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(msg, str(cm.exception))
        with self.assertRaises(size.SizeError) as cm:
            size.size_trade(plan(), cfg(blocked=["RELIANCE"]), today=TODAY)
        self.assertIn("blocked list", str(cm.exception))
        with self.assertRaises(size.SizeError) as cm:
            size.size_trade(plan(), cfg(budget=None), today=TODAY)
        self.assertIn("no budget", str(cm.exception))
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(cash_buffer_pct=100), today=TODAY)

    def test_under_the_minimum_order(self):
        s = size.size_trade(plan(), cfg(min_order_inr=40000), today=TODAY)
        self.assertEqual(s["qty"], 23)
        self.assertIn("under your ₹40,000 minimum order", s["refusal"])

    def test_reward_risk_under_the_minimum_is_a_refusal(self):
        s = size.size_trade(plan(target=1440.00), cfg(), today=TODAY)
        self.assertIn("reward:risk 0.35", s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        s = size.size_trade(plan(target=1440.00), cfg(min_reward_risk=0), today=TODAY)
        self.assertIsNone(s["refusal"])

    def test_missing_plan_and_weights_only_flags(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, err = run(["--plan", os.path.join(d, "nope.json"), "--config", CFG])
            self.assertEqual((code, text), (1, ""))
            self.assertIn("no plan (run nse-exit first", err)
            self.assertEqual(len(err.strip().splitlines()), 1)
            code, text, err = run(["--plan", PLAN, "--config", CFG, "--top", "3"])
            self.assertEqual(code, 1)
            self.assertIn("work with --weights or --targets", err)

    def test_ist_clock(self):
        self.assertEqual(size.to_ist(size.parse_now("2026-10-30T04:10:00Z")).strftime("%Y-%m-%d %H:%M"), "2026-10-30 09:40")
        self.assertEqual(size.parse_now("2026-10-30 09:40").isoformat(), "2026-10-30T04:10:00")
        self.assertEqual(size.parse_now("2026-10-30T09:40:00+05:30").isoformat(), "2026-10-30T04:10:00")
        # late evening IST is already tomorrow in UTC terms but still today in India
        self.assertEqual(size.to_ist(size.parse_now("2026-10-30 23:50")).date().isoformat(), "2026-10-30")
        with self.assertRaises(size.SizeError):
            size.parse_now("tomorrow")


class WeightsMode(unittest.TestCase):
    def go(self, weights, c=None, prices=PRICES, **kw):
        return size.size(size.parse_weights(weights), c or wcfg(), prices=prices, **kw)

    def test_from_the_weights_file(self):
        self.assertEqual([(r["ticker"], r["weight"]) for r in rows(TOP5)],
                         [("RELIANCE", 30.0), ("HDFCBANK", 25.0), ("TCS", 20.0), ("INFY", 15.0), ("ITC", 10.0)])
        self.assertEqual(rows(TARGETS), rows(TOP5))

    def test_always_whole_shares_and_within_budget(self):
        r = self.go({"RELIANCE": 30, "HDFCBANK": 25, "TCS": 20, "INFY": 15, "ITC": 10})
        self.assertTrue(r["whole_shares"])
        self.assertEqual(r["cash_buffer"], 10000)
        self.assertEqual(r["investable"], 90000)
        for p in r["positions"]:
            self.assertEqual(p["shares"], int(p["shares"]))
            self.assertAlmostEqual(p["inr"], p["shares"] * PRICES[p["ticker"]], places=2)
            self.assertLessEqual(p["inr"], r["cap"] + 0.005)
        self.assertLessEqual(r["invested"], r["investable"])
        self.assertAlmostEqual(r["invested"] + r["idle"], r["investable"], places=2)
        self.assertGreaterEqual(r["idle"], 0)
        # a fractional setting in an old config is ignored: NSE has no fractional shares
        self.assertTrue(self.go({"RELIANCE": 100}, wcfg(fractional=True, max_position_pct=100))["whole_shares"])

    def test_orders_are_whole_share_nse_orders(self):
        r = self.go({"RELIANCE": 50, "TCS": 50})
        orders = size.orders_from(r, "example")
        self.assertEqual(len(orders), 2)
        for o in orders:
            self.assertEqual((o["exchange"], o["side"], o["product"], o["order_type"], o["validity"]),
                             ("NSE", "buy", "CNC", "market", "DAY"))
            self.assertIsInstance(o["qty"], int)
            self.assertGreater(o["qty"], 0)
            self.assertNotIn("notional", o)
            self.assertNotIn("stop_loss", o)
            self.assertIn("weights from example", o["why"])
        self.assertEqual(size.orders_from(r, product="MIS")[0]["product"], "MIS")

    def test_cap_redistributes_until_nobody_is_over(self):
        r = self.go({"RELIANCE": 60, "HDFCBANK": 20, "TCS": 10, "INFY": 5, "ITC": 5})
        a = amounts(r)
        self.assertLessEqual(max(a.values()), 25000 + 0.005)
        self.assertTrue(next(p for p in r["positions"] if p["ticker"] == "RELIANCE")["capped"])
        self.assertGreater(a["ITC"], 0)

    def test_everything_capped_leaves_money_idle(self):
        r = self.go({"RELIANCE": 50, "TCS": 50}, wcfg(max_position_pct=20))
        self.assertEqual(r["cap"], 20000)
        self.assertGreaterEqual(r["idle"], 90000 - 40000 - 0.005)
        text = size.report(r, size.orders_from(r))
        self.assertIn("left idle", text)
        self.assertIn("Left idle", text)

    def test_slices_under_the_minimum_are_dropped_and_shared_out(self):
        r = self.go({"RELIANCE": 60, "HDFCBANK": 39, "ITC": 1}, wcfg(max_position_pct=100, min_order_inr=5000))
        self.assertEqual([d["ticker"] for d in r["dropped"]], ["ITC"])
        self.assertIn("under your ₹5,000 minimum order", r["dropped"][0]["reason"])
        self.assertEqual({p["ticker"] for p in r["positions"]}, {"RELIANCE", "HDFCBANK"})

    def test_one_share_dearer_than_the_slice_is_dropped(self):
        r = self.go({"RELIANCE": 50, "TCS": 50}, wcfg(max_position_pct=100, budget=3000, min_order_inr=100),
                    prices=dict(PRICES, TCS=3120.40))
        self.assertEqual([d["ticker"] for d in r["dropped"]], ["TCS"])
        self.assertIn("cannot be bought in whole shares", r["dropped"][0]["reason"])

    def test_blocked_tickers_are_dropped_first(self):
        r = self.go({"RELIANCE": 50, "TCS": 50}, wcfg(blocked=["tcs.ns"], max_position_pct=100))
        self.assertEqual([p["ticker"] for p in r["positions"]], ["RELIANCE"])
        self.assertEqual(r["dropped"][0]["reason"], "on your blocked list")

    def test_top_n(self):
        r = self.go({"RELIANCE": 30, "HDFCBANK": 25, "TCS": 20, "INFY": 15, "ITC": 10}, top=2)
        self.assertEqual({p["ticker"] for p in r["positions"]}, {"RELIANCE", "HDFCBANK"})
        self.assertEqual(len(r["dropped"]), 3)

    def test_whole_shares_need_prices(self):
        with self.assertRaises(size.SizeError) as cm:
            self.go({"RELIANCE": 50, "TCS": 50}, prices={"RELIANCE": 1432.60})
        self.assertIn("whole shares need a price for: TCS", str(cm.exception))

    def test_no_budget_and_duplicates_are_errors(self):
        with self.assertRaises(size.SizeError):
            self.go({"RELIANCE": 100}, wcfg(budget=None))
        with self.assertRaises(size.SizeError):
            size.size(size.parse_weights([{"ticker": "TCS", "weight": 1}, {"ticker": "tcs.ns", "weight": 2}]),
                      wcfg(), prices=PRICES)
        with self.assertRaises(size.SizeError):
            size.parse_weights([{"ticker": "TCS"}])
        with self.assertRaises(size.SizeError):
            size.parse_weights({"TCS": "lots"})

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            pp, out = os.path.join(d, "prices.json"), os.path.join(d, "orders.json")
            with open(pp, "w") as fh:
                json.dump({"prices": PRICES}, fh)
            with open(os.path.join(d, "cfg.json"), "w") as fh:
                json.dump(wcfg(), fh)
            cfgp = os.path.join(d, "cfg.json")
            code, text, err = run(["--weights", "RELIANCE=30,HDFCBANK=25,TCS=20,INFY=15,ITC=10", "--prices", pp,
                                   "--config", cfgp, "--write", out, "--now", NOW])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("Budget ₹1,00,000, cash buffer 10% = ₹10,000, investing ₹90,000", text)
            self.assertIn("rupees", text)
            self.assertIn("Proposed orders (nothing has been placed):", text)
            self.assertIn("on NSE, CNC, market, day", text)
            self.assertIn("These buys have no stop", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(len(doc["orders"]), 5)
            code, text, err = run(["--targets", TOP5, "--prices", pp, "--config", cfgp, "--json"])
            self.assertEqual(code, 0, err)
            self.assertEqual(len(json.loads(text)["orders"]), 5)
            code, text, err = run(["--weights", "TCS=1", "--config", cfgp, "--prices", os.path.join(d, "none")])
            self.assertEqual(code, 1)
            self.assertIn("no prices at", err)


if __name__ == "__main__":
    unittest.main()
