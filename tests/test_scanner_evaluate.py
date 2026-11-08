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
        "adjclose": [c * 0.98 for c in closes],
        "v": [100000] * len(closes),
        "meta": {}
    }


class Evaluation(unittest.TestCase):
    def test_next_session_open_entry_and_horizons(self):
        closes = [100 + i * 0.1 for i in range(40)]
        series = make_series(closes)
        trades, skipped = evaluate.evaluate_series(series)
        self.assertGreater(skipped, 0)  # latest signals lack the full 3- and 5-session look-ahead
        self.assertTrue(trades)
        one = next(t for t in trades if t["horizon"] == 1)
        self.assertEqual(one["entry_date"], one["exit_date"])
        self.assertGreater(one["entry_date"], one["signal_date"])
        self.assertGreater(one["entry"], 0)

    def test_incomplete_current_session_bar_is_excluded(self):
        closes = [100 + i * 0.1 for i in range(40)]
        series = make_series(closes)
        today = datetime.date(2025, 2, 10)  # Monday
        series["t"][-1] = int((scan.ist_to_utc(datetime.datetime.combine(
            today, datetime.time(0, 0))) - datetime.datetime(1970, 1, 1)).total_seconds())
        now_utc = scan.ist_to_utc(datetime.datetime.combine(
            today, datetime.time(10, 0)))
        trades, skipped = evaluate.evaluate_series(series, now_utc=now_utc)
        self.assertTrue(trades)
        self.assertTrue(all(t["signal_date"] < today.isoformat() for t in trades))
        self.assertTrue(all(t["entry_date"] < today.isoformat() for t in trades))
        self.assertTrue(all(t["exit_date"] < today.isoformat() for t in trades))

    def test_returns_use_adjusted_ohlc_for_dividends_and_splits(self):
        closes = [100 + i * 0.1 for i in range(40)]
        series = make_series(closes)
        trades, _ = evaluate.evaluate_series(series)
        one = next(t for t in trades if t["horizon"] == 1)
        start = datetime.date(2025, 1, 1)
        entry_i = (datetime.date.fromisoformat(one["entry_date"]) - start).days
        exit_i = (datetime.date.fromisoformat(one["exit_date"]) - start).days
        self.assertAlmostEqual(one["entry"], series["o"][entry_i] * 0.98)
        self.assertAlmostEqual(one["exit"], series["c"][exit_i] * 0.98)

    def test_missing_adjusted_close_is_rejected(self):
        closes = [100 + i * 0.1 for i in range(40)]
        series = make_series(closes)
        series.pop("adjclose")
        trades, skipped = evaluate.evaluate_series(series)
        self.assertEqual(trades, [])
        report = evaluate.evaluate({"TEST": series})
        self.assertEqual(report["symbols_loaded"], 0)
        self.assertEqual(report["symbols_rejected_missing_adjusted_close"], ["TEST"])

    def test_null_adjusted_close_rejects_entire_series(self):
        series = make_series([100 + i * 0.1 for i in range(40)])
        series["adjclose"][10] = None
        trades, skipped = evaluate.evaluate_series(series)
        self.assertEqual(trades, [])

    def test_adjustment_factor_changes_historical_return(self):
        series = make_series([100 + i * 0.1 for i in range(40)])
        series["adjclose"] = list(series["c"])
        series["adjclose"][32] *= 0.90
        trades, _ = evaluate.evaluate_series(series)
        three = next(t for t in trades if t["horizon"] == 3 and t["signal_date"] == "2025-01-30")
        self.assertAlmostEqual(three["exit"], series["adjclose"][32])
        self.assertAlmostEqual(three["return_pct"], (series["adjclose"][32] / series["o"][30] - 1) * 100)

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

    def test_round_trip_cost_is_subtracted_from_net_return_only(self):
        series = make_series([100 + i * 0.1 for i in range(40)])
        gross, _ = evaluate.evaluate_series(series)
        net, _ = evaluate.evaluate_series(series, round_trip_cost_pct=0.25)
        self.assertTrue(gross)
        self.assertEqual(len(gross), len(net))
        for before, after in zip(gross, net):
            self.assertAlmostEqual(before["return_pct"], after["return_pct"])
            self.assertAlmostEqual(after["net_return_pct"], before["return_pct"] - 0.25)

    def test_cost_assumption_is_reported_and_changes_net_summary(self):
        data = {"TEST": make_series([100 + i * 0.2 for i in range(45)])}
        report = evaluate.evaluate(data, round_trip_cost_pct=0.30)
        self.assertTrue(report["trade_costs_included"])
        self.assertEqual(report["round_trip_cost_assumption_pct"], 0.30)
        row = report["summary"][0]
        self.assertLessEqual(row["average_net_return_pct"], row["average_return_pct"])
        self.assertIn("net_win_rate_pct", row)

    def test_invalid_cost_assumptions_are_rejected(self):
        series = make_series([100 + i * 0.1 for i in range(40)])
        for invalid in (-0.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                evaluate.evaluate_series(series, round_trip_cost_pct=invalid)

    def test_evaluate_is_offline_and_reports_limitations(self):
        data = {"TEST": make_series([100 + i * 0.2 for i in range(45)])}
        report = evaluate.evaluate(data)
        self.assertEqual(report["symbols_loaded"], 1)
        self.assertFalse(report["trade_costs_included"])
        self.assertEqual(report["round_trip_cost_assumption_pct"], 0.0)
        self.assertTrue(any("survivorship bias" in x for x in report["limitations"]))


if __name__ == "__main__":
    unittest.main()
