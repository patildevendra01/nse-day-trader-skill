#!/usr/bin/env python3
"""Price alerts and the NSE clock for nse-alerts. Standard library only.

    python3 alerts.py clock                          # is the NSE open? next open / close
    python3 alerts.py quote RELIANCE --write         # quotes -> nse/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from nse/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 2871.40
    python3 alerts.py add --symbol RELIANCE --target 2934.60
    python3 alerts.py add --at 15:15 --label "flat by close" --before-close
    python3 alerts.py add --symbol INFY --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-30 15:15" --offline tests/fixtures/nse-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Symbols are bare NSE symbols (RELIANCE, TCS, M&M, BAJAJ-AUTO). Yahoo is asked
for SYMBOL.NS. All times are India Standard Time (IST, UTC+05:30, no daylight
saving): the NSE trades 09:15 to 15:30 IST, Monday to Friday.

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NSE opened today (rings once per trading day)
  time            the IST clock passed "at" (HH:MM) on a trading day
                  (rings once per trading day); "before_close": true moves it
                  earlier by the gap between the normal and the shortened close
                  on a day listed in NSE_EARLY_CLOSE. The NSE has no scheduled
                  half days, so that list is empty and the flag changes nothing
                  today.

A rule with "date" is only watched on that IST date (rules from a plan are for
the plan's day only). An above / below alert rings ONCE and then stays quiet
until you reset it, unless the rule has "rearm": true (it re-arms when the
price crosses back). A pct_move alert rings at most once per day.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS (falls
          back to query1 on a 429 or a 5xx; one retry with a pause on a 429;
          one request per symbol, half a second apart)
  --offline DIR reads DIR/yahoo-<SYMBOL>.json (or .json.gz) instead.

NSE calendar: the 2026 trading holidays as published on nseindia.com and
repeated by the large brokers (cross-checked 2026-10-31). The official 2027
list is not out yet, so for 2027 and later the clock says the holiday list is
unknown. Diwali Muhurat trading is a special evening session (on 2026-11-08,
a Sunday, timing announced by the NSE); this tool treats Sundays as closed.

Verified offline against fixtures; live endpoints not exercised at build time.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; nse-day-trader-skill/1.0)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

NSE_HOLIDAYS = {   # equity segment trading holidays that fall on a weekday
    "2026-01-15": "Maharashtra municipal elections",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
NSE_EARLY_CLOSE = {}   # date -> reason. The NSE has no scheduled half days; if it ever announces
#                        a shortened session, add the date here and set EARLY to that close.
CALENDAR_YEARS = (2026,)
PRE_OPEN = datetime.time(9, 0)      # call auction, no continuous trading
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
EARLY = datetime.time(12, 30)       # only used for a date listed in NSE_EARLY_CLOSE
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)   # fixed: India has no daylight saving


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Naive UTC datetime -> naive India Standard Time (UTC+05:30, no DST)."""
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    return local - IST_OFFSET


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as India time (IST)."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants a time like \"2026-10-19 10:15\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in IST, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NSE_EARLY_CLOSE else CLOSE)


def market_status(now_utc):
    local = to_ist(now_utc)
    d = local.date()
    ok, reason = trading_day(d)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_known": d.year in CALENDAR_YEARS,
          "open": False, "reason": reason, "early_close": NSE_EARLY_CLOSE.get(d.isoformat())}
    s = session(d)
    if s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "regular session"
        elif PRE_OPEN <= local.time() < o:
            st["reason"] = "pre-open call auction (no continuous trading until %s)" % o.strftime("%H:%M")
        elif local.time() < o:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    # next open
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd):
            st["next_open"] = "%s 09:15" % nd.isoformat()
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def norm(sym):
    """User text -> bare NSE symbol: RELIANCE.NS -> RELIANCE, m&m -> M&M. A .BO (BSE) suffix is kept."""
    t = str(sym or "").strip().upper()
    return t[:-3] if t.endswith(".NS") else t


def yahoo_symbol(sym):
    """Bare symbol -> Yahoo's name: RELIANCE -> RELIANCE.NS. Hyphens and & stay (BAJAJ-AUTO.NS, M&M.NS)."""
    t = norm(sym)
    return t if t.endswith(".BO") else t + ".NS"


def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (
        host, urllib.parse.quote(yahoo_symbol(sym), safe=".-"))


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": norm(sym), "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch(url, timeout=20):
    code, text = _get(url, timeout)
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (url.split("?")[0], code))
    return text


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, source, sym):
    base = os.path.join(folder, "%s-%s.json" % (source, sym))
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def get_quotes(symbols, source="yahoo", offline=None, pause=PAUSE):
    """{SYMBOL: quote} and [errors]. One request per symbol, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        sym = norm(sym)
        try:
            if offline:
                text = _offline_text(offline, source, sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(sym)
            quotes[sym] = parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes for alerts and sizing checks. Your broker's price is the one that fills. "
                    "detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:15\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that IST date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this IST date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_ist(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today. With "before_close" it moves earlier by the gap between the
    normal and the shortened close on a day in NSE_EARLY_CLOSE (never, for now: the NSE has no half days)."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("early_close"):
        base = datetime.date(2000, 1, 1)
        gap = datetime.datetime.combine(base, CLOSE) - datetime.datetime.combine(base, EARLY)
        at = (datetime.datetime.combine(base, at) - gap).time()
    return at


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_ist(now_utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: the NSE is open (%s IST)%s."
                              % (status["now_ist"], (", shortened session today: "
                                                     + status["early_close"]) if status.get("early_close") else "")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                            local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = norm(r.get("symbol"))
                if sym:
                    msg += (" If %s is still open, close it: nse-broker flatten --symbol %s builds the sell "
                            "ticket, then nse-risk and your yes. Brokers auto square off MIS positions "
                            "around 15:15 to 15:25 and charge for it." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = norm(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", inr(p), word, label, inr(r["price"], None),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, inr(p), inr(float(ref)))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def inr(x, dec=2):
    """₹ with Indian digit grouping: 1234567.5 -> ₹12,34,567.50. dec=None drops a zero fraction (₹2,900)."""
    x = float(x)
    if dec is None:
        dec = 0 if x == int(x) else 2
    neg = x < 0
    whole, _, frac = ("%.*f" % (dec, abs(x))).partition(".")
    if len(whole) > 3:
        head, tail, parts = whole[:-3], whole[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return "%s\u20b9%s%s" % ("-" if neg else "", whole, ("." + frac) if dec else "")


def rules_from_plan(plan, cfg_flat_by=None):
    """nse/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run nse-exit first)")
    sym = norm(plan["symbol"])
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run nse-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or "15:15"
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:15" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by close", "before_close": True})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(norm(p.get("symbol") or p.get("ticker")))
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(norm(plan["symbol"]))
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    st = market_status(parse_now(a.now))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("India %s IST (%s): NSE %s, %s." % (st["now_ist"], st["weekday"],
                                                  "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            print("Today's session: %s to %s IST%s." % (st["opens"], st["closes"],
                                                    (" (shortened: %s)" % st["early_close"]) if st.get("early_close") else ""))
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        if not st["calendar_known"]:
            print("Holiday list covers %s only: check nseindia.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([norm(s) for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE TCS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.source, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now, a.source)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1), inr(q["prev_close"]))
                print("%-10s %s%s  %s  [%s]" % (s, inr(q["price"]), chg, q.get("time", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    status = market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([norm(r.get("symbol")) for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.source, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now, a.source)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"],
                                         status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, inr(quotes[s]["price"])) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. Your stop-loss and take-profit orders sit at the "
                  "broker: check your positions and record any fill (ticket.py fill). To close a position "
                  "yourself: ticket.py flatten, then nse-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run nse-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; the stop-loss order at your broker is what actually stops you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:15")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = norm(a.symbol)
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = norm(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (nse-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--source", default="yahoo", choices=["yahoo"])
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = India time, IST)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--write", nargs="?", const="nse/prices.json", metavar="PATH",
                   help="save to prices.json (default nse/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p.add_argument("--prices-out", default="nse/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by time from the plan")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--config", default="nse/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by close\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert that moves earlier on a shortened session (the NSE has none scheduled)")
    p.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="nse/alerts.json")
    p.add_argument("--state", default="nse/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="nse/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    for _f in (sys.stdout, sys.stderr):
        try:
            _f.reconfigure(encoding="utf-8", errors="replace")   # the rupee sign on a legacy console
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
