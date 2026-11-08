import datetime
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from _load import load, FIX

tm = load("nse-timemachine", "timemachine")

DIR = os.path.join(FIX, "nse-timemachine")
REL = os.path.join(DIR, "RELIANCE-60d5m.json.gz")   # Yahoo v8 chart shape, range=60d interval=5m (synthetic), "fetched" 10:58 IST Fri 2026-10-30
with gzip.open(REL, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-10-30 11:00"
NOW_UTC = tm.parse_now(NOW)

# a plan as nse-exit writes it
PLAN = {"created_by": "nse-exit", "symbol": "RELIANCE", "side": "buy", "date": "2026-10-30", "setup": "orb",
        "entry": 1455.5, "entry_time": "10:15 AM", "entry_bar": 11, "interval": "5m",
        "opening_range": {"high": 1454.0, "low": 1448.0, "minutes": 15},
        "stop": 1451.0, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1464.5, "target_r": 2.0, "target_label": "2R (two times the risk)",
        "risk_per_share": 4.5, "reward_risk": 2.0, "flat_by": "15:15", "tick": 0.05}

# regression fingerprints of this algorithm on this synthetic fixture (seed 19)
BANDS_SHA = "ad05c3b946ae256d"
PATHS_SHA = "c3381f5fc2b5e4a2"


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


def utc_stamp(y, mo, d, h, mi):
    return int((datetime.datetime(y, mo, d, h, mi) - datetime.datetime(1970, 1, 1)).total_seconds())


class Baseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:30"))
        self.assertEqual(m["pool_sessions"], 39)
        self.assertEqual(m["bars_left"], 63)                      # bars 12..74: 10:15 to 15:30
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-09-01", "2026-10-29"))
        self.assertEqual(m["p_target"], 0.3234)
        self.assertEqual(m["p_stop"], 0.6454)
        self.assertEqual(m["p_neither"], 0.0312)
        self.assertEqual(m["p_green"], 0.3483)
        self.assertEqual(m["median_close"], 1457.51)
        self.assertEqual(m["p5_close"], 1439.22)
        self.assertEqual(m["p95_close"], 1476.0)
        self.assertEqual(m["avg_R"], 0.019)
        self.assertEqual(m["r_at_target"], 2.0)
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)

    def test_regression_fingerprints(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), BANDS_SHA)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), PATHS_SHA)
        self.assertEqual(sum(m["hist"]["bins"]), 9834)            # 1st to 99th percentile of 10,000 closes
        self.assertEqual((m["hist"]["lo"], m["hist"]["hi"]), (1431.95, 1483.83))

    def test_bands_start_at_the_entry_and_run_to_the_close(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1455.5] * 5)
        self.assertEqual(len(m["bands"]), 64)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("10:15", "15:30", 64))
        self.assertTrue(all(p[0] == 1455.5 and len(p) == 64 for p in m["sample_paths"]))

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (39, 26))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (6, 13, 7, 12))
        self.assertEqual(h["avg_R"], 0.082)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0,
                                     "tick": 0.05})
        first = h["trades"][0]
        self.assertEqual((first["date"], first["time"], first["entry"], first["stop"], first["target"],
                          first["outcome"]), ("2026-09-01", "09:40", 1427.15, 1421.7, 1438.05, "stop"))

    def test_the_history_matches_an_independent_decimal_recomputation(self):
        """Every past full session, the long way, with Decimal rounding instead of the tool's float code."""
        tick = Decimal("0.05")
        res = tm.chart_result(TEXT, "RELIANCE")
        bars = tm.parse_bars(res, NOW_UTC)[1]
        sessions, _ = tm.full_sessions(tm.split_sessions(bars), "2026-10-30")
        want = []
        for day, D in sessions:
            oh = max(Decimal(repr(b["h"])) for b in D[:3])
            ol = min(Decimal(repr(b["l"])) for b in D[:3])
            kk = next((i for i in range(3, 75) if Decimal(repr(D[i]["c"])) > oh), None)
            if kk is None:
                continue
            e = (Decimal(repr(D[kk]["c"])) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
            s = ((oh + ol) / 2 / tick).to_integral_value(rounding=ROUND_FLOOR) * tick      # stop rounds down
            risk = e - s
            if risk <= 0:
                continue
            t = ((e + 2 * risk) / tick).to_integral_value(rounding=ROUND_CEILING) * tick   # target rounds up
            outcome = "close"
            for b in D[kk + 1:75]:
                if Decimal(repr(b["l"])) <= s:
                    outcome = "stop"
                    break
                if Decimal(repr(b["h"])) >= t:
                    outcome = "target"
                    break
            want.append((day, float(e), float(s), float(t), outcome))
        got = [(x["date"], x["entry"], x["stop"], x["target"], x["outcome"]) for x in self.doc["history"]["trades"]]
        self.assertEqual(got, want)
        for _, e, s, t, _ in got:                                  # every price is on the NSE tick
            for x in (e, s, t):
                self.assertEqual(Decimal(repr(x)) % tick, 0, x)
            self.assertTrue(s < e < t)

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], "Close to break-even: at 2R the target has to come first in 33 % of the "
                                              "replays that hit one side; it did in 33 % (target first 32.3 % vs "
                                              "stop first 64.5 %).")
        self.assertEqual(self.doc["verdict_notes"], [])
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), round(even, 3)), (0.334, 0.333))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("RELIANCE", 1455.5, 1451.0, 1464.5, 11, "10:15"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"], p["tick"]), (4.5, 2.0, "15:15", 0.05))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["data"]["last_bar"], "2026-10-30 10:50 IST")
        self.assertEqual(self.doc["data"]["short_sessions_left_out"], [{"date": "2026-09-25", "bars": 50}])

    def test_the_text_report_is_in_rupees_and_india_time(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy, Friday 2026-10-30: entry ₹1,455.50 at 10:15 IST (bar 11), stop ₹1,451.00, "
                     "target ₹1,464.50 (2R, risk ₹4.50 a share)",
                     "10,000 replays", "63 five-minute bars", "10:15 to 15:30 IST", "seed 19", "39 full sessions",
                     "target first   32.3 %", "stop first     64.5 %", "neither        3.1 %    still open at 15:30",
                     "green          34.8 %", "median ₹1,457.51, 90 % of replays between ₹1,439.22 and "
                     "₹1,476.00", "Average R: +0.019", "fired 26 times: 6 target, 13 stop, 7 neither. Average R +0.082",
                     "Close to break-even", "Left out 1 short session(s) (special sessions or gaps): 2026-09-25",
                     "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$", " ET", "New York", "NYSE"):
            self.assertNotIn(word, text)


class HandChecked(unittest.TestCase):
    """Pools where every bar is the same, so each replay is the same and the answer is plain arithmetic."""

    @staticmethod
    def pool(h, l, c):
        return [[(1.0, h, l, c)] for _ in range(tm.SESSION_BARS)]

    def sim(self, pool, entry, stop, target, side="buy", **kw):
        return tm.simulate(pool, entry, stop, target, 11, 74, replays=200, window=0, side=side, **kw)

    def test_a_bar_that_touches_both_counts_as_the_stop(self):
        m = self.sim(self.pool(1.03, 0.97, 1.0), 100.0, 99.0, 102.0)     # first bar spans 97 to 103
        self.assertEqual((m["p_stop"], m["p_target"], m["p_neither"], m["p_green"]), (1.0, 0.0, 0.0, 0.0))
        self.assertEqual(m["avg_R"], -1.0)

    def test_a_clean_run_to_the_target(self):
        m = self.sim(self.pool(1.03, 0.995, 1.0), 100.0, 99.0, 102.0)    # 103 high, 99.5 low: target, no stop
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (1.0, 0.0, 0.0, 1.0))
        self.assertEqual(m["r_at_target"], 2.0)
        self.assertEqual(m["avg_R"], 2.0)

    def test_neither_by_the_close_is_judged_on_the_close(self):
        m = self.sim(self.pool(1.0003, 0.9999, 1.0002), 100.0, 99.0, 102.0)
        price = 100.0
        for _ in range(63):                                              # bars 12..74
            price *= 1.0002
        self.assertEqual((m["p_neither"], m["p_target"], m["p_stop"], m["p_green"]), (1.0, 0.0, 0.0, 1.0))
        self.assertAlmostEqual(m["median_close"], round(price, 2), places=2)
        self.assertAlmostEqual(m["avg_R"], price - 100.0, places=2)      # risk is 1.00, so R = rupees gained

    def test_a_sell_mirrors_the_rules(self):
        m = self.sim(self.pool(1.005, 0.975, 1.0), 100.0, 101.0, 98.0, side="sell")   # low 97.5 <= 98, high 100.5 < 101
        self.assertEqual((m["p_target"], m["p_stop"]), (1.0, 0.0))
        self.assertEqual(m["avg_R"], 2.0)
        m = self.sim(self.pool(1.02, 0.97, 1.0), 100.0, 101.0, 98.0, side="sell")     # both touched: the stop
        self.assertEqual((m["p_target"], m["p_stop"], m["avg_R"]), (0.0, 1.0, -1.0))
        m = self.sim(self.pool(1.0003, 0.9999, 0.9998), 100.0, 101.0, 98.0, side="sell")
        self.assertEqual((m["p_neither"], m["p_green"]), (1.0, 1.0))                  # drifted down: a sell is green

    def test_the_bar_count_comes_from_the_75_bar_session(self):
        self.assertEqual(tm.SESSION_BARS, 75)
        self.assertEqual((tm.OPEN_MIN, tm.CLOSE_MIN), (9 * 60 + 15, 15 * 60 + 30))
        self.assertEqual((tm.CLOSE_MIN - tm.OPEN_MIN) // 5, tm.SESSION_BARS)
        self.assertEqual(len(tm.build_pool([("d", [{"o": 1, "h": 1, "l": 1, "c": 1}] * 75)])), 75)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.3234, 0.6454, 1457.51))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), BANDS_SHA)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; this pins the first draws so a
        # failure points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(39)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE")

    def test_the_in_progress_tick_is_dropped(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        self.assertEqual(dropped, {"no_price": 0, "off_grid": 1, "not_complete": 1, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-30", "10:50", 19))
        self.assertEqual(ist(self.result["timestamp"][-1]).strftime("%H:%M:%S"), "10:58:07")   # the live tick
        # with the real clock (any time later) the data's own time still marks the 10:55 bar as in progress
        self.assertEqual(tm.parse_bars(self.result, tm.parse_now(None))[1][-1]["start"], "10:50")
        entry = [b for b in bars if b["date"] == "2026-10-30" and b["slot"] == 11][0]
        self.assertEqual(entry["start"], "10:10")

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-10-30 10:12"))[1]
        today = [b for b in bars if b["date"] == "2026-10-30"]
        self.assertEqual(len(today), 11)                           # 09:15 to 10:05 starts; 10:10 is still forming
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-30 10:12"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("10:05 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-10-30 10:15"), replays=200)["mc"]["pool_sessions"], 39)

    def test_india_time_has_no_daylight_saving(self):
        stamps = [utc_stamp(2026, 7, 1, 3, 45),      # 09:15 IST in July
                  utc_stamp(2026, 12, 1, 3, 45),     # 09:15 IST in December: the same offset
                  utc_stamp(2026, 12, 1, 3, 30),     # 09:00 IST, the pre-open auction: not a bar
                  utc_stamp(2026, 12, 1, 9, 55),     # 15:25 IST, the last bar
                  utc_stamp(2026, 12, 1, 10, 0)]     # 15:30 IST, after the close
        res = {"meta": {"symbol": "X.NS"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * 5, "high": [1] * 5, "low": [1] * 5, "close": [1] * 5,
                                         "volume": [0] * 5}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-12-02T00:00Z"))
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-07-01", "09:15", 0), ("2026-12-01", "09:15", 0), ("2026-12-01", "15:25", 74)])
        self.assertEqual(dropped["outside_hours"], 2)
        self.assertEqual(tm.to_ist(tm.utc_of(stamps[0])), datetime.datetime(2026, 7, 1, 9, 15))
        self.assertEqual(tm.ist_to_utc(datetime.datetime(2026, 12, 1, 9, 15)), datetime.datetime(2026, 12, 1, 3, 45))

    def test_a_short_session_is_left_out(self):
        def cut(res):   # 2026-09-10 ends at 13:00, like a special session: 45 bars
            keep_bars(res, lambda ts: not (ist(ts).date().isoformat() == "2026-09-10" and ist(ts).hour >= 13))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 38)
        self.assertIn({"date": "2026-09-10", "bars": 45}, doc["data"]["short_sessions_left_out"])
        self.assertEqual(doc["history"]["sessions"], 38)
        self.assertNotIn("2026-09-10", [t["date"] for t in doc["history"]["trades"]])
        self.assertIn("Left out 2 short session", "\n".join(tm.report(doc)))

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-29")
        self.assertNotIn("2026-10-30", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 1)
        self.assertEqual(doc["mc"]["pool_sessions"], 38)           # 2026-09-01 now has a gap

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        bars = tm.parse_bars(self.result, NOW_UTC)[1]
        full = [d for d, _ in tm.full_sessions(tm.split_sessions(bars), "2026-10-30")[0]]
        self.assertEqual(len(full), 39)

        def keep_last(n):
            first = full[-n]
            return lambda res: keep_bars(res, lambda ts: ist(ts).date().isoformat() >= first)
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(keep_last(19)))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))
        self.assertEqual(machine(text=edited(keep_last(20)), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_ticker(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS"))
        self.assertIn("data is for RELIANCE", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-31"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 11:00 has not happened in this data yet
            machine(plan(entry_time="11:00", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("stops at the 10:50 bar", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="10:17", entry_bar=None), "5-minute grid"),
            (dict(entry_time="15:35", entry_bar=None), "between 09:20 and 15:25 IST"),
            (dict(entry_time="16:00", entry_bar=None), "between 09:20 and 15:25 IST"),
            (dict(entry_bar=12), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1460.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(symbol="A.B"), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="30 Oct"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(opening_range={"minutes": 375}), "multiple of 5"),
            (dict(tick=0), "tick"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_symbols(self):
        self.assertEqual(plan(symbol="reliance.ns")["symbol"], "RELIANCE")
        self.assertEqual(plan(symbol="M&M")["symbol"], "M&M")
        self.assertEqual(plan(symbol="BAJAJ-AUTO")["symbol"], "BAJAJ-AUTO")
        self.assertEqual(plan(symbol="3MINDIA")["symbol"], "3MINDIA")

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "10:15")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 11)
        self.assertEqual(plan(entry_time="10:15", entry_bar=None)["entry_bar"], 11)
        self.assertEqual(plan(entry_time="10:15am", entry_bar=None)["entry_bar"], 11)
        self.assertEqual(plan(entry_time="3:15 PM", entry_bar=None)["entry_bar"], 71)   # 15:15
        self.assertEqual(plan(entry_time="3:15", entry_bar=None)["entry_bar"], 71)      # a bare 3 means 15:
        self.assertEqual(plan(entry_time="09:20", entry_bar=None)["entry_bar"], 0)

    def test_until_cuts_the_replay(self):
        doc = machine(until="15:15", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:15", 60, "15:15"))
        self.assertIn("still open at 15:15", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="3:15 PM", replays=500)["mc"], m)
        for bad, words in (("10:15", "nothing to replay"), ("15:35", "no later than 15:30"),
                           ("15:17", "5-minute grid"), ("noon", "India time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1470.0, stop=1465.0, target=1480.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_stops_and_targets_sit_on_the_tick(self):
        self.assertEqual(tm.stop_tick(1451.03, True), 1451.0)            # a buy's stop rounds down
        self.assertEqual(tm.stop_tick(1451.03, False), 1451.05)          # a sell's stop rounds up
        self.assertEqual(tm.stop_tick(1451.0, True), 1451.0)             # already on the tick
        self.assertEqual(tm.stop_tick(1450.9999999999998, True), 1451.0)   # float noise does not drop a tick
        self.assertEqual(tm.target_tick(1464.51, True), 1464.55)         # a buy's target rounds up
        self.assertEqual(tm.target_tick(1464.51, False), 1464.5)         # a sell's target rounds down
        self.assertEqual(tm.snap_tick(1455.52), 1455.5)
        self.assertEqual(tm.snap_tick(1455.53), 1455.55)
        self.assertEqual(tm.stop_tick(1451.037, True, 0.01), 1451.03)    # a stock on a one-paisa tick
        self.assertEqual(tm.stop_tick(1451.037, True, 0.10), 1451.0)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")                    # (1454.00 + 1448.00) / 2 = 1451.00
        rule = machine(plan(stop_rule=None, stop=1448.0, target=1470.5), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 2.0))
        rule = machine(plan(stop_rule="atr", stop=1450.0, target=1470.0), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "pct")
        self.assertEqual(rule["stop_pct"], round(100 * 5.5 / 1455.5, 4))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 39)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1460.0, target=1446.5, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 2.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        self.assertTrue(doc["history"]["trades"])
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
            for x in (t["entry"], t["stop"], t["target"]):
                self.assertEqual(Decimal(repr(x)) % Decimal("0.05"), 0, x)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        # 1R: the target's share of the replays that hit one side against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:15"}),
                         "No replay reached the target or the stop by 15:15: the price at 15:15 decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_many_replays_hitting_neither_is_said(self):
        notes = tm.verdict_notes({"p_target": 0.30, "p_stop": 0.25, "p_neither": 0.45, "r_at_target": 1.0,
                                  "avg_R": 0.03})
        self.assertEqual(notes, ["45.0 % of the replays hit neither by 15:30; the price at 15:30 decides those."])
        notes = tm.verdict_notes({"p_target": 0.30, "p_stop": 0.25, "p_neither": 0.45, "r_at_target": 1.0,
                                  "avg_R": 0.03, "until": "15:15"})
        self.assertIn("by 15:15", notes[0])

    def test_1r_on_the_fixture_is_a_coin_flip(self):
        doc = machine(plan(target=1460.0, target_r=1.0))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (1.0, 0.4799, 0.5197, 0.0004, -0.04))
        self.assertEqual(doc["verdict"], "Close to a coin flip: target first 48.0 % vs stop first 52.0 %.")

    def test_a_far_target_on_the_fixture_leans_to_the_stop(self):
        doc = machine(plan(target=1475.0, target_r=None))                  # R worked out from the prices: 4.33R
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (4.3333, 0.0928, 0.6705, 0.2367, 0.131))
        self.assertEqual(doc["verdict"], "Leans toward the stop: at 4.33R the target has to come first in 19 % of "
                                         "the replays that hit one side; it did in 12 % (target first 9.3 % vs "
                                         "stop first 67.0 %).")
        self.assertEqual(doc["verdict_notes"], ["Average R is +0.131 all the same: the replays that hit neither "
                                                "pull it the other way."])
        text = "\n".join(tm.report(doc))
        self.assertIn(doc["verdict"] + "\n" + doc["verdict_notes"][0], text)


class Muhurat(unittest.TestCase):
    def test_muhurat_day_replays_the_short_session_clock(self):
        raw = json.loads(TEXT)
        res = raw["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        source = [i for i, ts in enumerate(res["timestamp"])
                  if ist(ts).date().isoformat() == "2026-10-30"]
        source = source[-75:]
        for i in range(11):
            src = source[61 + i]
            res["timestamp"].append(utc_stamp(2026, 11, 8, 8, 50 + 5 * i))
            for key in ("open", "high", "low", "close", "volume"):
                q[key].append(q[key][src])
        res["meta"]["regularMarketTime"] = utc_stamp(2026, 11, 8, 9, 45)
        text = json.dumps(raw)
        now = tm.parse_now("2026-11-08 15:16")
        bars = tm.parse_bars(res, now)[1]
        special = [b for b in bars if b["date"] == tm.MUHURAT_DATE]
        self.assertEqual([b["slot"] for b in special], list(range(61, 72)))
        entry = q["close"][source[64]]
        p = plan(date=tm.MUHURAT_DATE, entry=entry, stop=entry - 1.0, target=entry + 2.0,
                 entry_time="14:40", entry_bar=3, opening_range={"minutes": 15},
                 stop_rule="or_mid", flat_by="15:10")
        self.assertEqual((p["entry_bar"], p["bar_slot"], p["entry_time"]), (3, 64, "14:40"))
        doc = machine(p=p, text=text, now=now, replays=200)
        self.assertEqual(doc["mc"]["until"], "15:10")
        self.assertEqual(doc["mc"]["bars_left"], 6)
        self.assertEqual((doc["mc"]["band_times"][0], doc["mc"]["band_times"][-1]),
                         ("14:40", "15:10"))
        self.assertEqual(doc["history"]["sessions"], 39)
        self.assertEqual(doc["history"]["rule"]["session_open_slot"], 61)


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_urls_and_user_agent(self):
        self.assertEqual(tm.chart_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("m&m"))               # & is URL-encoded
        self.assertIn("/chart/BAJAJ-AUTO.NS?", tm.chart_url("bajaj-auto", "query1"))
        self.assertIn("/chart/TCS.NS?", tm.chart_url("TCS.NS"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; nse-day-trader-skill/1.0)")
        self.assertNotIn("@", tm.UA)

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZNOSUCH")
        self.assertIn("check the ticker", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "nse", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "nse", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   32.3 %", text)
        self.assertIn("Close to break-even", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/nse-budget", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "nse-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual(doc["created_at"], "2026-10-30T05:30:00Z")           # 11:00 IST
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.3234, 0.019))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), PATHS_SHA)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (26, 0.082))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.6454)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:20], "Close to break-even:")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance", "--date", "2026-10-30", "--entry", "1455.5",
                               "--stop", "1451.0", "--target", "1464.5", "--entry-time", "10:15",
                               "--offline", REL, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"]), (0.3234, 0.3483))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (6, 13, 0.082))
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1455.5", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(REL, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("nse", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("nse", "timemachine.json")) as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--until", "9:00"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 10:15 entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("   1,464.50 |") and l.endswith("  target") for l in lines), text)
        self.assertTrue(any(l.startswith("   1,451.00 |") and l.endswith("  stop") for l in lines), text)
        self.assertTrue(any(l.startswith("   1,455.50 |") and l.endswith("  entry") for l in lines), text)
        self.assertIn("10:15", text)
        self.assertIn("15:30", text)
        self.assertNotIn("PM", text)
        self.assertEqual(lines[-1], self.expected_verdict())
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", REL, "--now", NOW])
        self.assertEqual(again, text)

    @staticmethod
    def expected_verdict():
        return machine()["verdict"]


if __name__ == "__main__":
    unittest.main()
