#!/usr/bin/env python3
"""News monitor for nse-news. Standard library only.

Watches headlines and NSE corporate announcements for the stocks you watch or
hold and prints only what is new since the last check, newest first, with how
far each stock has moved today and a flag on anything that touches your
tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers RELIANCE TCS "M&M"
    python3 news.py --tickers RELIANCE --all --hours 72   # everything in the last 3 days, seen or not
    python3 news.py --no-filings                     # headlines only
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in nse/watchlist.json
(nse-scanner), the symbol in nse/plan.json (nse-exit) and the positions in
nse/portfolio.json. Tickers are bare NSE symbols (RELIANCE, M&M, BAJAJ-AUTO).

Sources, one request each per ticker:
  - Yahoo Finance headline RSS (Indian edition):
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>.NS&region=IN&lang=en-IN
  - NSE corporate announcements (results, board meetings, director changes,
    acquisitions, insolvency, buy-backs and the rest of what listed companies
    must tell the exchange):
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYMBOL>
    The NSE site wants a browser User-Agent and the cookies it sets on its
    home page, so the tool visits https://www.nseindia.com first and reuses
    the cookies. NSE often refuses cloud, VPN and data-centre addresses: when
    it does, the tool says so once and the headlines still run.
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=1d&interval=5m

Times print in India time (IST, UTC+05:30, no daylight saving). State:
nse/state/news.json remembers what you have seen (30 days), so an item shows
once. Offline: --offline DIR reads yahoo-<SYMBOL>.xml, chart-<SYMBOL>.json and
nse-announcements-<SYMBOL>.json from DIR. The tests use it.

Verified offline against fixtures; the live endpoints were not exercised when
this was written.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
NSE_HOME = "https://www.nseindia.com/"
NSE_ANN = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving
IST = _dt.timedelta(hours=5, minutes=30)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# Announcement subjects that deserve a "!" in the list (matched on the NSE "desc" text).
IMPORTANT_DESC = [
    r"financial results", r"outcome of board meeting", r"change in directors", r"key managerial",
    r"acquisition", r"amalgamation", r"merger", r"scheme of arrangement", r"insolvency", r"default",
    r"fraud", r"delist", r"suspension", r"open offer", r"takeover", r"buy-?back", r"fund raising",
    r"preferential", r"qualified institutions", r"credit rating", r"resignation", r"auditor",
]
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b|\bQ[1-4]\b|\bnet profit\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|target price|\boutperform\b|\bunderperform\b"
               r"|\bbrokerage\b|\bbuy rating\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEBI (?:order|notice|probe)\b|\bantitrust\b"
              r"|\bCCI\b|\bjudge\b|\bcourt\b|\bNCLT\b|\bED\b|\bshow[- ]cause\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bamalgamation\b|\bbuyout\b|\btakeover\b|\bdeal\b"
             r"|\bstake\b|\bopen offer\b|\bbulk deal\b|\bblock deal\b"),
    ("capital", r"\bdividend\b|\bbuy-?back\b|\brepurchase\b|\bstock split\b|\bbonus\b|\bsub-?division\b|\boffering\b"
                r"|\bQIP\b|\bIPO\b|\brights issue\b|\bpreferential\b|\bfund ?raising\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bresign|\bsteps down\b|\bappoint|\bdirectors?\b|\bkey managerial\b"),
    ("risk", r"\brecall\b|\bbankrupt|\binsolvency\b|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b|\bfraud\b"
             r"|\bpledge\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)|\bcircuit\b|\bASM\b|\bGSM\b"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bCDSCO\b|\bUSFDA\b|\bFDA\b|\bapproval\b|\bapproves\b|\bclinical\b|\bDGCA\b"),
    ("macro", r"\bRBI\b|\brepo rate\b|\bMPC\b|\bCPI\b|\binflation\b|\bGDP\b|\bFII\b|\bFPI\b|\bDII\b"
              r"|\bNifty\b|\bSensex\b|\bBank Nifty\b|\bcrude\b|\bBudget\b|\btariffs?\b|\bfutures\b|\brupee\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Aware or naive UTC -> naive India wall-clock time. IST is UTC+05:30 all year."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + IST


def parse_nse_time(row):
    """An NSE announcement row -> aware UTC datetime, from its IST stamp, or None."""
    for key in ("an_dt", "exchdisstime", "sort_date", "dt"):
        text = str(row.get(key) or "").strip()
        m = re.match(r"^(\d{1,2})-([A-Za-z]{3})[A-Za-z]*-(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", text)
        if m and m.group(2).lower() in MONTHS:
            local = _dt.datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)),
                                 int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0))
            return (local - IST).replace(tzinfo=_dt.timezone.utc)
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$", text)
        if m:
            local = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                 int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0))
            return (local - IST).replace(tzinfo=_dt.timezone.utc)
    return None


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="nse/portfolio.json", watchlist="nse/watchlist.json",
                     plan="nse/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[bare(sym)] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(bare(pl["symbol"]), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[bare(sym)] = p.get("name") or out.get(bare(sym))
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.3):
        self.offline = offline
        self.pause = pause
        self._last = 0.0
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
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, req.full_url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (req.full_url, exc.reason))

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        self._wait()
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"}
        if nse:
            headers.update({"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
                            "Referer": NSE_HOME})
            if not self._primed:                      # the cookie handshake: visit the home page first
                self._read(urllib.request.Request(NSE_HOME, headers=dict(headers, Accept="text/html")),
                           self._opener)
                self._primed = True
                self._wait()
            return self._read(urllib.request.Request(url, headers=headers), self._opener)
        return self._read(urllib.request.Request(url, headers=headers))


def bare(ticker):
    """RELIANCE.NS / reliance -> RELIANCE. The symbol as NSE spells it (M&M, BAJAJ-AUTO)."""
    t = str(ticker).strip().upper()
    for suffix in (".NS", ".BO"):
        if t.endswith(suffix):
            t = t[:-len(suffix)]
    return t


def yahoo_symbol(ticker):
    """RELIANCE -> RELIANCE.NS, URL-safe (M&M -> M%26M.NS). The hyphen in BAJAJ-AUTO stays."""
    t = str(ticker).strip().upper()
    if not (t.endswith(".NS") or t.endswith(".BO")):
        t += ".NS"
    return urllib.parse.quote(t, safe="-.")


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_announcements(data, ticker):
    """NSE corporate-announcements JSON (a list, or {"data": [...]}) -> items."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError as exc:
        raise NewsError("NSE announcements for %s are not valid JSON (NSE sometimes answers with a web page "
                        "instead): %s" % (ticker, exc))
    if isinstance(doc, dict):
        doc = doc.get("data") or doc.get("rows") or []
    if not isinstance(doc, list):
        raise NewsError("NSE announcements for %s are not a list" % ticker)
    out = []
    for row in doc:
        if not isinstance(row, dict):
            continue
        desc = " ".join(str(row.get("desc") or "").split())
        if not desc:
            continue
        company = " ".join(str(row.get("sm_name") or "").split()) or ticker
        seq = str(row.get("seq_id") or "").strip()
        when = parse_nse_time(row)
        ident = seq or hashlib.sha1(("%s|%s|%s" % (ticker, desc, row.get("an_dt"))).encode("utf-8")).hexdigest()[:12]
        out.append({"id": "nse:%s:%s" % (ticker, ident), "source": "NSE", "kind": "filing", "form": desc,
                    "ticker": ticker, "title": "%s: %s" % (company, desc),
                    "summary": " ".join(str(row.get("attchmntText") or "").split()),
                    "important": any(re.search(rx, desc, re.I) for rx in IMPORTANT_DESC),
                    "link": str(row.get("attchmntFile") or "").strip(), "time": when})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\b(?:NSE|BSE)\s*:\s*%s(?![A-Za-z0-9])" % re.escape(t),
                r"#%s(?![A-Za-z0-9])" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and re.search(r"financial results", it.get("form") or "", re.I) \
                and "earnings" not in it["tags"]:
            it["tags"].insert(0, "earnings")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, filings=True, count=10, errors=None):
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        try:
            items += parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "yahoo-%s.xml" % t), t)
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
    if filings:
        for t in sorted(watch):
            try:
                data = fetcher.get(NSE_ANN % urllib.parse.quote(t, safe="-"), "nse-announcements-%s.json" % t,
                                   nse=True)
                rows = sorted(parse_announcements(data, t), key=lambda i: i["time"] or _dt.datetime.min.replace(
                    tzinfo=_dt.timezone.utc), reverse=True)
                items += rows[:count]
            except NewsError as exc:
                if fetcher.offline:
                    errors.append("NSE announcements for %s: %s" % (t, exc))
                    continue
                errors.append("NSE announcements for %s: %s. NSE often refuses cloud, VPN and data-centre "
                              "addresses; headlines still ran. Open nseindia.com for the company's "
                              "announcements." % (t, exc))
                break                                   # one refusal is enough: do not hammer the site
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


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


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-11s ₹%s %+.2f%% vs yesterday's close ₹%s%s  %s" % (
            m["ticker"], inr(m["price"]), m["pct"], inr(m["prev_close"]), when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        if it["link"]:
            extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="bare NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="nse/portfolio.json")
    ap.add_argument("--watchlist", default="nse/watchlist.json")
    ap.add_argument("--plan", default="nse/plan.json")
    ap.add_argument("--state", default="nse/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-filings", action="store_true", help="headlines only (skip NSE announcements)")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--config", default="nse/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time (UTC unless it carries an offset), for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.tickers:
        watch = {bare(t): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no tickers. Pass --tickers, or run /nse-scanner first (it writes "
              "nse/watchlist.json).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, state, filings=not args.no_filings, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
