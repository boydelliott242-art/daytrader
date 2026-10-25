"""trader-paper tests: the paper broker on the TSLA 2026-10-19 bracket from CONTRACTS (8 shares, stop
358.32, target 370.04), offline. tests/fixtures/trader-paper/ holds the orders file, the approval
(ticket.py approve at 10:16 New York), the risk PASS (check.py) and a paper config; the Yahoo-shaped
chart is built here from tests/fixtures/dt-cache, and some sessions are hand-built."""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from dt import bars, clock, net  # noqa: E402

paper = load("trader-paper", "paper")
ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

PFIX = os.path.join(FIX, "trader-paper")
DT_CACHE = os.path.join(FIX, "dt-cache")
DATE = "2026-10-19"
STOP, TARGET, QTY = 358.32, 370.04, 8
BOOK = os.path.join("trader", "paper", "positions.json")
STATE = os.path.join("trader", "state", "broker.json")
PF = os.path.join("trader", "portfolio.json")
SIM = "simulated from 5-minute bars"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = paper.main(argv)
    return code, out.getvalue(), err.getvalue()


def session_rows(sym="TSLA", date=DATE):
    """[[t, o, h, l, c, v], ...] of one full session from the fixture store (copies, safe to edit)."""
    return [list(r) for r in bars.load_store(sym, "bars5m", DT_CACHE)["sessions"][date]]


def yahoo_chart(rows, sym="TSLA"):
    """A Yahoo v8 chart document (what net.chart parses) from store rows: timestamps, indicators.quote
    and a meta block with exchangeTimezoneName America/New_York."""
    q = {"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows],
         "close": [r[4] for r in rows], "volume": [r[5] for r in rows]}
    meta = {"currency": "USD", "symbol": sym, "exchangeName": "NMS", "instrumentType": "EQUITY",
            "regularMarketTime": rows[-1][0] + 300, "gmtoffset": -14400, "timezone": "EDT",
            "exchangeTimezoneName": "America/New_York", "dataGranularity": "5m", "range": "1d"}
    return {"chart": {"result": [{"meta": meta, "timestamp": [r[0] for r in rows],
                                  "indicators": {"quote": [q]}}], "error": None}}


def replay(rows, k, stop, target, flat_k):
    """The test's own walk of the bars from the fill bar k: stop before target inside a bar, else
    the flat-by bar's close. -> (reason, slot, raw price)."""
    for j in range(k, flat_k + 1):
        if rows[j][3] <= stop:
            return "stop", j, stop
        if rows[j][2] >= target:
            return "target", j, target
    return "flat", flat_k, rows[flat_k][4]


def bar(o, h, l, c, t=0):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1}


class Fingerprint(unittest.TestCase):
    def test_same_as_the_upstream_and_the_fixtures_agree(self):
        order = read(os.path.join(PFIX, "orders.json"))["orders"][0]
        fp = ticket.fingerprint(order)
        self.assertEqual(paper.fingerprint(order), fp)
        self.assertEqual(check.fingerprint(order), fp)
        self.assertEqual((order["qty"], order["stop_loss_stop_price"], order["take_profit_limit_price"]),
                         ("8", "358.32", "370.04"))
        appr = read(os.path.join(PFIX, "broker.json"))["orders"][0]
        self.assertEqual((appr["fingerprint"], appr["status"], appr["at"], appr["date"], appr["paper"]),
                         (fp, "approved", "2026-10-19T10:16:00-04:00", DATE, True))
        risk = read(os.path.join(PFIX, "risk.json"))
        self.assertEqual((risk["date"], risk["results"][0]["fingerprint"], risk["results"][0]["verdict"]),
                         (DATE, fp, "PASS"))
        self.assertIs(read(os.path.join(PFIX, "config.json"))["paper"], True)


class Math(unittest.TestCase):
    def test_the_first_bar_that_starts_at_or_after_the_yes(self):
        def et(h, m, s=0):
            return datetime.datetime(2026, 10, 19, h, m, s)
        self.assertEqual(paper.first_slot_at_or_after(DATE, et(10, 16)), 10)      # the 10:20 bar
        self.assertEqual(paper.first_slot_at_or_after(DATE, et(10, 15)), 9)       # on the grid: that bar
        self.assertEqual(paper.first_slot_at_or_after(DATE, et(10, 15, 1)), 10)
        self.assertEqual(paper.first_slot_at_or_after(DATE, et(9, 0)), 0)
        self.assertEqual(paper.first_slot_at_or_after(DATE, et(15, 55)), 77)
        self.assertIsNone(paper.first_slot_at_or_after(DATE, et(15, 56)))
        self.assertEqual(paper.first_slot_at_or_after("2026-11-27", et(12, 51)), 41)   # early close: 42 slots
        self.assertIsNone(paper.first_slot_at_or_after("2026-11-27", et(13, 0)))

    def test_the_approval_time_is_new_york(self):
        at = paper.parse_at("2026-10-19T10:16:00-04:00")
        self.assertEqual(clock.to_eastern(at), datetime.datetime(2026, 10, 19, 10, 16))
        self.assertEqual(paper.parse_at("2026-10-19T14:16:00Z"), at)
        self.assertEqual(paper.parse_at("2026-10-19 10:16"), at)
        for bad in ("yesterday", None, ""):
            with self.assertRaises(paper.PaperError):
                paper.parse_at(bad)

    def test_fills_to_the_cent(self):
        o = 365.0450134277344                                   # the real 10:20 open
        self.assertEqual(paper.buy_fill(o, 4), 365.12)          # 365.045 x (1 + 2 / 10000) = 365.118
        self.assertEqual(paper.buy_fill(o, 0), 365.05)
        self.assertEqual(paper.sell_fill(358.32, 4), 358.25)    # 358.32 - 0.0717
        self.assertEqual(paper.sell_fill(367.2298889160156, 4), 367.16)
        self.assertEqual(paper.sell_fill(100, 4), 99.98)
        self.assertEqual(paper.sell_fill(100, 0), 100.0)

    def test_settle_stop_before_target_then_the_flat_bar(self):
        pos = {"id": "x", "entry": 100.02, "stop": 95.0, "target": 105.0, "entry_slot": 10, "qty": 1, "date": DATE}
        flat_k = clock.flat_slot(DATE)
        self.assertEqual(flat_k, 76)                             # the 15:50 bar, which closes at 15:55
        quiet = [bar(100, 100.5, 99.5, 100) for _ in range(78)]
        both = list(quiet)
        both[20] = bar(100, 106, 94, 100)                        # touches both sides: the stop
        r = paper.settle(both, pos, flat_k, 4)
        self.assertEqual((r["reason"], r["slot"], r["raw"], r["exit"], r["bars_held"]), ("stop", 20, 95.0, 94.98, 11))
        tgt = list(quiet)
        tgt[20] = bar(100, 105.0, 99.9, 104)
        r = paper.settle(tgt, pos, flat_k, 4)
        self.assertEqual((r["reason"], r["slot"], r["raw"], r["exit"], r["slippage"]), ("target", 20, 105.0, 105.0, 0.0))
        early = list(quiet)
        early[10] = bar(100.02, 100.1, 94.9, 95)                 # the fill bar itself counts
        self.assertEqual(paper.settle(early, pos, flat_k, 4)["slot"], 10)
        before = list(quiet)
        before[9] = bar(100, 110, 90, 100)                       # a bar before the fill never counts
        self.assertEqual(paper.settle(before, pos, flat_k, 4)["reason"], "flat")
        flat = list(quiet)
        flat[76] = bar(100, 100.4, 99.6, 100.2)
        r = paper.settle(flat, pos, flat_k, 4)
        self.assertEqual((r["reason"], r["slot"], r["raw"], r["exit"]), ("flat", 76, 100.2, 100.18))
        late = list(quiet)
        late[77] = bar(100, 110, 99, 100)                        # after the flat-by bar: never seen
        self.assertEqual(paper.settle(late, pos, flat_k, 4)["reason"], "flat")
        self.assertIsNone(paper.settle(quiet[:30], pos, flat_k, 4))      # still open
        self.assertIsNone(paper.settle(quiet[:76], pos, flat_k, 4))      # the flat bar has not closed
        self.assertIsNone(paper.settle([], pos, flat_k, 4))
        pos2 = dict(pos, entry_slot=None, entry_bar_start="10:20")        # the slot from the bar's start
        self.assertEqual(paper.settle(early, pos2, flat_k, 4)["slot"], 10)

    def test_settle_on_an_early_close(self):
        d = "2026-11-27"
        self.assertEqual(clock.slots_of_day(d), 42)
        flat_k = clock.flat_slot(d)
        self.assertEqual(flat_k, 40)                             # 12:55, moved up with the 1:00 p.m. close
        s = [bar(100, 100.5, 99.5, 100) for _ in range(42)]
        s[40] = bar(100, 100.3, 99.7, 100.1)
        s[41] = bar(100, 120, 80, 100)
        pos = {"id": "x", "entry": 100.02, "stop": 95.0, "target": 105.0, "entry_slot": 5, "qty": 1, "date": d}
        r = paper.settle(s, pos, flat_k, 4)
        self.assertEqual((r["reason"], r["slot"], r["exit"]), ("flat", 40, 100.08))


class Desk(unittest.TestCase):
    """A trader/ folder in a temp dir with the fixtures, the chart built from the store, no network."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs(os.path.join("trader", "state"))
        shutil.copy(os.path.join(PFIX, "orders.json"), os.path.join("trader", "orders.json"))
        shutil.copy(os.path.join(PFIX, "config.json"), os.path.join("trader", "config.json"))
        shutil.copy(os.path.join(PFIX, "risk.json"), os.path.join("trader", "state", "risk.json"))
        shutil.copy(os.path.join(PFIX, "broker.json"), STATE)
        self.rows = session_rows()
        self.chart = os.path.join(self.d, "TSLA.json")
        self.write_chart(self.rows)
        self._chart = net.chart
        net.chart = self.no_network

    def tearDown(self):
        net.chart = self._chart
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def no_network(self, *args, **kw):
        raise AssertionError("the tests never touch the network")

    def write_chart(self, rows, path=None, sym="TSLA"):
        write(path or self.chart, yahoo_chart(rows, sym))

    def place(self, now="2026-10-19 10:25", *extra):
        return run(["place", "--index", "1", "--now", now, "--offline", self.chart] + list(extra))

    def sync(self, now, *extra):
        return run(["sync", "--now", now, "--offline", self.chart] + list(extra))

    def statuses(self):
        return [r["status"] for r in read(STATE)["orders"]]


class Place(Desk):
    def test_fills_the_first_bar_after_the_yes_and_records_it_upstream(self):
        code, text, err = self.place()
        self.assertEqual(code, 0, err)
        book = read(BOOK)
        self.assertEqual(book["created_by"], "trader-paper")
        self.assertIn(SIM, book["note"])
        self.assertEqual(len(book["positions"]), 1)
        p = book["positions"][0]
        o = self.rows[10][1]                                     # the 10:20 bar: first to start at/after 10:16
        self.assertEqual((p["entry_bar_start"], p["entry_slot"], p["entry_time"]), ("10:20", 10, "10:25"))
        self.assertEqual(p["entry"], bars.half_up(o + o * 2 / 10000.0))
        self.assertEqual(p["entry"], 365.12)
        self.assertEqual(p["entry_open"], round(o, 4))
        self.assertAlmostEqual(p["slippage"], 365.12 - o, places=4)
        self.assertEqual((p["id"], p["symbol"], p["qty"], p["stop"], p["target"], p["flat_by"], p["date"], p["status"]),
                         ("paper-2026-10-19-1", "TSLA", 8, STOP, TARGET, "15:55", DATE, "open"))
        self.assertEqual((p["risk_per_share"], p["cost_bps"], p["source"]), (6.8, 4.0, "offline"))
        self.assertEqual(p["fingerprint"], ticket.fingerprint(read(os.path.join("trader", "orders.json"))["orders"][0]))
        # the upstream tool wrote the broker log and the portfolio
        st = read(STATE)
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        placed, filled = st["orders"][1], st["orders"][2]
        self.assertEqual((placed["broker_order_id"], placed["fingerprint"], placed["max_loss"], placed["kind"]),
                         ("paper-2026-10-19-1", p["fingerprint"], 46.88, "entry"))
        self.assertEqual(placed["at"], "2026-10-19T10:25:00-04:00")
        self.assertEqual((filled["symbol"], filled["side"], filled["qty"], filled["price"]), ("TSLA", "buy", "8", 365.12))
        pf = read(PF)
        self.assertEqual(pf["cash"], 2079.04)                    # 5000 - 8 x 365.12
        self.assertEqual((pf["positions"][0]["ticker"], pf["positions"][0]["shares"]), ("TSLA", 8))
        self.assertIn(SIM, text)
        self.assertIn("10:20", text)
        self.assertIn("365.12", text)
        self.assertIn("most people who day trade lose money", text.lower())
        self.assertNotIn("Traceback", err)
        # trader-broker now shows it as done, and trader-risk counts it
        out = io.StringIO()
        with redirect_stdout(out):
            ticket.main(["show", "--index", "1", "--orders", os.path.join("trader", "orders.json"),
                         "--config", os.path.join("trader", "config.json"), "--risk", os.path.join("trader", "state", "risk.json"),
                         "--state", STATE, "--now", "2026-10-19 10:30"])
        self.assertIn("DONE: placed", out.getvalue())
        res = check.check([read(os.path.join("trader", "orders.json"))["orders"][0]], {"budget": 5000}, state=st,
                          now=check.parse_now("2026-10-19 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        # a second place refuses: already placed, and nothing changes
        code, _, err = self.place("2026-10-19 10:30")
        self.assertEqual(code, 1)
        self.assertIn("already placed", err)
        self.assertIn("paper-2026-10-19-1", err)
        self.assertEqual(len(read(BOOK)["positions"]), 1)
        self.assertEqual(self.statuses(), ["approved", "placed", "filled"])
        self.assertEqual(read(PF)["cash"], 2079.04)

    def test_json_output(self):
        code, text, err = self.place("2026-10-19 10:25", "--json")
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["placed"]["entry"], doc["placed"]["entry_bar_start"], doc["note"]), (365.12, "10:20", SIM))

    def test_waits_for_the_bar(self):
        code, text, err = self.place("2026-10-19 10:22")
        self.assertEqual(code, 1)
        self.assertIn("waiting for the 10:20 bar", err)
        self.assertFalse(os.path.exists(BOOK))
        self.assertFalse(os.path.exists(PF))
        self.assertEqual(self.statuses(), ["approved"])
        code, _, err = self.place("2026-10-19 10:16")             # the yes itself: the 10:20 bar is not even open
        self.assertEqual(code, 1)
        self.assertIn("waiting for the 10:20 bar", err)
        code, _, err = self.place("2026-10-19 10:25")             # the bar closes at 10:25: complete
        self.assertEqual(code, 0, err)

    def test_a_lagging_chart_is_not_a_fill(self):
        self.write_chart(self.rows[:10])                          # the chart stops at the 10:15 bar
        code, _, err = self.place("2026-10-19 10:40")
        self.assertEqual(code, 1)
        self.assertIn("10:20 bar", err)
        self.assertIn("not in the chart yet", err)
        self.assertEqual(self.statuses(), ["approved"])

    def test_cost_bps_and_the_fill(self):
        code, _, err = self.place("2026-10-19 10:25", "--cost-bps", "0")
        self.assertEqual(code, 0, err)
        p = read(BOOK)["positions"][0]
        self.assertEqual((p["entry"], p["slippage"], p["cost_bps"]), (365.05, round(365.05 - self.rows[10][1], 4), 0.0))

    def test_refuses_without_the_recorded_yes(self):
        def refused(why, *extra, **kw):
            code, text, err = self.place(kw.get("now", "2026-10-19 10:25"), *extra)
            self.assertEqual(code, 1, text)
            self.assertIn(why, err)
            self.assertNotIn("Traceback", err)
            self.assertFalse(os.path.exists(BOOK))
            self.assertFalse(os.path.exists(PF))
            return err
        appr = read(STATE)["orders"][0]
        order = read(os.path.join("trader", "orders.json"))["orders"][0]
        # paper false
        cfg = read(os.path.join("trader", "config.json"))
        write(os.path.join("trader", "config.json"), dict(cfg, paper=False))
        refused("\"paper\" is false")
        write(os.path.join("trader", "config.json"), cfg)
        # no approval at all
        write(STATE, {"orders": []})
        refused("no yes recorded")
        # a yes for a different fingerprint (nine shares, not eight)
        other = dict(appr, fingerprint=ticket.fingerprint(dict(order, qty="9")))
        write(STATE, {"orders": [other]})
        refused("no yes recorded")
        # yesterday's yes
        write(STATE, {"orders": [dict(appr, at="2026-10-16T10:16:00-04:00", date="2026-10-16")]})
        refused("no yes recorded")
        # a no after the yes
        write(STATE, {"orders": [appr, dict(appr, status="declined", at="2026-10-19T10:18:00-04:00")]})
        refused("declined, not a yes")
        # the yes was recorded under a live config
        write(STATE, {"orders": [dict(appr, paper=False)]})
        refused("live config")
        # the yes is in the future
        write(STATE, {"orders": [appr]})
        refused("after", now="2026-10-19 10:10")
        # trader-risk: missing, stale, blocked, not this order
        shutil.move(os.path.join("trader", "state", "risk.json"), os.path.join("trader", "risk.bak"))
        refused("trader-risk has not checked")
        shutil.move(os.path.join("trader", "risk.bak"), os.path.join("trader", "state", "risk.json"))
        risk = read(os.path.join("trader", "state", "risk.json"))
        write(os.path.join("trader", "state", "risk.json"), dict(risk, date="2026-10-16"))
        refused("not 2026-10-19")
        blocked = dict(risk, results=[dict(risk["results"][0], verdict="BLOCK", reasons=["window: closed"])])
        write(os.path.join("trader", "state", "risk.json"), blocked)
        refused("blocked order 1: window: closed")
        write(os.path.join("trader", "state", "risk.json"), dict(risk, results=[]))
        refused("not in the last risk check")
        write(os.path.join("trader", "state", "risk.json"), risk)
        # the orders file
        refused("no order 2", "--index", "2") if False else None
        code, _, err = run(["place", "--index", "2", "--now", "2026-10-19 10:25", "--offline", self.chart])
        self.assertEqual(code, 1)
        self.assertIn("no order 2", err)
        doc = read(os.path.join("trader", "orders.json"))
        write(os.path.join("trader", "orders.json"), dict(doc, orders=[dict(order, side="sell")]))
        refused("bracket buys only")
        write(os.path.join("trader", "orders.json"), dict(doc, orders=[{"symbol": "TSLA", "side": "buy", "qty": "8",
                                                                        "type": "market", "time_in_force": "day"}]))
        refused("not a bracket")
        os.remove(os.path.join("trader", "orders.json"))
        refused("no orders file")
        write(os.path.join("trader", "orders.json"), doc)
        # no portfolio and no budget: ticket.py could not record the fill, so nothing is placed
        write(os.path.join("trader", "config.json"), {k: v for k, v in cfg.items() if k != "budget"})
        refused("no \"budget\"")
        write(os.path.join("trader", "config.json"), cfg)
        # the chart holds another symbol
        self.write_chart(self.rows, sym="AAPL")
        refused("holds AAPL, not TSLA")
        self.write_chart(self.rows)
        self.assertEqual(self.place()[0], 0)                      # everything back: it fills

    def test_the_live_path_asks_yahoo_for_one_day(self):
        calls = []

        def fake(sym, rng="60d", interval="5m"):
            calls.append((sym, rng, interval))
            return net.parse_chart(yahoo_chart(self.rows), sym)
        net.chart = fake
        code, text, err = run(["place", "--index", "1", "--now", "2026-10-19 10:25"])
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [("TSLA", "1d", "5m")])
        self.assertEqual(read(BOOK)["positions"][0]["source"], "yahoo")

        def down(*args, **kw):
            raise net.NetError("Yahoo answered HTTP 429 for /v8/finance/chart/TSLA")
        net.chart = down
        code, _, err = run(["sync", "--now", "2026-10-19 16:05"])
        self.assertEqual(code, 1)
        self.assertIn("Yahoo answered HTTP 429", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(read(BOOK)["positions"][0]["status"], "open")

    def test_a_past_day_comes_from_the_bar_store(self):
        code, text, err = run(["place", "--index", "1", "--now", "2026-10-20 09:00", "--cache-root", DT_CACHE])
        self.assertEqual(code, 0, err)
        p = read(BOOK)["positions"][0]
        self.assertEqual((p["entry"], p["source"]), (365.12, "cache"))


class Sync(Desk):
    def test_the_real_bars_flatten_at_the_close(self):
        self.assertEqual(self.place()[0], 0)
        reason, j, raw = replay(self.rows, 10, STOP, TARGET, 76)
        self.assertEqual((reason, j), ("flat", 76))                # on the real day neither side is hit after 10:20
        self.assertLess(max(r[2] for r in self.rows[10:77]), TARGET)
        self.assertGreater(min(r[3] for r in self.rows[10:77]), STOP)
        code, text, err = self.sync("2026-10-19 16:05")
        self.assertEqual(code, 0, err)
        p = read(BOOK)["positions"][0]
        self.assertEqual((p["status"], p["exit_reason"], p["exit_slot"], p["exit_bar_start"], p["exit_time"]),
                         ("closed", "flat", 76, "15:50", "15:55"))
        self.assertEqual(p["exit_price_raw"], round(raw, 4))
        self.assertEqual(p["exit"], paper.sell_fill(raw, 4))
        self.assertEqual(p["exit"], 367.16)
        self.assertEqual(p["R"], round((367.16 - 365.12) / (365.12 - STOP), 3))
        self.assertEqual(p["pnl_usd"], round((367.16 - 365.12) * 8, 2))
        self.assertEqual((p["pnl_label"], p["fill_recorded"], p["bars_held"]), (SIM, True, 67))
        st = read(STATE)
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled", "filled"])
        sell = st["orders"][-1]
        self.assertEqual((sell["side"], sell["qty"], sell["price"], sell["date"], sell["at"]),
                         ("sell", "8", 367.16, DATE, "2026-10-19T15:55:00-04:00"))
        pf = read(PF)
        self.assertEqual((pf["cash"], pf["positions"]), (round(2079.04 + 8 * 367.16, 2), []))
        self.assertEqual(check.day_trades(st, DATE), (1, ["2026-10-19 TSLA"]))
        self.assertIn("flat at 15:55", text)
        self.assertIn("367.16", text)
        self.assertIn("+$16.32", text)
        self.assertIn(SIM, text)
        self.assertIn("ticket.py fill", text)
        # nothing left to sync; the book shows it with the interval
        code, text, _ = self.sync("2026-10-19 16:10")
        self.assertEqual(code, 0)
        self.assertIn("No open paper positions", text)
        code, text, _ = run(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Closed (1)", text)
        self.assertIn("target first 0 of 1 (0 %, 95 % CI 0 to 79 %)", text)
        self.assertIn("1 closed trades say little", text)
        self.assertEqual(text.count(SIM), 4)                       # the header, the line, the totals, the note
        code, text, _ = run(["status", "--json"])
        doc = json.loads(text)
        self.assertEqual((doc["summary"]["closed"], doc["summary"]["flat"], doc["summary"]["target_rate"]), (1, 1, 0.0))
        self.assertEqual(doc["summary"]["target_ci95"][0], 0.0)
        self.assertEqual(doc["summary"]["pnl_usd_total"], 16.32)

    def test_still_open_before_the_flat_bar(self):
        self.assertEqual(self.place()[0], 0)
        code, text, err = self.sync("2026-10-19 11:00")
        self.assertEqual(code, 0, err)
        self.assertIn("still open after the 10:55 bar", text)
        self.assertEqual(read(BOOK)["positions"][0]["status"], "open")
        self.assertEqual(self.statuses(), ["approved", "placed", "filled"])
        code, text, _ = self.sync("2026-10-19 15:55")                # the 15:50 bar has just closed
        self.assertEqual(read(BOOK)["positions"][0]["status"], "closed")
        self.assertIn("flat", text)

    def test_a_bar_that_touches_both_is_the_stop(self):
        rows = self.rows
        t, o, h, l, c, v = rows[20]                                  # the 11:10 bar
        rows[20] = [t, o, 371.0, 358.0, c, v]
        self.write_chart(rows)
        self.assertEqual(self.place()[0], 0)                         # the fill bar is unchanged: 365.12
        self.assertEqual(replay(rows, 10, STOP, TARGET, 76), ("stop", 20, STOP))
        code, text, _ = self.sync("2026-10-19 11:10")                # that bar is still open
        self.assertIn("still open after the 11:05 bar", text)
        code, text, err = self.sync("2026-10-19 11:15")
        self.assertEqual(code, 0, err)
        p = read(BOOK)["positions"][0]
        self.assertEqual((p["exit_reason"], p["exit_slot"], p["exit_bar_start"], p["exit"], p["exit_price_raw"]),
                         ("stop", 20, "11:10", 358.25, STOP))
        self.assertEqual(p["R"], round((358.25 - 365.12) / 6.8, 3))
        self.assertEqual(p["pnl_usd"], -54.96)
        self.assertEqual(p["bars_held"], 11)
        sell = read(STATE)["orders"][-1]
        self.assertEqual((sell["status"], sell["side"], sell["price"], sell["at"]), ("filled", "sell", 358.25, "2026-10-19T11:15:00-04:00"))
        self.assertEqual(read(PF)["cash"], round(2079.04 + 8 * 358.25, 2))
        self.assertIn("stop in the 11:10 bar", text)
        self.assertIn("-$54.96", text)

    def test_the_target_fills_at_its_price(self):
        rows = self.rows
        t, o, h, l, c, v = rows[30]                                  # the 12:00 bar
        rows[30] = [t, o, 370.5, l, c, v]
        self.write_chart(rows)
        self.assertEqual(self.place()[0], 0)
        self.assertEqual(replay(rows, 10, STOP, TARGET, 76), ("target", 30, TARGET))
        code, text, err = self.sync("2026-10-19 16:05")
        self.assertEqual(code, 0, err)
        p = read(BOOK)["positions"][0]
        self.assertEqual((p["exit_reason"], p["exit_bar_start"], p["exit"], p["exit_slippage"]), ("target", "12:00", TARGET, 0.0))
        self.assertEqual(p["R"], round((TARGET - 365.12) / 6.8, 3))
        self.assertEqual(p["pnl_usd"], round((TARGET - 365.12) * 8, 2))
        self.assertEqual(read(PF)["cash"], round(2079.04 + 8 * TARGET, 2))
        self.assertIn("target in the 12:00 bar", text)

    def test_a_failed_record_stays_visible(self):
        self.assertEqual(self.place()[0], 0)
        pf = read(PF)
        write(PF, dict(pf, positions=[]))                            # someone emptied the portfolio by hand
        code, text, err = self.sync("2026-10-19 16:05")
        self.assertEqual(code, 1)
        p = read(BOOK)["positions"][0]
        self.assertEqual((p["status"], p["exit_reason"], p["fill_recorded"]), ("closed", "flat", False))
        self.assertIn("cannot record selling", p["fill_error"])
        self.assertIn("NOT recorded", text)
        self.assertIn("ticket.py fill", text)
        self.assertEqual(self.statuses(), ["approved", "placed", "filled"])

    def test_an_offline_folder_and_json(self):
        self.assertEqual(self.place()[0], 0)
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        self.write_chart(self.rows, os.path.join(folder, "TSLA.json"))
        code, text, err = run(["sync", "--now", "2026-10-19 16:05", "--offline", folder, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["synced"][0]["status"], doc["synced"][0]["exit_reason"], doc["synced"][0]["exit"]),
                         ("closed", "flat", 367.16))
        code, _, err = run(["sync", "--now", "2026-10-19 16:05", "--offline", os.path.join(self.d, "nowhere")])
        self.assertEqual(code, 0)                                     # nothing open any more


class Cancel(Desk):
    def test_cancel_marks_it_closed_without_a_fill(self):
        self.assertEqual(self.place()[0], 0)
        code, text, err = run(["cancel", "--id", "paper-2026-10-19-1", "--note", "closed it at the broker myself"])
        self.assertEqual(code, 0, err)
        self.assertIn("Cancelled paper-2026-10-19-1", text)
        self.assertIn("without a fill", text)
        self.assertIn("ticket.py fill", text)
        p = read(BOOK)["positions"][0]
        self.assertEqual((p["status"], p["exit_reason"], p["exit"], p["R"], p["pnl_usd"], p["fill_recorded"]),
                         ("closed", "cancelled", None, None, None, False))
        self.assertEqual(p["cancel_note"], "closed it at the broker myself")
        self.assertEqual(self.statuses(), ["approved", "placed", "filled"])      # the broker log is untouched
        self.assertEqual(read(PF)["positions"][0]["shares"], 8)
        code, _, err = run(["cancel", "--id", "paper-2026-10-19-1"])
        self.assertEqual(code, 1)
        self.assertIn("already closed", err)
        code, _, err = run(["cancel", "--id", "paper-2026-10-19-9"])
        self.assertEqual(code, 1)
        self.assertIn("no position paper-2026-10-19-9", err)
        code, text, _ = run(["status"])
        self.assertIn("cancelled by you, no fill", text)
        self.assertIn("Closed: none yet", text)
        code, text, _ = self.sync("2026-10-19 16:05")
        self.assertIn("No open paper positions", text)
        code, _, err = run(["cancel", "--id", "x", "--book", os.path.join(self.d, "none.json")])
        self.assertEqual(code, 1)

    def test_status_on_an_empty_book(self):
        code, text, _ = run(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Empty", text)
        self.assertIn(SIM, text)
        write(BOOK, {"positions": "no"})
        code, _, err = run(["status"])
        self.assertEqual(code, 1)
        self.assertIn("not a paper book", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-paper", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            exit_md = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-paper\n"))
        fm = text[4:text.index("\n---\n", 4)]
        self.assertIn("description:", fm)
        self.assertIn("argument-hint:", fm)
        self.assertGreater(len(fm), 200)
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], exit_md[exit_md.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("simulated from 5-minute bars", "ticket.py result", "ticket.py fill", "most people who day trade lose money",
                     "365.12", "10:20", "367.16", "358.32", "370.04", "stop before the target", "${claude_skill_dir}",
                     "## what it reads and writes", "## steps", "## rules", "## what it never does",
                     "never places an order", "not financial advice", "paper trade", "trader/paper/positions.json",
                     "never a projection", "95 % ci"):
            self.assertIn(must, low, must)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "guaranteed", "a buy signal"):
            self.assertNotIn(never, low, never)


class Repo(unittest.TestCase):
    FILES = [os.path.join(ROOT, "trader-paper", "paper.py"), os.path.join(ROOT, "trader-paper", "SKILL.md"),
             os.path.join(ROOT, "tests", "test_paper.py")] + [
        os.path.join(PFIX, f) for f in sorted(os.listdir(PFIX))]

    def test_no_em_dashes(self):
        for path in self.FILES:
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn("\u2014", fh.read(), path)

    def test_python_3_8_only(self):
        import re
        with open(os.path.join(ROOT, "trader-paper", "paper.py"), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("import " + "zoneinfo", "from " + "zoneinfo", ".remove" + "prefix(", ".remove" + "suffix("):
            self.assertNotIn(bad, src)
        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$")
        for bad in ("list" + "[", "dict" + "[", "tuple" + "["):
            self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad))
        self.assertNotIn("capture_output", src)                       # 3.7+, but keep to the plain Popen args


if __name__ == "__main__":
    unittest.main()
