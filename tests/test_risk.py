import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("nse-risk", "check")
ticket = load("nse-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
RELIANCE_PATH = os.path.join(FIX, "nse-risk", "orders-reliance.json")
BASKET_PATH = os.path.join(FIX, "nse-risk", "orders-basket.json")
NOW = "2026-10-30 09:40"          # India time (IST), a Friday
TODAY = "2026-10-30"
PRICES = {"RELIANCE": 1432.60, "HDFCBANK": 1012.40, "TCS": 3120.40, "INFY": 1488.10, "ITC": 412.35,
          "SBIN": 830.00}
# a delivery (CNC) basket has no stops: these are the rules for it
BASKET_CFG = {"max_position_pct": 25, "require_stop": False, "min_reward_risk": 0, "max_trades_per_day": 0,
              "no_entries_before": None, "no_entries_after": None}


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def at(s):
    return check.parse_now(s)


def reliance(**over):
    with open(RELIANCE_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def basket():
    with open(BASKET_PATH) as fh:
        return json.load(fh)["orders"]


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


def placed(sym, side="buy", max_loss=300.0, **kw):
    r = {"status": "placed", "date": TODAY, "symbol": sym, "side": side, "kind": "entry", "max_loss": max_loss}
    r.update(kw)
    return r


class TheReel(unittest.TestCase):
    def test_the_reliance_order_passes_every_check(self):
        res = check.check([reliance()], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 1432.60, 1411.55, 1453.65))
        self.assertEqual((r["risk_per_share"], r["max_loss"], r["reward_risk"]), (21.05, 484.15, 1.0))
        self.assertEqual(r["value_inr"], 32949.80)
        self.assertEqual(r["product"], "MIS")
        self.assertEqual(res["cash_after"], 67050.20)
        self.assertEqual(res["date"], TODAY)
        self.assertIs(res["paper"], True)

    def test_twenty_four_shares_is_blocked_by_the_max_loss_only(self):
        res = check.check([reliance(qty=24)], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 24 shares x 21.05 risk a share = ₹505.20 at the stop, over your ₹500 max loss per trade"])

    def test_fingerprint_is_shared_with_the_broker(self):
        o = reliance()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={})), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, qty=24)), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, stop_loss=1411.50)), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, product="CNC")), check.fingerprint(o))
        self.assertEqual(check.ORDER_FIELDS, ticket.ORDER_FIELDS)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", RELIANCE_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy RELIANCE 23 shares, entry", text)
            self.assertIn("entry 1432.60 (the plan's entry), stop 1411.55, target 1453.65: risk 21.05 a share", text)
            self.assertIn("max loss ₹484.15 (limit ₹500), reward:risk 1.00 (min 1)", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-30 09:40 IST", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(reliance()))


class Format(unittest.TestCase):
    def one(self, order, **over):
        return check.check([order], cfg(**over), now=at(NOW))

    def test_whole_share_qty_only(self):
        for bad in (23.5, 0, -3, "lots", None):
            o = reliance(qty=bad)
            if bad is None:
                o.pop("qty")
            self.assertIn("format:", reasons(self.one(o)), bad)
        self.assertEqual(verdicts(self.one(reliance(qty="23"))), ["PASS"])
        res = self.one(dict(reliance(), notional="5000"))
        self.assertIn("no rupee-amount (notional) or fractional orders", reasons(res))

    def test_prices_must_sit_on_the_tick(self):
        self.assertIn("limit_price 1432.62 is not a multiple of the 0.05 tick",
                      reasons(self.one(reliance(order_type="limit", limit_price=1432.62))))
        self.assertIn("stop_loss 1411.57 is not a multiple", reasons(self.one(reliance(stop_loss=1411.57))))
        self.assertIn("take_profit 1453.62 is not a multiple", reasons(self.one(reliance(take_profit=1453.62))))
        self.assertEqual(verdicts(self.one(reliance(order_type="limit", limit_price=1432.60))), ["PASS"])
        # a stock with a 0.01 tick: your config says so
        self.assertEqual(verdicts(self.one(reliance(order_type="limit", limit_price=1432.62), tick=0.01)), ["PASS"])
        for v in (1432.6, 1432.65, 1432.55, 100.0, 0.05):
            self.assertTrue(check.on_tick(v, 0.05), v)
        for v in (1432.62, 1432.601, 0.03):
            self.assertFalse(check.on_tick(v, 0.05), v)

    def test_price_fields_must_be_positive_numbers(self):
        self.assertIn("stop_loss must be a positive number", reasons(self.one(reliance(stop_loss=-5))))
        self.assertIn("take_profit must be a positive number", reasons(self.one(reliance(take_profit="high"))))

    def test_product_type_and_validity(self):
        self.assertEqual(verdicts(self.one(reliance(product="CNC"))), ["PASS"])
        self.assertIn("product must be MIS (intraday) or CNC (delivery), got 'NRML'",
                      reasons(self.one(reliance(product="NRML"))))
        o = reliance()
        o.pop("product")
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("treated as MIS", warnings(res))
        self.assertIn("validity must be DAY or IOC", reasons(self.one(reliance(validity="GTC"))))
        self.assertEqual(verdicts(self.one(reliance(validity="ioc"))), ["PASS"])

    def test_order_types(self):
        self.assertIn("unknown order_type 'stop'", reasons(self.one(reliance(order_type="stop"))))
        self.assertIn("a limit order needs a limit_price", reasons(self.one(reliance(order_type="limit"))))
        self.assertIn("a sl order needs a limit_price", reasons(self.one(reliance(order_type="sl", trigger_price=1432.60))))
        self.assertIn("a sl order needs a trigger_price", reasons(self.one(reliance(order_type="sl", limit_price=1432.60))))
        self.assertIn("a sl-m order needs a trigger_price", reasons(self.one(reliance(order_type="SL-M"))))
        self.assertEqual(verdicts(self.one(reliance(order_type="SL_M", trigger_price=1432.60))), ["PASS"])

    def test_exchange_and_legs(self):
        self.assertIn("exchange must be NSE or BSE", reasons(self.one(reliance(exchange="NYSE"))))
        self.assertEqual(verdicts(self.one(reliance(exchange="BSE"))), ["PASS"])
        o = reliance()
        o.pop("take_profit")
        res = self.one(o, min_reward_risk=0)
        self.assertIn("an entry with legs needs both stop_loss and take_profit", reasons(res))

    def test_side_and_missing_symbol(self):
        self.assertIn("side must be buy or sell", reasons(self.one(reliance(side="hold"))))
        self.assertIn("no symbol", reasons(self.one(reliance(symbol=""))))


class Assets(unittest.TestCase):
    def one(self, sym, product="CNC", **over):
        o = {"symbol": sym, "side": "buy", "qty": 1, "product": product, "order_type": "market", "validity": "DAY"}
        return check.check([o], cfg(require_stop=False, min_reward_risk=0, no_entries_before=None,
                                    no_entries_after=None, min_order_inr=0, **over),
                           prices={sym: 100.0}, now=at(NOW))

    def test_plain_nse_symbols_pass(self):
        for s in ("RELIANCE", "M&M", "BAJAJ-AUTO", "3MINDIA", "20MICRONS", "reliance"):
            self.assertEqual(verdicts(self.one(s)), ["PASS"], s)

    def test_a_suffix_is_refused_with_a_hint(self):
        self.assertIn("use the bare NSE symbol (RELIANCE, not RELIANCE.NS)", reasons(self.one("RELIANCE.NS")))
        self.assertIn("has a suffix", reasons(self.one("TCS.BO")))

    def test_junk_symbols(self):
        for s in ("$$", "SBIN/USD", "BTC USD", "-ABC", "A" * 25):
            self.assertIn("asset:", reasons(self.one(s)), s)

    def test_derivatives_off_unless_allowed(self):
        for s in ("NIFTY26NOV24500CE", "BANKNIFTY26NOV52000PE", "RELIANCE26NOVFUT", "NIFTY26NOVFUT"):
            self.assertIn("derivatives are off", reasons(self.one(s)), s)
        res = self.one("NIFTY26NOV24500CE", allow_options=True)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("can lose 100%", warnings(res))

    def test_trade_to_trade_series_have_no_intraday(self):
        res = self.one("XYZ-BE", product="MIS")
        self.assertIn("trade-to-trade series: no intraday (MIS)", reasons(res))
        res = self.one("XYZ-BZ", product="MIS")
        self.assertIn("trade-to-trade", reasons(res))
        res = self.one("XYZ-BE", product="CNC")
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("every buy is delivery", warnings(res))
        # a hyphen is not a series
        self.assertEqual(verdicts(self.one("BAJAJ-AUTO", product="MIS")), ["PASS"])


class IntradayChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_a_stop_is_required(self):
        o = reliance()
        for k in ("take_profit", "stop_loss", "plan"):
            o.pop(k)
        o["plan"] = {"entry": 1432.60}
        res = self.one(o)
        self.assertIn("stop: no stop", reasons(res))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop: the max loss is not capped", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = reliance()
        o.pop("stop_loss")
        o.pop("take_profit")
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("the stop 1411.55 is in the plan only, not on the order", warnings(res))
        o = reliance(stop_loss=1411.50)
        self.assertIn("differs from the plan's stop 1411.55", warnings(self.one(o)))

    def test_stop_and_target_on_the_right_side(self):
        self.assertIn("stop: the stop 1440.00 must be below the entry 1432.60 for a buy",
                      reasons(self.one(reliance(stop_loss=1440.00))))
        self.assertIn("target: the target 1430.00 must be above the entry 1432.60",
                      reasons(self.one(reliance(take_profit=1430.00))))

    def test_reward_risk(self):
        res = self.one(reliance(take_profit=1440.00))
        self.assertIn("reward:risk: 0.35 (target 1440.00, entry 1432.60, stop 1411.55) is under your minimum 1",
                      reasons(res))
        self.assertEqual(verdicts(self.one(reliance(take_profit=1440.00), min_reward_risk=0.3)), ["PASS"])
        o = reliance()
        o.pop("take_profit")
        o.pop("stop_loss")
        o["plan"] = {"entry": 1432.60, "stop": 1411.55}
        self.assertIn("no target to measure it against", reasons(self.one(o)))

    def test_a_fresh_higher_price_is_the_entry(self):
        res = check.check([reliance(qty=17)], cfg(min_reward_risk=0), prices={"RELIANCE": 1440.00}, now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["entry_ref"], r["entry_ref_from"], r["max_loss"]), ("PASS", 1440.0, "price", 483.65))
        self.assertIn("RELIANCE last 1440.00 is above the plan's entry 1432.60", " | ".join(r["info"]))
        res = check.check([reliance()], cfg(), prices={"RELIANCE": 1440.00}, now=at(NOW))   # 23 x 28.45
        self.assertIn("loss: 23 shares x 28.45 risk a share = ₹654.35", reasons(res))

    def test_a_stale_or_future_price_is_not_used(self):
        old = {"RELIANCE": at("2026-10-30 09:00")}          # 40 minutes before NOW
        res = check.check([reliance()], cfg(), prices={"RELIANCE": 1440.00}, now=at(NOW), price_times=old)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("older than 15 minutes: not used", warnings(res))
        future = {"RELIANCE": at("2026-10-30 10:30")}
        res = check.check([reliance()], cfg(), prices={"RELIANCE": 1440.00}, now=at(NOW), price_times=future)
        self.assertEqual(verdicts(res), ["PASS"])
        fresh = {"RELIANCE": at("2026-10-30 09:36")}
        res = check.check([reliance()], cfg(), prices={"RELIANCE": 1440.00}, now=at(NOW), price_times=fresh)
        self.assertEqual(verdicts(res), ["BLOCK"])

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = check.check([reliance()], cfg(), prices={"RELIANCE": 1420.00}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 1432.60)

    def test_a_limit_order_uses_its_limit_price(self):
        res = self.one(reliance(order_type="limit", limit_price=1430.00, qty=27))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["entry_ref"], r["entry_ref_from"], r["max_loss"]), ("PASS", 1430.0, "limit", 498.15))
        res = check.check([reliance(order_type="limit", limit_price=1430.00, qty=27)], cfg(),
                          prices={"RELIANCE": 1500.00}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 1430.0)

    def test_daily_loss_and_trades_per_day(self):
        state = {"orders": [placed("TCS", max_loss=900.0)]}
        res = check.check([reliance()], cfg(), state=state, now=at(NOW))        # 900 + 484.15 <= 1500
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 1 of 3 trades, ₹900 of ₹1,500 daily loss", " | ".join(res["results"][0]["info"]))
        state = {"orders": [placed("TCS", max_loss=1100.0)]}                    # 1100 + 484.15 > 1500
        res = check.check([reliance()], cfg(), state=state, now=at(NOW))
        self.assertIn("daily loss: ₹1,100 already at risk today plus ₹484.15 for this one = ₹1,584.15, "
                      "over your ₹1,500 daily loss limit", reasons(res))
        state = {"orders": [placed("TCS", max_loss=10.0), placed("INFY", max_loss=10.0), placed("ITC", max_loss=10.0)]}
        res = check.check([reliance()], cfg(), state=state, now=at(NOW))
        self.assertIn("trades: 3 entries placed today", reasons(res))
        # yesterday's trades do not count
        state = {"orders": [placed("TCS", max_loss=9000.0, date="2026-10-29")]}
        self.assertEqual(verdicts(check.check([reliance()], cfg(), state=state, now=at(NOW))), ["PASS"])
        # an entry with no max loss in the log is a warning, not a guess
        state = {"orders": [placed("TCS", max_loss=None)]}
        res = check.check([reliance()], cfg(), state=state, now=at(NOW))
        self.assertIn("has no max loss in the log", " | ".join(res["warnings"]))

    def test_exits_and_declines_do_not_count_as_trades(self):
        state = {"orders": [placed("TCS", side="sell", kind="exit", max_loss=None),
                            {"status": "declined", "date": TODAY, "symbol": "INFY", "side": "buy"},
                            {"status": "approved", "date": TODAY, "symbol": "ITC", "side": "buy"}]}
        res = check.check([reliance()], cfg(max_trades_per_day=1), state=state, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [reliance(), reliance(symbol="TCS"), reliance(symbol="INFY")]
        res = check.check(orders, cfg(max_trades_per_day=2, max_daily_loss_inr=100000), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("trades: 0 entries placed today plus 2 passed earlier in this batch, your limit is 2 a day",
                      reasons(res, 2))
        res = check.check(orders, cfg(max_daily_loss_inr=1000), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("daily loss:", reasons(res, 2))

    def test_entry_window_india_time(self):
        def v(now, **over):
            return check.check([reliance()], cfg(**over), now=at(now))
        cases = [
            ("2026-10-30 09:00", "market is closed", "the session is 09:15 to 15:30"),   # pre-open auction
            ("2026-10-30 09:14", "market is closed", None),
            ("2026-10-30 09:15", "no new entries before 09:30 IST (it is 09:15)", None),
            ("2026-10-30 09:29", "no new entries before 09:30 IST", None),
            ("2026-10-30 09:30", None, None),
            ("2026-10-30 14:30", None, None),
            ("2026-10-30 14:31", "no new entries after 14:30 IST (it is 14:31)", None),
            ("2026-10-30 15:29", "no new entries after 14:30 IST", None),
            ("2026-10-30 15:30", "market is closed", None),
            ("2026-10-30 21:00", "market is closed", None),
        ]
        for now, want, extra in cases:
            res = v(now)
            if want is None:
                self.assertEqual(verdicts(res), ["PASS"], now)
            else:
                self.assertIn(want, reasons(res), now)
            if extra:
                self.assertIn(extra, reasons(res))
        self.assertEqual(verdicts(v("2026-10-30 09:15", no_entries_before="09:15")), ["PASS"])
        self.assertEqual(verdicts(v("2026-10-30 03:00", no_entries_before=None, no_entries_after=None)), ["PASS"])

    def test_muhurat_entry_window_is_mandatory(self):
        # Config cannot widen or disable the special-session limits.
        c = cfg(no_entries_before=None, no_entries_after=None)

        res = check.check([reliance()], c, now=at("2026-11-08 14:19"))
        self.assertIn("market is closed", reasons(res))

        res = check.check([reliance()], c, now=at("2026-11-08 14:20"))
        self.assertIn("no new entries before 14:40", reasons(res))

        res = check.check([reliance()], c, now=at("2026-11-08 14:39"))
        self.assertIn("no new entries before 14:40", reasons(res))

        res = check.check([reliance()], c, now=at("2026-11-08 14:40"))
        self.assertNotIn("no new entries", reasons(res))

        res = check.check([reliance()], c, now=at("2026-11-08 15:00"))
        self.assertNotIn("no new entries", reasons(res))

        res = check.check([reliance()], c, now=at("2026-11-08 15:01"))
        self.assertIn("no new entries after 15:00", reasons(res))

        res = check.check([reliance()], c, now=at("2026-11-08 15:15"))
        self.assertIn("market is closed", reasons(res))

    def test_weekends_and_holidays_are_closed(self):
        res = check.check([reliance()], cfg(), now=at("2026-10-31 10:00"))     # Saturday
        self.assertIn("the market is closed today (weekend, Sat 2026-10-31 10:00 IST)", reasons(res))
        res = check.check([reliance()], cfg(), now=at("2026-11-08 18:00"))     # Sunday, Diwali Muhurat evening
        self.assertIn("market is closed", reasons(res))
        res = check.check([reliance()], cfg(), now=at("2026-10-20 10:00"))     # Dussehra, a Tuesday
        self.assertIn("NSE holiday: Dussehra", reasons(res))
        res = check.check([reliance()], cfg(), now=at("2026-11-24 10:00"))
        self.assertIn("NSE holiday: Prakash Gurpurb", reasons(res))
        res = check.check([reliance()], cfg(), now=at("2026-11-10 10:00"))
        self.assertIn("NSE holiday: Diwali-Balipratipada", reasons(res))
        res = check.check([reliance()], cfg(), now=at("2026-10-21 10:00"))     # the next day is a normal day
        self.assertEqual(verdicts(res), ["PASS"])

    def test_the_2026_holiday_list(self):
        self.assertEqual(len(check.NSE_HOLIDAYS), 16)
        for day in check.NSE_HOLIDAYS:
            self.assertTrue(day.startswith("2026-"), day)
            import datetime
            self.assertLess(datetime.date.fromisoformat(day).weekday(), 5, day)     # weekday closures only
        self.assertEqual(check.CALENDAR_YEARS, (2026,))
        self.assertEqual(check.NSE_EARLY_CLOSE, {})
        self.assertEqual((check.OPEN.strftime("%H:%M"), check.CLOSE.strftime("%H:%M")), ("09:15", "15:30"))

    def test_an_unknown_calendar_year_warns(self):
        res = check.check([reliance()], cfg(), now=at("2027-01-05 10:00"))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("the holiday list covers 2026 only: check nseindia.com for 2027-01-05", " | ".join(res["warnings"]))
        res = check.check([reliance()], cfg(no_entries_before=None, no_entries_after=None), now=at("2027-01-05 10:00"))
        self.assertEqual(res["warnings"], [])        # window off: no window warning

    def test_the_window_uses_india_time_not_the_machine(self):
        # 04:10Z is 09:40 IST whatever the machine's clock says
        res = check.check([reliance()], cfg(), now=at("2026-10-30T04:10:00Z"))
        self.assertEqual((verdicts(res), res["date"]), (["PASS"], TODAY))
        # 19:00Z on the 29th is already 00:30 on the 30th in India
        res = check.check([reliance()], cfg(), now=at("2026-10-29T19:00:00Z"))
        self.assertEqual(res["date"], TODAY)
        self.assertIn("market is closed", reasons(res))
        self.assertEqual(at("2026-10-30 09:40").isoformat(), "2026-10-30T04:10:00")
        self.assertEqual(check.to_ist(at("2026-10-30T04:10:00Z")).strftime("%H:%M"), "09:40")
        with self.assertRaises(check.RiskError):
            check.parse_now("soon")
        with self.assertRaises(check.RiskError):
            check.check([reliance()], cfg(no_entries_before="9.30"), now=at(NOW))

    def test_there_is_no_pattern_day_trader_rule(self):
        self.assertNotIn("pdt_guard", check.DEFAULTS)
        self.assertFalse(hasattr(check, "day_trades"))
        res = check.check([reliance()], cfg(pdt_guard=True), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertNotIn("day_trades_5d", res["today"])

    def test_a_short_entry_when_allowed(self):
        o = reliance(side="sell", stop_loss=1453.65, take_profit=1411.55)
        res = check.check([o], cfg(), prices={"RELIANCE": 1432.60}, now=at(NOW))
        self.assertIn("short: selling more RELIANCE than you hold (0 shares); short selling is off", reasons(res))
        res = check.check([o], cfg(allow_short=True), now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["max_loss"]), ("PASS", "entry", 484.15), r["reasons"])


class Exits(unittest.TestCase):
    PF = {"cash": 50000, "positions": [{"ticker": "RELIANCE", "shares": 23, "lots": []}]}

    def sell(self, qty=23, **kw):
        o = {"symbol": "RELIANCE", "exchange": "NSE", "side": "sell", "qty": qty, "product": "MIS",
             "order_type": "market", "validity": "DAY"}
        o.update(kw)
        return o

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        state = {"orders": [placed("TCS", max_loss=9999.0)] * 3}
        for now in ("2026-10-30 15:20", "2026-10-30 09:16", "2026-10-31 11:00", "2026-10-20 11:00"):
            res = check.check([self.sell()], cfg(max_orders_per_day=1), self.PF, PRICES, state, now=at(now))
            r = res["results"][0]
            self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), (now, r["reasons"]))
        # the same sell after the entry window and at the closing hour: still an exit, still a PASS
        res = check.check([self.sell()], cfg(), self.PF, PRICES, state, now=at("2026-10-30 15:20"))
        self.assertEqual((verdicts(res), res["results"][0]["reasons"]), (["PASS"], []))

    def test_a_partial_exit_and_the_rest(self):
        res = check.check([self.sell(10), self.sell(13, order_type="limit", limit_price=1440.00)],
                          cfg(), self.PF, PRICES, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        # a duplicate sell is only a warning on an exit
        res = check.check([self.sell(10), self.sell(10)], cfg(), self.PF, PRICES, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertIn("duplicate", warnings(res, 1))

    def test_format_asset_and_short_still_apply_to_a_sell(self):
        self.assertIn("format:", reasons(check.check([self.sell(qty=0)], cfg(), self.PF, PRICES, now=at(NOW))))
        self.assertIn("format:", reasons(check.check([self.sell(order_type="limit")], cfg(), self.PF, PRICES, now=at(NOW))))
        self.assertIn("asset:", reasons(check.check([self.sell(symbol="RELIANCE.NS")], cfg(), self.PF, PRICES, now=at(NOW))))
        res = check.check([self.sell(qty=30)], cfg(), self.PF, PRICES, now=at(NOW))
        self.assertIn("short: selling more RELIANCE than you hold (23 shares)", reasons(res))

    def test_holdings_match_with_or_without_a_suffix(self):
        pf = {"cash": 50000, "positions": [{"ticker": "reliance.ns", "shares": 23, "lots": []}]}
        res = check.check([self.sell()], cfg(), pf, PRICES, now=at(NOW))
        self.assertEqual(res["results"][0]["kind"], "exit")

    def test_buy_then_sell_in_one_batch(self):
        buy = reliance()
        sell = self.sell()
        res = check.check([buy, sell], cfg(), None, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["entry", "exit"])


class Basket(unittest.TestCase):
    """A delivery (CNC) basket: five buys on a fresh Rs 1,00,000 account, no stops."""

    def test_the_five_buys_pass_on_a_fresh_account(self):
        res = check.check(basket(), cfg(**BASKET_CFG), prices=PRICES, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"] * 5, [r["reasons"] for r in res["results"]])
        self.assertEqual(res["cash_after"], 12433.30)
        self.assertEqual(res["results"][0]["value_inr"], 21489.0)

    def test_a_sixth_buy_is_blocked_on_cash(self):
        extra = {"symbol": "SBIN", "exchange": "NSE", "side": "buy", "qty": 25, "product": "CNC",
                 "order_type": "market", "validity": "DAY"}
        res = check.check(basket() + [extra], cfg(**BASKET_CFG), prices=PRICES, now=at(NOW))
        self.assertEqual(verdicts(res)[-1], "BLOCK")
        self.assertIn("cash: needs ₹20,750 but only ₹12,433.30 cash; that would be borrowed money (MIS leverage)",
                      reasons(res, 5))
        res = check.check(basket() + [extra], cfg(allow_margin=True, cash_buffer_pct=0, **BASKET_CFG), prices=PRICES,
                          now=at(NOW))
        self.assertEqual(verdicts(res)[-1], "PASS")

    def test_the_default_config_blocks_buys_without_a_stop(self):
        res = check.check(basket(), cfg(), prices=PRICES, now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"] * 5)
        self.assertIn("stop: no stop", reasons(res))

    def test_max_and_min_order(self):
        res = check.check(basket(), cfg(max_order_inr=20000, **{k: v for k, v in BASKET_CFG.items()}),
                          prices=PRICES, now=at(NOW))
        self.assertIn("size: ₹21,489 is over your ₹20,000 max order", reasons(res, 0))
        res = check.check(basket(), cfg(min_order_inr=15000, **BASKET_CFG), prices=PRICES, now=at(NOW))
        self.assertIn("size: ₹12,370.50 is under your ₹15,000 minimum order", reasons(res, 4))

    def test_position_cap(self):
        res = check.check(basket(), cfg(**dict(BASKET_CFG, max_position_pct=20)), prices=PRICES, now=at(NOW))
        self.assertIn("position: RELIANCE would be ₹21,489, 21.5% of the ₹1,00,000 account, over your 20% cap",
                      reasons(res, 0))

    def test_blocked_list(self):
        res = check.check(basket(), cfg(blocked=["itc.ns"], **BASKET_CFG), prices=PRICES, now=at(NOW))
        self.assertIn("blocked: ITC is on your blocked list", reasons(res, 4))

    def test_the_cash_buffer(self):
        res = check.check(basket(), cfg(cash_buffer_pct=20, **BASKET_CFG), prices=PRICES, now=at(NOW))
        self.assertIn("cash: leaves ₹12,433.30 cash, under your 20% buffer of ₹20,000", reasons(res, 4))

    def test_share_orders_need_a_price(self):
        res = check.check(basket()[:1], cfg(**BASKET_CFG), prices={}, now=at(NOW))
        self.assertIn("price: no price for RELIANCE to size a 15-share order", reasons(res))

    def test_a_sell_frees_cash_for_a_later_buy(self):
        pf = {"cash": 1000, "positions": [{"ticker": "TCS", "shares": 5, "lots": []}]}
        sell = {"symbol": "TCS", "side": "sell", "qty": 5, "product": "CNC", "order_type": "market"}
        buy = {"symbol": "ITC", "side": "buy", "qty": 30, "product": "CNC", "order_type": "market"}
        rules = dict(BASKET_CFG, cash_buffer_pct=0, max_position_pct=100)
        res = check.check([buy, sell], cfg(**rules), pf, PRICES, now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK", "PASS"])
        res = check.check([sell, buy], cfg(**rules), pf, PRICES, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])

    def test_daily_order_limit_and_already_placed_today(self):
        res = check.check(basket(), cfg(max_orders_per_day=2, **BASKET_CFG), prices=PRICES, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK", "BLOCK", "BLOCK"])
        self.assertIn("daily limit: 0 placed today plus 2 passed earlier in this batch, your limit is 2 a day",
                      reasons(res, 2))
        state = {"orders": [{"status": "placed", "date": TODAY, "symbol": "TCS", "side": "buy"}]}
        res = check.check(basket(), cfg(**BASKET_CFG), prices=PRICES, state=state, now=at(NOW))
        self.assertIn("duplicate: a buy TCS was already placed today", reasons(res, 2))
        res = check.check([basket()[0], basket()[0]], cfg(**BASKET_CFG), prices=PRICES, now=at(NOW))
        self.assertIn("duplicate: a second buy RELIANCE in this batch", reasons(res, 1))

    def test_live_account_warns(self):
        res = check.check(basket()[:1], cfg(paper=False, **BASKET_CFG), prices=PRICES, now=at(NOW))
        self.assertIn("LIVE account", res["warnings"][0])
        self.assertIs(res["paper"], False)

    def test_account_needs_a_budget_or_portfolio(self):
        with self.assertRaises(check.RiskError):
            check.check(basket(), cfg(budget=None, **BASKET_CFG), prices=PRICES, now=at(NOW))
        with self.assertRaises(check.RiskError):
            check.check(basket(), cfg(**BASKET_CFG), {"positions": []}, PRICES, now=at(NOW))
        res = check.check(basket()[:1], cfg(budget=None, **BASKET_CFG), {"cash": 100000, "positions": [
            {"ticker": "TCS", "shares": 2, "lots": [{"shares": 2, "cost": 6000}]}]}, PRICES, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])


class Rupees(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(check.money(100000), "₹1,00,000")
        self.assertEqual(check.money(1234567.5), "₹12,34,567.50")
        self.assertEqual(check.money(999.999), "₹1,000")
        self.assertEqual(check.money(0), "₹0")
        self.assertEqual(check.money(-2500), "-₹2,500")


class Cli(unittest.TestCase):
    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            none = os.path.join(d, "none")

            def run(quote_time):
                with open(pp, "w") as fh:
                    json.dump({"as_of": quote_time, "prices": {"RELIANCE": 1440.00},
                               "detail": {"RELIANCE": {"time": quote_time}}}, fh)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = check.main(["--orders", RELIANCE_PATH, "--config", CFG_PATH, "--portfolio", none,
                                       "--prices", pp, "--state", none, "--now", NOW, "--no-write"])
                return code, buf.getvalue()
            code, text = run("2026-10-30T04:05:00Z")            # 5 minutes old: used, so the loss is too big
            self.assertEqual(code, 1)
            self.assertIn("BLOCK", text)
            self.assertIn("loss: 23 shares x 28.45", text)
            code, text = run("2026-10-30T03:30:00Z")            # 40 minutes old: ignored
            self.assertEqual(code, 0)
            self.assertIn("older than 15 minutes: not used", text)

    def test_basket_with_a_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            cp, pp, none = os.path.join(d, "c.json"), os.path.join(d, "p.json"), os.path.join(d, "none")
            with open(cp, "w") as fh:
                json.dump(dict(cfg(**BASKET_CFG)), fh)
            with open(pp, "w") as fh:
                json.dump({"prices": PRICES}, fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", BASKET_PATH, "--config", cp, "--portfolio", none, "--prices", pp,
                                   "--state", none, "--now", NOW, "--json", "--no-write"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["pass"], 5)

    def test_blocked_orders_exit_1_and_errors_are_one_line(self):
        with tempfile.TemporaryDirectory() as d:
            none = os.path.join(d, "none")
            err = io.StringIO()
            with redirect_stderr(err):
                code = check.main(["--orders", os.path.join(d, "missing.json"), "--config", none, "--now", NOW])
            self.assertEqual(code, 1)
            self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
            self.assertIn("no orders file (run nse-budget first)", err.getvalue())
            err = io.StringIO()
            with redirect_stderr(err):
                code = check.main(["--orders", RELIANCE_PATH, "--config", CFG_PATH, "--now", "later"])
            self.assertEqual(code, 1)
            self.assertIn("--now wants a time", err.getvalue())
            err = io.StringIO()
            with redirect_stderr(err):
                code = check.main(["--orders", RELIANCE_PATH, "--config", CFG_PATH, "--today", "30-10-2026"])
            self.assertIn("--today wants a date like 2026-10-30", err.getvalue())


if __name__ == "__main__":
    unittest.main()
