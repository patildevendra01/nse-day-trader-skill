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


def _is_zero_volume_flat_placeholder(series, index):
    """Identify provider placeholder rows with zero volume and a flat OHLC candle.

    These rows are not treated as exchange sessions. Raw downloaded data is
    left untouched; only evaluation and quality audits ignore the row.
    """
    volumes = series.get("v")
    if not isinstance(volumes, list) or index >= len(volumes):
        return False
    try:
        volume = float(volumes[index])
        o, h, lo, close = (float(series[key][index]) for key in ("o", "h", "l", "c"))
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return False
    return (math.isfinite(volume) and volume == 0 and
            all(math.isfinite(x) for x in (o, h, lo, close)) and
            o == h == lo == close)


def evaluate_series(series, horizons=HORIZONS, now_utc=None, round_trip_cost_pct=0.0):
    """Evaluate adjusted-close signals using next-session adjusted-open entry.

    Horizon 1 exits at the next session adjusted close; horizon 3/5 exit at
    that session's adjusted close. High/low excursion uses adjusted OHLC.
    Missing adjustment data rejects the series rather than bridging a gap.
    """
    if not _valid_nonnegative_number(round_trip_cost_pct):
        raise ValueError("round_trip_cost_pct must be a finite, non-negative number")
    round_trip_cost_pct = float(round_trip_cost_pct)
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
        if _is_zero_volume_flat_placeholder(series, i):
            continue
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
                    "net_return_pct": (exit_price / entry - 1.0) * 100.0 - round_trip_cost_pct,
                    "mfe_pct": (max(b["high"] for b in held) / entry - 1.0) * 100.0,
                    "mae_pct": (min(b["low"] for b in held) / entry - 1.0) * 100.0,
                })
    return out, skipped


def summarize(trades):
    groups = defaultdict(list)
    for item in trades:
        groups[(item["setup"], item["horizon"])].append(item)
    summary = []
    for (setup, horizon), values in sorted(groups.items()):
        gross = [item["return_pct"] for item in values]
        net = [item.get("net_return_pct", item["return_pct"]) for item in values]
        ordered = sorted(gross)
        ordered_net = sorted(net)
        n = len(ordered)
        mid = n // 2
        median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        net_median = ordered_net[mid] if n % 2 else (ordered_net[mid - 1] + ordered_net[mid]) / 2
        sample_status = ("very_small" if n < 30 else
                         "limited" if n < 100 else "larger_sample")
        summary.append({
            "setup": setup, "horizon_sessions": horizon, "signals": n,
            "sample_status": sample_status,
            "sample_warning": (
                "Very small sample: do not draw performance conclusions." if n < 30 else
                "Limited sample: results are uncertain and need more observations." if n < 100 else
                "Larger descriptive sample, but not proof of statistical significance or future performance."
            ),
            "win_rate_pct": round(sum(v > 0 for v in gross) / n * 100, 2),
            "average_return_pct": round(sum(gross) / n, 3),
            "median_return_pct": round(median, 3),
            "best_return_pct": round(max(gross), 3),
            "worst_return_pct": round(min(gross), 3),
            "net_win_rate_pct": round(sum(v > 0 for v in net) / n * 100, 2),
            "average_net_return_pct": round(sum(net) / n, 3),
            "median_net_return_pct": round(net_median, 3),
            "best_net_return_pct": round(max(net), 3),
            "worst_net_return_pct": round(min(net), 3),
        })
    return summary


def _valid_nonnegative_number(value):
    try:
        return math.isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_positive_number(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def inspect_series_quality(series):
    """Inspect daily OHLC/adjustment integrity without claiming source completeness."""
    import datetime
    errors = []
    gaps = []
    if not isinstance(series, dict):
        return {"status": "rejected", "bars_checked": 0,
                "errors": ["chart is not an object"], "possible_date_gaps": []}
    timestamps = series.get("t")
    if not isinstance(timestamps, list) or not timestamps:
        return {"status": "rejected", "bars_checked": 0,
                "errors": ["missing or empty timestamp array"], "possible_date_gaps": []}
    keys = ("o", "h", "l", "c", "adjclose")
    arrays = {}
    for key in keys:
        value = series.get(key)
        if not isinstance(value, list):
            errors.append("missing %s array" % key)
        elif len(value) != len(timestamps):
            errors.append("%s length does not match timestamps" % key)
        else:
            arrays[key] = value
    dates = []
    ignored_placeholders = 0
    checked_bars = 0
    for i, stamp in enumerate(timestamps):
        if _is_zero_volume_flat_placeholder(series, i):
            ignored_placeholders += 1
            continue
        checked_bars += 1
        try:
            if isinstance(stamp, bool):
                raise ValueError("boolean timestamp")
            day = scan.ts_to_ist(stamp).date()
        except (TypeError, ValueError, OverflowError):
            errors.append("invalid timestamp at row %d" % (i + 1))
            continue
        dates.append(day)
        if i and dates[-1] <= dates[-2]:
            errors.append("timestamps are duplicated or out of chronological order at row %d" % (i + 1))
        if not all(key in arrays for key in keys):
            continue
        try:
            o, h, lo, c, adj = (float(arrays[key][i]) for key in keys)
        except (TypeError, ValueError, OverflowError):
            errors.append("non-numeric or null OHLC/adjusted-close value at row %d" % (i + 1))
            continue
        if any(not math.isfinite(x) or x <= 0 for x in (o, h, lo, c, adj)):
            errors.append("non-finite or non-positive OHLC/adjusted-close value at row %d" % (i + 1))
            continue
        if h < max(o, c, lo) or lo > min(o, c, h):
            errors.append("inconsistent OHLC high/low at row %d" % (i + 1))
    # A gap of four or more calendar days can contain a missing weekday session.
    # It is only a warning: historical exchange holidays are not fully represented here.
    ordered_dates = sorted(set(dates))
    for previous, current in zip(ordered_dates, ordered_dates[1:]):
        calendar_days = (current - previous).days
        if calendar_days >= 4:
            gaps.append({"after": previous.isoformat(), "before": current.isoformat(),
                         "calendar_days_apart": calendar_days,
                         "warning": "possible missing session; verify exchange holidays"})
    return {
        "status": "review_required" if errors or gaps else "no_detectable_issue",
        "bars_checked": checked_bars,
        "zero_volume_flat_placeholders_ignored": ignored_placeholders,
        "errors": sorted(set(errors)),
        "possible_date_gaps": gaps,
    }



def load_trading_calendar(path):
    """Load a complete daily NSE equities calendar; never infer missing years."""
    import csv
    import datetime
    calendar = {}
    years = set()
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"date", "status", "description"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise scan.ScanError("trading calendar CSV needs date,status,description columns")
            for line, row in enumerate(reader, 2):
                date_text = (row.get("date") or "").strip()
                status = (row.get("status") or "").strip().lower()
                if not date_text or status not in ("trading", "holiday", "weekend", "special"):
                    raise scan.ScanError("trading calendar row %d has a blank date or invalid status" % line)
                try:
                    day = datetime.date.fromisoformat(date_text)
                except ValueError:
                    raise scan.ScanError("trading calendar row %d has an invalid ISO date" % line)
                if day.isoformat() != date_text:
                    raise scan.ScanError("trading calendar row %d date must use YYYY-MM-DD" % line)
                if day in calendar:
                    raise scan.ScanError("duplicate trading calendar date: %s" % date_text)
                if status == "trading" and day.weekday() >= 5:
                    raise scan.ScanError("weekend date must use special or weekend status: %s" % date_text)
                if status == "weekend" and day.weekday() < 5:
                    raise scan.ScanError("weekday date cannot use weekend status: %s" % date_text)
                calendar[day] = {
                    "status": status,
                    "description": (row.get("description") or "").strip(),
                }
                years.add(day.year)
    except OSError as exc:
        raise scan.ScanError("cannot read trading calendar CSV: %s" % exc)
    if not calendar:
        raise scan.ScanError("trading calendar CSV contains no dates")
    for year in years:
        first = datetime.date(year, 1, 1)
        last = datetime.date(year, 12, 31)
        expected_days = (last - first).days + 1
        present_days = sum(1 for day in calendar if day.year == year)
        if present_days != expected_days:
            raise scan.ScanError(
                "trading calendar year %d is incomplete: expected %d daily rows, found %d"
                % (year, expected_days, present_days))
    return calendar


def audit_trading_sessions(series, calendar):
    """Compare observed bars with a complete supplied equities calendar."""
    import datetime
    dates = []
    for i, stamp in enumerate(series.get("t") or []):
        if _is_zero_volume_flat_placeholder(series, i):
            continue
        try:
            dates.append(scan.ts_to_ist(stamp).date())
        except (TypeError, ValueError, OverflowError):
            continue
    if not dates:
        return {"status": "not_checked", "missing_trading_sessions": [],
                "bars_on_closed_dates": [], "calendar_coverage": "no_valid_chart_dates"}
    start, end = min(dates), max(dates)
    required_years = set(range(start.year, end.year + 1))
    available_years = {day.year for day in calendar}
    missing_years = sorted(required_years - available_years)
    if missing_years:
        return {
            "status": "not_checked",
            "chart_date_start": start.isoformat(),
            "chart_date_end": end.isoformat(),
            "missing_calendar_years": missing_years,
            "missing_trading_sessions": [],
            "bars_on_closed_dates": [],
            "calendar_coverage": "incomplete",
        }
    observed = set(dates)
    expected = sorted(
        day for day, item in calendar.items()
        if start <= day <= end and item["status"] in ("trading", "special")
    )
    missing = [day.isoformat() for day in expected if day not in observed]
    unexpected = sorted(
        day.isoformat() for day in observed
        if day in calendar and calendar[day]["status"] in ("holiday", "weekend")
    )
    return {
        "status": "review_required" if missing or unexpected else "no_detectable_gap",
        "chart_date_start": start.isoformat(),
        "chart_date_end": end.isoformat(),
        "calendar_coverage": "complete_for_chart_years",
        "missing_calendar_years": [],
        "expected_trading_sessions": len(expected),
        "observed_trading_sessions": len(observed),
        "missing_trading_sessions": missing,
        "bars_on_closed_dates": unexpected,
        "warning": "A missing stock bar can also reflect suspension, no trades, or a source omission; verify flagged dates before treating them as missing vendor data."
    }


def load_universe_history(path):
    """Read inclusive point-in-time membership intervals from a CSV file."""
    import csv
    import datetime
    intervals = defaultdict(list)
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"symbol", "effective_from", "effective_to"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise scan.ScanError("universe history CSV needs symbol,effective_from,effective_to columns")
            for line, row in enumerate(reader, 2):
                symbol = (row.get("symbol") or "").strip().upper()
                if symbol.endswith(".NS"):
                    symbol = symbol[:-3]
                start_text = (row.get("effective_from") or "").strip()
                end_text = (row.get("effective_to") or "").strip()
                if not symbol or not start_text:
                    raise scan.ScanError("universe history row %d has a blank symbol or effective_from" % line)
                try:
                    start = datetime.date.fromisoformat(start_text)
                    end = datetime.date.fromisoformat(end_text) if end_text else None
                except ValueError:
                    raise scan.ScanError("universe history row %d has an invalid ISO date" % line)
                if start.isoformat() != start_text or (end_text and end.isoformat() != end_text):
                    raise scan.ScanError("universe history row %d dates must use YYYY-MM-DD" % line)
                if end is not None and end < start:
                    raise scan.ScanError("universe history row %d ends before it starts" % line)
                intervals[symbol].append((start, end))
    except OSError as exc:
        raise scan.ScanError("cannot read universe history CSV: %s" % exc)
    for symbol, spans in intervals.items():
        spans.sort(key=lambda span: span[0])
        previous_end = None
        for index, (start, end) in enumerate(spans):
            if index and (previous_end is None or start <= previous_end):
                raise scan.ScanError("overlapping universe history intervals for %s" % symbol)
            previous_end = end
    if not intervals:
        raise scan.ScanError("universe history CSV contains no membership rows")
    return dict(intervals)


def _is_member(history, symbol, signal_date):
    import datetime
    day = datetime.date.fromisoformat(signal_date)
    return any(start <= day and (end is None or day <= end)
               for start, end in history.get(symbol, []))


def universe_history_quality(history, data, requested_symbols):
    """Report detectable per-symbol membership/chart coverage gaps; never certify completeness."""
    import datetime
    requested = {scan.bare(symbol) for symbol in requested_symbols}
    loaded = requested.intersection(data)
    history_symbols = set(history)
    missing_history = sorted(loaded - history_symbols)
    missing_charts = sorted((history_symbols & requested) - set(data))
    chart_ranges = {}
    chart_days = []
    for symbol in loaded:
        series = data.get(symbol) or {}
        symbol_days = []
        for stamp in series.get("t") or []:
            try:
                symbol_days.append(scan.ts_to_ist(stamp).date())
            except (TypeError, ValueError, OverflowError):
                continue
        if symbol_days:
            chart_ranges[symbol] = (min(symbol_days), max(symbol_days))
            chart_days.extend(symbol_days)

    chart_start = min(chart_days) if chart_days else None
    chart_end = max(chart_days) if chart_days else None
    out_of_range = []
    per_symbol_gaps = []
    for symbol, spans in history.items():
        if symbol not in requested:
            continue
        symbol_range = chart_ranges.get(symbol)
        for start, end in spans:
            if chart_start and (start > chart_end or (end is not None and end < chart_start)):
                out_of_range.append({"symbol": symbol, "effective_from": start.isoformat(),
                                     "effective_to": end.isoformat() if end else ""})
            if symbol_range is None:
                continue
            symbol_start, symbol_end = symbol_range
            if start < symbol_start:
                per_symbol_gaps.append({
                    "symbol": symbol, "gap_side": "before_chart",
                    "missing_from": start.isoformat(),
                    "missing_to": (min(end, symbol_start - datetime.timedelta(days=1))
                                   if end is not None else symbol_start - datetime.timedelta(days=1)).isoformat(),
                    "chart_date_start": symbol_start.isoformat(),
                    "chart_date_end": symbol_end.isoformat(),
                })
            if end is None or end > symbol_end:
                gap_start = max(start, symbol_end + datetime.timedelta(days=1))
                if end is None or gap_start <= end:
                    per_symbol_gaps.append({
                        "symbol": symbol, "gap_side": "after_chart",
                        "missing_from": gap_start.isoformat(),
                        "missing_to": end.isoformat() if end else None,
                        "chart_date_start": symbol_start.isoformat(),
                        "chart_date_end": symbol_end.isoformat(),
                    })
    return {
        "status": "review_required" if missing_history or missing_charts or out_of_range or per_symbol_gaps else "no_detectable_gap",
        "chart_date_start": chart_start.isoformat() if chart_start else None,
        "chart_date_end": chart_end.isoformat() if chart_end else None,
        "loaded_symbols_without_membership_history": missing_history,
        "requested_membership_symbols_without_chart_data": missing_charts,
        "membership_intervals_outside_chart_date_range": out_of_range,
        "per_symbol_chart_coverage_gaps": per_symbol_gaps,
        "completeness_verified": False,
        "warning": "This checks detectable per-symbol file and date-range gaps only; it cannot prove the membership history or chart universe is complete or accurate."
    }


def evaluate(data, symbols=None, now_utc=None, round_trip_cost_pct=0.0, universe_history=None, trading_calendar=None):
    if not _valid_nonnegative_number(round_trip_cost_pct):
        raise ValueError("round_trip_cost_pct must be a finite, non-negative number")
    round_trip_cost_pct = float(round_trip_cost_pct)
    symbols = list(symbols or sorted(data))
    all_trades, skipped, loaded = [], 0, 0
    missing_adjustments, invalid_adjustments = [], []
    data_quality = {}
    rejected_data_quality = []
    excluded_by_universe = 0
    if universe_history is not None and not isinstance(universe_history, dict):
        universe_history = load_universe_history(universe_history)
    history_quality = (universe_history_quality(universe_history, data, symbols)
                       if universe_history is not None else None)
    if isinstance(trading_calendar, (str, os.PathLike)):
        trading_calendar = load_trading_calendar(trading_calendar)
    session_quality = {}
    for symbol in symbols:
        series = data.get(symbol)
        if not series:
            continue
        if trading_calendar is not None:
            session_quality[symbol] = audit_trading_sessions(series, trading_calendar)
        if not isinstance(series.get("adjclose"), list):
            missing_adjustments.append(symbol)
            continue
        if len(series["adjclose"]) < len(series.get("t") or []) or any(
                value is None or not _valid_positive_number(value)
                for value in series["adjclose"][:len(series.get("t") or [])]):
            invalid_adjustments.append(symbol)
            continue
        quality = inspect_series_quality(series)
        data_quality[symbol] = quality
        if quality["errors"]:
            rejected_data_quality.append(symbol)
            continue
        loaded += 1
        trades, n_skipped = evaluate_series(series, now_utc=now_utc,
                                            round_trip_cost_pct=round_trip_cost_pct)
        for trade in trades:
            if universe_history is not None and not _is_member(universe_history, symbol, trade["signal_date"]):
                excluded_by_universe += 1
                continue
            trade["symbol"] = symbol
            all_trades.append(trade)
        skipped += n_skipped
    return {
        "mode": "offline_daily_setup_evaluation",
        "symbols_requested": len(symbols),
        "symbols_loaded": loaded,
        "symbols_rejected_missing_adjusted_close": missing_adjustments,
        "symbols_rejected_incomplete_adjusted_close": invalid_adjustments,
        "symbols_rejected_data_quality": rejected_data_quality,
        "data_quality": data_quality,
        "symbols_with_possible_date_gaps": sorted(
            symbol for symbol, quality in data_quality.items() if quality["possible_date_gaps"]),
        "corporate_action_adjustment": "Yahoo adjusted close; each OHLC bar scaled by adjusted-close / raw-close. This incorporates Yahoo's split and dividend adjustments when supplied by the feed.",
        "trade_costs_included": round_trip_cost_pct > 0,
        "round_trip_cost_assumption_pct": round_trip_cost_pct,
        "net_returns_account_for_cost_assumption": True,
        "point_in_time_universe_applied": universe_history is not None,
        "universe_history_symbols": sorted(universe_history) if universe_history is not None else [],
        "universe_history_quality": history_quality,
        "trading_calendar_applied": trading_calendar is not None,
        "trading_session_quality": session_quality,
        "observations_excluded_outside_historical_universe": excluded_by_universe,
        "entry_rule": "next available daily session adjusted open after the signal close",
        "exit_rule": "adjusted daily close after 1, 3 or 5 held sessions",
        "returns_are_predictions": False,
        "trades": len(all_trades),
        "skipped_insufficient_future_data": skipped,
        "summary": summarize(all_trades),
        "observations": all_trades,
        "limitations": [
            "Historical results do not predict future performance.",
            ("A dated membership CSV was applied to signal dates; its completeness and accuracy are not independently verified." if universe_history is not None else "No point-in-time membership file was supplied. Current-symbol data can create survivorship bias; results are not survivorship-bias corrected."),
            "Yahoo adjusted-close data is required; missing or incomplete adjustment arrays are rejected. Adjustment quality and historical corporate-action coverage still depend on Yahoo's feed.",
            "Net returns subtract only the user-supplied all-in round-trip cost assumption; actual brokerage, taxes, spread, slippage and liquidity impact can differ by broker, order type, price and date.",
            "This evaluates daily setups only; it is not an intraday opening-range backtest."
        ]
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline evaluation of NSE daily scanner setups.")
    ap.add_argument("--offline", required=True, help="saved Yahoo v8 daily chart bundle or folder")
    ap.add_argument("--symbols", help="comma-separated symbols; default is every chart in the bundle")
    ap.add_argument("--json", action="store_true", help="print the full machine-readable report")
    ap.add_argument("--universe-history", help="CSV with symbol,effective_from,effective_to membership intervals; dates inclusive, blank end means still a member")
    ap.add_argument("--trading-calendar", help="complete daily NSE equities calendar CSV with date,status,description; include every date in each covered year")
    ap.add_argument("--round-trip-cost-pct", type=float, default=0.0,
                    help="user-supplied all-in round-trip cost as %% of entry value; includes estimated fees, taxes and slippage")
    a = ap.parse_args(argv)
    try:
        data, errors = scan.load_offline(a.offline)
        symbols = [scan.bare(s) for s in a.symbols.split(",") if s.strip()] if a.symbols else sorted(data)
        if not symbols:
            raise scan.ScanError("no symbols to evaluate")
        if not _valid_nonnegative_number(a.round_trip_cost_pct):
            raise scan.ScanError("--round-trip-cost-pct must be a finite, non-negative number")
        report = evaluate(data, symbols, round_trip_cost_pct=a.round_trip_cost_pct,
                          universe_history=a.universe_history, trading_calendar=a.trading_calendar)
        report["failed_charts"] = errors
        if a.json:
            print(json.dumps(report, indent=2))
        else:
            print("Offline daily setup evaluation (historical data; not a live signal).")
            print("Loaded %d of %d requested symbols; %d completed observations; %d skipped for insufficient future data."
                  % (report["symbols_loaded"], report["symbols_requested"], report["trades"],
                     report["skipped_insufficient_future_data"]))
            print("Entry: next session adjusted open. Exit: adjusted close after 1, 3 or 5 held sessions.")
            print("User-supplied all-in round-trip cost assumption: %.4f%% of entry value." % a.round_trip_cost_pct)
            if a.universe_history:
                print("Point-in-time universe membership applied from: %s" % a.universe_history)
                print("Observations excluded outside dated membership: %d" % report["observations_excluded_outside_historical_universe"])
                quality = report["universe_history_quality"]
                print("Membership-data quality check: %s (completeness is not verified)." % quality["status"])
                if quality["loaded_symbols_without_membership_history"]:
                    print("Loaded symbols missing from membership CSV: %s" % ", ".join(quality["loaded_symbols_without_membership_history"]))
                if quality["requested_membership_symbols_without_chart_data"]:
                    print("Requested historical members missing chart data: %s" % ", ".join(quality["requested_membership_symbols_without_chart_data"]))
                if quality["membership_intervals_outside_chart_date_range"]:
                    print("Membership intervals outside combined chart dates: %d" % len(quality["membership_intervals_outside_chart_date_range"]))
                if quality["per_symbol_chart_coverage_gaps"]:
                    print("WARNING: %d per-symbol membership/chart coverage gap(s); inspect JSON for affected symbols and dates."
                          % len(quality["per_symbol_chart_coverage_gaps"]))
            else:
                print("WARNING: no point-in-time universe supplied; results may have survivorship bias.")
            if a.trading_calendar:
                print("NSE equities trading calendar applied: %s" % a.trading_calendar)
                for symbol, quality in report["trading_session_quality"].items():
                    if quality["status"] == "not_checked":
                        print("WARNING: %s session audit not checked; calendar coverage missing for years %s."
                              % (symbol, quality.get("missing_calendar_years", [])))
                    elif quality["missing_trading_sessions"] or quality["bars_on_closed_dates"]:
                        print("WARNING: %s session audit found %d missing expected session(s) and %d bar(s) on closed dates."
                              % (symbol, len(quality["missing_trading_sessions"]),
                                 len(quality["bars_on_closed_dates"])))
            else:
                print("WARNING: no official trading calendar supplied; four-day date gaps remain approximate.")
            if report["symbols_rejected_missing_adjusted_close"]:
                print("Rejected (no adjusted-close data): %s" % ", ".join(report["symbols_rejected_missing_adjusted_close"]))
            if report["symbols_rejected_incomplete_adjusted_close"]:
                print("Rejected (incomplete adjusted-close data): %s" % ", ".join(report["symbols_rejected_incomplete_adjusted_close"]))
            if report["symbols_rejected_data_quality"]:
                print("Rejected (invalid OHLC, timestamps or adjusted-close structure): %s" % ", ".join(report["symbols_rejected_data_quality"]))
            for symbol, quality in report["data_quality"].items():
                if quality["possible_date_gaps"]:
                    print("WARNING: %s has %d possible date gap(s); verify holidays and missing sessions."
                          % (symbol, len(quality["possible_date_gaps"])))
            if not report["summary"]:
                print("No evaluable signals in this data.")
            for row in report["summary"]:
                print("%-18s %d-session n=%-5d sample=%-13s gross avg=%+7.3f%% net avg=%+7.3f%% net win=%5.1f%%"
                      % (row["setup"], row["horizon_sessions"], row["signals"], row["sample_status"],
                         row["average_return_pct"], row["average_net_return_pct"], row["net_win_rate_pct"]))
                print("  Sample caution: %s" % row["sample_warning"])
            print("Caution: historical results do not predict future performance; net returns subtract only the supplied cost assumption, not verified broker charges.")
            if errors:
                print("%d charts failed to load." % len(errors))
        return 0 if report["trades"] else 1
    except (scan.ScanError, ValueError) as exc:
        print("evaluate.py: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
