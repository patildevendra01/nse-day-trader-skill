import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

rs = load("nse-analyst", "results")
DIR = os.path.join(FIX, "nse-analyst")


def read(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rs.main(args)
    return code, out.getvalue(), err.getvalue()


class Labels(unittest.TestCase):
    def test_indian_fiscal_quarters(self):
        cases = {"2026-06-30": "Q1 FY27", "2026-09-30": "Q2 FY27", "2026-12-31": "Q3 FY27",
                 "2027-03-31": "Q4 FY27", "2026-03-31": "Q4 FY26", "2025-06-30": "Q1 FY26"}
        for end, want in cases.items():
            self.assertEqual(rs.fiscal_label(end), want, end)

    def test_symbols_and_money(self):
        self.assertEqual(rs.bare("m&m.ns"), "M&M")
        self.assertEqual(rs.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertEqual(rs.yahoo_symbol("bajaj-auto"), "BAJAJ-AUTO.NS")
        self.assertEqual(rs.inr(1234567.5), "12,34,567.50")
        self.assertEqual(rs.inr(106500, 0), "1,06,500")
        self.assertEqual(rs.pct(110, 100), 10.0)
        self.assertIsNone(rs.pct(1, 0))
        self.assertIsNone(rs.pct(None, 5))
        self.assertEqual(rs.pct(-50, -100), 50.0)      # a smaller loss is an improvement
        self.assertIsNone(rs.margin(5, 0))


class Yahoo(unittest.TestCase):
    def setUp(self):
        self.q = rs.parse_yahoo(read("timeseries-RELIANCE.json"), "RELIANCE")
        self.r = rs.analyse(self.q, quarters_shown=8)

    def test_quarters_are_in_crore(self):
        self.assertEqual(len(self.q), 9)
        self.assertEqual(self.q["2026-09-30"]["revenue"], 27900.0)        # 2.79e11 rupees
        self.assertEqual(self.q["2026-09-30"]["net_income"], 2120.0)
        self.assertEqual(self.q["2026-09-30"]["eps_diluted"], 31.3)      # EPS is not divided
        self.assertNotIn("gross_profit", self.q["2026-09-30"])

    def test_latest_quarter(self):
        r = self.r["rows"][0]
        self.assertEqual((r["end"], r["fiscal"]), ("2026-09-30", "Q2 FY27"))
        self.assertEqual((r["revenue_yoy"], r["revenue_qoq"]), (10.7, 4.1))      # vs 25,200 and 26,800
        self.assertEqual((r["net_income_yoy"], r["net_income_qoq"]), (14.6, 3.4))  # vs 1,850 and 2,050
        self.assertEqual((r["eps_diluted_yoy"], r["eps_diluted_qoq"]), (14.7, 3.3))
        self.assertEqual((r["operating_margin"], r["net_margin"]), (17.2, 7.6))
        self.assertIsNone(r["gross_margin"])
        self.assertEqual(r["operating_margin_change"], 0.1)                     # 17.2 vs 17.1 a year ago

    def test_yoy_needs_a_quarter_a_year_back(self):
        rows = self.r["rows"]
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[4]["end"], "2025-09-30")
        self.assertEqual(rows[4]["revenue_yoy"], 7.2)                           # vs 2024-09-30
        self.assertIsNone(rs.analyse(self.q, 9)["rows"][8]["revenue_yoy"])      # the oldest has nothing behind it
        self.assertIsNone(rs.analyse(self.q, 9)["rows"][8]["revenue_qoq"])

    def test_the_last_four_quarters(self):
        t = self.r["ttm"]
        self.assertEqual((t["revenue"], t["net_income"], t["eps_diluted"]), (106500.0, 8020.0, 118.5))
        # a gap in the quarters is not summed as if it were a year
        gappy = {k: v for k, v in self.q.items() if k != "2026-03-31"}
        self.assertEqual(rs.analyse(gappy)["ttm"], {})

    def test_notes(self):
        self.assertEqual(self.r["notes"], [n for n in self.r["notes"] if n.startswith("No gross profit")])
        self.assertEqual(len(self.r["notes"]), 1)
        short = rs.parse_yahoo(read("timeseries-TCS.json"), "TCS")
        notes = rs.analyse(short)["notes"]
        self.assertTrue(notes[0].startswith("No quarter from a year earlier"))
        high = dict(self.q)
        high["2026-09-30"] = dict(high["2026-09-30"], net_income=5000.0)       # above operating profit
        self.assertTrue(any("Net profit is above operating profit" in n for n in rs.analyse(high)["notes"]))

    def test_sentences(self):
        self.assertEqual(rs.changes(self.r), [
            "Q2 FY27 (quarter ended 2026-09-30): revenue Rs 27,900 crore, +10.7% YoY, +4.1% QoQ.",
            "Net profit Rs 2,120 crore, +14.6% YoY, +3.4% QoQ; diluted EPS Rs 31.30 (+14.7% YoY).",
            "Operating margin 17.2% (+0.1 points YoY)."])

    def test_bad_answers(self):
        with self.assertRaises(rs.ResultsError) as cm:
            rs.parse_yahoo(read("timeseries-ZZZ.json"), "ZZZ")
        self.assertIn("No fundamentals data", str(cm.exception))
        with self.assertRaises(rs.ResultsError):
            rs.parse_yahoo("<html>Too Many Requests</html>", "X")
        with self.assertRaises(rs.ResultsError) as cm:
            rs.parse_yahoo('{"timeseries": {"result": [], "error": null}}', "X")
        self.assertIn("--file", str(cm.exception))

    def test_url(self):
        self.assertIn("/timeseries/M%26M.NS?type=quarterlyTotalRevenue,quarterlyNetIncome,quarterlyOperatingIncome,"
                      "quarterlyDilutedEPS,quarterlyGrossProfit&merge=false", rs.TS_URL % (rs.yahoo_symbol("M&M"), 1, 2))


class Files(unittest.TestCase):
    def test_csv_with_commas_and_blank_cells(self):
        sym, q = rs.parse_file(read("infy-results.csv"), "infy-results.csv")
        self.assertIsNone(sym)
        self.assertEqual(len(q), 5)
        self.assertEqual(q["2025-09-30"], {"revenue": 40986.0, "operating_income": 8500.0, "net_income": 6506.0,
                                           "eps_diluted": 15.7})
        r = rs.analyse(q)["rows"][0]
        self.assertEqual((r["revenue_yoy"], r["net_income_yoy"]), (5.2, 11.4))

    def test_json_with_a_symbol(self):
        sym, q = rs.parse_file(read("hdfcbank-results.json"))
        self.assertEqual(sym, "HDFCBANK")
        self.assertEqual(q["2026-09-30"]["net_income"], 18480.0)

    def test_header_variants_and_negative_in_brackets(self):
        text = "Period,Total Income,Net Profit\n30-09-2026,100,(25)\n2026-06-30,90,10\n"
        _, q = rs.parse_file(text)
        self.assertEqual(q, {"2026-06-30": {"revenue": 90.0, "net_income": 10.0}})   # an unreadable date is skipped
        _, q = rs.parse_file("Period,Total Income,Net Profit\n2026-09-30,100,(25)\n")
        self.assertEqual(q["2026-09-30"]["net_income"], -25.0)

    def test_nothing_usable(self):
        for text in ("", "a,b\n1,2\n", "[]", "{\"quarters\": []}"):
            with self.assertRaises(rs.ResultsError):
                rs.parse_file(text)
        with self.assertRaises(rs.ResultsError):
            rs.parse_file("{not json")


class Cli(unittest.TestCase):
    def test_a_symbol_offline(self):
        code, text, err = run(["RELIANCE", "--offline", DIR])
        self.assertEqual(code, 0, err)
        self.assertIn("Rs crore except EPS (1 crore = 10 million)", text)
        self.assertIn("Q2 FY27   2026-09-30      27,900   +10.7%    +4.1%      2,120   +14.6%    +3.4%   17.2%    7.6%    31.30", text)
        self.assertIn("Last four quarters: revenue Rs 1,06,500 crore, net profit Rs 8,020 crore, EPS Rs 118.50.", text)
        self.assertIn("Not financial advice", text)
        for bad in ("US$", "SEC", "10-Q", "8-K"):
            self.assertNotIn(bad, text)

    def test_two_symbols_and_one_missing(self):
        code, text, err = run(["RELIANCE", "TCS", "NOSUCH", "--offline", DIR])
        self.assertEqual(code, 1)                          # some worked, one did not
        self.assertIn("RELIANCE.", text)
        self.assertIn("TCS.", text)
        self.assertIn("results: NOSUCH: offline: no file", err)
        code, _, err = run(["NOSUCH", "--offline", DIR])
        self.assertEqual(code, 2)

    def test_file_route(self):
        code, text, err = run(["--file", os.path.join(DIR, "infy-results.csv"), "--symbol", "infy"])
        self.assertEqual(code, 0, err)
        self.assertTrue(text.startswith("INFY."))
        self.assertIn("filed numbers", text)
        self.assertIn("Operating margin 21.1%", text)
        code, _, err = run(["--file", os.path.join(DIR, "missing.csv")])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("results: "))

    def test_json_and_metrics(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "nse", "metrics.json")
        code, text, _ = run(["RELIANCE", "--offline", DIR, "--json"])
        doc = json.loads(text)
        self.assertEqual(doc[0]["ticker"], "RELIANCE")
        self.assertEqual(doc[0]["result"]["rows"][0]["fiscal"], "Q2 FY27")
        run(["RELIANCE", "--offline", DIR, "--metrics", path, "--today", "2026-11-06"])
        run(["TCS", "--offline", DIR, "--metrics", path, "--today", "2026-11-06"])      # merges, not overwrites
        with open(path) as fh:
            m = json.load(fh)
        self.assertEqual(m["as_of"], "2026-11-06")
        self.assertEqual([c["ticker"] for c in m["companies"]], ["RELIANCE", "TCS"])
        row = m["companies"][0]
        self.assertEqual((row["revenue_ttm_cr"], row["eps_ttm"], row["revenue_growth_yoy"], row["fiscal"]),
                         (106500.0, 118.5, 10.7, "Q2 FY27"))

    def test_no_arguments_prints_help(self):
        code, text, _ = run([])
        self.assertEqual(code, 2)


class Example(unittest.TestCase):
    def test_example_metrics_file_is_the_tool_s_own_shape(self):
        with open(os.path.join(os.path.dirname(rs.__file__), "metrics.example.json")) as fh:
            ex = json.load(fh)
        self.assertEqual([c["ticker"] for c in ex["companies"]], ["RELIANCE", "TCS"])
        self.assertIn("synthetic", ex["companies"][0]["source"])
        self.assertEqual(set(ex["companies"][0]), set(rs.metrics_row("X", rs.analyse(rs.parse_yahoo(
            read("timeseries-RELIANCE.json"))), "s")))


if __name__ == "__main__":
    unittest.main()
