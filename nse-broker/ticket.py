#!/usr/bin/env python3
"""Order tickets and the decision log for nse-broker (NSE cash equity, rupees). Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact parameters (Kite Connect place_order names) for each leg:
            the entry, then a separate stop-loss order and a take-profit
            order, because most Indian brokers have discontinued intraday
            bracket orders. The ticket shows the max loss at the stop. An
            order is READY only if nse-risk passed THIS exact order today
            (matched by fingerprint) and nobody has decided on it yet.
  approve   records your explicit yes for ONE ticket, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
            placed (with the broker's order id), denied, or failed.
  fill      records a fill in nse/portfolio.json (cash and shares).
  flatten   builds a sell ticket that closes a symbol's open position by
            flat_by. It still goes through nse-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every ticket, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 2610300001234
    python3 ticket.py fill --symbol RELIANCE --side buy --qty 34 --price 1432.60
    python3 ticket.py flatten --symbol RELIANCE --qty 34
    python3 ticket.py log

"paper": true (the default) means PAPER mode: nothing is sent to any broker,
because no Indian broker offers a free paper-trading order API. You get the
same tickets and the same yes, and you record a simulated fill with
"result --status placed --filled-qty Q --filled-price P". Set "paper": false
to let Claude send approved tickets through your broker connector.

Everything is written to nse/state/broker.json (append only). Dates are
India (IST) dates; --now "2026-10-30 10:15" pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

ORDER_FIELDS = ("symbol", "exchange", "side", "qty", "product", "order_type", "limit_price",
                "trigger_price", "validity", "stop_loss", "take_profit")
VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel"}
BROKER_NAMES = {"zerodha": "Zerodha Kite", "kite": "Zerodha Kite", "dhan": "Dhan", "upstox": "Upstox",
                "angelone": "Angel One", "angel": "Angel One", "groww": "Groww", "other": "your broker"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
EPS = 1e-9
STOP_LIMIT_PCT = 0.3     # an "SL" stop-loss's limit sits this percent under its trigger


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time (India: IST = UTC+05:30, no daylight saving)

IST = datetime.timedelta(hours=5, minutes=30)


def to_ist(utc):
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as India time (IST)."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TicketError("--now wants a time like \"2026-10-30 10:15\" (India time) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ist_iso(now_utc):
    """naive UTC -> "2026-10-30T10:15:00+05:30" (India wall clock with its offset)."""
    return "%s+05:30" % to_ist(now_utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def bare(sym):
    s = str(sym or "").strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    return s


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as nse-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _group(n):
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def _money(v):
    """1234567.5 -> "₹12,34,567.50" (Indian digit grouping); whole rupees drop the .00."""
    x = float(v)
    neg = x < 0
    x = abs(x)
    whole = int(x)
    frac = int(round((x - whole) * 100))
    if frac == 100:
        whole, frac = whole + 1, 0
    return ("-" if neg else "") + "₹" + _group(whole) + ("" if frac == 0 else ".%02d" % frac)


def _p(v):
    return "%.2f" % float(v)


def _tick_down(x, tick):
    t = int(round(float(tick) * 100))
    return math.floor(float(x) * 100.0 / t + EPS) * t / 100.0


def tick_of(cfg):
    t = _f((cfg or {}).get("tick"))
    return t if t and t > 0 else 0.05


def has_legs(order):
    """An entry that carries its stop_loss / take_profit legs."""
    return order.get("stop_loss") is not None or order.get("take_profit") is not None


def _otype(order):
    return str(order.get("order_type", "market")).strip().lower().replace("_", "-")


def _kite_type(t):
    return {"market": "MARKET", "limit": "LIMIT", "sl": "SL", "sl-m": "SL-M"}.get(t, str(t).upper())


def entry_params(order):
    """The entry leg as Kite Connect place_order parameters (names as in Kite's API; map them to
    whatever your broker's connector calls them). "why" and "plan" are ours and are never sent."""
    t = _otype(order)
    p = {"variety": "regular", "exchange": str(order.get("exchange") or "NSE").upper(),
         "tradingsymbol": bare(order.get("symbol")),
         "transaction_type": str(order.get("side", "")).upper(),
         "order_type": _kite_type(t),
         "quantity": int(round(_f(order.get("qty")) or 0)),
         "product": str(order.get("product") or "MIS").upper(),
         "validity": str(order.get("validity") or "DAY").upper()}
    if t in ("limit", "sl") and order.get("limit_price") is not None:
        p["price"] = float(order["limit_price"])
    if t in ("sl", "sl-m") and order.get("trigger_price") is not None:
        p["trigger_price"] = float(order["trigger_price"])
    return p


def legs(order, cfg=None, quantity=None):
    """[(role, params)]: the entry, then (for an entry with legs) the stop-loss and the take-profit.
    The two exit legs are sells of the same quantity, placed ONLY after the entry has filled."""
    out = [("entry", entry_params(order))]
    if not has_legs(order):
        return out
    base = entry_params(order)
    opposite = "SELL" if base["transaction_type"] == "BUY" else "BUY"
    common = {"variety": "regular", "exchange": base["exchange"], "tradingsymbol": base["tradingsymbol"],
              "transaction_type": opposite, "quantity": int(quantity if quantity is not None else base["quantity"]), "product": base["product"],
              "validity": "DAY"}
    stop_type = str((cfg or {}).get("stop_type") or "SL-M").upper()
    if order.get("stop_loss") is not None:
        trig = float(order["stop_loss"])
        s = dict(common)
        if stop_type == "SL":
            pct = _f((cfg or {}).get("stop_limit_pct")) or STOP_LIMIT_PCT
            s.update(order_type="SL", trigger_price=trig,
                     price=_tick_down(trig * (1 - pct / 100.0), tick_of(cfg)))
        else:
            s.update(order_type="SL-M", trigger_price=trig)
        out.append(("stop-loss", s))
    if order.get("take_profit") is not None:
        t = dict(common)
        t.update(order_type="LIMIT", price=float(order["take_profit"]))
        out.append(("take-profit", t))
    return out


def tool_input(order):
    """The entry leg's exact parameters (the first thing Claude sends)."""
    return entry_params(order)


def filled_max_loss(qty, price, stop):
    """Actual entry risk at the recorded stop; None means the stop cannot cap loss."""
    q, p, s = _f(qty), _f(price), _f(stop)
    if q is None or p is None or s is None or not all(math.isfinite(x) for x in (q, p, s)):
        return None
    if q <= 0 or p <= s:
        return None
    return round(q * (p - s), 2)

def max_loss(order):
    """(rupees, stop) lost if the stop fills exactly at the stop, or (None, stop)."""
    stop = _f(order.get("stop_loss")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    if _otype(order) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    return round(q * int(round(risk * 100)) / 100.0, 2), stop


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    side = str(order.get("side", "")).upper()
    sym = bare(order.get("symbol")) or "?"
    typ = _otype(order)
    tif = VALIDITY_WORDS.get(str(order.get("validity", "DAY")).upper(), order.get("validity"))
    prod = str(order.get("product") or "MIS").upper()
    exch = str(order.get("exchange") or "NSE").upper()
    q = _str_num(order.get("qty"))
    if typ == "limit":
        how = "limit %s" % _p(order.get("limit_price"))
    elif typ == "sl":
        how = "stop-limit, trigger %s, limit %s" % (_p(order.get("trigger_price")), _p(order.get("limit_price")))
    elif typ == "sl-m":
        how = "stop-market, trigger %s" % _p(order.get("trigger_price"))
    else:
        how = "market"
    parts = ["%s %s %s (%s, %s), %s, %s" % (side, q, sym, exch, prod, how, tif)]
    if has_legs(order):
        opp = "SELL" if side == "BUY" else "BUY"
        if order.get("stop_loss") is not None:
            parts.append("then stop-loss: %s %s at trigger %s" % (opp, q, _p(order["stop_loss"])))
        if order.get("take_profit") is not None:
            parts.append("then take-profit: %s %s at limit %s" % (opp, q, _p(order["take_profit"])))
        loss, stop = max_loss(order)
        if loss is not None:
            parts.append("max loss about %s if the stop fills at %s (it can fill worse in a fast market)"
                         % (_money(loss), _p(stop)))
    return parts


def plain(order):
    """BUY 34 RELIANCE (NSE, MIS), market, good for today / then stop-loss: SELL 34 at trigger 1424.30 / ..."""
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    for k in ("qty", "limit_price", "trigger_price", "stop_loss", "take_profit"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = bare(order.get("symbol"))
    tokens = set(re.findall(r"[A-Z][A-Z0-9&.\-]*", raw.upper()))
    others = {bare(s) for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = []
    for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw):
        try:
            nums.append(float(n.replace(",", "")))
        except ValueError:
            continue
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_str_num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "nse-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today, cfg=None):
    """(ready, label, risk_result); risk must have been checked in the current mode."""
    if risk is not None:
        checked_paper = bool(risk.get("paper", True))
        current_paper = bool((cfg or {}).get("paper", True))
        if checked_paper != current_paper:
            return False, "NOT READY: paper/live mode changed since the risk check; run nse-risk again", None
        checked_config = risk.get("config_sha256")
        current_config = (cfg or {}).get("_config_sha256")
        if not checked_config or checked_config != current_config:
            return False, "NOT READY: risk settings changed since the risk check (or the check is old); run nse-risk again", None
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by nse-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the order to be placed" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: nse-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as nse-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


# ---------------------------------------------------------------- rendering

def broker_name(cfg):
    b = str(cfg.get("broker", "zerodha")).lower()
    return BROKER_NAMES.get(b, str(cfg.get("broker")).title())


def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today, cfg)
    paper = cfg.get("paper", True)
    mode = "PAPER mode (nothing is sent to a broker)" if paper else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s, %s" % (idx, total, mode, broker_name(cfg))]
    parts = plain_parts(order)
    lines.append("  " + parts[0])
    if len(parts) > 1 and parts[-1].startswith("max loss "):
        lines.append("  " + parts[-1])
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if paper:
        lines.append("  Paper mode: nothing is placed. After your yes, record a simulated fill with")
        lines.append("  ticket.py result --index %d --status placed --filled-qty Q --filled-price P." % idx)
    else:
        lines.append("  Claude places it with your broker connector's order tool, one leg at a time, using these")
        lines.append("  parameters (Kite Connect names; map them to the connector's own fields, and read the")
        lines.append("  connector's tool list rather than guessing names):")
    for role, params in legs(order, cfg)[:1]:
        lines.append("  %s:" % role)
        for l in json.dumps(params, indent=2).splitlines():
            lines.append("    " + l)
    if has_legs(order):
        lines.append("  Exit orders are NOT shown yet. After the entry fills, check the broker's actual filled")
        lines.append("  quantity, then run: ticket.py exits --index %d --filled-qty Q" % idx)
        lines.append("  Never use the original requested quantity unless the broker confirms it all filled.")
        lines.append("  The stop-loss and target are separate orders and do NOT cancel each other; when one fills,")
        lines.append("  cancel the other yourself.")
    if ready:
        lines.append("  Reply \"yes\" to approve this entry and its planned protection, or \"no\" to skip it.")
        if not paper:
            lines.append("  Claude's own card then asks again for each order: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today, cfg)
    loss, stop = max_loss(order)
    summary = plain_parts(order)[0]
    if loss is not None:
        summary += " / max loss about %s if the stop fills at %s" % (_money(loss), _p(stop))
    return {"index": idx, "ready": ready, "status": label, "plain": summary,
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
            "broker": cfg.get("broker", "zerodha"), "tool": "your broker connector's order-placement tool",
            "tool_input": tool_input(order), "legs": [{"role": r, "params": p} for r, p in legs(order, cfg)[:1]],
            "exits_available_after_fill": has_legs(order),
            "why": order.get("why", ""), "has_legs": has_legs(order), "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, fill_id=None):
    """Record a fill once in nse/portfolio.json; fill_id makes retries idempotent."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    if fill_id:
        applied = pf.setdefault("applied_fill_ids", [])
        if fill_id in applied:
            return pf
    symbol = bare(symbol)
    qty, price = float(qty), float(price)
    if not math.isfinite(qty) or qty <= 0 or abs(qty - round(qty)) > 1e-9:
        raise TicketError("a fill needs a positive whole-share quantity")
    if not math.isfinite(price) or price <= 0:
        raise TicketError("a fill needs a positive finite price")
    qty = int(round(qty))
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if bare(p.get("ticker") or p.get("symbol")) == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    if fill_id:
        pf.setdefault("applied_fill_ids", []).append(fill_id)
    write_json(portfolio_path, pf)
    return pf


def _append_fill_log(state, now, today, symbol, side, qty, price, fill_id=None):
    record = {
        "at": ist_iso(now), "date": today, "status": "filled", "symbol": bare(symbol), "side": side,
        "qty": _str_num(float(qty)), "price": float(price),
        "plain": "%s %s %s at %s" % (side.upper(), _str_num(float(qty)), bare(symbol), _p(price))}
    if fill_id:
        record["fill_id"] = fill_id
    state.setdefault("orders", []).append(record)


def log_fill(state_path, now, today, symbol, side, qty, price):
    """A fill in the log, so the day's trades are on record (a buy and a sell the same day)."""
    state = load_state(state_path)
    _append_fill_log(state, now, today, symbol, side, qty, price)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-30")
    return now, today


def _config_sha256(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        raw = b""
    return hashlib.sha256(raw).hexdigest()


def _load(a):
    doc = read_json(a.orders, "orders file (run nse-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    cfg["_config_sha256"] = _config_sha256(a.config)
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne ticket at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today, cfg)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    # Use the conservative loss calculated by nse-risk, which may use a fresher
    # market reference than the plan's original entry estimate.
    loss = rv.get("max_loss")
    stop = rv.get("stop")
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": bare(order.get("symbol")), "side": order.get("side"),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", "zerodha")}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain_parts(order)[0]))
    if has_legs(order):
        print("The stop-loss and target will be generated only after the actual filled quantity is confirmed.")
    if cfg.get("paper", True):
        print("Paper mode: do not call any broker tool. Record the simulated fill with: ticket.py result "
              "--index %d --status placed --filled-qty %s --filled-price PRICE" % (a.index, _str_num(order.get("qty"))))
    else:
        print("Now place the entry with your broker connector's order tool, with exactly:")
        print(json.dumps(tool_input(order), indent=2))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
              "placed|denied|failed" % a.index)
        if has_legs(order):
            print("After the entry fills, check the broker's actual position, then generate exits with:")
            print("  ticket.py exits --index %d --filled-qty Q" % a.index)
            print("Only place exits for the shares actually held; each live exit needs its own Allow once.")
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": bare(order.get("symbol")), "side": order.get("side"),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def _confirmed_fill_qty(value, requested):
    qty = _f(value)
    want = _f(requested)
    if qty is None or not math.isfinite(qty) or qty <= 0 or abs(qty - round(qty)) > 1e-9:
        raise TicketError("--filled-qty must be the positive whole-share quantity confirmed by the broker")
    if want is None or qty > want:
        raise TicketError("--filled-qty cannot exceed the requested order quantity (%s)" % _str_num(requested))
    return int(round(qty))


def cmd_exits(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    if not has_legs(order):
        raise TicketError("this order has no stop-loss or take-profit legs")
    rec = latest(state, fingerprint(order), today)
    if not rec or rec.get("status") != "placed" or rec.get("kind") != "entry":
        raise TicketError("record the entry as placed first; exits are only for a filled entry")
    recorded_qty = rec.get("filled_qty")
    if recorded_qty is None:
        raise TicketError("no entry fill quantity is recorded; record the broker-confirmed fill with "
                          "--filled-qty and --filled-price before generating exits")
    qty = _confirmed_fill_qty(a.filled_qty, order.get("qty"))
    if qty != recorded_qty:
        raise TicketError("exit quantity must match the recorded filled quantity (%s); update the fill "
                          "record after checking the broker's current position"
                          % _str_num(recorded_qty))
    print("EXIT ORDERS ONLY for %s: %s shares." % (bare(order.get("symbol")), qty))
    print("First cancel any unfilled remainder of the entry order. Then verify this is the broker's")
    print("current net position; use this quantity only if it matches the shares actually held.")
    for role, params in legs(order, cfg, quantity=qty)[1:]:
        print("  %s:" % role)
        for line in json.dumps(params, indent=2).splitlines():
            print("    " + line)
    print("These are separate orders. When one exit fills, cancel the other immediately.")
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    awaiting_first_fill = bool(rec and rec.get("status") == "placed"
                               and rec.get("kind") == "entry"
                               and rec.get("filled_qty") is None and a.status == "placed")
    if not rec or (rec.get("status") != "approved" and not awaiting_first_fill):
        raise TicketError("order %d has no approval awaiting a result, or its result is already recorded; "
                          "use reconcile-fill for later partial fills" % a.index)
    if bool(a.filled_qty) != bool(a.filled_price):
        raise TicketError("--filled-qty and --filled-price must be supplied together")
    filled_qty = None
    if a.filled_qty is not None:
        if a.status != "placed":
            raise TicketError("fill details can only be recorded with --status placed")
        filled_qty = _confirmed_fill_qty(a.filled_qty, rec.get("qty"))
        price = _f(a.filled_price)
        if price is None or not math.isfinite(price) or price <= 0:
            raise TicketError("--filled-price must be a positive number")
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop", "broker_order_id", "note"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if cfg.get("paper", True):
        new["paper"] = True
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    if filled_qty is not None:
        new["filled_qty"] = filled_qty
        new["filled_price"] = price
        if rec.get("kind") == "entry":
            actual_loss = filled_max_loss(filled_qty, price, rec.get("stop"))
            new["max_loss"] = actual_loss
            if actual_loss is None:
                new["fill_risk_warning"] = "actual fill is at or below the recorded stop, or the stop is missing; daily loss cannot be safely estimated"
    if a.status == "placed" and filled_qty is not None:
        fill_id = "entry:%s:%s:%d" % (today, fp, filled_qty)
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], filled_qty, price, today, fill_id=fill_id)
    state["orders"].append(new)
    if a.status == "placed" and filled_qty is not None:
        _append_fill_log(state, now, today, rec["symbol"], rec["side"], filled_qty, price)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and filled_qty is not None:
        print("Added the fill to %s." % a.portfolio)
        if new.get("fill_risk_warning"):
            print("WARNING: " + new["fill_risk_warning"])
        elif rec.get("kind") == "entry":
            print("Daily loss record updated to the actual fill risk: %s." % _money(new["max_loss"]))
    elif a.status == "placed":
        print("When the first fill is confirmed, attach it to this order with:")
        print("  ticket.py result --index %d --status placed --filled-qty Q --filled-price P" % a.index)
    if a.status == "placed" and rec.get("kind") == "entry" and has_legs(order):
        if filled_qty is not None:
            print("Entry fill recorded: %d shares. Confirm the broker's current position, then generate exits:"
                  % filled_qty)
            print("  ticket.py exits --index %d --filled-qty %d" % (a.index, filled_qty))
        else:
            print("Do NOT place exits yet. Record the actual fill with --filled-qty Q and --filled-price P,")
            print("then confirm the broker's current position and run ticket.py exits --index %d --filled-qty Q."
                  % a.index)
        print("When either exit fills, cancel the other one immediately.")
    return 0


def cmd_reconcile_fill(a):
    """Update an entry's cumulative broker fill; apply only the newly filled delta."""
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "placed" or rec.get("kind") != "entry":
        raise TicketError("reconcile-fill needs an entry already recorded as placed")
    old_qty = rec.get("filled_qty")
    old_price = _f(rec.get("filled_price"))
    if old_qty is None or old_price is None:
        raise TicketError("no earlier fill is recorded; use result --status placed with --filled-qty and --filled-price first")
    new_qty = _confirmed_fill_qty(a.filled_qty, order.get("qty"))
    if new_qty <= int(old_qty):
        raise TicketError("cumulative --filled-qty must be greater than the previously recorded fill (%s)"
                          % _str_num(old_qty))
    avg_price = _f(a.filled_price)
    if avg_price is None or not math.isfinite(avg_price) or avg_price <= 0:
        raise TicketError("--filled-price must be the broker-confirmed cumulative average fill price")
    delta_qty = new_qty - int(old_qty)
    delta_value = avg_price * new_qty - old_price * int(old_qty)
    delta_price = delta_value / delta_qty
    if not math.isfinite(delta_price) or delta_price <= 0:
        raise TicketError("new cumulative average price implies an invalid incremental fill price")
    new = dict(rec)
    new.update(at=ist_iso(now), date=today, status="placed", filled_qty=new_qty,
               filled_price=avg_price, fill_update=True, fill_delta_qty=delta_qty,
               fill_delta_price=round(delta_price, 8))
    actual_loss = filled_max_loss(new_qty, avg_price, rec.get("stop"))
    new["max_loss"] = actual_loss
    if actual_loss is None:
        new["fill_risk_warning"] = "actual cumulative fill is at or below the recorded stop, or the stop is missing; daily loss cannot be safely estimated"
    else:
        new.pop("fill_risk_warning", None)
    fill_id = "entry:%s:%s:%d" % (today, fp, new_qty)
    apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], delta_qty, delta_price, today, fill_id=fill_id)
    state["orders"].append(new)
    _append_fill_log(state, now, today, rec["symbol"], rec["side"], delta_qty, delta_price)
    write_json(a.state, state)
    print("Updated cumulative entry fill: %d shares at average %s." % (new_qty, _p(avg_price)))
    print("Added only the new %d-share fill (incremental price %s) to %s." %
          (delta_qty, _p(delta_price), a.portfolio))
    if new.get("fill_risk_warning"):
        print("WARNING: " + new["fill_risk_warning"])
    else:
        print("Daily loss record updated to the actual cumulative fill risk: %s." % _money(actual_loss))
    print("Verify the broker's current net position before generating exits:")
    print("  ticket.py exits --index %d --filled-qty %d" % (a.index, new_qty))
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-30")
    fill_id = (a.fill_id or "").strip()
    if a.fill_id is not None and not fill_id:
        raise TicketError("--fill-id cannot be blank")
    if len(fill_id) > 200:
        raise TicketError("--fill-id must be 200 characters or fewer")
    qty, price = _f(a.qty), _f(a.price)
    if qty is None or price is None:
        raise TicketError("--qty and --price must be finite numbers")
    state = load_state(a.state)
    prior_log = next((r for r in state.get("orders", [])
                      if r.get("status") == "filled" and r.get("fill_id") == fill_id), None) if fill_id else None
    if prior_log:
        same = (bare(prior_log.get("symbol")) == bare(a.symbol)
                and prior_log.get("side") == a.side
                and _f(prior_log.get("qty")) == qty and _f(prior_log.get("price")) == price
                and prior_log.get("date") == date)
        if not same:
            raise TicketError("this --fill-id was already used with different fill details")
        print("This broker fill ID is already recorded; no portfolio or log changes were made.")
        return 0
    pf_before = read_json(a.portfolio, "portfolio", required=False) or {}
    already_applied = fill_id in pf_before.get("applied_fill_ids", []) if fill_id else False
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, qty, price, date, fill_id=fill_id or None)
    if not already_applied:
        if fill_id:
            _append_fill_log(state, now, date, a.symbol, a.side, qty, price, fill_id=fill_id)
            write_json(a.state, state)
        else:
            log_fill(a.state, now, date, a.symbol, a.side, qty, price)
    elif fill_id:
        # Recover the state log if a previous run saved the portfolio before the log.
        _append_fill_log(state, now, date, a.symbol, a.side, qty, price, fill_id=fill_id)
        write_json(a.state, state)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (a.side, a.qty, bare(a.symbol),
                                                                _money(a.price), date, a.portfolio,
                                                                _money(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = bare(a.symbol)
    now, today = _clock(a)
    source = "the broker's positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or abs(qty - round(qty)) > 1e-9:
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 34")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if bare(p.get("ticker") or p.get("symbol")) == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check your broker's positions "
                              "and pass --qty if you do hold some" % (sym, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if bare(plan.get("symbol")) == sym else None) \
        or cfg.get("flat_by") or "15:15"
    order = {"symbol": sym, "exchange": "NSE", "side": "sell", "qty": int(round(qty)),
             "product": str(cfg.get("product") or "MIS").upper(), "order_type": "market", "validity": "DAY",
             "why": "flat by %s IST: closes the %s intraday trade before your broker's auto square-off "
             "(qty from %s)" % (flat_by, sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "nse-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until nse-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against your broker's positions before you say yes." % source)
    print("Next:")
    print("  1. nse-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    print("  3. FIRST cancel the open stop-loss and take-profit orders for these shares (the connector's")
    print("     orders / cancel tools, each behind Needs approval). If they stay open next to this sell, a")
    print("     leg can fill after it and sell the same shares again, which opens an unwanted short MIS")
    print("     position. Then place the sell with the ticket.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _money(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Order tickets and your yes (nse-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this India time (IST), e.g. \"2026-10-30 10:15\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")

    def common(p):
        p.add_argument("--orders", default="nse/orders.json")
        p.add_argument("--config", default="nse/config.json")
        p.add_argument("--risk", default="nse/state/risk.json")
        p.add_argument("--state", default="nse/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("exits", help="render exit orders for the broker-confirmed filled quantity only")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--filled-qty", required=True)
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card (or a paper fill)")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p = sub.add_parser("reconcile-fill", help="add a newly confirmed partial fill to an entry")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--filled-qty", required=True, help="new cumulative filled quantity from broker")
    p.add_argument("--filled-price", required=True, help="new cumulative average fill price from broker")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in nse/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--fill-id", help="unique broker trade/execution ID; makes retries safe")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--config", default="nse/config.json")
    p.add_argument("--state", default="nse/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from your broker's positions (default: nse/portfolio.json)")
    p.add_argument("--orders", default="nse/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="nse/portfolio.json")
    p.add_argument("--plan", default="nse/plan.json")
    p.add_argument("--config", default="nse/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="nse/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "exits": cmd_exits, "approve": cmd_approve, "decline": cmd_decline,
                "result": cmd_result, "reconcile-fill": cmd_reconcile_fill, "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
