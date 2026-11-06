"""nse-exit tests. Bars for the ATR stop reuse tests/fixtures/nse-entry/ (the SYNTHETIC Reliance-like
five-minute session of Friday 2026-10-30, see test_entry.py). Prices are rupees on the NSE tick of 0.05:
a long's stop rounds DOWN to the tick, a target rounds UP."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("nse-exit", "exit")
entry = load("nse-entry", "entry")

RELI = os.path.join(FIX, "nse-entry", "RELIANCE-2026-10-30-5m.json")

# nse/plan.json for the session, exactly as the contract writes it
REEL_PLAN = {"created_by": "nse-exit", "symbol": "RELIANCE", "side": "buy", "date": "2026-10-30", "setup": "orb",
             "entry": 2840.55, "entry_time": "10:00 AM", "entry_bar": 8, "interval": "5m",
             "opening_range": {"high": 2825.0, "low": 2764.85, "minutes": 15},
             "stop": 2794.9, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 2886.2, "target_r": 1.0, "target_label": "1R (one times the risk)",
             "risk_per_share": 45.65, "reward_risk": 1.0, "flat_by": "15:15", "tick": 0.05}
REEL_ENTRY = {"created_by": "nse-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy", "date": "2026-10-30",
              "status": "triggered", "time": "10:00 AM", "bar_start": "09:55", "bar_index": 8, "price": 2840.55,
              "level": 2825.0, "interval": "5m", "opening_range": {"high": 2825.0, "low": 2764.85, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-10-30T04:30:00Z"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=2825.0, ol=2764.85, atr=None, n=None, tick=Decimal("0.05")):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, tick)


class Math(unittest.TestCase):
    def test_the_session_or_mid_1r(self):
        p = c(2840.55, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("2794.90"))          # floor to the tick of 2794.925
        self.assertEqual(p["risk"], Decimal("45.65"))
        self.assertEqual(p["target"], Decimal("2886.20"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertIn("rounded down to the ₹0.05 tick", p["stop_detail"])
        self.assertEqual(p["tick"], Decimal("0.05"))

    def test_the_default_2r(self):
        p = c(2840.55, "or-mid")
        self.assertEqual(p["target"], Decimal("2931.85"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_paise(self):
        p = c(2840.5500244140625, "or-mid", "1R", 2825.0, 2764.8500976562)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("2840.55"), Decimal("2794.90"), Decimal("2886.20")))

    def test_other_stops(self):
        p = c(2840.55, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("2764.85"), Decimal("75.70"), Decimal("2991.95")))
        p = c(2840.55, "price:2794.929")
        self.assertEqual(p["stop"], Decimal("2794.90"))             # rounds away from the entry, to the tick
        p = c(2840.55, "price:2794.95")
        self.assertEqual(p["stop"], Decimal("2794.95"))             # already on the tick
        p = c(2840.55, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("2812.10"), Decimal("28.45"), Decimal("2897.45")))
        p = c(2840.55, "atr:1.5", atr=25.1807, n=9)
        self.assertEqual(p["stop"], Decimal("2802.75"))             # 2840.55 - 37.77105 = 2802.77895, down to the tick
        self.assertEqual(p["stop_label"], "1.5 x ATR(9) under the entry")

    def test_the_rupee_sign_and_commas_are_accepted_in_prices(self):
        self.assertEqual(exitp.parse_stop("price:₹2,794.90"), ("price", Decimal("2794.90")))
        self.assertEqual(exitp.parse_target("price:₹2,910"), ("price", Decimal("2910")))
        self.assertEqual(exitp.D("₹1,00,000.50"), Decimal("100000.50"))

    def test_target_rounds_up_to_the_tick_and_price_targets(self):
        p = c(100, "price:99.35", "1.5R", None, None)
        self.assertEqual(p["target"], Decimal("101.00"))            # 100 + 1.5 x 0.65 = 100.975, up to the tick
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")
        p = c(100, "price:99.01", "1.5R", None, None)
        self.assertEqual((p["stop"], p["target"]), (Decimal("99.00"), Decimal("101.50")))
        p = c(2840.55, "or-mid", "price:2910")
        self.assertEqual(p["target"], Decimal("2910.00"))
        self.assertEqual(p["reward_risk"], Decimal("1.52"))
        self.assertEqual(p["target_label"], "your price (1.52R)")
        self.assertEqual(p["target_r"], Decimal("1.52"))
        self.assertEqual(c(2840.55, "or-mid", "price:2910.02")["target"], Decimal("2910.05"))

    def test_the_tick_is_configurable(self):
        p = c(2840.55, "or-mid", "1R", tick=Decimal("0.01"))
        self.assertEqual((p["stop"], p["target"]), (Decimal("2794.92"), Decimal("2886.18")))
        p = c(2840.55, "or-mid", "1R", tick=Decimal("0.25"))
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("2794.75"), Decimal("45.80"), Decimal("2886.50")))
        with self.assertRaises(exitp.PlanError):
            c(2840.55, "or-mid", tick=Decimal("0"))

    def test_refusals_say_why(self):
        cases = [("price:2840.55", "2R", "at or above the entry"),
                 ("price:2845", "2R", "at or above the entry"),
                 ("or-mid", "price:2840", "at or below the entry"),
                 ("price:2840.40", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(2840.55, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 2,840.55 is 1.42: a Rs 1.45 risk is allowed, Rs 1.40 is not
        self.assertEqual(c(2840.55, "price:2839.10")["risk"], Decimal("1.45"))
        with self.assertRaises(exitp.PlanError):
            c(2840.55, "price:2839.15")
        # a risk under one tick is refused even on a cheap stock
        with self.assertRaises(exitp.PlanError) as cm:
            c(10.02, "price:10.00")
        self.assertIn("one tick", str(cm.exception))
        self.assertEqual(c(10.00, "price:9.95")["risk"], Decimal("0.05"))
        with self.assertRaises(exitp.PlanError):
            c(2790.00, "or-mid")                                    # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(2840.55, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(2840.55, "or-mid", oh=2764.85, ol=2825.0)

    def test_money_is_rupees_with_indian_grouping(self):
        self.assertEqual(exitp.money("2794.9"), "₹2,794.90")
        self.assertEqual(exitp.money(100000), "₹1,00,000.00")
        self.assertEqual(exitp.money("1234567.5"), "₹12,34,567.50")
        self.assertEqual(exitp.money("-45.65"), "-₹45.65")
        self.assertEqual(exitp.money(5), "₹5.00")

    def test_symbols_and_yahoo_names(self):
        self.assertEqual(exitp.norm("reliance.ns"), "RELIANCE")
        self.assertEqual(exitp.norm("m&m"), "M&M")
        self.assertEqual(exitp.yahoo_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(exitp.yahoo_symbol("M&M"), "M&M.NS")
        self.assertEqual(exitp.yahoo_symbol("bajaj-auto"), "BAJAJ-AUTO.NS")

    def test_the_session_hours_and_calendar(self):
        self.assertEqual((exitp.OPEN, exitp.CLOSE), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertEqual(exitp.session(datetime.date(2026, 10, 30)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(exitp.session(datetime.date(2026, 10, 20)))     # Dussehra
        self.assertIsNone(exitp.session(datetime.date(2026, 10, 31)))     # Saturday
        self.assertEqual(exitp.NSE_EARLY_CLOSE, {})
        self.assertEqual(exitp.to_ist(datetime.datetime(2026, 10, 30, 4, 30)), datetime.datetime(2026, 10, 30, 10, 0))
        self.assertEqual(exitp.parse_now("2026-10-30 10:00"), datetime.datetime(2026, 10, 30, 4, 30))
        self.assertEqual(exitp.parse_now("2026-10-30 10:00 IST"), datetime.datetime(2026, 10, 30, 4, 30))

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("RELIANCE", "5m", None, RELI)
        day = exitp.datetime.date(2026, 10, 30)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=8)
        self.assertEqual(len(bars), 9)                              # 9:15 ... the 09:55 entry bar
        # by hand: the true range of each bar, the first one without the overnight gap
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 2825.0 - 2764.85, places=4)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 9)
        self.assertAlmostEqual(atr, sum(trs) / 9, places=9)
        self.assertAlmostEqual(atr, 25.1, places=4)                 # 225.9 / 9
        # later in the day it uses the last 14 bars; the file's in-progress 10:30 bar and tick never count
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 15)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)

    def test_the_offline_chart_must_be_the_same_symbol(self):
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.load_chart("TCS", "5m", None, RELI)
        self.assertIn("holds RELIANCE.NS, not TCS", str(cm.exception))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("nse")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-30 10:01"] + list(args))

    def read_plan(self):
        with open(os.path.join("nse", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "RELIANCE", "--offline", RELI, "--now", "2026-10-30 10:00", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Stop          ₹2,794.90  opening-range midpoint", text)
        self.assertIn("rounded down to the ₹0.05 tick", text)
        self.assertIn("Target        ₹2,886.20  1R (one times the risk)", text)
        self.assertIn("Risk a share  ₹45.65 (1.61% of the entry)", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:15 IST", text)
        self.assertIn("NSE tick of ₹0.05", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/nse-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 2931.85)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-30 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("₹2,931.85", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-02 10:20"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "reliance", "--entry", "2840.55", "--or-high", "2825", "--or-low", "2764.85",
                                  "--time", "10:00", "--entry-bar", "8", "--date", "2026-10-30", "--target", "1R",
                                  "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])
        code, text, _ = self.plan("--symbol", "RELIANCE.NS", "--entry", "2840.55", "--or-high", "2825",
                                  "--or-low", "2764.85", "--date", "2026-10-30", "--json")
        self.assertEqual(json.loads(text)["symbol"], "RELIANCE")

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", RELI, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (2802.9, 37.65, 2915.85))   # 2840.55 - 1.5 x 25.10
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 9))
        self.assertAlmostEqual(p["atr"], 25.1, places=4)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", RELI, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_the_tick_from_the_flag_and_the_config(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--target", "1R", "--tick", "0.25", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["target"], p["tick"]), (2794.75, 2886.5, 0.25))
        self.write(os.path.join("nse", "config.json"), {"tick": "0.01"})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual((p["stop"], p["target"], p["tick"]), (2794.92, 2886.18, 0.01))
        code, _, err = self.plan("--tick", "0")
        self.assertEqual(code, 1)
        self.assertIn("tick must be above 0", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "2825.05", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (2825.05, 2794.9, None))

        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, setup="level", level=2825.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("this pack plans longs only", err)
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        code, _, err = self.plan("--stop", "price:2840.10")
        self.assertIn("longs only", err)
        os.remove(os.path.join("nse", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--entry", "2840.55", "--or-high", "2825")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_and_late_entries(self):
        self.write(os.path.join("nse", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("nse", "config.json"), {"flat_by": "15:20", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:20")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        os.remove(os.path.join("nse", "config.json"))

        code, text, _ = self.plan("--target", "1R", "--json")           # no config: the 15:15 default
        self.assertEqual(json.loads(text)["flat_by"], "15:15")
        code, _, err = self.plan("--flat-by", "15:45")
        self.assertEqual(code, 1)
        self.assertIn("after the 3:30 PM close", err)
        code, _, err = self.plan("--flat-by", "25:00")
        self.assertIn("IST time like 15:15", err)
        self.write(os.path.join("nse", "entry.json"), dict(REEL_ENTRY, time="3:20 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_no_half_day_flat_by_unless_the_nse_announces_one(self):
        half = dict(REEL_ENTRY, date="2026-11-27")
        self.write(os.path.join("nse", "entry.json"), half)
        code, text, _ = run(exitp, ["plan", "--now", "2026-11-27 10:01"])
        self.assertEqual(code, 0)
        self.assertIn("Flat by       15:15 IST", text)                 # a normal day on the NSE
        saved = (exitp.NSE_EARLY_CLOSE, exitp.EARLY)
        exitp.NSE_EARLY_CLOSE = {"2026-11-27": "test session"}
        exitp.EARLY = datetime.time(12, 30)
        try:
            code, text, _ = run(exitp, ["plan", "--now", "2026-11-27 10:01"])
            self.assertEqual(code, 0)
            self.assertIn("Flat by       12:15 IST", text)             # 15:15 minus the 3-hour gap
            self.assertIn("shortened session", text)
            code, _, err = run(exitp, ["plan", "--now", "2026-11-27 10:01", "--flat-by", "14:00"])
            self.assertEqual(code, 1)
            self.assertIn("after the 12:30 PM close", err)
        finally:
            exitp.NSE_EARLY_CLOSE, exitp.EARLY = saved

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "nse-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/nse-timemachine", "/nse-budget", "longs only", "₹2,794.90", "₹2,886.20", "₹45.65",
                     "2,931.85", "too tight to survive the spread", "0.05", "15:15", "ist", "round", "mis", "nse"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "nyse", "new york"):
            self.assertNotIn(never, low)
        self.assertNotRegex(low, r"\$\d")


if __name__ == "__main__":
    unittest.main()
