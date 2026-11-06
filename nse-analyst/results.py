#!/usr/bin/env python3
"""Quarterly results reader for nse-analyst. Standard library only.

Lays out what changed in a listed company's reported quarterly numbers:
revenue, gross, operating and net margins, net profit and diluted EPS,
quarter by quarter, each against the same quarter a year earlier (YoY) and
the quarter before (QoQ), plus the trailing four quarters. Money is in
rupees crore (1 crore = 10 million). Quarters carry Indian fiscal labels
(April to March): the quarter ending 30 Sep 2026 is Q2 FY27.

    python3 results.py RELIANCE
    python3 results.py RELIANCE TCS "M&M" --quarters 8
    python3 results.py --file tcs-results.csv --symbol TCS     # numbers you copied from the exchange filing
    python3 results.py RELIANCE TCS --metrics nse/metrics.json
    python3 results.py RELIANCE --json

Sources:
  - Yahoo Finance fundamentals-timeseries (free, unofficial, best effort; it
    usually carries only the last four or five quarters):
    https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<SYMBOL>.NS
        ?type=quarterlyTotalRevenue,quarterlyNetIncome,quarterlyOperatingIncome,
              quarterlyDilutedEPS,quarterlyGrossProfit&merge=false&period1=..&period2=..
  - --file: a JSON or CSV of results you paste from the company's filing on
    NSE or BSE. CSV columns: quarter_end, revenue_cr, gross_profit_cr,
    operating_income_cr, net_income_cr, eps_diluted. JSON: {"symbol": "TCS",
    "quarters": [{"quarter_end": "2026-09-30", "revenue_cr": 65800, ...}]}.
    Filed numbers beat Yahoo's: use this route whenever you can.

Offline: --offline DIR reads timeseries-<SYMBOL>.json from DIR (the tests use
it). Verified offline against fixtures; the live endpoint was not exercised
when this was written.
"""

import argparse
import csv
import datetime as _dt
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TS_URL = ("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
          "?type=quarterlyTotalRevenue,quarterlyNetIncome,quarterlyOperatingIncome,quarterlyDilutedEPS,"
          "quarterlyGrossProfit&merge=false&period1=%d&period2=%d")
UA = "Mozilla/5.0 (compatible; nse-day-trader-skill/1.0)"
CRORE = 1e7
YAHOO_TYPES = {"quarterlyTotalRevenue": "revenue", "quarterlyGrossProfit": "gross_profit",
               "quarterlyOperatingIncome": "operating_income", "quarterlyNetIncome": "net_income",
               "quarterlyDilutedEPS": "eps_diluted"}
MONEY = ("revenue", "gross_profit", "operating_income", "net_income")
FIELDS = MONEY + ("eps_diluted",)


class ResultsError(Exception):
    pass


def bare(sym):
    s = str(sym or "").strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    return s


def yahoo_symbol(sym):
    return urllib.parse.quote(bare(sym) + ".NS", safe="-")


def inr(x, dec=2):
    """1234567.5 -> '12,34,567.50' (lakh / crore grouping), no rupee sign."""
    s = "%.*f" % (dec, abs(x))
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if x < 0 else "") + whole + ("." + frac if frac else "")


def fiscal_label(end):
    """2026-09-30 -> 'Q2 FY27' (Indian fiscal year, April to March)."""
    d = _dt.date.fromisoformat(end)
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    if q is None:                      # an odd quarter end: place it by month
        q = ((d.month - 4) % 12) // 3 + 1
    fy = d.year + 1 if d.month >= 4 else d.year
    return "Q%d FY%02d" % (q, fy % 100)


def _date(s):
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- inputs

def parse_yahoo(text, symbol=""):
    """Yahoo fundamentals-timeseries JSON -> {quarter_end: {field: value}} (money in crore)."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise ResultsError("Yahoo's answer for %s was not JSON" % symbol)
    ts = doc.get("timeseries") or {}
    if ts.get("error"):
        raise ResultsError("Yahoo error for %s: %s" % (symbol, (ts["error"] or {}).get("description", ts["error"])))
    out = {}
    for item in ts.get("result") or []:
        types = (item.get("meta") or {}).get("type") or []
        for typ in types:
            field = YAHOO_TYPES.get(typ)
            if not field:
                continue
            for pt in item.get(typ) or []:
                if not isinstance(pt, dict):
                    continue
                end = _date(pt.get("asOfDate"))
                raw = (pt.get("reportedValue") or {}).get("raw")
                if end is None or not isinstance(raw, (int, float)):
                    continue
                if str(pt.get("periodType", "3M")).upper() not in ("3M", "Q"):
                    continue
                out.setdefault(end.isoformat(), {})[field] = raw if field == "eps_diluted" else raw / CRORE
    if not out:
        raise ResultsError("no quarterly results for %s in Yahoo's answer (try --file with the filing's numbers)"
                           % symbol)
    return out


FILE_KEYS = {"quarterend": "end", "period": "end", "date": "end", "revenue": "revenue", "revenuecr": "revenue",
             "totalincome": "revenue", "grossprofit": "gross_profit", "grossprofitcr": "gross_profit",
             "operatingincome": "operating_income", "operatingincomecr": "operating_income",
             "operatingprofit": "operating_income", "operatingprofitcr": "operating_income",
             "netincome": "net_income", "netincomecr": "net_income", "netprofit": "net_income",
             "netprofitcr": "net_income", "epsdiluted": "eps_diluted", "eps": "eps_diluted",
             "dilutedeps": "eps_diluted"}


def _num(x):
    if x is None or x == "":
        return None
    t = re.sub(r"[,\s]", "", str(x))
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse_file(text, name="results"):
    """A pasted JSON or CSV of results -> (symbol or None, {quarter_end: {field: value}}). Money in crore."""
    text = text.lstrip("﻿")
    symbol, rows = None, []
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise ResultsError("%s is not valid JSON: %s" % (name, exc))
        if isinstance(doc, dict):
            symbol = doc.get("symbol")
            rows = doc.get("quarters") or []
        else:
            rows = doc
        rows = [dict(r) for r in rows if isinstance(r, dict)]
    else:
        rows = list(csv.DictReader(io.StringIO(text)))
    out = {}
    for r in rows:
        rec = {}
        for k, v in r.items():
            f = FILE_KEYS.get(re.sub(r"[^a-z]", "", str(k).lower()))
            if f:
                rec[f] = v
        end = _date(rec.get("end"))
        if end is None:
            continue
        vals = {f: _num(rec.get(f)) for f in FIELDS if _num(rec.get(f)) is not None}
        if vals:
            out[end.isoformat()] = vals
    if not out:
        raise ResultsError("%s has no usable quarters (need quarter_end plus revenue_cr, net_income_cr ...)" % name)
    return symbol, out


# ---------------------------------------------------------------- analysis

def pct(a, b):
    """Percent change from b to a, one decimal; None when it is not meaningful."""
    if a is None or b is None or b == 0:
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def _near(quarters, end, days, slack):
    target = _date(end) - _dt.timedelta(days=days)
    best = None
    for e in quarters:
        gap = abs((_date(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return best[1] if best else None


def analyse(quarters, quarters_shown=6, name=None):
    """quarters: {end: {field: value}} -> rows newest first with growth, margins and the TTM."""
    ends = sorted(quarters, reverse=True)
    rows = []
    for e in ends[:quarters_shown]:
        cur = quarters[e]
        yo = quarters.get(_near(quarters, e, 365, 12) or "", {})
        qo = quarters.get(_near(quarters, e, 91, 14) or "", {})
        row = {"end": e, "fiscal": fiscal_label(e)}
        for f in FIELDS:
            row[f] = cur.get(f)
        for f in ("revenue", "net_income", "eps_diluted"):
            row[f + "_yoy"] = pct(cur.get(f), yo.get(f))
            row[f + "_qoq"] = pct(cur.get(f), qo.get(f))
        row["operating_income_yoy"] = pct(cur.get("operating_income"), yo.get("operating_income"))
        row["gross_margin"] = margin(cur.get("gross_profit"), cur.get("revenue"))
        row["operating_margin"] = margin(cur.get("operating_income"), cur.get("revenue"))
        row["net_margin"] = margin(cur.get("net_income"), cur.get("revenue"))
        row["gross_margin_change"] = _change(quarters, e, yo, "gross_profit")
        row["operating_margin_change"] = _change(quarters, e, yo, "operating_income")
        row["net_margin_change"] = _change(quarters, e, yo, "net_income")
        rows.append(row)
    ttm = {}
    last4 = ends[:4]
    if len(last4) == 4 and (_date(last4[0]) - _date(last4[3])).days <= 290:
        for f in FIELDS:
            vals = [quarters[e].get(f) for e in last4]
            if all(v is not None for v in vals):
                ttm[f] = round(sum(vals), 2)
    notes = []
    if rows and rows[0]["revenue_yoy"] is None and rows[0]["net_income_yoy"] is None:
        notes.append("No quarter from a year earlier in the data (%d quarter(s) in all), so YoY growth is "
                     "missing; paste more with --file." % len(ends))
    if rows and rows[0]["net_income"] is not None and rows[0]["operating_income"] is not None \
            and rows[0]["net_income"] > rows[0]["operating_income"]:
        notes.append("Net profit is above operating profit: other income, exceptional gains or tax items are "
                     "inside it. Read the filing's notes before calling it growth.")
    if rows and rows[0]["gross_profit"] is None:
        notes.append("No gross profit reported (banks, NBFCs and many Indian companies do not show it): "
                     "gross margin is blank, not zero.")
    return {"entity": name, "rows": rows, "ttm": ttm, "notes": notes}


def _change(quarters, end, yo, field):
    cur = quarters[end]
    a = margin(cur.get(field), cur.get("revenue"))
    b = margin(yo.get(field), yo.get("revenue")) if yo else None
    return round(a - b, 1) if a is not None and b is not None else None


def changes(result):
    """Plain sentences about the latest quarter."""
    if not result["rows"]:
        return []
    r = result["rows"][0]
    out = []
    if r["revenue"] is not None:
        s = "%s (quarter ended %s): revenue Rs %s crore" % (r["fiscal"], r["end"], inr(r["revenue"], 0))
        if r["revenue_yoy"] is not None:
            s += ", %+.1f%% YoY" % r["revenue_yoy"]
        if r["revenue_qoq"] is not None:
            s += ", %+.1f%% QoQ" % r["revenue_qoq"]
        out.append(s + ".")
    if r["net_income"] is not None:
        s = "Net profit Rs %s crore" % inr(r["net_income"], 0)
        if r["net_income_yoy"] is not None:
            s += ", %+.1f%% YoY" % r["net_income_yoy"]
        if r["net_income_qoq"] is not None:
            s += ", %+.1f%% QoQ" % r["net_income_qoq"]
        if r["eps_diluted"] is not None:
            s += "; diluted EPS Rs %s" % inr(r["eps_diluted"])
            if r["eps_diluted_yoy"] is not None:
                s += " (%+.1f%% YoY)" % r["eps_diluted_yoy"]
        out.append(s + ".")
    if r["operating_margin"] is not None:
        s = "Operating margin %.1f%%" % r["operating_margin"]
        if r["operating_margin_change"] is not None:
            s += " (%+.1f points YoY)" % r["operating_margin_change"]
        out.append(s + ".")
    return out


# ---------------------------------------------------------------- output

def _c(v):
    return "-" if v is None else inr(v, 0)


def _p(v):
    return "-" if v is None else "%+.1f%%" % v


def _m(v):
    return "-" if v is None else "%.1f%%" % v


def render(symbol, result, source):
    name = result.get("entity")
    out = ["%s%s. Rs crore except EPS (1 crore = 10 million). Source: %s." % (
        symbol, " (%s)" % name if name else "", source), ""]
    out.append("%-9s %-11s %10s %8s %8s %10s %8s %8s %7s %7s %8s" % (
        "quarter", "ended", "revenue", "YoY", "QoQ", "net profit", "YoY", "QoQ", "op mgn", "net mgn", "EPS"))
    for r in result["rows"]:
        out.append("%-9s %-11s %10s %8s %8s %10s %8s %8s %7s %7s %8s" % (
            r["fiscal"], r["end"], _c(r["revenue"]), _p(r["revenue_yoy"]), _p(r["revenue_qoq"]),
            _c(r["net_income"]), _p(r["net_income_yoy"]), _p(r["net_income_qoq"]),
            _m(r["operating_margin"]), _m(r["net_margin"]),
            "-" if r["eps_diluted"] is None else inr(r["eps_diluted"])))
    t = result["ttm"]
    if t:
        out += ["", "Last four quarters: revenue Rs %s crore, net profit Rs %s crore%s." % (
            inr(t["revenue"], 0) if "revenue" in t else "-", inr(t["net_income"], 0) if "net_income" in t else "-",
            (", EPS Rs %s" % inr(t["eps_diluted"])) if "eps_diluted" in t else "")]
    out += [""] + changes(result)
    for n in result["notes"]:
        out.append("NOTE: " + n)
    out += ["", "Reported numbers, not a forecast. Not financial advice."]
    return "\n".join(out) + "\n"


def metrics_row(symbol, result, source):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"ticker": symbol, "name": result.get("entity"), "period_end": cur.get("end"),
            "fiscal": cur.get("fiscal"), "revenue_ttm_cr": t.get("revenue"),
            "net_income_ttm_cr": t.get("net_income"), "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "revenue_growth_qoq": cur.get("revenue_qoq"),
            "net_income_growth_yoy": cur.get("net_income_yoy"), "eps_growth_yoy": cur.get("eps_diluted_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "source": source}


# ---------------------------------------------------------------- fetching

def fetch_yahoo(symbol, offline=None, now=None):
    if offline:
        path = os.path.join(offline, "timeseries-%s.json" % bare(symbol))
        if not os.path.isfile(path):
            raise ResultsError("offline: no file %s" % path)
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    now = int(now or time.time())
    url = TS_URL % (yahoo_symbol(symbol), now - 3 * 365 * 86400, now)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ResultsError("Yahoo answered HTTP %s for %s (use --file with the filing's numbers)" % (exc.code, symbol))
    except urllib.error.URLError as exc:
        raise ResultsError("could not reach Yahoo: %s" % exc.reason)


def update_metrics(path, rows, today):
    existing = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            existing = {r["ticker"]: r for r in json.load(fh).get("companies", [])}
    for r in rows:
        existing[r["ticker"]] = r
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"as_of": today, "companies": sorted(existing.values(), key=lambda r: r["ticker"])}, fh, indent=2)
        fh.write("\n")
    return len(existing)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbols", nargs="*", help="bare NSE symbols, e.g. RELIANCE TCS M&M")
    ap.add_argument("--file", metavar="FILE", help="a JSON or CSV of results you copied from the filing")
    ap.add_argument("--symbol", help="the symbol for --file when the file does not say")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per symbol into this JSON")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--today", help="YYYY-MM-DD for the metrics as_of (tests)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.symbols and not args.file:
        ap.print_help()
        return 2
    today = args.today or (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=5, minutes=30)).date().isoformat()
    jobs = []
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                sym, q = parse_file(fh.read(), args.file)
        except (OSError, ResultsError) as exc:
            print("results: %s" % exc, file=sys.stderr)
            return 2
        sym = bare(args.symbol or sym or os.path.splitext(os.path.basename(args.file))[0])
        jobs.append((sym, q, "the file you gave (filed numbers)"))
    out, rows, failed = [], [], 0
    for s in args.symbols:
        sym = bare(s)
        try:
            jobs.append((sym, parse_yahoo(fetch_yahoo(sym, args.offline), sym), "Yahoo Finance (unofficial)"))
        except ResultsError as exc:
            print("results: %s: %s" % (sym, exc), file=sys.stderr)
            failed += 1
    for sym, q, source in jobs:
        res = analyse(q, args.quarters)
        out.append({"ticker": sym, "source": source, "result": res, "changes": changes(res)})
        rows.append(metrics_row(sym, res, source))
        if not args.json:
            sys.stdout.write(render(sym, res, source) + "\n")
    if args.json:
        print(json.dumps(out, indent=2))
    if args.metrics and rows:
        n = update_metrics(args.metrics, rows, today)
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, n))
    return 0 if out and not failed else (1 if out else 2)


if __name__ == "__main__":
    sys.exit(main())
