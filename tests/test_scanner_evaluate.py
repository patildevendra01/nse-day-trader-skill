import datetime
import unittest

from _load import load
scan = load("nse-scanner", "scan")
evaluate = load("nse-scanner", "evaluate")


def make_series(closes):
    start = datetime.datetime(2025, 1, 1)
    stamps = []
    for i in range(len(closes)):
        day = start + datetime.timedelta(days=i)
        stamps.append(int((scan.ist_to_utc(day) - datetime.datetime(1970, 1, 1)).total_seconds()))
    return {
        "t": stamps,
        "o": [c * 0.99 for c in closes],
        "h": [c * 1.02 for c in closes],
        "l": [c * 0.97 for c in closes],
        "c": list(closes),
        "v": [100000] * len(closes),
        "meta": {}
    }


class Evaluation(unittest.TestCase):
    def test_next_session_open_entry_and_horizons(self):
        closes = [100 + i * 0.1 for i in range(40)]
        series = make_series(closes)
        trades, skipped = evaluate.evaluate_series(series)
        self.assertEqual(skipped, 0)
        self.assertTrue(trades)
        one = next(t for t in trades if t["horizon"] == 1)
        self.assertEqual(one["entry_date"], one["exit_date"])
        self.assertGreater(one["entry_date"], one["signal_date"])
        self.assertGreater(one["entry"], 0)

    def test_no_future_bars_are_counted_as_trades(self):
        closes = [100 + i * 0.1 for i in range(30)]
        series = make_series(closes)
        trades, skipped = evaluate.evaluate_series(series)
        self.assertEqual(trades, [])
        self.assertEqual(skipped, 3 * len(scan.find_setups(closes)[0]))

    def test_summary_counts_wins_and_medians(self):
        trades = [
            {"setup": "breakout_20d", "horizon": 1, "return_pct": 2.0},
            {"setup": "breakout_20d", "horizon": 1, "return_pct": -1.0},
            {"setup": "breakout_20d", "horizon": 1, "return_pct": 1.0},
        ]
        row = evaluate.summarize(trades)[0]
        self.assertEqual(row["signals"], 3)
        self.assertEqual(row["win_rate_pct"], 66.67)
        self.assertEqual(row["median_return_pct"], 1.0)

    def test_evaluate_is_offline_and_reports_limitations(self):
        data = {"TEST": make_series([100 + i * 0.2 for i in range(45)])}
        report = evaluate.evaluate(data)
        self.assertEqual(report["symbols_loaded"], 1)
        self.assertFalse(report["trade_costs_included"])
        self.assertTrue(any("survivorship bias" in x for x in report["limitations"]))


if __name__ == "__main__":
    unittest.main()
