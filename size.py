#!/usr/bin/env python3
"""Position sizing for nse-budget (NSE cash equity, rupees). Standard library only, no network.

Default mode: sizes ONE intraday trade from nse/plan.json (written by
nse-exit) to your budget and your max loss per trade, then writes one
order (entry with a stop-loss and a take-profit leg) to nse/orders.json.
It never places anything: nse-risk checks the order and nse-broker asks
you yes or no. nse-broker turns the order into an entry ticket plus a
separate stop-loss ticket and take-profit ticket, because most Indian
brokers have discontinued intraday bracket orders.

    python3 size.py                                  # nse/plan.json + nse/config.json
    python3 size.py --write                          # write nse/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 2845.50 --write          # a limit order at 2845.50
    python3 size.py --now "2026-10-30 10:15" --json  # pin the clock (India time, IST)

How many shares. Whole shares only (cash equity has a lot size of 1):

    shares = floor of the smallest of
      max loss       max_loss_per_trade_inr / risk per share (entry minus stop)
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in nse/portfolio.json, if you have one)

Prices are whole ticks (config "tick", default 0.05): the stop is rounded
DOWN to a tick, the target UP, the entry to the nearest tick, all in exact
integer paise. The entry it sizes at: a limit order's limit price; for a
market order the plan's entry, or the price in nse/prices.json if that is
fresh (under 15 minutes old) and higher, because a price that ran up since
the plan means a bigger loss at the same stop. Zero shares (the stop is too
far away for your max loss) is a plain refusal: no order is written. So is a
reward:risk under min_reward_risk (the price ran toward the target since the
plan).

It also prints an ESTIMATE of the intraday round-trip cost (brokerage, STT,
exchange charges, SEBI fee, stamp duty, GST) at the target and at the stop.
Rates are in the "costs" block of the config; check your broker's contract note.

Weights mode (kept from v1, no longer the headline): split a budget across a
list of stocks by weight. Always whole shares (no fractional shares in India),
so it needs a price for each stock.

    python3 size.py --weights RELIANCE=22.04,TCS=17.14,INFY=12.62,ITC=10.86,SBIN=9.20 --prices nse/prices.json
    python3 size.py --targets my-weights.json --budget 100000 --write

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   tickers are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_inr is dropped and its weight
                   shared out, so you never place a Rs 2 order
  5. whole shares  floor each slice, then one more share at a time for the
                   stock furthest below its slice while cash lasts

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
import json
import math
import os
import sys

# v2 defaults: the contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 50000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 500,
    "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:30",
    "flat_by": "15:15",
    "tick": 0.05,
    "product": "MIS",
    "weights_product": "CNC",
    "broker": "zerodha",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "costs": {},
}
# Intraday (MIS) cost model, percent of turnover unless noted. An ESTIMATE: check your broker's contract note.
COSTS = {
    "brokerage_flat": 20.0,       # rupees per executed order, capped at brokerage_pct of turnover
    "brokerage_pct": 0.03,
    "stt_sell_pct": 0.025,        # securities transaction tax, sell side only
    "exchange_txn_pct": 0.00297,  # NSE transaction charge, both sides
    "sebi_pct": 0.0001,           # SEBI turnover fee, both sides
    "stamp_buy_pct": 0.003,       # stamp duty, buy side only
    "gst_pct": 18.0,              # on brokerage + exchange charge + SEBI fee
}
TICK = 0.05
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}


class SizeError(Exception):
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
        raise SizeError("--now wants a time like \"2026-10-30 10:15\" (India time) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-19T14:52:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": bare(r["ticker"]), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """RELIANCE=22.04,TCS=17.14 -> {ticker: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants TICKER=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[bare(t)] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[bare(t)] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def round_tick(x, tick=TICK, mode="nearest"):
    """Round a price to a multiple of tick in exact integer paise.
    mode: "nearest" (half up), "down" (stops) or "up" (targets)."""
    t = int(round(float(tick) * 100))
    p = float(x) * 100.0
    if mode == "down":
        n = math.floor(p / t + EPS)
    elif mode == "up":
        n = math.ceil(p / t - EPS)
    else:
        n = math.floor(p / t + 0.5 + EPS)
    return n * t / 100.0


def shares_for(inr, per_share):
    """Whole shares that fit in inr at per_share, in exact integer paise."""
    u, p = int(round(float(inr) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def bare(sym):
    """RELIANCE.NS -> RELIANCE. NSE symbols keep their & and -  (M&M, BAJAJ-AUTO)."""
    s = str(sym or "").strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    return s


def _price(x):
    """2845.5 -> "2845.50"."""
    return "%.2f" % round(float(x) + 1e-9, 2)


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13"."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def _group(n):
    """Indian digit grouping of a non-negative integer: 1234567 -> "12,34,567"."""
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


def inr(x):
    """1234567.5 -> "Rs 12,34,567.50" with the rupee sign; whole rupees drop the .00."""
    x = float(x)
    neg = x < 0
    x = abs(x)
    whole = int(x)
    frac = int(round((x - whole) * 100))
    if frac == 100:
        whole, frac = whole + 1, 0
    body = _group(whole) + ("" if frac == 0 else ".%02d" % frac)
    return ("-" if neg else "") + "\u20b9" + body


money = inr


def estimate_costs(qty, buy, sell, costs=None):
    """Estimated intraday round-trip cost in rupees for qty shares bought at buy and sold at sell."""
    k = dict(COSTS)
    k.update(costs or {})
    tb, ts = qty * float(buy), qty * float(sell)
    per_order = lambda t: min(float(k["brokerage_flat"]), t * float(k["brokerage_pct"]) / 100.0)
    brokerage = per_order(tb) + per_order(ts)
    stt = ts * float(k["stt_sell_pct"]) / 100.0
    txn = (tb + ts) * float(k["exchange_txn_pct"]) / 100.0
    sebi = (tb + ts) * float(k["sebi_pct"]) / 100.0
    stamp = tb * float(k["stamp_buy_pct"]) / 100.0
    gst = (brokerage + txn + sebi) * float(k["gst_pct"]) / 100.0
    parts = {"brokerage": brokerage, "stt": stt, "exchange": txn, "sebi": sebi, "stamp": stamp, "gst": gst}
    total = sum(parts.values())
    out = {k2: round(v, 2) for k2, v in parts.items()}
    out["total"] = round(total, 2)
    out["per_share"] = round(total / qty, 4) if qty else 0.0
    return out


# ---------------------------------------------------------------- plan mode (the default)

def tick_of(c):
    t = _f((c or {}).get("tick"))
    return t if t and t > 0 else TICK


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in nse/portfolio.json, if there is one
    today  India date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    tick = tick_of(c)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = bare(plan.get("symbol"))
    if not sym:
        raise SizeError("the plan has no symbol (run nse-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) intraday trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India: make a new plan with nse-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run nse-exit); an order with a stop-loss and a "
                            "take-profit needs entry, stop and target" % name)
    blocked = {bare(b) for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in nse/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    if limit is not None:
        ref, ref_from = round_tick(limit, tick), "limit"
    elif price is not None and round_tick(price, tick) > round_tick(entry_plan, tick):
        ref, ref_from = round_tick(price, tick), "price"
    else:
        ref, ref_from = round_tick(entry_plan, tick), "plan"
    stop, target = round_tick(stop, tick, "down"), round_tick(target, tick, "up")
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with nse-exit" % (_price(target), _price(ref)))
    risk_c = int(round((ref - stop) * 100))
    reward_c = int(round((target - ref) * 100))
    risk = risk_c / 100.0

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (inr(budget), inr(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in nse/portfolio.json minus the %s buffer" % (inr(cash), inr(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": shares_for(max_loss, risk),
                       "how": "%s max loss / %s risk a share" % (inr(max_loss), _price(risk))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), inr(budget), inr(cap), _price(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (inr(max_order), _price(ref))})
    limits.append({"name": "cash", "inr": available, "per": ref, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, inr(available), _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss = round(qty * risk_c / 100.0, 2)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s), more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with nse-exit."
                       % (inr(risk), _price(ref), _price(stop), inr(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, inr(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_c < _f(c["min_reward_risk"]) * risk_c - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so nse-risk "
                   "would block it. No order. Skip it, or make a new plan with nse-exit."
                   % (reward_c / float(risk_c), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, inr(cost), inr(c["min_order_inr"])))
    costs = None
    if qty > 0:
        costs = {"at_target": estimate_costs(qty, ref, target, c.get("costs")),
                 "at_stop": estimate_costs(qty, ref, stop, c.get("costs"))}
    return {
        "symbol": sym, "exchange": "NSE", "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_tick(entry_plan, tick),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target, "tick": tick,
        "risk_per_share": risk, "reward_risk": round(reward_c / float(risk_c), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "max_loss": loss, "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "product": str(c.get("product") or "MIS").upper(),
        "costs": costs, "refusal": refusal,
    }


def bracket_order(s):
    """The one proposed order: NSE fields plus its stop_loss and take_profit legs, why and plan.
    nse-broker turns it into an entry ticket, a stop-loss ticket and a take-profit ticket."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "exchange": "NSE", "side": "buy", "qty": int(s["qty"]),
         "product": s["product"], "order_type": s["order_type"], "validity": "DAY"}
    if s["order_type"] == "limit":
        o["limit_price"] = round(s["entry"], 2)
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "intraday trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop)"
           % (setup, s["symbol"], s["qty"], {"max loss": "%s max loss per trade" % inr(s["max_loss_per_trade_inr"] or 0),
                                             "position cap": "position cap", "max order": "max order",
                                             "cash": "cash after the buffer"}[s["bound_by"]],
              s["qty"], _price(s["risk_per_share"]), inr(s["max_loss"])))
    o.update({
        "stop_loss": round(s["stop"], 2),
        "take_profit": round(s["target"], 2),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    })
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f. Tick %s."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"], _num(s["tick"])))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-58s %3d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s. Max loss at the stop: %d x %s = %s."
                 % (s["qty"], s["symbol"], inr(s["cost"]), _price(s["entry"]), s["qty"],
                    _price(s["risk_per_share"]), inr(s["max_loss"])))
    if s.get("costs"):
        ct, cs = s["costs"]["at_target"], s["costs"]["at_stop"]
        lines.append("Estimated intraday costs (brokerage, STT, exchange, SEBI, stamp, GST): about %s if it exits at "
                     "the target, %s at the stop. Net at the stop about %s lost; net at the target about %s gained. "
                     "Estimate only: check your broker's contract note."
                     % (inr(ct["total"]), inr(cs["total"]), inr(s["max_loss"] + cs["total"]),
                        inr(s["qty"] * (s["target"] - s["entry"]) - ct["total"])))
    lines.append("A stop is an order, not a promise: in a fast market, or at a circuit limit, it can fill "
                 "below %s or not fill at all." % _price(s["stop"]))
    lines.append("")
    lines.append("Proposed order (nothing has been placed):")
    how = "limit %s" % _price(order["limit_price"]) if order["order_type"] == "limit" else "market"
    lines.append("  buy %d %s on NSE, %s, %s, day, stop-loss %s, take-profit %s"
                 % (order["qty"], order["symbol"], order["product"], how, _price(order["stop_loss"]),
                    _price(order["take_profit"])))
    lines.append("nse-broker splits this into an entry ticket plus a separate stop-loss and take-profit ticket "
                 "(most Indian brokers no longer offer intraday bracket orders).")
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (nse-alerts rings, "
                     "nse-broker flatten). Your broker may auto-square-off MIS positions around 15:15 to 15:25 "
                     "and charge for it." % s["flat_by"])
    lines.append("")
    lines.append("Next: nse-risk checks it, then nse-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {bare(b) for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) rupees per ticker with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: rupees}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to whole rupees (unit 1) or paise (unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference (usually one unit) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(rupees, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(rupees[t] / prices[t] + EPS)) if rupees[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = rupees[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, whole=None, cents=False, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict)."""
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in nse/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order = float(cfg.get("min_order_inr", 500) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    whole = True   # no fractional shares on NSE: always whole shares
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01 if (cents or whole) else 1.0

    if whole:
        missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
        if missing:
            raise SizeError("whole shares need a price for: %s (add them to nse/prices.json, "
                            "e.g. with nse-alerts: alerts.py quote %s --write, or pass --prices)"
                            % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        rupees = round_to_unit(exact, capped, order, cap, unit)
        shares = None
        if whole:
            shares = whole_shares(rupees, prices, order, cap, sum(rupees.values()))
            rupees = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if rupees[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if whole and shares is not None and shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs %s, more than its slice; it cannot be bought in whole shares"
                      % inr(prices[worst["ticker"]]))
        else:
            reason = "its slice (%s) is under your %s minimum order" % (
                inr(rupees[worst["ticker"]]), inr(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        p = {"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
             "source_weight": r["source_weight"], "inr": rupees[t],
             "capped": t in capped}
        if prices.get(t):
            p["price"] = prices[t]
            p["shares"] = shares[t] if whole else round(rupees[t] / prices[t], 4)
        positions.append(p)
    invested = round(sum(p["inr"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_inr": min_order, "whole_shares": bool(whole),
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, source="", product="CNC"):
    """Weights mode: proposed buy orders, one per position (whole shares, delivery by default)."""
    out = []
    for p in result["positions"]:
        if p["inr"] <= 0 or not int(p.get("shares") or 0):
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), inr(result["budget"]))
        out.append({"symbol": p["ticker"], "exchange": "NSE", "side": "buy", "qty": int(p["shares"]),
                    "product": product, "order_type": "market", "validity": "DAY", "why": why})
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (inr(r["budget"]), _num(r["cash_buffer_pct"]), inr(r["cash_buffer"]),
                    inr(r["investable"]), _num(r["max_position_pct"]), inr(r["cap"])))
    lines.append("")
    has_price = any("price" in p for p in r["positions"])
    head = "%-12s %-22s %8s %14s" % ("ticker", "name", "weight", "rupees")
    if has_price:
        head += " %12s %8s" % ("price", "shares")
    lines.append(head + "  note")
    for p in r["positions"]:
        row = "%-12s %-22s %7.2f%% %14s" % (p["ticker"], (p["name"] or "")[:22], p["weight"], inr(p["inr"]))
        if has_price:
            row += " %12s %8s" % (inr(p["price"]) if "price" in p else "", p.get("shares", ""))
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-12s %-22s %8s %14s" % ("", "invested", "", inr(r["invested"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "cash buffer", "", inr(r["cash_buffer"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "left idle", "", inr(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: %s" % (inr(r["idle"]), (
            "whole shares do not divide evenly" if r["whole_shares"]
            else "every stock is at its cap; add stocks or raise max_position_pct")))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (nothing has been placed):")
    for o in orders:
        lines.append("  %s %d %s on NSE, %s, %s, %s" % (o["side"], o["qty"], o["symbol"], o["product"],
                                                        o["order_type"], o["validity"].lower()))
    lines.append("")
    lines.append("These buys have no stop. nse-risk blocks them while \"require_stop\" is true and applies "
                 "your intraday limits to every buy.")
    lines.append("Next: nse-risk checks every order, then nse-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "nse-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until nse-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run nse-exit first, or use --weights for a split)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 2845.50")
    sym = bare((plan or {}).get("symbol"))
    notes, price = [], None
    ppath = a.prices or ("nse/prices.json" if os.path.exists("nse/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_tick(p, tick_of(cfg)) > round_tick(_f(plan.get("entry")) or 0, tick_of(cfg)):
                notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), to_ist(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = bracket_order(s)
    if order and a.write:
        src = "nse/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order (entry, stop-loss, take-profit) to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("nse/prices.json") and not a.prices:
        prices = load_prices("nse/prices.json")
    whole = True if a.whole_shares else None
    result = size(rows, cfg, a.budget, prices, whole, False, a.top)
    orders = orders_from(result, source, str(cfg.get("weights_product") or "CNC").upper())
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Size an intraday trade to your budget and max loss (nse-budget).")
    ap.add_argument("--plan", default="nse/plan.json", help="the trade plan (default nse/plan.json)")
    ap.add_argument("--config", default="nse/config.json", help="rules (default nse/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget (rupees) in the config")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default nse/prices.json if it exists)")
    ap.add_argument("--portfolio", default="nse/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this India time (IST), e.g. \"2026-10-30 10:15\"")
    ap.add_argument("--write", nargs="?", const="nse/orders.json", metavar="PATH",
                    help="write the proposed order (default nse/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: RELIANCE=22.04,TCS=17.14")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    g.add_argument("--whole-shares", action="store_true", help="whole shares (always on in India; needs --prices)")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top or a.whole_shares:
            raise SizeError("--top and --whole-shares work with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
