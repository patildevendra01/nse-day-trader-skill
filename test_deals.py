import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

dl = load("nse-deals", "deals")
DIR = os.path.join(FIX, "nse-deals")
NOW = "2026-10-30T18:00:00+05:30"


def read(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return fh.read()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = dl.main(argv)
    return code, out.getvalue(), err.getvalue()


class Parsing(unittest.TestCase):
    def test_bulk_rows(self):
        rows = dl.parse_deals(read("bulk.csv"), "bulk")
        self.assertEqual(len(rows), 5)
        r = rows[0]
        self.assertEqual((r["date"], r["symbol"], r["side"], r["qty"], r["price"]),
                         ("2026-10-30", "RELIANCE", "BUY", 5200000, 1455.35))
        self.assertEqual(r["value_cr"], 756.78)                      # 52,00,000 x 1455.35 / 1e7
        self.assertEqual(rows[4]["symbol"], "M&M")
        self.assertEqual(rows[4]["client"], "GOLDMAN SACHS (SINGAPORE) PTE. - ODI")   # comma-quoted fields survive

    def test_block_rows_and_date_spelling(self):
        rows = dl.parse_deals(read("block.csv"), "block")
        self.assertEqual([(r["symbol"], r["side"], r["date"]) for r in rows],
                         [("HDFCBANK", "SELL", "2026-10-30"), ("INFY", "BUY", "2026-10-30")])
        self.assertEqual(rows[0]["value_cr"], 153.92)

    def test_tolerant_headers_and_junk(self):
        text = "DATE,SYMBOL,CLIENT NAME,BUY/SELL,QUANTITY TRADED,TRADE PRICE\n\n30/10/2026,tcs.ns,X FUND,Sell,100,4000\nbad,row\n,,,,,\n"
        rows = dl.parse_deals(text, "bulk")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["symbol"], rows[0]["side"], rows[0]["date"]), ("TCS", "SELL", "2026-10-30"))
        self.assertEqual(dl.parse_deals("", "bulk"), [])
        self.assertEqual(dl.parse_deals("<html>blocked</html>", "bulk"), [])

    def test_insider_rows(self):
        rows = dl.parse_pit(read("pit-RELIANCE.json"), "RELIANCE")
        self.assertEqual(len(rows), 2)                               # the nameless row is dropped
        a, b = rows
        self.assertEqual((a["client"], a["category"], a["side"], a["qty"], a["value_cr"], a["date"]),
                         ("Promoter Entity One", "Promoters", "BUY", 250000, 36.38, "2026-10-29"))
        self.assertEqual((b["side"], b["qty"]), ("SELL", 5000))
        self.assertEqual(dl.parse_pit(json.dumps([{"acqName": "A", "acqtoDt": "01-Oct-2026",
                                                  "tdpTransactionType": "Market Purchase"}]), "TCS")[0]["side"], "BUY")
        with self.assertRaises(dl.DealsError):
            dl.parse_pit("<html>Access Denied</html>", "RELIANCE")

    def test_helpers(self):
        self.assertEqual(dl.parse_date("05-NOV-2026"), "2026-11-05")
        self.assertIsNone(dl.parse_date("soon"))
        self.assertEqual(dl.number("1,20,000"), 120000.0)
        self.assertIsNone(dl.number("-x"))
        self.assertEqual(dl.bare("m&m.ns"), "M&M")
        self.assertEqual(dl.bare("BAJAJ-AUTO"), "BAJAJ-AUTO")
        self.assertEqual(dl.inr(1234567.5), "12,34,567.50")
        self.assertEqual(dl.inr(756.78), "756.78")
        self.assertEqual(dl.inr(-100000, 0), "-1,00,000")
        self.assertEqual(dl.now_ist(NOW).isoformat(), "2026-10-30T18:00:00")
        self.assertEqual(dl.now_ist("2026-10-30T12:30:00Z").isoformat(), "2026-10-30T18:00:00")


class Flags(unittest.TestCase):
    def setUp(self):
        self.rows = dl.parse_deals(read("bulk.csv"), "bulk") + dl.parse_deals(read("block.csv"), "block")

    def test_watched_names_match_loosely(self):
        self.assertEqual(dl.match_client("RAKESH JHUNJHUNWALA", ["Rakesh Jhunjhunwala"]), ["Rakesh Jhunjhunwala"])
        self.assertEqual(dl.match_client("VANGUARD EMERGING MARKETS STOCK INDEX FUND", ["vanguard"]), ["vanguard"])
        self.assertEqual(dl.match_client("GOLDMAN SACHS (SINGAPORE) PTE. - ODI", ["Goldman Sachs Singapore"]),
                         ["Goldman Sachs Singapore"])
        self.assertEqual(dl.match_client("ACME TRADERS LLP", ["Vanguard", ""]), [])

    def test_flags(self):
        dl.flag(self.rows, ["RELIANCE"], ["Vanguard", "Jhunjhunwala"])
        by = {(r["symbol"], r["kind"]): r["flags"] for r in self.rows}
        self.assertEqual(by[("RELIANCE", "bulk")], ["YOURS", "WATCHED: Vanguard", "BIG"])
        self.assertEqual(by[("TCS", "bulk")], ["WATCHED: Jhunjhunwala"])
        self.assertEqual(by[("SMALLCAP", "bulk")], [])
        self.assertEqual(by[("INFY", "block")], ["WATCHED: Vanguard"])

    def test_select_dedupes_and_filters(self):
        got = dl.select(self.rows)
        self.assertEqual(len(got), 6)                                 # the repeated SMALLCAP row shows once
        self.assertEqual(got[0]["symbol"], "RELIANCE")                # biggest value first on the same day
        only = dl.select(self.rows, ["tcs.ns", "M&M"])
        self.assertEqual(sorted(d["symbol"] for d in only), ["M&M", "TCS"])

    def test_seen_deals_are_not_repeated(self):
        first = dl.select(self.rows)
        state = dl.remember({"seen": {}}, first, "2026-10-30")
        self.assertEqual(dl.select(self.rows, seen=state["seen"]), [])
        self.assertEqual(len(dl.select(self.rows, seen=state["seen"], show_all=True)), 6)
        late = dl.remember(state, [], "2026-12-15")                   # older than 30 days: forgotten
        self.assertEqual(late["seen"], {})


class Fetching(unittest.TestCase):
    def test_offline_collect(self):
        errors = []
        rows = dl.collect(dl.Fetcher(DIR), insiders=["RELIANCE"], errors=errors)
        self.assertEqual(errors, [])
        self.assertEqual([r["kind"] for r in rows].count("bulk"), 5)
        self.assertEqual([r["kind"] for r in rows].count("insider"), 2)

    def test_a_missing_file_is_a_note_not_a_crash(self):
        d = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(DIR, "bulk.csv"), d)
            errors = []
            rows = dl.collect(dl.Fetcher(d), errors=errors)
            self.assertEqual(len(rows), 5)
            self.assertEqual(len(errors), 1)
            self.assertIn("block deals", errors[0])
        finally:
            shutil.rmtree(d)

    def test_nse_refusal_stops_the_insider_lookups(self):
        calls = []

        class Refusing(dl.Fetcher):
            def get(self, url, name, api=False):
                calls.append(name)
                if api:
                    raise dl.DealsError("HTTP 403 for " + url)
                return read(name)
        errors = []
        dl.collect(Refusing(), insiders=["RELIANCE", "TCS", "INFY"], errors=errors)
        self.assertEqual(calls.count("pit-RELIANCE.json"), 1)
        self.assertNotIn("pit-TCS.json", calls)
        self.assertTrue(any("NSE refused" in e for e in errors))

    def test_urls(self):
        self.assertEqual(dl.BULK_URL, "https://nsearchives.nseindia.com/content/equities/bulk.csv")
        self.assertEqual(dl.BLOCK_URL, "https://nsearchives.nseindia.com/content/equities/block.csv")
        self.assertEqual(dl.PIT_URL % "RELIANCE",
                         "https://www.nseindia.com/api/corporates-pit?index=equities&symbol=RELIANCE")


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs("nse")
        with open("nse/portfolio.json", "w") as fh:
            json.dump({"positions": [{"ticker": "RELIANCE", "qty": 10}]}, fh)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_report_flags_and_remembers(self):
        code, text, err = run(["--offline", DIR, "--watch", "Rakesh Jhunjhunwala", "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("6 new deal(s), 2 on your list.", text)
        self.assertIn("RELIANCE    BUY   52,00,000 shares at ₹1,455.35, ₹756.78 crore  bulk: VANGUARD", text)
        self.assertIn("[YOURS, BIG]", text)
        self.assertIn("[WATCHED: Rakesh Jhunjhunwala]", text)
        self.assertIn("Everything else:", text)
        self.assertIn("Not financial advice", text)
        self.assertTrue(os.path.exists("nse/state/deals.json"))
        code, text, _ = run(["--offline", DIR, "--now", NOW])
        self.assertIn("No new deals.", text)
        code, text, _ = run(["--offline", DIR, "--now", NOW, "--all", "--tickers", "TCS"])
        self.assertIn("1 new deal(s)", text)

    def test_insiders_and_write(self):
        code, text, err = run(["--offline", DIR, "--insiders", "--write", "--now", NOW, "--kind", "block"])
        self.assertEqual(code, 0, err)
        self.assertIn("insider: Promoter Entity One (Promoters)", text)
        with open("nse/deals.json") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "nse-deals")
        self.assertEqual(sorted({d["kind"] for d in doc["deals"]}), ["block", "insider"])

    def test_json_and_no_remember(self):
        code, text, _ = run(["--offline", DIR, "--json", "--no-remember", "--now", NOW])
        self.assertEqual(len(json.loads(text)["deals"]), 6)
        self.assertFalse(os.path.exists("nse/state/deals.json"))

    def test_errors_are_one_plain_line(self):
        code, text, err = run(["--offline", os.path.join(self.d, "nowhere"), "--now", NOW])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("deals.py: "))
        self.assertEqual(len(err.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
