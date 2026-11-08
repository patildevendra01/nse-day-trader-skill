#!/usr/bin/env python3
"""Offline, close-based evaluation of daily scanner setups. No network or orders."""
import argparse
import json
import math
import os
import sys
from collections import defaultdict

# Works both as a CLI beside scan.py and when loaded by the repository tests.
try:
    import scan
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scan


HORIZONS = (1, 3, 5)
SETUP_KEYS = tuple(key for key, _ in scan.SETUPS)


def evaluate_series(series, horizons=HORIZONS, now_utc=None):
    """Evaluate adjusted-close signals using next-session adjusted-open entry.

    Horizon 1 exits at the next session adjusted close; horizon 3/5 exit at
    that session's adjusted close. High/low excursion uses adjusted OHLC.
    Missing adjustment data rejects the series rather than bridging a gap.
    """
    if now_utc is None:
        now_utc = scan.parse_now(None)
    now_ist = scan.to_ist(now_utc)
    required = ("t", "o", "h", "l", "c", "adjclose")
    if any(not isinstance(series.get(k), list) for k in required):
        return [], 0
    if any(len(series[k]) < len(series["t"]) for k in required):
        return [], 0
    # Never bridge over a missing adjusted value: doing so would silently
    # change the meaning of "next session" and the holding-period horizon.
    for value in series["adjclose"][:len(series["t"])]:
        try:
            if value is None or not math.isfinite(float(value)) or float(value) <= 0:
                return [], 0
        except (TypeError, ValueError, OverflowError):
            return [], 0
    rows = {}
    for i, stamp in enumerate(series["t"]):
        try:
            day = scan.ts_to_ist(stamp).date()
            o, h, lo, c, adj = (series[k][i] for k in ("o", "h", "l", "c", "adjclose"))
            if any(x is None for x in (o, h, lo, c, adj)):
                return [], 0
            vals = tuple(float(x) for x in (o, h, lo, c, adj))
            if any(not math.isfinite(x) or x <= 0 for x in vals):
                return [], 0
            factor = vals[4] / vals[3]
            rows[day] = {"open": vals[0] * factor, "high": vals[1] * factor,
                         "low": vals[2] * factor, "close": vals[4]}
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
    # Yahoo may expose today's daily candle before the NSE session closes.
    # Exclude that partial candle so it cannot create a signal or outcome.
    today_session = scan.session(now_ist.date())
    if today_session and now_ist.time() < today_session[1]:
        rows.pop(now_ist.date(), None)

    days = sorted(rows)
    bars = [rows[d] for d in days]
    closes = []
    out = []
    skipped = 0
    for i, day in enumerate(days):
        closes.append(bars[i]["close"])
        if len(closes) < scan.MIN_DAILY_BARS:
            continue
        tags, _ = scan.find_setups(closes)
        if not tags:
            continue
        for setup in tags:
            for horizon in horizons:
                exit_i = i + horizon
                if exit_i >= len(bars):
                    skipped += 1
                    continue
                entry = bars[i + 1]["open"]
                if entry <= 0:
                    skipped += 1
                    continue
                held = bars[i + 1:exit_i + 1]
                exit_price = bars[exit_i]["close"]
                out.append({
                    "setup": setup,
                    "signal_date": day.isoformat(),
                    "entry_date": days[i + 1].isoformat(),
                    "exit_date": days[exit_i].isoformat(),
                    "horizon": horizon,
                    "entry": entry,
                    "exit": exit_price,
                    "return_pct": (exit_price / entry - 1.0) * 100.0,
                    "mfe_pct": (max(b["high"] for b in held) / entry - 1.0) * 100.0,
                    "mae_pct": (min(b["low"] for b in held) / entry - 1.0) * 100.0,
                })
    return out, skipped


def summarize(trades):
    groups = defaultdict(list)
    for item in trades:
        groups[(item["setup"], item["horizon"])].append(item["return_pct"])
    summary = []
    for (setup, horizon), values in sorted(groups.items()):
        ordered = sorted(values)
        n = len(ordered)
        mid = n // 2
        median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        summary.append({
            "setup": setup, "horizon_sessions": horizon, "signals": n,
            "win_rate_pct": round(sum(v > 0 for v in values) / n * 100, 2),
            "average_return_pct": round(sum(values) / n, 3),
            "median_return_pct": round(median, 3),
            "best_return_pct": round(max(values), 3),
            "worst_return_pct": round(min(values), 3),
        })
    return summary


def _valid_positive_number(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def evaluate(data, symbols=None, now_utc=None):
    symbols = list(symbols or sorted(data))
    all_trades, skipped, loaded = [], 0, 0
    missing_adjustments, invalid_adjustments = [], []
    for symbol in symbols:
        series = data.get(symbol)
        if not series:
            continue
        if not isinstance(series.get("adjclose"), list):
            missing_adjustments.append(symbol)
            continue
        if len(series["adjclose"]) < len(series.get("t") or []) or any(
                value is None or not _valid_positive_number(value)
                for value in series["adjclose"][:len(series.get("t") or [])]):
            invalid_adjustments.append(symbol)
            continue
        loaded += 1
        trades, n_skipped = evaluate_series(series, now_utc=now_utc)
        for trade in trades:
            trade["symbol"] = symbol
        all_trades.extend(trades)
        skipped += n_skipped
    return {
        "mode": "offline_daily_setup_evaluation",
        "symbols_requested": len(symbols),
        "symbols_loaded": loaded,
        "symbols_rejected_missing_adjusted_close": missing_adjustments,
        "symbols_rejected_incomplete_adjusted_close": invalid_adjustments,
        "corporate_action_adjustment": "Yahoo adjusted close; each OHLC bar scaled by adjusted-close / raw-close. This incorporates Yahoo's split and dividend adjustments when supplied by the feed.",
        "trade_costs_included": False,
        "entry_rule": "next available daily session adjusted open after the signal close",
        "exit_rule": "adjusted daily close after 1, 3 or 5 held sessions",
        "returns_are_predictions": False,
        "trades": len(all_trades),
        "skipped_insufficient_future_data": skipped,
        "summary": summarize(all_trades),
        "observations": all_trades,
        "limitations": [
            "Historical results do not predict future performance.",
            "Current symbol lists can create survivorship bias.",
            "Yahoo adjusted-close data is required; missing or incomplete adjustment arrays are rejected. Adjustment quality and historical corporate-action coverage still depend on Yahoo's feed.",
            "No fees, taxes, slippage, bid-ask spread or liquidity impact are included.",
            "This evaluates daily setups only; it is not an intraday opening-range backtest."
        ]
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline evaluation of NSE daily scanner setups.")
    ap.add_argument("--offline", required=True, help="saved Yahoo v8 daily chart bundle or folder")
    ap.add_argument("--symbols", help="comma-separated symbols; default is every chart in the bundle")
    ap.add_argument("--json", action="store_true", help="print the full machine-readable report")
    a = ap.parse_args(argv)
    try:
        data, errors = scan.load_offline(a.offline)
        symbols = [scan.bare(s) for s in a.symbols.split(",") if s.strip()] if a.symbols else sorted(data)
        if not symbols:
            raise scan.ScanError("no symbols to evaluate")
        report = evaluate(data, symbols)
        report["failed_charts"] = errors
        if a.json:
            print(json.dumps(report, indent=2))
        else:
            print("Offline daily setup evaluation (historical data; not a live signal).")
            print("Loaded %d of %d requested symbols; %d completed observations; %d skipped for insufficient future data."
                  % (report["symbols_loaded"], report["symbols_requested"], report["trades"],
                     report["skipped_insufficient_future_data"]))
            print("Entry: next session adjusted open. Exit: adjusted close after 1, 3 or 5 held sessions. Costs are NOT included.")
            if report["symbols_rejected_missing_adjusted_close"]:
                print("Rejected (no adjusted-close data): %s" % ", ".join(report["symbols_rejected_missing_adjusted_close"]))
            if report["symbols_rejected_incomplete_adjusted_close"]:
                print("Rejected (incomplete adjusted-close data): %s" % ", ".join(report["symbols_rejected_incomplete_adjusted_close"]))
            if not report["summary"]:
                print("No evaluable signals in this data.")
            for row in report["summary"]:
                print("%-18s %d-session  n=%-5d win=%5.1f%% avg=%+7.3f%% median=%+7.3f%% worst=%+7.3f%%"
                      % (row["setup"], row["horizon_sessions"], row["signals"], row["win_rate_pct"],
                         row["average_return_pct"], row["median_return_pct"], row["worst_return_pct"]))
            print("Caution: historical results do not predict future performance; fees, taxes and slippage are excluded.")
            if errors:
                print("%d charts failed to load." % len(errors))
        return 0 if report["trades"] else 1
    except scan.ScanError as exc:
        print("evaluate.py: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
