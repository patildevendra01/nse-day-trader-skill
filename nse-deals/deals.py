#!/usr/bin/env python3
"""Deal tracker for nse-deals. Standard library only.

Reads NSE's daily bulk-deal and block-deal files and, best effort, insider and
promoter trading disclosures (SEBI PIT), and prints only the deals that are
new since the last check. Flags deals in stocks you watch or hold, and deals
by clients you name.

    python3 deals.py                                   # new bulk + block deals, flagged against your files
    python3 deals.py --kind block
    python3 deals.py --watch "Rakesh Jhunjhunwala" Vanguard    # also flag these clients
    python3 deals.py --tickers RELIANCE TCS --all      # only these, seen or not
    python3 deals.py --insiders                        # add insider / promoter trades for your tickers
    python3 deals.py --write                           # save nse/deals.json
    python3 deals.py --offline DIR                     # bulk.csv, block.csv, pit-<SYMBOL>.json from DIR

Sources:
  - https://nsearchives.nseindia.com/content/equities/bulk.csv   (static file, latest session)
  - https://nsearchives.nseindia.com/content/equities/block.csv
  - https://www.nseindia.com/api/corporates-pit?index=equities&symbol=<SYMBOL>
    (needs a browser User-Agent and the cookie from the NSE home page; NSE often
    refuses cloud, VPN and data-centre addresses, and the tool says so once)

A bulk deal is a trade of more than 0.5 % of a company's shares in one day;
a block deal is a negotiated trade in the special window. Both name the
client. State: nse/state/deals.json (30 days). Times are India time.

Verified offline against fixtures; the live endpoints were not exercised when
this was written.
"""

import argparse
import csv
import datetime as _dt
import gzip
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BULK_URL = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
BLOCK_URL = "https://nsearchives.nseindia.com/content/equities/block.csv"
PIT_URL = "https://www.nseindia.com/api/corporates-pit?index=equities&symbol=%s"
NSE_HOME = "https://www.nseindia.com/"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
IST = _dt.timedelta(hours=5, minutes=30)
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
KEEP_DAYS = 30
BIG_CRORE = 100.0


class DealsError(Exception):
    pass


def now_ist(now=None):
    """Naive India wall-clock time; --now takes an ISO stamp (a trailing Z or offset is honoured)."""
    if not now:
        return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + IST
    d = _dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    if d.tzinfo is None:
        return d
    return d.astimezone(_dt.timezone.utc).replace(tzinfo=None) + IST


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


def bare(sym):
    s = str(sym or "").strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    return s


def parse_date(text):
    """'30-OCT-2026', '30-Oct-2026', '2026-10-30', '30/10/2026' -> 'YYYY-MM-DD' or None."""
    t = str(text or "").strip()
    m = re.match(r"^(\d{1,2})[- ]([A-Za-z]{3})[A-Za-z]*[- ](\d{4})", t)
    if m and m.group(2).lower() in MONTHS:
        return "%04d-%02d-%02d" % (int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return "%s-%s-%s" % m.groups()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", t)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def number(text):
    t = re.sub(r"[,\s]", "", str(text if text is not None else ""))
    try:
        return float(t)
    except ValueError:
        return None


# ---------------------------------------------------------------- parsing

def _norm(h):
    return re.sub(r"[^a-z]", "", str(h).lower())


def parse_deals(text, kind):
    """NSE bulk.csv / block.csv -> list of deal dicts. Tolerant of header spelling and a BOM."""
    text = text.lstrip("﻿")
    rows = []
    reader = csv.reader(io.StringIO(text))
    header = None
    for raw in reader:
        if not any(c.strip() for c in raw):
            continue
        if header is None:
            header = [_norm(c) for c in raw]
            continue
        row = dict(zip(header, [c.strip() for c in raw]))

        def pick(*names):
            for n in names:
                for h in header:
                    if h == n or h.startswith(n):
                        return row.get(h, "")
            return ""
        sym = bare(pick("symbol"))
        side = pick("buysell", "buysale", "side").upper()
        qty = number(pick("quantitytraded", "quantity", "qty"))
        price = number(pick("tradepricewghtavgprice", "tradeprice", "price", "wavgprice"))
        date = parse_date(pick("date", "tradedate"))
        if not sym or qty is None or price is None or not date:
            continue
        side = "BUY" if side.startswith("B") else "SELL" if side.startswith("S") else side
        rows.append({"kind": kind, "date": date, "symbol": sym, "name": pick("securityname", "companyname"),
                     "client": re.sub(r"\s+", " ", pick("clientname", "client")), "side": side,
                     "qty": int(qty), "price": price, "value_cr": round(qty * price / 1e7, 2),
                     "remarks": pick("remarks")})
    return rows


def deal_key(d):
    return "|".join([d["kind"], d["date"], d["symbol"], d["client"].upper(), d["side"], str(d["qty"]),
                    "%.2f" % (d["price"] if d["price"] is not None else (d.get("value_cr") or 0))])


def parse_pit(text, symbol):
    """NSE corporates-pit JSON ({"data": [...]} or a list) -> insider trade dicts."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise DealsError("insider data for %s was not JSON (NSE may have refused the request)" % symbol)
    rows = doc.get("data", []) if isinstance(doc, dict) else doc
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        typ = str(r.get("tdpTransactionType") or r.get("acqMode") or "").strip()
        side = "BUY" if typ.lower().startswith(("buy", "acq", "market purchase")) else \
            "SELL" if typ.lower().startswith(("sell", "disp", "market sale")) else typ.upper()
        qty = number(r.get("secAcq") or r.get("noOfSecurity") or r.get("secVal"))
        value = number(r.get("secVal") if r.get("secAcq") else r.get("value"))
        date = parse_date(r.get("acqtoDt") or r.get("acqfromDt") or r.get("date") or r.get("intimDt"))
        who = str(r.get("acqName") or r.get("name") or "").strip()
        if not who or not date:
            continue
        out.append({"kind": "insider", "date": date, "symbol": bare(r.get("symbol") or symbol),
                    "name": str(r.get("company") or ""), "client": who,
                    "category": str(r.get("personCategory") or "").strip(), "side": side,
                    "qty": int(qty) if qty is not None else None,
                    "value_cr": round(value / 1e7, 2) if value is not None else None,
                    "price": None, "remarks": typ})
    return out


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def tickers_from_files(portfolio="nse/portfolio.json", watchlist="nse/watchlist.json", plan="nse/plan.json"):
    out = set()
    for h in (load_json(watchlist) or {}).get("hits", []):
        s = h.get("symbol") or h.get("ticker")
        if s:
            out.add(bare(s))
    s = (load_json(plan) or {}).get("symbol")
    if s:
        out.add(bare(s))
    for p in (load_json(portfolio) or {}).get("positions", []):
        s = p.get("ticker") or p.get("symbol")
        if s:
            out.add(bare(s))
    return out


def load_state(path):
    return load_json(path) or {"seen": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.5):
        self.offline, self.pause, self._last = offline, pause, 0.0
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._primed = False

    def _wait(self):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()

    def _read(self, req, opener=None):
        try:
            with (opener or urllib.request.build_opener()).open(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise DealsError("HTTP %s for %s" % (exc.code, req.full_url))
        except urllib.error.URLError as exc:
            raise DealsError("could not reach %s: %s" % (req.full_url, exc.reason))

    def get(self, url, name, api=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise DealsError("offline: no file %s" % path)
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        self._wait()
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"}
        if api:
            headers.update({"Accept": "application/json, text/plain, */*", "Referer": NSE_HOME})
            if not self._primed:
                self._read(urllib.request.Request(NSE_HOME, headers=dict(headers, Accept="text/html")), self._opener)
                self._primed = True
                self._wait()
            return self._read(urllib.request.Request(url, headers=headers), self._opener)
        return self._read(urllib.request.Request(url, headers=headers))


def collect(fetcher, kinds=("bulk", "block"), insiders=(), errors=None):
    errors = errors if errors is not None else []
    rows = []
    for kind, url in (("bulk", BULK_URL), ("block", BLOCK_URL)):
        if kind not in kinds:
            continue
        try:
            rows += parse_deals(fetcher.get(url, kind + ".csv"), kind)
        except DealsError as exc:
            errors.append("%s deals: %s" % (kind, exc))
    for sym in insiders:
        try:
            rows += parse_pit(fetcher.get(PIT_URL % sym, "pit-%s.json" % sym, api=True), sym)
        except DealsError as exc:
            errors.append("insider trades: %s" % exc)
            if "refused" in str(exc) or "HTTP 40" in str(exc):
                errors.append("NSE refused the request; skipping the other insider lookups (try from a home connection)")
                break
    return rows


# ---------------------------------------------------------------- selecting

def match_client(client, names):
    """The watched names that appear in the client name, ignoring case and punctuation."""
    flat = re.sub(r"[^a-z0-9 ]", " ", client.lower())
    flat = " ".join(flat.split())
    hits = []
    for n in names:
        want = " ".join(re.sub(r"[^a-z0-9 ]", " ", n.lower()).split())
        if want and want in flat:
            hits.append(n)
    return hits


def flag(rows, tickers=(), names=()):
    tickers = {bare(t) for t in tickers}
    for d in rows:
        d["flags"] = []
        if d["symbol"] in tickers:
            d["flags"].append("YOURS")
        for n in match_client(d["client"], names):
            d["flags"].append("WATCHED: " + n)
        if d.get("value_cr") is not None and d["value_cr"] >= BIG_CRORE:
            d["flags"].append("BIG")
    return rows


def select(rows, only=None, seen=None, show_all=False):
    only = {bare(t) for t in only} if only else None
    out, dup = [], set()
    for d in rows:
        if only is not None and d["symbol"] not in only:
            continue
        k = deal_key(d)
        if k in dup:
            continue
        dup.add(k)
        if not show_all and seen is not None and k in seen:
            continue
        out.append(d)
    out.sort(key=lambda d: (d["date"], d["value_cr"] or 0), reverse=True)
    return out


def remember(state, rows, today, keep_days=KEEP_DAYS):
    for d in rows:
        state["seen"][deal_key(d)] = today
    cut = (_dt.datetime.strptime(today, "%Y-%m-%d") - _dt.timedelta(days=keep_days)).strftime("%Y-%m-%d")
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cut}
    return state


# ---------------------------------------------------------------- output

def describe(d):
    who = d["client"] + ((" (%s)" % d["category"]) if d.get("category") else "")
    qty = inr(d["qty"], 0) if d.get("qty") is not None else "?"
    px = (" at ₹%s" % inr(d["price"])) if d.get("price") else ""
    val = (", ₹%s crore" % inr(d["value_cr"])) if d.get("value_cr") is not None else ""
    tag = {"bulk": "bulk", "block": "block", "insider": "insider"}[d["kind"]]
    flags = ("  [%s]" % ", ".join(d["flags"])) if d.get("flags") else ""
    return "%s  %-11s %-5s %s shares%s%s  %s: %s%s" % (d["date"], d["symbol"], d["side"], qty, px, val, tag, who, flags)


def render(rows, errors=(), asof=None):
    lines = []
    if not rows:
        lines.append("No new deals.")
    else:
        mine = [d for d in rows if any(f == "YOURS" or f.startswith("WATCHED") for f in d.get("flags", []))]
        lines.append("%d new deal(s)%s." % (len(rows), (", %d on your list" % len(mine)) if mine else ""))
        lines.append("")
        for d in mine:
            lines.append(describe(d))
        if mine and len(mine) < len(rows):
            lines += ["", "Everything else:"]
        for d in rows:
            if d not in mine:
                lines.append(describe(d))
    for e in errors:
        lines.append("Note: " + e)
    lines += ["", "A deal is a fact about the past, not a signal. Not financial advice."]
    return "\n".join(lines)


def _parser():
    p = argparse.ArgumentParser(prog="deals.py", description=__doc__.split("\n")[0])
    p.add_argument("--kind", choices=["bulk", "block", "both"], default="both")
    p.add_argument("--tickers", nargs="*", help="only these NSE symbols (default: show all, flag yours)")
    p.add_argument("--watch", nargs="*", default=[], help="client names to flag")
    p.add_argument("--insiders", action="store_true", help="add insider / promoter trades for your tickers")
    p.add_argument("--all", action="store_true", help="include deals you have already seen")
    p.add_argument("--offline", help="folder with bulk.csv, block.csv, pit-<SYMBOL>.json")
    p.add_argument("--state", default="nse/state/deals.json")
    p.add_argument("--write", nargs="?", const="nse/deals.json", help="save the result as JSON")
    p.add_argument("--json", action="store_true")
    p.add_argument("--now", help="pin the clock (ISO)")
    p.add_argument("--no-remember", action="store_true", help="do not mark deals as seen")
    return p


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        today = now_ist(args.now).strftime("%Y-%m-%d")
        mine = set(tickers_from_files()) | {bare(t) for t in (args.tickers or [])}
        kinds = ("bulk", "block") if args.kind == "both" else (args.kind,)
        errors = []
        rows = collect(Fetcher(args.offline), kinds, sorted(mine) if args.insiders else (), errors)
        if not rows and errors and len(errors) >= len(kinds):
            raise DealsError(errors[0])
        flag(rows, mine, args.watch)
        state = load_state(args.state)
        new = select(rows, args.tickers, state["seen"], args.all)
        if not args.no_remember:
            save_state(args.state, remember(state, new, today))
        doc = {"created_by": "nse-deals", "created_at": today, "deals": new, "errors": errors}
        if args.write:
            os.makedirs(os.path.dirname(args.write) or ".", exist_ok=True)
            with open(args.write, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1)
        print(json.dumps(doc, indent=1) if args.json else render(new, errors))
        return 0
    except DealsError as exc:
        sys.stderr.write("deals.py: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
