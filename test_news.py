import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("nse-news", "news")
DIR = os.path.join(FIX, "nse-news")
NOW = "2026-10-30T11:00:00+05:30"          # 11:00 IST on Fri 2026-10-30 = 05:30 UTC
UTC = datetime.timezone.utc


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-RELIANCE.xml"), "RELIANCE")
        self.assertEqual(len(items), 9)
        first = items[0]
        self.assertEqual(first["title"], "Reliance Industries shares in focus ahead of quarterly results")
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 30, 2, 10, tzinfo=UTC))
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertTrue(first["id"].startswith("rss:"))

    def test_nse_announcements_list_shape(self):
        items = news.parse_announcements(raw("nse-announcements-RELIANCE.json"), "RELIANCE")
        self.assertEqual([i["form"] for i in items][:3],
                         ["Financial Results", "Board Meeting Intimation",
                          "Change in Directors/ Key Managerial Personnel/ Auditor/ Compliance Officer/ Share Transfer Agent"])
        results = items[0]
        self.assertEqual(results["id"], "nse:RELIANCE:101")
        self.assertEqual(results["source"], "NSE")
        self.assertEqual(results["kind"], "filing")
        self.assertEqual(results["title"], "Reliance Industries Limited: Financial Results")
        self.assertTrue(results["important"])
        self.assertTrue(results["link"].startswith("https://nsearchives.nseindia.com/"))
        # 17:42:10 India time is 12:12:10 UTC
        self.assertEqual(results["time"].isoformat(), "2026-10-29T12:12:10+00:00")
        self.assertFalse(items[1]["important"])                  # a board-meeting intimation is not the outcome
        self.assertTrue(items[2]["important"])                   # director / KMP change

    def test_nse_announcements_wrapped_shape(self):
        items = news.parse_announcements(raw("nse-announcements-TCS.json"), "TCS")   # {"data": [...]}
        self.assertEqual([i["form"] for i in items], ["Outcome of Board Meeting", "Updates"])
        self.assertTrue(items[0]["important"])
        self.assertFalse(items[1]["important"])
        self.assertEqual(items[0]["time"].isoformat(), "2026-10-29T12:40:00+00:00")   # 18:10 IST

    def test_nse_answering_with_a_web_page(self):
        with self.assertRaises(news.NewsError) as cm:
            news.parse_announcements(b"<html>Access Denied</html>", "TCS")
        self.assertIn("not valid JSON", str(cm.exception))
        with self.assertRaises(news.NewsError):
            news.parse_announcements(b'{"data": 5}', "TCS")
        self.assertEqual(news.parse_announcements(b"[]", "TCS"), [])

    def test_nse_time_formats(self):
        f = news.parse_nse_time
        self.assertEqual(f({"an_dt": "29-Oct-2026 17:42:10"}).isoformat(), "2026-10-29T12:12:10+00:00")
        self.assertEqual(f({"sort_date": "2026-10-29 17:42:10"}).isoformat(), "2026-10-29T12:12:10+00:00")
        self.assertEqual(f({"dt": "29-Oct-2026"}).isoformat(), "2026-10-28T18:30:00+00:00")   # midnight IST
        self.assertIsNone(f({"an_dt": "soon"}))
        self.assertIsNone(f({}))

    def test_times(self):
        self.assertEqual(news.parse_time("Tue, 13 Oct 2026 20:02:03 +0000").hour, 20)
        self.assertEqual(news.parse_time("2026-10-30T11:00:00+05:30").hour, 5)
        self.assertIsNone(news.parse_time("soon"))

    def test_symbols(self):
        self.assertEqual(news.yahoo_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(news.yahoo_symbol("m&m"), "M%26M.NS")              # & is URL-encoded
        self.assertEqual(news.yahoo_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")   # the hyphen stays
        self.assertEqual(news.yahoo_symbol("TCS.NS"), "TCS.NS")
        self.assertEqual(news.bare("tcs.ns"), "TCS")
        self.assertEqual(news.bare("M&M"), "M&M")


class Tagging(unittest.TestCase):
    WATCH = {"RELIANCE": "Reliance Industries", "TCS": "Tata Consultancy Services"}

    def test_touches_and_tags(self):
        items = news.parse_rss(raw("yahoo-RELIANCE.xml"), "RELIANCE")
        news.tag(items, self.WATCH)
        def pick(prefix):
            return [i for i in items if i["title"].startswith(prefix)][0]
        sebi = pick("SEBI order on a group")
        self.assertEqual(sebi["touches"], ["RELIANCE", "TCS"])       # names TCS in the headline
        self.assertIn("legal", sebi["tags"])
        self.assertIn("regulatory", sebi["tags"])
        self.assertIn("capital", pick("Reliance board to consider")["tags"])  # dividend, buyback
        self.assertIn("analyst", pick("Brokerage upgrade")["tags"])
        self.assertIn("deal", pick("Reliance unit signs")["tags"])
        nifty = pick("Nifty futures edge")
        self.assertIn("macro", nifty["tags"])                         # Nifty, futures, RBI
        self.assertEqual(nifty["touches"], ["RELIANCE"])

    def test_ticker_mentions(self):
        pats = news.mention_patterns({"M&M": "Mahindra & Mahindra", "TCS": None, "BAJAJ-AUTO": None})
        self.assertTrue(pats["M&M"].search("Auto stocks rally (M&M) leads"))
        self.assertTrue(pats["M&M"].search("NSE: M&M jumps 3%"))
        self.assertTrue(pats["TCS"].search("#TCS results today"))
        self.assertTrue(pats["BAJAJ-AUTO"].search("BSE:BAJAJ-AUTO hits a high"))
        self.assertFalse(pats["TCS"].search("TCSX is another company"))
        self.assertFalse(pats["TCS"].search("NSE: TCSX"))

    def test_results_filings_are_tagged_earnings(self):
        items = news.tag(news.parse_announcements(raw("nse-announcements-RELIANCE.json"), "RELIANCE"),
                         {"RELIANCE": None})
        self.assertEqual(items[0]["tags"][0], "earnings")             # "Financial Results"
        self.assertIn("people", items[2]["tags"])                     # change in directors / KMP
        self.assertFalse(items[4]["important"])                       # a trading-window notice

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("yahoo-RELIANCE.xml"), "RELIANCE")
        b = news.parse_rss(raw("yahoo-RELIANCE.xml"), "TCS")
        items = news.dedupe(news.tag(a + b, self.WATCH))
        self.assertEqual(len(items), 8)                               # 9 headlines, one repeated title the same day
        self.assertEqual(items[0]["touches"], ["RELIANCE", "TCS"])

    def test_day_trading_tags(self):
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "TCS",
                  "title": "Trading halted in XYZ after SEBI order; Nifty futures rise before RBI policy",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"TCS": None})[0]["tags"]
        for t in ("halt", "legal", "regulatory", "macro"):
            self.assertIn(t, tags)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--watchlist", os.path.join(self.tmp.name, "none.json"),
                     "--plan", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def expected_ids(self, tickers, hours):
        now = news.parse_time(NOW)
        ids = set()
        for t in tickers:
            rows = news.parse_rss(raw("yahoo-%s.xml" % t), t)
            if os.path.exists(os.path.join(DIR, "nse-announcements-%s.json" % t)):
                rows += news.parse_announcements(raw("nse-announcements-%s.json" % t), t)
            for r in rows:
                if now - datetime.timedelta(hours=hours) <= r["time"] <= now + datetime.timedelta(minutes=10):
                    ids.add(r["id"])
        return ids

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "RELIANCE", "TCS", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        # the long way: every headline and announcement inside the window and not after the pinned clock,
        # with the repeated headline counted once
        want = self.expected_ids(["RELIANCE", "TCS"], 24)
        got = {i["id"] for i in data["items"]}
        self.assertEqual(len(got), len(data["items"]))
        self.assertTrue(got <= want)
        self.assertEqual(len(want) - len(got), 1)                    # the repeated "Brokerage upgrade" headline
        self.assertNotIn("nse:RELIANCE:106", got)                    # the 16:00 IST press release is after "now"
        code, out, _ = run(["--tickers", "RELIANCE", "TCS", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_filings_show_in_a_wider_window(self):
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "2400"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-10-29 17:42 IST  RELIANCE", out)       # 12:12 UTC, India time
        self.assertIn("Financial Results", out)
        self.assertIn("Checked 2026-10-30 11:00 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)
        self.assertNotIn(" ET", out)
        self.assertNotIn("New York", out)

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": [{"symbol": "RELIANCE", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "nse-exit", "symbol": "M&M"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["M&M", "RELIANCE"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        args[args.index("--plan") + 1] = plan
        code, out, err = run(["--json", "--dry-run", "--hours", "48"] + args)       # no --tickers: files decide
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        tickers = {t for i in data["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"RELIANCE"})                      # TCS is not watched, so it is not "touched"
        self.assertTrue(any("M&M" in n for n in data["notes"]))      # watched, but the offline folder has no files
        self.assertFalse(os.path.exists(self.state))

    def test_positions_with_ns_suffix_are_read_as_bare(self):
        pf = os.path.join(self.tmp.name, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"positions": [{"ticker": "tcs.ns", "qty": 5}]}, fh)
        self.assertEqual(list(news.watch_from_files(pf, "/x.json", "/y.json")), ["TCS"])

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)

    def test_no_filings_skips_nse(self):
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "2400", "--no-filings", "--json"] + self.base)
        data = json.loads(out)
        self.assertEqual({i["source"] for i in data["items"]}, {"Yahoo Finance"})

    def test_moves(self):
        m = news.parse_chart(raw("chart-RELIANCE.json"), "RELIANCE")
        self.assertEqual(m["prev_close"], 1448.9)
        self.assertEqual(m["price"], 1494.6)
        self.assertEqual(m["pct"], 3.15)                             # 1494.6 / 1448.9 - 1
        self.assertEqual(news.to_ist(m["time"]).strftime("%H:%M"), "10:52")
        line = news.render_moves([m])[0]
        self.assertIn("₹1,494.60 +3.15% vs yesterday's close ₹1,448.90 at 10:52 IST", line)
        self.assertTrue(line.rstrip().endswith("MOVING"))
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # the CLI line, from the chart fixture
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "24", "--dry-run"] + self.base)
        self.assertIn("₹1,494.60 +3.15%", out)
        # no chart file offline: no move line, no error note
        code, out, _ = run(["--tickers", "TCS", "--hours", "24", "--dry-run"] + self.base)
        self.assertNotIn("vs yesterday's close", out)
        self.assertNotIn("note:", out)

    def test_india_time_has_no_daylight_saving(self):
        jan = datetime.datetime(2026, 1, 15, 4, 30, tzinfo=UTC)
        jul = datetime.datetime(2026, 7, 15, 4, 30, tzinfo=UTC)
        self.assertEqual(news.to_ist(jan).strftime("%H:%M"), "10:00")
        self.assertEqual(news.to_ist(jul).strftime("%H:%M"), "10:00")
        self.assertEqual(news.to_ist(datetime.datetime(2026, 10, 30, 3, 45)).strftime("%H:%M"), "09:15")

    def test_inr_grouping(self):
        self.assertEqual(news.inr(1234567.5), "12,34,567.50")
        self.assertEqual(news.inr(999), "999.00")
        self.assertEqual(news.inr(1494.6), "1,494.60")
        self.assertEqual(news.inr(100000, 0), "1,00,000")
        self.assertEqual(news.inr(-2500.5), "-2,500.50")

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


class Fetching(unittest.TestCase):
    """The NSE cookie handshake and a refusal, with no network."""

    def test_handshake_visits_the_home_page_once(self):
        f = news.Fetcher(pause=0)
        calls = []

        def fake_read(req, opener=None):
            calls.append((req.full_url, req.get_header("User-agent"), req.get_header("Referer")))
            return b"[]"
        f._read = fake_read
        f.get(news.NSE_ANN % "M%26M", "x", nse=True)
        f.get(news.NSE_ANN % "TCS", "x", nse=True)
        urls = [c[0] for c in calls]
        self.assertEqual(urls, [news.NSE_HOME,
                                "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=M%26M",
                                "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=TCS"])
        self.assertIn("Mozilla/5.0", calls[1][1])                    # a browser User-Agent
        self.assertNotIn("@", calls[1][1])                           # nobody's email goes to a website
        self.assertEqual(calls[1][2], news.NSE_HOME)

    def test_a_refusal_is_said_once_and_headlines_still_run(self):
        class Refused(news.Fetcher):
            def __init__(self):
                news.Fetcher.__init__(self, offline=DIR)
                self.offline = None                                   # act like a live fetcher ...
                self.nse_calls = 0

            def get(self, url, name, nse=False):
                if nse:
                    self.nse_calls += 1
                    raise news.NewsError("HTTP 403 for %s" % url)
                with open(os.path.join(DIR, name), "rb") as fh:       # ... but answer headlines from the fixtures
                    return fh.read()
        f = Refused()
        errors = []
        items = news.collect(f, {"RELIANCE": None, "TCS": None}, {}, filings=True, errors=errors)
        self.assertEqual(f.nse_calls, 1)                              # it stopped after the first refusal
        self.assertEqual(len(errors), 1)
        self.assertIn("HTTP 403", errors[0])
        self.assertIn("refuses cloud, VPN and data-centre", errors[0])
        self.assertEqual({i["source"] for i in items}, {"Yahoo Finance"})
        self.assertEqual(len(items), 11)                              # 9 + 2 headlines

    def test_offline_missing_file_is_a_note_per_ticker(self):
        errors = []
        news.collect(news.Fetcher(offline=DIR), {"RELIANCE": None, "ZZNONE": None}, {}, errors=errors)
        self.assertTrue(any("ZZNONE" in e and "no file" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
