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

from _load import load, FIX

scan = load("nse-scanner", "scan")

SDIR = os.path.join(FIX, "nse-scanner")
WALL = os.path.join(SDIR, "wall5m-2026-10-30.json.gz")        # 46 five-minute charts (synthetic, Yahoo v8 shape), Fri 2026-10-30
DAILY = os.path.join(SDIR, "daily-spark-2026-10-30.json")     # 19 daily charts as one spark answer
SYMDIR = os.path.join(SDIR, "symdir")                         # trimmed NSE archive CSVs
SPARK5 = os.path.join(SDIR, "spark-5m-live-2026-10-30-1001.json")  # spark 5m answer "fetched" 10:01:48 IST
REL5 = os.path.join(SDIR, "RELIANCE-5m-live-2026-10-30-1001.json")  # v8 chart "fetched" with it
DT = datetime.datetime
D = datetime.date
T_ORB_END = datetime.time(15, 15)                             # flat_by / no_entries_after style cut-off


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """India wall-clock text -> unix seconds, through the tool's own IST rule."""
    fmt = "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2 else "%Y-%m-%d %H:%M"
    return int((scan.ist_to_utc(DT.strptime(ist_text, fmt)) - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, lows=None, start="09:15"):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    return {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
            "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
            "v": [1000] * len(closes), "meta": {}}


def read_texts(folder):
    out = {}
    for f in os.listdir(folder):
        with open(os.path.join(folder, f), encoding="utf-8") as fh:
            out[f] = fh.read()
    return out


class Universe(unittest.TestCase):
    def setUp(self):
        self.texts = read_texts(SYMDIR)

    def test_nifty500_keeps_only_eq_series_common_stock(self):
        kept, counts = scan.build_universe(self.texts, "nifty500")
        syms = [s for s, _ in kept]
        self.assertEqual(syms, ["3MINDIA", "BAJAJ-AUTO", "HDFCBANK", "INFY", "M&M", "RELIANCE", "SBIN", "TCS",
                                "YESBANK", "ZZBLANK"])
        self.assertEqual(counts["rows"], 12)
        self.assertEqual(counts["trade-to-trade (BE/BZ)"], 1)    # BE series cannot be traded intraday
        self.assertEqual(counts["ETF or fund"], 1)
        self.assertEqual(dict(kept)["M&M"], "Mahindra & Mahindra Ltd.")
        self.assertIn("ZZBLANK", syms)                           # a blank series is read as EQ

    def test_nifty50_and_all_eq(self):
        kept, counts = scan.build_universe(self.texts, "nifty50")
        self.assertEqual(len(kept), 7)
        self.assertEqual(counts, {"rows": 7, "keep": 7})
        kept, counts = scan.build_universe(self.texts, "all-eq")
        self.assertEqual([s for s, _ in kept],
                         ["3MINDIA", "BAJAJ-AUTO", "HDFCBANK", "INFY", "M&M", "RELIANCE", "SBIN", "TCS", "YESBANK"])
        self.assertEqual(counts["trade-to-trade (BE/BZ)"], 2)    # BE and BZ
        self.assertEqual(counts["SME (SM/ST)"], 2)
        self.assertEqual(counts["other series"], 1)              # RE, rights entitlement
        self.assertEqual(counts["not a plain symbol"], 1)        # ZZ.BAD
        self.assertEqual(counts["ETF or fund"], 1)

    def test_one_case_per_rule(self):
        row = lambda sym, series="EQ", name="X Limited": {"SYMBOL": sym, "SERIES": series, "NAME OF COMPANY": name}
        self.assertEqual(scan.classify(row("TCS")), "keep")
        self.assertEqual(scan.classify(row("M&M")), "keep")           # & and - are real NSE symbol characters
        self.assertEqual(scan.classify(row("BAJAJ-AUTO")), "keep")
        self.assertEqual(scan.classify(row("ABC", "BE")), "trade-to-trade (BE/BZ)")
        self.assertEqual(scan.classify(row("ABC", "BZ")), "trade-to-trade (BE/BZ)")
        self.assertEqual(scan.classify(row("ABC", "SM")), "SME (SM/ST)")
        self.assertEqual(scan.classify(row("ABC", "RE")), "other series")
        self.assertEqual(scan.classify(row("A.B")), "not a plain symbol")
        self.assertEqual(scan.classify(row("NIFTYBEES", name="Nippon India ETF Nifty BeES")), "ETF or fund")
        self.assertEqual(scan.classify(row("LIQUIDBEES", name="Nippon India ETF Liquid BeES")), "ETF or fund")

    def test_missing_file_and_unknown_universe(self):
        with self.assertRaises(scan.ScanError):
            scan.build_universe({}, "nifty500")
        with self.assertRaises(scan.ScanError):
            scan.build_universe(self.texts, "nasdaq")


class Time(unittest.TestCase):
    def test_ist_is_fixed_plus_530(self):
        self.assertEqual(scan.ts_to_ist(ts("2026-10-30 09:15")), DT(2026, 10, 30, 9, 15))
        # 09:15 IST is 03:45 UTC, in July and in December alike: India has no daylight saving
        self.assertEqual(scan.ist_to_utc(DT(2026, 7, 1, 9, 15)), DT(2026, 7, 1, 3, 45))
        self.assertEqual(scan.ist_to_utc(DT(2026, 12, 1, 9, 15)), DT(2026, 12, 1, 3, 45))
        self.assertEqual(scan.to_ist(DT(2026, 10, 30, 3, 45)), DT(2026, 10, 30, 9, 15))

    def test_parse_now_and_labels(self):
        self.assertEqual(scan.parse_now("2026-10-30 10:15"), DT(2026, 10, 30, 4, 45))   # stored as naive UTC
        self.assertEqual(scan.parse_now("2026-10-30T04:45:00Z"), DT(2026, 10, 30, 4, 45))
        self.assertEqual(scan.clock_label(DT(2026, 10, 30, 10, 15)), "10:15")
        self.assertEqual(scan.clock_label(datetime.time(15, 5)), "15:05")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_calendar(self):
        self.assertEqual(scan.trading_day(D(2026, 10, 30)), (True, ""))
        self.assertEqual(scan.trading_day(D(2026, 10, 20)), (False, "NSE holiday: Dussehra"))
        self.assertEqual(scan.trading_day(D(2026, 10, 31)), (False, "weekend"))
        self.assertEqual(scan.trading_day(D(2026, 1, 26))[1], "NSE holiday: Republic Day")
        self.assertEqual(len(scan.NSE_HOLIDAYS), 16)
        self.assertTrue(all(D.fromisoformat(d).weekday() < 5 for d in scan.NSE_HOLIDAYS))  # weekday closures only
        self.assertEqual(scan.session(D(2026, 10, 30)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertEqual(scan.CALENDAR_YEARS, (2026,))                  # 2027 is not published yet
        self.assertTrue(scan.trading_day(D(2027, 1, 5))[0])             # unknown year: weekdays treated as open


class Symbols(unittest.TestCase):
    def test_yahoo_symbols(self):
        self.assertEqual(scan.to_yahoo("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(scan.to_yahoo("m&m"), "M&M.NS")
        self.assertEqual(scan.to_yahoo("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")    # the hyphen stays: no US "." -> "-" rule
        self.assertEqual(scan.to_yahoo("TCS.NS"), "TCS.NS")
        self.assertEqual(scan.bare("RELIANCE.NS"), "RELIANCE")
        self.assertEqual(scan.bare("m&m"), "M&M")

    def test_urls(self):
        self.assertEqual(scan.spark_path(["M&M", "tcs"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=M%26M.NS,TCS.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("BAJAJ-AUTO", "1d", "5m"),
                         "/v8/finance/chart/BAJAJ-AUTO.NS?range=1d&interval=5m")
        self.assertEqual(scan.chart_path("M&M", "1d", "5m"), "/v8/finance/chart/M%26M.NS?range=1d&interval=5m")

    def test_inr_grouping(self):
        self.assertEqual(scan.inr(12345678.5), "₹1,23,45,678.50")
        self.assertEqual(scan.inr(100000, 0), "₹1,00,000")
        self.assertEqual(scan.inr(999.5), "₹999.50")
        self.assertEqual(scan.inr(1234.5), "₹1,234.50")


class Spark(unittest.TestCase):
    def test_a_spark_answer(self):
        with open(DAILY, encoding="utf-8") as fh:
            text = fh.read()
        wanted = {scan.to_yahoo(s): s for s in ["RELIANCE", "M&M", "BAJAJ-AUTO", "ZZNOSUCH"]}
        data = scan.parse_spark(text, wanted)
        self.assertEqual(len(data), 19)                           # all 19 charts the answer holds
        self.assertNotIn("ZZNOSUCH", data)                        # a made-up symbol Yahoo silently drops
        t = data["RELIANCE"]
        self.assertIsNone(t["h"])                                 # spark is closes only
        self.assertEqual(t["meta"]["currency"], "INR")
        self.assertIn("M&M", data)                                # ".NS" is stripped: they come back as ours
        self.assertEqual(sorted(scan.parse_spark(text)), sorted(data))

    def test_the_session_bar_is_in_progress_until_the_close(self):
        data, _ = scan.load_offline(DAILY)
        t = data["AXISBANK"]
        during = scan.daily_closes(t, scan.parse_now("2026-10-30 12:00"))
        self.assertEqual(during[-1][0], D(2026, 10, 29))          # Friday's bar is still forming
        after = scan.daily_closes(t, scan.parse_now("2026-10-30 15:30"))
        self.assertEqual(after[-1][0], D(2026, 10, 30))           # complete at the 15:30 close
        before_close = scan.daily_closes(t, scan.parse_now("2026-10-30 15:29"))
        self.assertEqual(before_close[-1][0], D(2026, 10, 29))
        # the same bar the next morning
        morning = scan.daily_closes(t, scan.parse_now("2026-11-02 09:00"))
        self.assertEqual(morning[-1][0], D(2026, 10, 30))

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        with open(DAILY, encoding="utf-8") as fh:
            self.body = fh.read()

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20, ua=None):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get("/v7/finance/spark?symbols=TCS.NS&range=3mo&interval=1d")
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/TCS.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        real = sorted(scan.load_offline(DAILY)[0])
        syms = real + ["ZZNOSUCH"] + ["X%02d" % i for i in range(20)]      # 40 symbols: two batches of 20
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["ZZNOSUCH"], "no data from Yahoo")
        self.assertEqual(len(failed), 21)
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])       # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-10-30 09:00"), 20, 0)
        self.assertEqual(counts["requested"], 40)
        self.assertEqual(counts["fetched"], 19)

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    # The fixture's closes were written so each setup can be checked by hand; the rule is re-derived below.
    BEFORE = {   # as of the 2026-10-29 close (the 10-30 bar is still forming)
        "LT": ["breakout_20d"], "HDFCBANK": ["breakout_20d"], "SBIN": ["breakout_20d"], "ITC": ["breakout_20d"],
        "NTPC": ["breakout_20d"], "M&M": ["bull_flag"], "TATASTEEL": ["bull_flag"],
        "INFY": ["reclaim_ma20"], "BAJAJ-AUTO": ["reclaim_ma20"], "AXISBANK": ["reclaim_ma20"],
        "WIPRO": ["oversold"], "ONGC": ["oversold"], "COALINDIA": ["oversold"], "IDEA": ["oversold"],
        "MARUTI": ["oversold_bounce"],
    }

    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.syms = sorted(self.data)

    def test_setups_before_the_open(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 09:00"), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.BEFORE)
        self.assertEqual(last, D(2026, 10, 29))
        self.assertEqual(counts, {"requested": 19, "fetched": 19, "illiquid": 0, "short": 0, "scanned": 19,
                                  "hits": 15})

    def test_breakout_rule_is_what_the_hits_say(self):
        """Re-derive 'close above the best close of the 20 sessions before' straight from the closes."""
        now = scan.parse_now("2026-10-30 09:00")
        hits, _, _ = scan.scan_daily(self.data, self.syms, now, 0, 0)
        tagged = {h["symbol"] for h in hits if "breakout_20d" in h["setups"]}
        derived = set()
        for sym in self.syms:
            closes = [c for _, c in scan.daily_closes(self.data[sym], now)]
            if len(closes) > 20 and closes[-1] > max(closes[-21:-1]):
                derived.add(sym)
        self.assertEqual(tagged, derived)
        self.assertEqual(tagged, {"LT", "HDFCBANK", "SBIN", "ITC", "NTPC"})

    def test_oversold_is_rsi_under_30(self):
        now = scan.parse_now("2026-10-30 09:00")
        hits, _, _ = scan.scan_daily(self.data, self.syms, now, 0, 0)
        for h in hits:
            if "oversold" in h["setups"]:
                self.assertLess(h["rsi"], 30, h["symbol"])
        names = {h["symbol"] for h in hits if "oversold" in h["setups"]}
        self.assertEqual(names, {"WIPRO", "ONGC", "COALINDIA", "IDEA"})

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 09:00"), 0, 0)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 12:00"), 0, 0)
        self.assertEqual(before, during)
        after = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 15:30"), 0, 0)
        self.assertEqual(after[2], D(2026, 10, 30))
        self.assertEqual(len(after[0]), 14)
        # once Friday's close is in, the tags move: INFY and BAJAJ-AUTO break out, four stocks bounce
        got = {h["symbol"]: h["setups"] for h in after[0]}
        self.assertEqual(got["INFY"], ["breakout_20d"])
        self.assertEqual(got["WIPRO"], ["oversold", "oversold_bounce"])

    def test_liquidity_filter(self):
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 09:00"), 20, 1000000)
        for h in hits:
            self.assertGreaterEqual(h["price"], 20)
            self.assertGreaterEqual(h["volume"], 1000000)
        self.assertEqual(counts["illiquid"] + counts["scanned"], 19)
        self.assertGreater(counts["illiquid"], 0)                # BAJAJ-AUTO trades 300,000 shares in the fixture
        self.assertNotIn("BAJAJ-AUTO", {h["symbol"] for h in hits})

    def test_turnover_floor_in_crore(self):
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 09:00"), 0, 0,
                                          min_turnover_cr=100.0)    # 1 crore = 1,00,00,000 rupees
        for h in hits:
            self.assertGreaterEqual(h["price"] * h["volume"], 100.0 * 1e7)
        self.assertGreater(counts["illiquid"], 0)

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(1, 11)], "c": [10.0] * 10,
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X": s, "E": {"t": [], "c": [], "meta": {}}}, ["X", "E", "GONE"],
                                             scan.parse_now("2026-10-30 09:00"), 20, 1000000)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["scanned"]), (2, 1, 0))
        self.assertIsNone(last)

    def test_rsi_matches_wilders_example(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(set(cls.data) | set(cls.errors))

    def scan_at(self, now, min_price=0, syms=None, or_minutes=15):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, T_ORB_END)
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def brute_force(self, sym, cutoff, n_range=3):
        """The rule, written the long way: first completed bar after the range that CLOSES above the range high."""
        s = self.data[sym]
        bars = []
        for t, c, h, l in zip(s["t"], s["c"], s["h"], s["l"]):
            start = scan.ts_to_ist(t)
            if start.date() != cutoff.date() or start.second or start.minute % 5:
                continue                                          # a live tick off the 5-minute grid
            if start + datetime.timedelta(minutes=5) > cutoff:
                continue                                          # bar not finished yet
            bars.append((start, c, h, l))
        if len(bars) <= n_range or any(b[1] is None for b in bars):
            return None
        hi = max(b[2] for b in bars[:n_range])
        for i, b in enumerate(bars[n_range:], n_range):
            if b[1] > hi:
                return (b[0].strftime("%H:%M"), b[1], hi)
        return None

    def test_the_session_starts_at_0915(self):
        s = self.data["TCS"]
        self.assertEqual(scan.ts_to_ist(s["t"][0]), DT(2026, 10, 30, 9, 15))
        self.assertEqual(s["meta"]["exchangeTimezoneName"], "Asia/Kolkata")

    def test_the_scan_at_1015(self):
        hits, counts, failed = self.scan_at("2026-10-30 10:15")
        self.assertEqual(len(self.syms), 46)
        self.assertEqual(self.errors, {"ZOMATO": "no data for ZOMATO: No data found, symbol may be delisted"})
        self.assertEqual(counts["fetched"], 45)
        self.assertEqual(counts["scanned"], 43)
        self.assertEqual(counts["unusable"], 2)                  # one chart with no bars, one with a missing bar
        self.assertEqual(len(hits), 32)
        rel = [h for h in hits if h["symbol"] == "RELIANCE"][0]
        self.assertEqual(rel, {"symbol": "RELIANCE", "setups": ["orb"], "price": 1455.5, "at": "10:15",
                               "bar_start": "10:10", "bar_index": 11, "or_high": 1454.0, "or_low": 1448.0,
                               "pct_over": 0.1})
        self.assertEqual(hits[0]["at"], "09:35")                 # sorted by time, then symbol
        self.assertEqual([h["at"] for h in hits], sorted(h["at"] for h in hits))

    def test_every_hit_matches_a_brute_force_check(self):
        for now in ("2026-10-30 09:40", "2026-10-30 10:15", "2026-10-30 11:00"):
            hits, _, _ = self.scan_at(now)
            cutoff = scan.ts_to_ist(int((scan.parse_now(now) - DT(1970, 1, 1)).total_seconds()))
            got = {h["symbol"]: (h["bar_start"], h["price"], h["or_high"]) for h in hits}
            want = {}
            for sym in self.data:
                r = self.brute_force(sym, scan.to_ist(scan.parse_now(now)))
                if r:
                    want[sym] = r
            self.assertEqual(got, want, now)

    def test_later_scans_only_add_hits(self):
        early = {h["symbol"] for h in self.scan_at("2026-10-30 09:40")[0]}
        mid = {h["symbol"] for h in self.scan_at("2026-10-30 10:15")[0]}
        late = {h["symbol"] for h in self.scan_at("2026-10-30 11:00")[0]}
        self.assertTrue(early <= mid <= late)
        self.assertEqual((len(early), len(mid), len(late)), (14, 32, 38))

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-10-30 10:15", min_price=100)
        self.assertNotIn("SUZLON", [h["symbol"] for h in hits])        # ₹55
        self.assertGreater(counts["illiquid"], 0)
        self.assertTrue(all(h["price"] >= 100 for h in hits))

    def test_in_progress_bar_and_no_breakout_yet(self):
        hits, _, _ = self.scan_at("2026-10-30 10:14", syms=["RELIANCE"])
        self.assertEqual(hits, [])                               # the 10:10 bar is not done until 10:15
        hits, _, _ = self.scan_at("2026-10-30 10:15", syms=["RELIANCE"])
        self.assertEqual(hits[0]["at"], "10:15")

    def test_the_live_tick_is_dropped(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.1, 101.3, 101.4]    # 09:15 ... 09:55 (in progress)
        s = bars_series("2026-10-30", closes, highs=[c + 0.2 for c in closes])
        s["t"].append(ts("2026-10-30 09:58:07"))                 # a live tick, off the grid
        for k in ("c", "h", "l"):
            s[k].append(105.0)
        s["v"].append(10)
        bars = scan.session_bars(s, D(2026, 10, 30), DT(2026, 10, 30, 9, 59))
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars],
                         ["09:15", "09:20", "09:25", "09:30", "09:35", "09:40", "09:45", "09:50"])  # 09:55 in progress
        self.assertTrue(scan.usable(bars, D(2026, 10, 30), DT(2026, 10, 30, 9, 59), 3))

    def test_stage_one_is_a_necessary_condition(self):
        day, cut = D(2026, 10, 30), DT(2026, 10, 30, 10, 15)
        for sym, s in self.data.items():
            bars = scan.session_bars(s, day, cut)
            if scan.usable(bars, day, cut, 3) and scan.orb_hit(bars, 3):
                self.assertTrue(scan.orb_candidate(bars, 3), sym)

    def test_confirm_uses_the_v8_highs(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}   # closes only, like spark
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, failed = scan.scan_orb(spark_like, self.syms, D(2026, 10, 30), DT(2026, 10, 30, 10, 15), 15,
                                             0, 0, confirm)
        self.assertEqual(len(hits), 32)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), 43)                          # stage 1 skipped some charts

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s.NS" % sym)
        hits, counts, failed = scan.scan_orb(spark_like, ["RELIANCE", "TCS"], D(2026, 10, 30),
                                             DT(2026, 10, 30, 10, 15), 15, 0, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["RELIANCE"])

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-10-30", [10, 10.2, 10.1, None, 10.5, 10.6, 10.7, 10.8, 10.9])
        bars = scan.session_bars(s, D(2026, 10, 30), DT(2026, 10, 30, 10, 15))
        self.assertFalse(scan.usable(bars, D(2026, 10, 30), DT(2026, 10, 30, 10, 15), 3))

    def test_a_30_minute_range(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 40
        s = bars_series("2026-10-30", closes, highs=[c + 0.2 for c in closes])
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], D(2026, 10, 30), DT(2026, 10, 30, 12, 0), 30, 0, 0)
        self.assertEqual(counts["scanned"], 1)
        # range 09:15 to 09:45 is six bars (high 101.4); the 09:45 bar closes 101.5, done at 09:50
        self.assertEqual(hits[0]["at"], "09:50")
        self.assertEqual(hits[0]["or_high"], 101.4)

    def test_window_messages(self):
        def msg(now, at=None, until=T_ORB_END):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-10-31 10:15"))
        self.assertIn("Dussehra", msg("2026-10-20 10:15"))
        self.assertIn("Run orb again after 09:35", msg("2026-10-30 09:20"))
        self.assertIn("Run orb again after 09:35", msg("2026-10-30 09:10"))
        self.assertIn("expired", msg("2026-10-30 15:25"))
        self.assertIn("expired", msg("2026-10-30 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 15:30", msg("2026-10-30 16:00"))
        self.assertIn("later than now", msg("2026-10-30 10:00", at=datetime.time(10, 15)))
        self.assertIn("after the 15:30 close", msg("2026-10-30 17:00", at=datetime.time(16, 0)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-30 16:00"), datetime.time(10, 15), 15, T_ORB_END),
                         (D(2026, 10, 30), DT(2026, 10, 30, 10, 15)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-30 09:40"), None, 15, T_ORB_END)[1],
                         DT(2026, 10, 30, 9, 40))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-10-30 10:15"), None, 7, T_ORB_END)    # not a multiple of 5

    def test_2027_runs_with_a_calendar_warning_flag(self):
        day, cut = scan.orb_window(scan.parse_now("2027-01-05 10:15"), None, 15, T_ORB_END)
        self.assertEqual(day, D(2027, 1, 5))                     # unknown year: allowed, the output warns


class LiveFiveMinute(unittest.TestCase):
    """A spark 5-minute answer and RELIANCE's v8 chart, 'fetched' at 10:01:48 IST on Fri 2026-10-30."""

    def setUp(self):
        with open(SPARK5, encoding="utf-8") as fh:
            self.spark = scan.parse_spark(fh.read())
        with open(REL5, encoding="utf-8") as fh:
            self.v8 = scan.parse_chart(fh.read(), "RELIANCE")
        self.now = scan.parse_now("2026-10-30 10:01:48")

    def test_in_progress_bar_and_live_tick_are_dropped(self):
        self.assertEqual(len(self.spark), 20)
        self.assertIn("M&M", self.spark)
        r = self.spark["RELIANCE"]
        self.assertEqual(len(r["t"]), 11)                        # 09:15 ... 10:00 (in progress) + the 10:01:47 tick
        self.assertEqual(scan.ts_to_ist(r["t"][-1]), DT(2026, 10, 30, 10, 1, 47))
        day, cut = scan.orb_window(self.now, None, 15, T_ORB_END)
        self.assertEqual(cut, DT(2026, 10, 30, 10, 1))
        bars = scan.session_bars(r, day, cut)
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars],
                         ["09:15", "09:20", "09:25", "09:30", "09:35", "09:40", "09:45", "09:50", "09:55"])
        self.assertEqual(len(scan.session_bars(self.v8, day, cut)), 9)

    def test_reliance_broke_its_range_at_0945(self):
        day, cut = scan.orb_window(self.now, None, 15, T_ORB_END)
        hits, counts, failed = scan.scan_orb(self.spark, ["RELIANCE", "TCS"], day, cut, 15, 20, 0,
                                             lambda s: self.v8)
        # range 09:15 to 09:30: high 1454.0 (the 09:20 bar), low 1448.0; the 09:40 bar closed 1455.5
        self.assertEqual(hits, [{"symbol": "RELIANCE", "setups": ["orb"], "price": 1455.5, "at": "09:45",
                                 "bar_start": "09:40", "bar_index": 5, "or_high": 1454.0, "or_low": 1448.0,
                                 "pct_over": 0.1}])
        self.assertEqual(counts["scanned"], 2)

    def test_stage_two_cap_takes_the_most_traded(self):
        data, _ = scan.load_offline(WALL)
        spark_like = {k: dict(v, h=None, l=None) for k, v in data.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return data[sym]
        day, cut = D(2026, 10, 30), DT(2026, 10, 30, 10, 15)
        hits, counts, _ = scan.scan_orb(spark_like, sorted(data), day, cut, 15, 0, 0, confirm, max_confirm=10)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (10, counts["candidates"] - 10))
        self.assertLessEqual(len(hits), 10)
        hits_all, _, _ = scan.scan_orb(spark_like, sorted(data), day, cut, 15, 0, 0, confirm, max_confirm=0)
        self.assertEqual(len(hits_all), 32)
        for h in hits:
            self.assertIn(h, hits_all)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "nse")
        self.wl = os.path.join(self.t, "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--offline", SYMDIR, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("Kept 10 EQ-series stocks", text)
        self.assertIn("1 trade-to-trade (BE/BZ)", text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertIn("M&M|Mahindra & Mahindra Ltd.", lines)

        # daily over that universe with the fixture charts: 7 of the 10 symbols are in both
        code, text, _ = run(["daily", "--universe", uni, "--offline", DAILY, "--now", "2026-10-30 09:00",
                             "--out", self.wl, "--min-volume", "0"])
        self.assertEqual(code, 0)
        self.assertIn("Fetched 7 of 10 charts (3 failed or had no data)", text)
        self.assertIn("₹3,796.25", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"]), ("daily", 10, 7, 3))
        self.assertEqual(doc["as_of"], "2026-10-29 close")
        self.assertEqual(doc["created_at"], "2026-10-30T03:30:00Z")             # 09:00 IST
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["M&M", "HDFCBANK", "SBIN", "INFY", "BAJAJ-AUTO"])
        self.assertEqual(doc["hits"][0], {"symbol": "M&M", "setups": ["bull_flag"], "price": 3796.25,
                                          "close_date": "2026-10-29", "rsi": 80.8, "volume": 1800000})

        # the 10:15 opening-range scan
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-30 10:15", "--min-price", "0",
                             "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("32 of 43 charts closed above their opening-range high by 10:15", text)
        self.assertIn("09:35  HINDUNILVR      ₹2,402.25  range 2,394.90 to 2,400.30  +0.08% over", text)
        self.assertIn("15-minute range (09:15 to 09:30)", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source")},
                         {"created_by": "nse-scanner", "created_at": "2026-10-30T04:45:00Z", "mode": "orb",
                          "as_of": "2026-10-30 10:15 IST", "universe": 46, "scanned": 43, "source": "yahoo"})
        self.assertEqual(len(doc["hits"]), 32)
        self.assertIn({"symbol": "RELIANCE", "setups": ["orb"], "price": 1455.5, "at": "10:15", "bar_start": "10:10",
                       "bar_index": 11, "or_high": 1454.0, "or_low": 1448.0, "pct_over": 0.1}, doc["hits"])

        # the hits read back as a watchlist, with --at instead of --now
        code, text, _ = run(["orb", "--offline", WALL, "--at", "10:15", "--watchlist", self.wl, "--min-price", "0",
                             "--json", "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (32, 32, 32))

    def test_all_eq_universe(self):
        uni = os.path.join(self.t, "all.txt")
        code, text, _ = run(["universe", "--offline", SYMDIR, "--universe", "all-eq", "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("Kept 9 EQ-series stocks", text)
        self.assertIn("2 SME (SM/ST)", text)

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the 2026-10-30 bundle."""
        bundle = json.loads(scan._read_maybe_gz(WALL))
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                res = []
                from urllib.parse import unquote
                for y in unquote(path.split("symbols=")[1].split("&")[0]).split(","):
                    r = ((bundle.get(y[:-3]) or {}).get("chart") or {}).get("result")
                    if r:
                        closes = r[0]["indicators"]["quote"][0].get("close", [])
                        res.append({"symbol": y, "response": [dict(r[0], indicators={"quote": [{"close": closes}]})]})
                return json.dumps({"spark": {"result": res, "error": None}})
            from urllib.parse import unquote
            return json.dumps(bundle[unquote(path.split("/chart/")[1].split("?")[0])[:-3]])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-10-30 10:20")      # the machine clock, pinned
        uni = os.path.join(self.d, "u.txt")
        with open(uni, "w") as fh:
            fh.write("\n".join(sorted(bundle)) + "\n")
        try:
            code, text, _ = run(["orb", "--universe", uni, "--now", "2026-10-30 10:15", "--min-price", "0",
                                 "--max-confirm", "10", "--no-write"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0)
        self.assertIn("PARTIAL: 29 more candidates were not checked", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 10)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 3)      # 46 symbols, 20 a request
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))
        self.assertTrue(any("M%26M.NS" in c for c in calls))                    # & is URL-encoded

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-31 10:15", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-20 10:15", "--out", self.wl])
        self.assertIn("NSE holiday: Dussehra", err)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-30 09:20", "--out", self.wl])
        self.assertIn("after 09:35", err)
        cfg = os.path.join(self.t, "config.json")
        os.makedirs(self.t)
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-30 10:15", "--config", cfg])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-30 10:10", "--symbols", "RELIANCE",
                             "--out", self.wl])
        self.assertEqual(code, 1)                                # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 10:10", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(scan._read_maybe_gz(WALL))
        for sym in ("RELIANCE", "TCS", "SBIN", "ZOMATO"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump(bundle[sym], fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-10-30 10:15", "--no-write", "--json"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["SBIN", "TCS", "RELIANCE"])   # by time, 09:35 09:45 10:15
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nRELIANCE|Reliance Industries Ltd.\nm&m\n\nRELIANCE\n")
        self.assertEqual(scan.symbols_from_file(txt), ["RELIANCE", "M&M", "RELIANCE"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "tcs"}, {"symbol": "M&M"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["TCS", "M&M"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-10-30 09:00",
                             "--json", "--no-write", "--min-volume", "0"])
        out = json.loads(text)
        self.assertEqual(out["universe"], 2)                     # the repeated RELIANCE is counted once
        self.assertEqual(out["scanned"], 2)
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-10-30 09:00", "--json",
                             "--no-write", "--min-volume", "0"])
        self.assertEqual(json.loads(text)["universe"], 3)

    def test_daily_json(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-10-30 09:00", "--json", "--no-write",
                             "--min-volume", "0", "--min-price", "0"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(len(out["hits"]), 15)
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")


if __name__ == "__main__":
    unittest.main()
