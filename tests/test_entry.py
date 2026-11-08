"""nse-entry tests. Fixtures in tests/fixtures/nse-entry/ are SYNTHETIC (not market data), shaped like
Yahoo chart JSON v8 for an NSE stock:

RELIANCE-2026-10-30-5m.json  a Reliance-like five-minute session on Friday 2026-10-30, prices on the
                             Rs 0.05 tick. 16 bars from 09:15 IST, the in-progress 10:30 bar, then
                             Yahoo's live tick at 10:34:45. Opening range 2,764.85 to 2,825.00 (the
                             09:15, 09:20 and 09:25 bars), wicks above the high, and a first close
                             above it on bar 8 (the 09:55 bar, closing at 10:00 AM) at 2,840.55.
DEMO-1m-synthetic.json       one-minute bars: a wick above the range high, a close AT the high, a
                             null bar, a live tick.
"""

import copy
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

from _load import load, FIX, ROOT

entry = load("nse-entry", "entry")

EDIR = os.path.join(FIX, "nse-entry")
RELI = os.path.join(EDIR, "RELIANCE-2026-10-30-5m.json")
DEMO = os.path.join(EDIR, "DEMO-1m-synthetic.json")
DT = datetime.datetime
OPEN_TS = int((DT(2026, 10, 30, 3, 45) - DT(1970, 1, 1)).total_seconds())     # 09:15 IST, 2026-10-30
HIGH = 2825.0
SPIKE = 2830.0


def ist(text):
    return entry.parse_now(text)


def doc(path=RELI):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="RELIANCE"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift_to(d, day):
    """The same session moved to another date: 09:15 IST is 03:45 UTC on every date (no daylight saving)."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, datetime.time(3, 45)) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d



def shift_to_muhurat(d):
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT(2026, 11, 8, 8, 50) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d

def no_breakout(d=None):
    """Every bar after the range stays under the 2,825.00 high."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(3, len(q["close"])):
        q["close"][i] = min(q["close"][i], 2822.0)
        q["high"][i] = min(q["high"][i], 2824.5)
        q["open"][i] = min(q["open"][i], 2822.0)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheSession(unittest.TestCase):

    def test_muhurat_pre_open(self):
        r = at("2026-11-08 14:19", shift_to_muhurat(no_breakout()))
        self.assertEqual(r["phase"], "pre-open")
        self.assertEqual((r["opens"], r["closes"]), ("14:20", "15:15"))

    def test_muhurat_entry_window(self):
        d = shift_to_muhurat(no_breakout())
        r = at("2026-11-08 14:40", d)
        self.assertEqual(r["phase"], "open")
        self.assertEqual(r["no_entries_before"], "14:40")
        self.assertEqual(r["no_entries_after"], "15:00")

    def test_muhurat_cutoff(self):
        r = at("2026-11-08 15:00", shift_to_muhurat(no_breakout()))
        self.assertEqual(r["status"], "expired")
        self.assertEqual(r["phase"], "after-cutoff")

    def test_triggers_at_1000_on_the_0955_bar(self):
        r = at("2026-10-30 10:00")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["symbol"], "RELIANCE")
        self.assertEqual(r["opening_range"]["high"], 2825.0)
        self.assertEqual(r["opening_range"]["low"], 2764.85)
        self.assertEqual(r["opening_range"]["bars"], 3)
        self.assertEqual((r["opens"], r["closes"]), ("09:15", "15:30"))
        t = r["trigger"]
        self.assertEqual(t["time"], "10:00 AM")
        self.assertEqual(t["bar_start"], "09:55")
        self.assertEqual(t["bar_index"], 8)
        self.assertEqual(t["price"], 2840.55)
        self.assertEqual(t["close"], 2840.55)
        self.assertEqual(t["pct_over"], 0.55)
        self.assertEqual(r["level"], 2825.0)
        self.assertEqual(r["prev_close"], 2766.8)

    def test_at_0957_the_0955_bar_is_still_forming(self):
        r = at("2026-10-30 09:57")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:50")
        self.assertEqual(r["last"]["close"], 2821.1)
        self.assertEqual(r["forming"]["bar_start"], "09:55")
        self.assertIn("₹3.90 under the range high ₹2,825.00", r["reason"])
        self.assertEqual(at("2026-10-30 09:59:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-30 09:05")                                # the pre-open call auction: no bars
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("the NSE opens at 9:15 IST", r["reason"])
        self.assertIn("9:30", r["reason"])
        r = at("2026-10-30 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-30 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        c = chart()
        self.assertEqual(c["bars"][-1]["t"], OPEN_TS + 79 * 60 + 45)       # 10:34:45 is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))               # read as of the file's own time
        self.assertEqual(r["now"], "2026-10-30 10:34:45")
        self.assertEqual(r["_bars"][-1]["slot"], 14)                      # the 10:25 bar
        self.assertEqual(r["forming"]["bar_start"], "10:30")
        self.assertEqual(r["bars_closed"], 15)
        # 10:35: closed by the clock, but Yahoo's last trade (10:34:45) is before its close
        r = at("2026-10-30 10:35")
        self.assertEqual(r["_bars"][-1]["slot"], 14)
        self.assertIn("Yahoo's last trade is 10:34:45", r["lag_note"])
        # a minute later with no newer trade it counts; the 10:34:45 tick never becomes a bar
        r = at("2026-10-30 10:36")
        self.assertEqual(r["_bars"][-1]["slot"], 15)
        self.assertEqual(r["bars_closed"], 16)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_the_ist_grid_matches_yahoos_five_minute_stamps(self):
        c = chart()
        first = entry.to_ist(entry.utc_of(c["bars"][0]["t"]))
        self.assertEqual(first, DT(2026, 10, 30, 9, 15))
        self.assertEqual(c["bars"][0]["t"] % 300, 0)                       # 09:15 IST is on the 5-minute grid

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-30 15:35")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "10:00 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)

    def test_the_session_is_09_15_to_15_30(self):
        self.assertEqual((entry.OPEN, entry.CLOSE), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertEqual(entry.DEFAULTS, {"no_entries_before": "09:30", "no_entries_after": "14:30"})
        r = at("2026-10-30 15:31")
        self.assertEqual(r["phase"], "after-close")
        # a bar stamped 15:30 or later is outside the session
        d = copy.deepcopy(doc())
        res = d["chart"]["result"][0]
        res["timestamp"].append(OPEN_TS + 75 * 300)
        for k, v in res["indicators"]["quote"][0].items():
            v.append(9999.0 if k != "volume" else 1)
        self.assertEqual(at("2026-10-30 15:45", d)["bars_closed"], 16)


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-10-30 10:34", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 1658945)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("10:20 AM", 12, 2852.85))
        self.assertEqual(t["volume_x"], 7.98)
        r = at("2026-10-30 10:01", min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.60x the range's volume, under your 1.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-30 10:15", setup="level", level=2850.9)
        self.assertEqual(r["trigger"]["time"], "10:10 AM")
        self.assertEqual(r["trigger"]["bar_index"], 10)
        self.assertEqual(r["trigger"]["price"], 2852.85)
        # a level under the open: the first eligible close is the 9:25 bar's, at 9:30 (no_entries_before)
        r = at("2026-10-30 10:15", setup="level", level=2730)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-30 10:15", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        r = at("2026-10-30 10:34", d)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(at("2026-10-30 14:29", d)["status"], "waiting")
        r = at("2026-10-30 14:30", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 14:30", r["reason"])
        self.assertEqual(r["no_entries_after"], "14:30")
        self.assertEqual(at("2026-10-30 15:45", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = no_breakout()
        r0 = d["chart"]["result"][0]
        keep = [i for i, t in enumerate(r0["timestamp"]) if t <= OPEN_TS + 14 * 300]   # live: up to the 10:25 bar
        r0["timestamp"] = [r0["timestamp"][i] for i in keep]
        q = r0["indicators"]["quote"][0]
        for k in q:
            q[k] = [q[k][i] for i in keep]
        r0["meta"]["regularMarketTime"] = OPEN_TS + 74 * 60 + 40                       # last trade 10:29:40
        cfg = {"no_entries_before": "09:30", "no_entries_after": "10:30"}
        r = at("2026-10-30 10:30:10", d, cfg=cfg)                           # the 10:25 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:29:40", r["lag_note"])
        self.assertEqual(at("2026-10-30 10:31", d, cfg=cfg)["status"], "expired")

    def test_config_cutoff(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:55"}
        r = at("2026-10-30 10:00", cfg=cfg)
        self.assertEqual(r["status"], "expired")
        cfg = {"no_entries_before": "10:05", "no_entries_after": "14:30"}
        r = at("2026-10-30 10:15", cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "10:05 AM")       # the 10:00 bar, first to close at or after 10:05

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-30 10:00", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-30 10:00", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-10-30 10:00"), DT(2026, 10, 30, 4, 30))
        self.assertEqual(entry.parse_now("2026-10-30T04:30:00Z"), DT(2026, 10, 30, 4, 30))
        self.assertEqual(entry.parse_now("2026-10-30 10:00 IST"), DT(2026, 10, 30, 4, 30))
        self.assertEqual(entry.parse_now("2026-10-30T10:00+05:30"), DT(2026, 10, 30, 4, 30))

    def test_or_minutes_30(self):
        r = at("2026-10-30 10:34", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 2830.8)
        self.assertEqual(r["trigger"]["time"], "10:00 AM")


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(DEMO)), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-30 09:51:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:44 AM", "09:43", 28, 100.6))
        r = entry.evaluate(c, ist("2026-10-30 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")
        self.assertEqual(r["last"]["close"], 100.5)             # the 9:42 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:47 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 9:34 came as nulls
        self.assertEqual(slots[-1], 35)                           # 9:51 still forming, 9:51:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:51")

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_no_daylight_saving_any_day_of_the_year(self):
        for day in (datetime.date(2026, 12, 7), datetime.date(2026, 11, 2),
                    datetime.date(2026, 10, 30), datetime.date(2026, 3, 9)):
            d = shift_to(doc(), day)
            r = entry.evaluate(chart(d), ist("%s 10:00" % day))
            self.assertEqual(r["status"], "triggered", day)
            self.assertEqual(r["trigger"]["time"], "10:00 AM", day)
            self.assertEqual(r["trigger"]["bar_index"], 8, day)
            self.assertEqual(entry.evaluate(chart(d), ist("%s 09:57" % day))["status"], "waiting", day)
        # Dec 7 at 10:00 IST is 04:30 UTC
        self.assertEqual(ist("2026-12-07 10:00"), DT(2026, 12, 7, 4, 30))

    def test_a_shortened_session_moves_the_cutoff_up_if_the_nse_ever_announces_one(self):
        saved = (entry.NSE_EARLY_CLOSE, entry.EARLY)
        entry.NSE_EARLY_CLOSE = {"2026-11-27": "test session"}
        entry.EARLY = datetime.time(12, 30)
        try:
            d = shift_to(no_breakout(), datetime.date(2026, 11, 27))
            r = entry.evaluate(chart(d), ist("2026-11-27 11:29"))
            self.assertEqual(r["status"], "waiting")
            self.assertEqual((r["closes"], r["no_entries_after"]), ("12:30", "11:30"))   # 14:30 minus the 3-hour gap
            r = entry.evaluate(chart(d), ist("2026-11-27 11:30"))
            self.assertEqual(r["status"], "expired")
            self.assertIn("shortened session", r["reason"])
            r = entry.evaluate(chart(shift_to(doc(), datetime.date(2026, 11, 27))), ist("2026-11-27 10:00"))
            self.assertEqual(r["trigger"]["time"], "10:00 AM")
            self.assertIn("Shortened session today", "\n".join(entry.describe(r)))
            # a cutoff already before the shortened close stays put
            cfg = {"no_entries_before": "09:30", "no_entries_after": "11:00"}
            self.assertEqual(entry.evaluate(chart(d), ist("2026-11-27 10:45"), cfg=cfg)["no_entries_after"], "11:00")
        finally:
            entry.NSE_EARLY_CLOSE, entry.EARLY = saved

    def test_no_half_days_today(self):
        self.assertEqual(entry.NSE_EARLY_CLOSE, {})
        r = at("2026-11-27 10:00", shift_to(doc(), datetime.date(2026, 11, 27)))
        self.assertEqual((r["closes"], r["no_entries_after"]), ("15:30", "14:30"))
        self.assertIsNone(r["early_close"])

    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-10-31 10:30")                                 # a Saturday
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-11-02 09:15")
        r = at("2026-10-20 10:30")                                 # Dussehra
        self.assertIn("Dussehra", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-21 09:15")
        self.assertIn("NSE holiday", r["reason"] + " NSE holiday")
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-30 10:00", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-11-02 10:00")                                # Yahoo sent Friday's bars only
        self.assertEqual(r["status"], "no-data")

    def test_the_2027_calendar_is_unknown_until_the_nse_publishes_it(self):
        self.assertTrue(at("2026-10-30 10:30")["calendar_known"])
        self.assertFalse(at("2027-03-01 10:30")["calendar_known"])
        self.assertFalse(at("2028-03-01 10:30")["calendar_known"])
        self.assertIn("Holiday list covers 2026 only: check nseindia.com", "\n".join(entry.describe(at("2027-03-01 10:30"))))

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Symbols(unittest.TestCase):
    def test_bare_nse_symbols_and_yahoo_names(self):
        self.assertEqual(entry.norm(" reliance.ns "), "RELIANCE")
        self.assertEqual(entry.norm("m&m"), "M&M")
        self.assertEqual(entry.yahoo_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(entry.yahoo_symbol("RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(entry.yahoo_symbol("bajaj-auto"), "BAJAJ-AUTO.NS")
        self.assertEqual(entry.yahoo_symbol("M&M"), "M&M.NS")
        self.assertEqual(entry.yahoo_symbol("RELIANCE.BO"), "RELIANCE.BO")
        self.assertIn("/chart/M%26M.NS?", entry.chart_path("m&m", "1d", "5m"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", entry.chart_path("bajaj-auto", "1d", "5m"))
        self.assertEqual(entry.chart_path("reliance", "1d", "5m"), "/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")

    def test_the_chart_symbol_comes_back_bare(self):
        self.assertEqual(chart()["symbol"], "RELIANCE")                    # meta.symbol is RELIANCE.NS

    def test_rupee_grouping(self):
        self.assertEqual(entry.inr(2840.55), "₹2,840.55")
        self.assertEqual(entry.inr(100000), "₹1,00,000.00")
        self.assertEqual(entry.inr(1234567.5), "₹12,34,567.50")
        self.assertEqual(entry.inr(-3.9), "-₹3.90")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(RELI) as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 30), today=datetime.date(2026, 10, 30))
        self.assertEqual(len(c["bars"]), 17)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_m_and_m_goes_out_encoded(self):
        d = doc()
        d["chart"]["result"][0]["meta"]["symbol"] = "M&M.NS"
        self.body = json.dumps(d)
        self.answers(200)
        c = entry.get_chart("m&m", "5m", datetime.date(2026, 10, 30), today=datetime.date(2026, 10, 30))
        self.assertEqual(c["symbol"], "M&M")
        self.assertIn("/chart/M%26M.NS?range=1d&interval=5m", self.calls[0])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 30), today=datetime.date(2026, 10, 30))
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 30), today=datetime.date(2026, 10, 30))
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "RELIANCE", "--now", "2026-10-30 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", datetime.date(2026, 10, 30), today=datetime.date(2026, 10, 30))
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 10, 30)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 29), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 12), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 10), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 12), today, "1m")


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 2,830.00 (above the 2,825.00 high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = SPIKE, max(b["h"], SPIKE)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": SPIKE, "h": SPIKE, "l": SPIKE, "c": SPIKE, "v": 0})
        return {"symbol": "RELIANCE", "meta": {}, "bars": bars, "prev_close": 2766.8, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-10-30 09:52", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:00 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 2840.55)
        # it looked at 09:52 (09:50 bar spiking to 2,830 in progress) and at 09:55:02 (that bar closed 2,821.10)
        self.assertEqual([entry.to_ist(c).strftime("%H:%M:%S") for c in calls], ["09:52:00", "09:55:02", "10:00:02"])
        text = "\n".join(lines)
        self.assertIn(" 9:50 bar  closed ₹2,821.10  ₹3.90 under ₹2,825.00", text)
        self.assertIn(" 9:55 bar  closed ₹2,840.55  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-10-30 10:00:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=lag, calls=calls),
                                                   "2026-10-30 09:57", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["09:57:00", "10:00:02", "10:00:32"])   # 10:00:02: Yahoo still at 09:59:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:00 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-10-30 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn("watching from the first bar's close at 9:20", lines[0])
        self.assertIn(" 9:25 bar  closed ₹2,811.00  opening range set: high ₹2,825.00, low ₹2,764.85", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 9)    # 9:15 ... 9:55, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-30 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:31:00", "09:49:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:30", " 9:35", " 9:40", " 9:45"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:00 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-30 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:30")
        until = DT(2026, 10, 30, 10, 45)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-30 10:15", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 10:45", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-31 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-20 10:00")
        self.assertEqual(code, 1)
        self.assertIn("NSE holiday: Dussehra", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-30 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", RELI, "--now", "2026-10-30 10:00", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 10:00 AM at ₹2,840.55", text)
        self.assertIn("IST", text)
        self.assertIn("/nse-exit", text)
        with open(os.path.join("nse", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "nse-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy", "date": "2026-10-30",
                "status": "triggered", "time": "10:00 AM", "bar_start": "09:55", "bar_index": 8, "price": 2840.55,
                "level": 2825.0, "interval": "5m", "opening_range": {"high": 2825.0, "low": 2764.85, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-10-30T04:30:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", RELI, "--now", "2026-10-30 09:57", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "RELIANCE", "--offline", RELI, "--now", "2026-10-30 09:57", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open(RELI, "rb") as src, gzip.open(os.path.join("charts", "RELIANCE.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "reliance.ns", "--offline", "charts", "--now", "2026-10-30 10:00"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "TCS", "--offline", RELI])
        self.assertEqual(code, 1)
        self.assertIn("holds RELIANCE, not TCS", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "RELIANCE", "--offline", RELI, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("this pack plans longs only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", RELI, "--level", "2800"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", RELI, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("nse")
        with open(os.path.join("nse", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:55"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", RELI, "--now", "2026-10-30 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("nse", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", RELI])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", RELI, "--now", "2026-10-30 09:35", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn(" 9:55 bar  closed ₹2,840.55  TRIGGERED", text)
        with open(os.path.join("nse", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 8)
        code, text, _ = run(["watch", "RELIANCE", "--offline", RELI, "--now", "2026-10-30 09:35", "--until", "09:45",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(RELI, os.path.join("charts", "RELIANCE.json"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        os.mkdir("nse")
        with open(os.path.join("nse", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": [
                {"symbol": "RELIANCE", "setups": ["orb"], "or_high": 2825.0, "or_low": 2764.85},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-30 10:00"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertRegex(rows[1], r"^RELIANCE\s+triggered\s+10:00 AM\s+₹2,840\.55\s+₹2,825\.00\s")
        self.assertRegex(rows[2], r"^ZZZZ\s+error: no offline file")
        self.assertRegex(rows[3], r"^DEMO\s+error: DEMO has 1m bars")
        self.assertIn("1 of 3 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-30 09:57", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /nse-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "nse-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/nse-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "10:00 am", "₹2,840.55", "bar 8",
                     "09:15", "15:30", "ist", "nse", "m&m", "pre-open", "nseindia.com"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "nyse", "new york", "nasdaq"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
