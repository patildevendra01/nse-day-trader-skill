"""nse-alerts tests. Fixtures in tests/fixtures/nse-alerts/ are SYNTHETIC, shaped like Yahoo chart
JSON v8 (not market data): yahoo-RELIANCE.json is a five-minute session on 2026-10-30 with a live
tick at 10:37:13 IST, yahoo-TCS/HDFCBANK/INFY.json are one-day closes on 2026-10-29,
yahoo-ZZZZ.json is Yahoo's "No data found" answer, plan-reliance.json is an nse-exit plan."""

import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("nse-alerts", "alerts")

QDIR = os.path.join(FIX, "nse-alerts")
PLAN = os.path.join(QDIR, "plan-reliance.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; nse-day-trader-skill/1.0)"

# NSE equity trading holidays on weekdays, 2026 (nseindia.com, cross-checked 2026-10-31)
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
            "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
            "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25"]


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class Calendar(unittest.TestCase):
    def test_holidays_match_the_published_list(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        self.assertEqual(alerts.NSE_EARLY_CLOSE, {})                 # the NSE has no scheduled half days
        self.assertEqual(alerts.CALENDAR_YEARS, (2026,))
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)

    def test_muhurat_session_status_and_next_open(self):
        self.assertEqual(alerts.trading_day(datetime.date(2026, 11, 8)),
                         (True, "special Muhurat session"))
        self.assertEqual(alerts.session(datetime.date(2026, 11, 8)),
                         (datetime.time(14, 20), datetime.time(15, 15)))

        before = alerts.market_status(ist("2026-11-08T14:04"))
        self.assertFalse(before["open"])
        self.assertEqual(before["next_open"], "2026-11-08 14:20")

        pre = alerts.market_status(ist("2026-11-08T14:05"))
        self.assertFalse(pre["open"])
        self.assertIn("pre-open", pre["reason"])

        opened = alerts.market_status(ist("2026-11-08T14:20"))
        self.assertTrue(opened["open"])
        self.assertEqual((opened["opens"], opened["closes"]), ("14:20", "15:15"))
        self.assertIn("Muhurat", opened["reason"])

        closed = alerts.market_status(ist("2026-11-08T15:15"))
        self.assertFalse(closed["open"])
        self.assertEqual(closed["reason"], "after the close")
        self.assertNotEqual(closed["next_open"], "2026-11-08 09:15")

    def test_india_standard_time_is_fixed_plus_0530(self):
        self.assertEqual(alerts.to_ist(DT(2026, 10, 30, 3, 45)), DT(2026, 10, 30, 9, 15))
        self.assertEqual(alerts.to_ist(DT(2026, 3, 9, 3, 45)), DT(2026, 3, 9, 9, 15))      # no daylight saving
        self.assertEqual(alerts.to_ist(DT(2026, 7, 1, 3, 45)), DT(2026, 7, 1, 9, 15))
        self.assertEqual(alerts.ist_to_utc(DT(2026, 10, 30, 15, 30)), DT(2026, 10, 30, 10, 0))
        self.assertFalse(hasattr(alerts, "eastern_offset"))

    def test_parse_now(self):
        self.assertEqual(ist("2026-10-30T09:15"), DT(2026, 10, 30, 3, 45))
        self.assertEqual(ist("2026-10-30 10:00"), DT(2026, 10, 30, 4, 30))
        self.assertEqual(ist("2026-12-01T09:15"), DT(2026, 12, 1, 3, 45))
        self.assertEqual(ist("2026-10-29T10:00:01Z"), DT(2026, 10, 29, 10, 0, 1))
        self.assertEqual(ist("2026-10-30T09:15+05:30"), DT(2026, 10, 30, 3, 45))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        st = alerts.market_status(ist("2026-10-30T10:00"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"]), ("09:15", "15:30"))
        self.assertEqual(st["now_ist"], "2026-10-30 10:00")
        self.assertTrue(st["calendar_known"])
        self.assertTrue(alerts.market_status(ist("2026-10-30T09:15"))["open"])
        before = alerts.market_status(ist("2026-10-30T09:14"))
        self.assertFalse(before["open"])
        self.assertEqual(before["next_open"], "2026-10-30 09:15")
        pre = alerts.market_status(ist("2026-10-30T09:05"))
        self.assertFalse(pre["open"])
        self.assertIn("pre-open", pre["reason"])
        self.assertEqual(alerts.market_status(ist("2026-10-30T08:00"))["reason"], "before the open")
        self.assertTrue(alerts.market_status(ist("2026-10-30T15:29"))["open"])
        after = alerts.market_status(ist("2026-10-30T15:30"))
        self.assertFalse(after["open"])
        self.assertEqual(after["reason"], "after the close")
        self.assertEqual(after["next_open"], "2026-11-02 09:15")                           # Friday -> Monday
        dussehra = alerts.market_status(ist("2026-10-20T11:00"))
        self.assertFalse(dussehra["open"])
        self.assertIn("Dussehra", dussehra["reason"])
        self.assertEqual(dussehra["next_open"], "2026-10-21 09:15")
        xmas = alerts.market_status(ist("2026-12-25T10:00"))
        self.assertIn("Christmas", xmas["reason"])
        self.assertEqual(xmas["next_open"], "2026-12-28 09:15")
        self.assertEqual(alerts.market_status(ist("2026-10-31T10:00"))["reason"], "weekend")

    def test_the_2027_calendar_is_unknown_until_the_nse_publishes_it(self):
        st = alerts.market_status(ist("2027-03-01T10:00"))
        self.assertFalse(st["calendar_known"])
        self.assertTrue(st["open"])                              # a plain Monday still reads as open
        self.assertFalse(alerts.market_status(ist("2028-03-01T10:00"))["calendar_known"])


class Money(unittest.TestCase):
    def test_rupees_use_indian_grouping(self):
        self.assertEqual(alerts.inr(999.5), "₹999.50")
        self.assertEqual(alerts.inr(2858.5), "₹2,858.50")
        self.assertEqual(alerts.inr(100000), "₹1,00,000.00")
        self.assertEqual(alerts.inr(1234567.5), "₹12,34,567.50")
        self.assertEqual(alerts.inr(-4521.05), "-₹4,521.05")
        self.assertEqual(alerts.inr(2900, None), "₹2,900")
        self.assertEqual(alerts.inr(2794.9, None), "₹2,794.90")

    def test_symbols_and_yahoo_names(self):
        self.assertEqual(alerts.norm(" reliance.ns "), "RELIANCE")
        self.assertEqual(alerts.norm("m&m"), "M&M")
        self.assertEqual(alerts.norm("RELIANCE.BO"), "RELIANCE.BO")
        self.assertEqual(alerts.yahoo_symbol("TCS"), "TCS.NS")
        self.assertEqual(alerts.yahoo_symbol("TCS.NS"), "TCS.NS")
        self.assertEqual(alerts.yahoo_symbol("bajaj-auto"), "BAJAJ-AUTO.NS")
        self.assertEqual(alerts.yahoo_symbol("RELIANCE.BO"), "RELIANCE.BO")


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-TCS.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "TCS")
        self.assertEqual(q["price"], 3412.7)
        self.assertEqual(q["prev_close"], 3409.7)
        self.assertEqual(q["time"], "2026-10-29T10:00:01Z")                  # 15:30:01 IST
        self.assertEqual(q["currency"], "INR")
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "RELIANCE")
        self.assertEqual((q["price"], q["prev_close"], q["open"]), (2858.5, 2766.8, 2788.4))
        self.assertEqual(q["time"], "2026-10-30T05:07:13Z")                  # 10:37:13 IST
        self.assertEqual(q["symbol"], "RELIANCE")

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d")
        self.assertIn("/chart/M%26M.NS?", alerts.yahoo_url("m&m"))              # & must be encoded
        self.assertIn("/chart/BAJAJ-AUTO.NS?", alerts.yahoo_url("bajaj-auto"))
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("TCS", alerts.YAHOO_HOSTS[1]))
        self.assertFalse(hasattr(alerts, "stooq_url"))

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["TCS", "INFY", "ZZZZ", "NOPE"], "yahoo", QDIR)
        self.assertEqual(sorted(q), ["INFY", "TCS"])
        self.assertEqual(q["INFY"]["price"], 1870.1)
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])
        q, _ = alerts.get_quotes(["reliance.ns"], "yahoo", QDIR)                # .NS and case are normalised
        self.assertEqual(list(q), ["RELIANCE"])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-RELIANCE.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["RELIANCE"], "yahoo", d)
            self.assertEqual((q["RELIANCE"]["price"], errors), (2858.5, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps, self.urls = list(codes), [], [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        self.urls.append(req.full_url)
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query2_first_with_the_contract_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (2858.5, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA)])
        self.assertEqual(f.urls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d"])

    def test_m_and_m_is_url_encoded_on_the_wire(self):
        f = self.net([200])
        alerts.get_quotes(["M&M"])
        self.assertIn("/chart/M%26M.NS?", f.urls[0])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 2858.5)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_symbols(self):
        f = self.net([200, 200])
        alerts.get_quotes(["RELIANCE", "TCS"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["TCS", "HDFCBANK", "INFY", "RELIANCE"], "yahoo", QDIR)
        self.open = alerts.market_status(ist("2026-10-30T10:00"))
        self.now = ist("2026-10-30T10:00")

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_muhurat_before_close_alert_rings_at_1510(self):
        rule = [{"id": "flat", "type": "time", "at": "15:15",
                 "label": "flat by close", "before_close": True}]
        self.assertEqual(self.at("2026-11-08 15:09", rule)[0], [])
        rings, state = self.at("2026-11-08 15:10", rule)
        self.assertEqual(len(rings), 1)
        self.assertIn("set for 15:10", rings[0]["message"])
        self.assertIn("market closes at 15:15", rings[0]["message"])
        self.assertEqual(self.at("2026-11-08 15:11", rule, state)[0], [])

    def test_a_target_rings_once(self):
        rules = [{"id": "tcs-3400", "symbol": "TCS", "type": "above", "price": 3400}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("TCS is ₹3,412.70: at or above your alert level ₹3,400", rings[0]["message"])
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "hdfc-stop", "symbol": "HDFCBANK", "type": "stop", "price": 1695, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop ₹1,695", rings[0]["message"])
        self.q["HDFCBANK"]["price"] = 1700.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("hdfc-stop", st["rung"])
        self.q["HDFCBANK"]["price"] = 1688.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "hdfc-3", "symbol": "HDFCBANK", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "hdfc-1", "symbol": "HDFCBANK", "type": "pct_move", "pct": 1, "direction": "down"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("-1.14%", rings[0]["message"])
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])
        up = [{"id": "hdfc-up", "symbol": "HDFCBANK", "type": "pct_move", "pct": 1, "direction": "up"}]
        self.assertEqual(alerts.evaluate(up, self.q, self.open, {}, self.now)[0], [])
        rel = [{"id": "rel-3", "symbol": "RELIANCE", "type": "pct_move", "pct": 3}]
        rings, _ = self.at("2026-10-30 10:37", rel)
        self.assertIn("RELIANCE moved +3.31% to ₹2,858.50 (from ₹2,766.80)", rings[0]["message"])

    def test_rules_accept_ns_suffix_and_lowercase_symbols(self):
        rules = [{"id": "x", "symbol": "tcs.ns", "type": "above", "price": 3400}]
        self.assertEqual(len(alerts.evaluate(rules, self.q, self.open, {}, self.now)[0]), 1)

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        closed = alerts.market_status(ist("2026-10-30T08:00"))
        self.assertEqual(alerts.evaluate(rules, {}, closed, {}, ist("2026-10-30T08:00"))[0], [])
        pre = alerts.market_status(ist("2026-10-30T09:10"))                      # the pre-open auction is not the bell
        self.assertEqual(alerts.evaluate(rules, {}, pre, {}, ist("2026-10-30T09:10"))[0], [])
        rings, st = alerts.evaluate(rules, {}, self.open, {}, self.now)
        self.assertIn("Opening bell: the NSE is open (2026-10-30 10:00 IST)", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, {}, self.open, st, ist("2026-10-30T11:00"))[0], [])
        monday = alerts.market_status(ist("2026-11-02T09:16"))
        self.assertEqual(len(alerts.evaluate(rules, {}, monday, st, ist("2026-11-02T09:16"))[0]), 1)

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"), ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (2794.9, 2886.2))
        self.assertEqual((rules[2]["at"], rules[2]["label"], rules[2]["before_close"]), ("15:15", "flat by close", True))
        self.assertTrue(all(r["date"] == "2026-10-30" for r in rules))
        rings, st = self.at("2026-10-30 10:37", rules)          # the 10:37 quote, 2858.50, sits between stop and target
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 2794.50
        rings, st = self.at("2026-10-30 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertIn("RELIANCE is ₹2,794.50: at or below your stop ₹2,794.90 (plan stop: opening-range midpoint)",
                      rings[0]["message"])
        q["RELIANCE"]["price"] = 2886.20
        self.assertEqual([r["id"] for r in self.at("2026-10-30 12:00", rules, st, q)[0]], ["reliance-plan-target"])

    def test_flat_by_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-30 15:14", rules)[0], [])
        rings, st = self.at("2026-10-30 15:15", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by close: it is 15:15 IST (set for 15:15), the market closes at 15:30", rings[0]["message"])
        self.assertIn("nse-broker flatten --symbol RELIANCE", rings[0]["message"])
        self.assertIn("auto square off MIS", rings[0]["message"])
        self.assertEqual(self.at("2026-10-30 15:29", rules, st)[0], [])
        rings, _ = self.at("2026-10-30 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-11-02 15:16", rules, st)[0], [])      # plan rules: their day only

    def test_time_alerts_skip_weekends_and_holidays_and_ring_in_utc_stamps(self):
        rule = [{"id": "flat", "type": "time", "at": "15:15", "label": "flat by close", "before_close": True}]
        self.assertEqual(self.at("2026-10-31 15:16", rule)[0], [])           # Saturday: no session
        self.assertEqual(self.at("2026-10-20 15:16", rule)[0], [])           # Dussehra
        rings, st = self.at("2026-12-01 15:15", rule)
        self.assertEqual(len(rings), 1)
        self.assertEqual(st["rung"]["flat"], "2026-12-01T09:45:00Z")         # 15:15 IST is 09:45 UTC, all year
        rings, st = self.at("2026-11-27 15:15", rule)                        # no half day on the NSE
        self.assertIn("set for 15:15), the market closes at 15:30", rings[0]["message"])

    def test_before_close_moves_with_a_shortened_session_if_the_nse_ever_announces_one(self):
        saved = (alerts.NSE_EARLY_CLOSE, alerts.EARLY)
        alerts.NSE_EARLY_CLOSE = {"2026-11-27": "test session"}
        alerts.EARLY = datetime.time(12, 30)
        try:
            rule = [{"id": "flat", "type": "time", "at": "15:15", "label": "flat by close", "before_close": True}]
            self.assertEqual(self.at("2026-11-27 12:14", rule)[0], [])
            rings, _ = self.at("2026-11-27 12:15", rule)                     # 15:15 minus the 3-hour gap
            self.assertIn("set for 12:15), the market closes at 12:30", rings[0]["message"])
            plain = [{"id": "t", "type": "time", "at": "15:15", "label": "check"}]
            self.assertEqual(self.at("2026-11-27 12:15", plain)[0], [])
            self.assertEqual(alerts.market_status(ist("2026-11-27T12:30"))["open"], False)
        finally:
            alerts.NSE_EARLY_CLOSE, alerts.EARLY = saved

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "nse-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-plan") or r["id"] == "reliance-flat-by"],
                         [dict(r, date="2026-10-30") for r in plan_rules()])

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "TCS"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.evaluate([{"id": "x", "type": "time", "at": "4pm"}], {}, self.open, {}, self.now)
        self.assertIn("IST time", str(cm.exception))
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 2794.9})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-10-30 10:37")
        self.assertEqual(code, 0)                                      # the opening bell rings first
        code, text, _ = self.check("2026-10-30 10:38")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE ₹2,858.50.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 2858.5})
        self.assertEqual(doc["detail"]["RELIANCE"]["time"], "2026-10-30T05:07:13Z")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-10-30T05:08:00Z")

        code, text, _ = self.check("2026-10-30 15:15")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-10-30 15:17")[0], 1)
        code, text, _ = self.check("2026-11-02 10:00")                 # the next trading day after the weekend
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-02 10:05")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "reliance-flat-by", "reliance-plan-stop", "reliance-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_the_portfolio_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "TCS", "--above", "3450"], ["--symbol", "HDFCBANK", "--stop", "1600"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "TCS", "--above", "3450"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-10-29T20:00")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"TCS": 3412.7, "HDFCBANK": 1692.5})

        code, text, _ = self.check("2026-10-30T09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-30T09:45")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:15", "--label", "flat by close",
                             "--before-close", "--symbol", "reliance"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "RELIANCE.NS", "--target", "2886.2",
                             "--date", "2026-10-30"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1515", "type": "time", "at": "15:15", "label": "flat by close",
                                    "symbol": "RELIANCE", "before_close": True})
        self.assertEqual(rules[1], {"id": "reliance-target-2886.20", "symbol": "RELIANCE", "type": "target",
                                    "price": 2886.2, "date": "2026-10-30"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "4pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "TCS", "INFY", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-29T10:00:01Z"])
        self.assertEqual(code, 0)
        self.assertIn("₹3,412.70 (+0.09% vs prev close ₹3,409.70)", text)
        self.assertIn("delayed", text)
        run(["quote", "HDFCBANK", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["HDFCBANK", "INFY", "TCS"])
        self.assertEqual(doc["detail"]["INFY"]["prev_close"], 1864.15)
        self.assertEqual(doc["detail"]["INFY"]["fetched"], "2026-10-29T10:00:01Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "INFY", "shares": 1}, {"symbol": "tcs.ns", "shares": 2}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["INFY", "RELIANCE", "TCS"])

    def test_clock_exit_codes(self):
        code, text, _ = run(["clock", "--now", "2026-10-30 10:15"])
        self.assertEqual(code, 0)
        self.assertIn("India 2026-10-30 10:15 IST (Friday): NSE OPEN", text)
        self.assertIn("09:15 to 15:30 IST", text)
        code, text, _ = run(["clock", "--now", "2026-12-25T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Christmas", text)
        self.assertIn("Next open: 2026-12-28 09:15 IST", text)
        code, text, _ = run(["clock", "--now", "2027-03-01 10:00"])
        self.assertIn("Holiday list covers 2026 only: check nseindia.com", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)

    def test_stooq_is_gone(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                run(["quote", "TCS", "--source", "stooq"])


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-alerts", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "nse-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-alerts\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("ist", "09:15", "15:30", "15:15", "nseindia.com", "dussehra", "m%26m.ns", "\u20b92,858.50",
                     "verified offline against fixtures; live endpoints not exercised at build time",
                     "mis", "/nse-exit", "calendar is unknown"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "nyse", "new york", "nasdaq"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
