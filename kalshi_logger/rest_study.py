"""Resting-order study: could a small (~$100) resting order capture the maker edge, or is it eaten by
not getting filled and by adverse selection?

Read-only research. It places no orders; it replays history.

For each sampled moment (taken from the calibration study's sampled trades) it imagines a resting order:
* Entertainment and Mentions: a NO bid at the best NO bid, when NO cost 30-60c (YES 40-70c).
* Crypto (comparison): a YES bid at the best YES bid, when YES cost 95-99c.

The order is about $100 (contracts = floor(100 / price)). Whether and when it would have filled comes
from Kalshi's public trade history: our order fills only after the contracts resting ahead of it at
that price (the queue) have traded. We never see the real queue, so three queue cases are reported:
* front: nobody ahead of us (best case);
* typical: the median size at the best price in that category;
* long: the 90th-percentile size.
These sizes come from the logger's own snapshots (order books, scans, crypto checks) when available.

Wait times: 5 minutes, 1 hour, and until the market closes (unfilled orders are cancelled at the end).

Measured:
* fill rates;
* profit per filled contract and per order placed, at settlement, after the maker fee in effect then;
* adverse selection: (a) settlement profit of the orders that filled vs. the same orders had they all
  filled instantly, and (b) how the mid price moved 5 and 30 minutes after a fill.
"""
import json
import logging
import math
import random
import statistics
import time
from collections import defaultdict

from . import config, http
from .calib_report import FeeBook, trade_fees, _table
from .util import num, parse_ts

log = logging.getLogger(__name__)

ORDER_DOLLARS = 100.0
WAITS = [("5 minutes", 300), ("1 hour", 3600), ("until close", None)]
CASES = ["front", "typical", "long"]
SAMPLE_PER_GROUP = 350
MAX_PER_MARKET = 3
MAX_TAPE_PAGES = 20
CANDLE_AFTER_SEC = 5400

GROUPS = {
    # name: (calibration category, YES price range of the sampled trade, side we rest on)
    "Entertainment (NO 30-60c)": ("Entertainment", 0.40, 0.70, "no"),
    "Mentions (NO 30-60c)": ("Mentions", 0.40, 0.70, "no"),
    "Crypto (YES 95-99c), comparison": ("Crypto", 0.95, 0.995, "yes"),
}
DEFAULT_QUEUE = {   # used only if the logger has no size data yet (measured on 2026-10-03/05)
    "Entertainment (NO 30-60c)": (100, 500),
    "Mentions (NO 30-60c)": (40, 200),
    "Crypto (YES 95-99c), comparison": (15000, 60000),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rest_points (
    point_id INTEGER PRIMARY KEY,
    grp TEXT, ticker TEXT, series_ticker TEXT, event_ticker TEXT, result TEXT,
    t INTEGER, close_ts INTEGER,   -- t: the sampled trade's time, then the order time once quoted
    side TEXT,                -- 'no' = NO bid; 'yes' = YES bid
    yes_bid REAL, yes_ask REAL,
    mids TEXT,                -- JSON: minute ts -> mid, from t to t+90 min (for markouts)
    done INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS rest_tape (
    trade_id TEXT PRIMARY KEY, ticker TEXT, ts INTEGER, yes_price REAL, count REAL, taker_side TEXT
);
CREATE INDEX IF NOT EXISTS rest_tape_ticker ON rest_tape(ticker, ts);
CREATE TABLE IF NOT EXISTS rest_tape_done (ticker TEXT PRIMARY KEY, from_ts INTEGER, truncated INTEGER);
CREATE TABLE IF NOT EXISTS rest_rerun_months (month TEXT PRIMARY KEY, sampled_ts INTEGER);
"""


# ---------- data pull ----------

def _sample_points(conn):
    rng = random.Random("kalshi-resting-order-study")
    have = {r[0] for r in conn.execute("SELECT DISTINCT grp FROM rest_points")}
    for grp, (cat, lo, hi, side) in GROUPS.items():
        if grp in have:
            continue
        rows = conn.execute(
            "SELECT t.ticker, t.ts, m.series_ticker, m.event_ticker, m.result, m.close_ts FROM calib_trades t "
            "JOIN calib_markets m ON m.ticker=t.ticker WHERE m.category=? AND m.result IN ('yes','no') "
            "AND t.yes_price>=? AND t.yes_price<? AND m.close_ts > t.ts", (cat, lo, hi)).fetchall()
        rng.shuffle(rows)
        per_market, picked = defaultdict(int), []
        for r in rows:
            if per_market[r["ticker"]] >= MAX_PER_MARKET:
                continue
            per_market[r["ticker"]] += 1
            picked.append(r)
            if len(picked) >= SAMPLE_PER_GROUP:
                break
        conn.executemany(
            "INSERT INTO rest_points(grp,ticker,series_ticker,event_ticker,result,t,close_ts,side) VALUES(?,?,?,?,?,?,?,?)",
            [(grp, r["ticker"], r["series_ticker"], r["event_ticker"], r["result"], r["ts"], r["close_ts"], side)
             for r in picked])
        conn.commit()


def _candles(series, ticker, t0, t1):
    params = {"start_ts": t0, "end_ts": t1, "period_interval": 1}
    for path in (f"/series/{series}/markets/{ticker}/candlesticks", f"/historical/markets/{ticker}/candlesticks"):
        try:
            cs = http.kalshi.get(path, params).get("candlesticks") or []
            if cs:
                return cs
        except http.ApiError:
            continue
    return []


def _cv(c, field):
    d = c.get(field) or {}
    return num(d.get("close_dollars", d.get("close")))


def _quotes(conn, p):
    """Best bid/ask at the moment of the order, and mids for the next 90 minutes."""
    cs = _candles(p["series_ticker"], p["ticker"], p["t"] - 600, p["t"] + CANDLE_AFTER_SEC)
    before = [c for c in cs if c["end_period_ts"] <= p["t"] + 59]
    if not before:
        conn.execute("UPDATE rest_points SET done=1, note='no quote at order time' WHERE point_id=?", (p["point_id"],))
        return
    last = before[-1]
    bid, ask = _cv(last, "yes_bid"), _cv(last, "yes_ask")
    mids = {}
    b, a = bid, ask
    for c in cs:
        if c["end_period_ts"] < p["t"]:
            continue
        b = _cv(c, "yes_bid") or b
        a = _cv(c, "yes_ask") or a
        if b is not None and a is not None:
            mids[c["end_period_ts"]] = (b + a) / 2
    # The quote is the close of that minute, so the order goes in then (no trades from before it count).
    order_ts = max(p["t"], last["end_period_ts"])
    conn.execute("UPDATE rest_points SET t=?, yes_bid=?, yes_ask=?, mids=? WHERE point_id=?",
                 (order_ts, bid, ask, json.dumps(mids), p["point_id"]))


def _tape(conn, ticker, t0, t1, cut_trades):
    """All trades in a market from t0 to t1, from the live and/or historical trade endpoints."""
    done = conn.execute("SELECT from_ts FROM rest_tape_done WHERE ticker=?", (ticker,)).fetchone()
    if done and done[0] <= t0:
        return
    truncated = 0
    for ep, a, b in (("/historical/trades", t0, min(t1, cut_trades)), ("/markets/trades", max(t0, cut_trades), t1)):
        if a >= b:
            continue
        pages = 0
        for page in http.kalshi.paginate(ep, {"ticker": ticker, "min_ts": a, "max_ts": b, "limit": 1000}, "trades",
                                         max_pages=MAX_TAPE_PAGES):
            pages += 1
            conn.executemany(
                "INSERT OR IGNORE INTO rest_tape(trade_id,ticker,ts,yes_price,count,taker_side) VALUES(?,?,?,?,?,?)",
                [(x["trade_id"], ticker, parse_ts(x["created_time"]), num(x.get("yes_price_dollars")),
                  num(x.get("count_fp")), x.get("taker_side")) for x in page])
        truncated |= int(pages >= MAX_TAPE_PAGES)
    conn.execute("INSERT OR REPLACE INTO rest_tape_done VALUES(?,?,?)", (ticker, t0, truncated))


def pull(conn, progress=print):
    conn.executescript(SCHEMA)
    _sample_points(conn)
    cut = parse_ts(http.kalshi.get("/historical/cutoff")["trades_created_ts"])
    todo = conn.execute("SELECT * FROM rest_points WHERE done=0 ORDER BY ticker, t").fetchall()
    progress(f"Resting-order study: {len(todo)} order moments to fetch...")
    first_t = {}
    for p in todo:
        first_t[p["ticker"]] = min(first_t.get(p["ticker"], p["t"]), p["t"])
    t0 = time.time()
    for i, p in enumerate(todo, 1):
        try:
            _tape(conn, p["ticker"], first_t[p["ticker"]], p["close_ts"] + 60, cut)
            _quotes(conn, p)
            conn.execute("UPDATE rest_points SET done=1 WHERE point_id=? AND done=0", (p["point_id"],))
        except http.ApiError as exc:
            log.warning("Skipping %s for now: %s", p["ticker"], exc)
        conn.commit()
        if i % 50 == 0 or i == len(todo):
            progress(f"  {i}/{len(todo)} (about {(time.time() - t0) / i * (len(todo) - i) / 60:.0f} min left)")


# ---------- queue sizes from the logger's own data ----------

def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None and x > 0)
    return xs[min(int(q * len(xs)), len(xs) - 1)] if xs else None


def queue_sizes(conn):
    """(typical, long) queue size per group, plus a note on where the numbers came from."""
    out = {}
    for grp, (cat, lo, hi, side) in GROUPS.items():
        sizes = []
        try:
            if side == "no":
                # NO bid size at best = YES ask size; NO 30-60c <=> YES ask 40-70c.
                sizes += [r[0] for r in conn.execute(
                    "SELECT s.ask_size FROM scan_snapshots s JOIN markets m USING(market_id) WHERE m.category=? "
                    "AND s.ask_cc BETWEEN ? AND ?", (cat, int(lo * 10000), int(hi * 10000)))]
                sizes += [r[0] for r in conn.execute(
                    "SELECT b.nb1_size FROM book_snapshots b JOIN markets m USING(market_id) WHERE m.category=? "
                    "AND b.nb1_cc BETWEEN ? AND ?", (cat, int((1 - hi) * 10000), int((1 - lo) * 10000)))]
            else:
                sizes += [r[0] for r in conn.execute(
                    "SELECT s.bid_size FROM scan_snapshots s JOIN markets m USING(market_id) WHERE m.category=? "
                    "AND s.bid_cc BETWEEN ? AND ?", (cat, int(lo * 10000), int(hi * 10000)))]
                sizes += [r[0] for r in conn.execute(
                    "SELECT bid_size FROM crypto_fv WHERE yes_bid BETWEEN ? AND ?", (lo, hi))]
        except Exception:
            sizes = []
        typ, lng = _pct(sizes, 0.5), _pct(sizes, 0.9)
        if typ is None:
            typ, lng = DEFAULT_QUEUE[grp]
            out[grp] = (typ, lng, "no logged sizes yet; using sizes measured on Oct 3-5, 2026")
        else:
            out[grp] = (typ, lng, f"from {len([s for s in sizes if s]):,} logged snapshots")
    return out


# ---------- simulation ----------

def order_price(point):
    """Price we would pay per contract: the best NO bid (1 - YES ask) or the best YES bid."""
    level = point["yes_ask"] if point["side"] == "no" else point["yes_bid"]
    if level is None:
        return None
    return 1 - level if point["side"] == "no" else level


def _in_range(price, lo, hi, side):
    """lo/hi are YES prices; for a NO order the range flips (YES 40-70c = NO 30-60c)."""
    if price is None:
        return False
    a, b = (1 - hi, 1 - lo) if side == "no" else (lo, hi)
    return a - 1e-9 <= price <= b + 1e-9


def simulate(point, trades, queue, wait, child=None, through_fills_all=True):
    """Contracts filled and time of the first fill for one resting order of about $100.

    NO bid at q (= YES ask at a = 1 - q): fills against takers buying YES at a. A taker buying YES above a
    means everything at a was used up first (the queue ahead is gone).
    YES bid at p: fills against takers selling YES (buying NO) at p; a trade below p means the same.

    child=None: the whole $100 order rests at once (the original study).
    child=S: realistic sizing. At most S contracts rest at a time; when they have all filled, the next S are
    posted at the same price at the back of the queue (the same queue size again), until $100 has filled
    or the wait ends. A partly filled child is cancelled at the end.
    through_fills_all=True: a trade through our price fills everything we have resting (the original study).
    False: it clears the queue ahead but fills us only with that trade's own contracts.
    """
    side = point["side"]
    if side == "no":
        level = point["yes_ask"]
        price = None if level is None else 1 - level
    else:
        level = point["yes_bid"]
        price = level
    if level is None or not 0 < price < 1:
        return None
    size = math.floor(ORDER_DOLLARS / price)
    step = size if child is None else max(1, min(int(child), size))
    end = point["close_ts"] if wait is None else min(point["close_ts"], point["t"] + wait)
    cur, got, ahead, filled = step, 0.0, float(queue), 0.0
    first = full = None
    for tr in trades:
        if tr["ts"] <= point["t"] or tr["ts"] > end or tr["yes_price"] is None:
            continue
        if side == "no":
            hits = tr["taker_side"] == "yes" and abs(tr["yes_price"] - level) < 1e-9
            through = tr["taker_side"] == "yes" and tr["yes_price"] > level + 1e-9
        else:
            hits = tr["taker_side"] == "no" and abs(tr["yes_price"] - level) < 1e-9
            through = tr["taker_side"] == "no" and tr["yes_price"] < level - 1e-9
        if not (hits or through):
            continue
        vol = tr["count"] or 0.0
        if through:
            ahead = 0.0
            if through_fills_all:
                vol = math.inf
        take = min(max(vol - ahead, 0.0), cur - got)
        ahead = max(ahead - vol, 0.0)
        if take > 0:
            got += take
            filled += take
            if first is None:
                first = tr["ts"]
        if filled >= size:
            full = tr["ts"]
            break
        if got >= cur:   # this child is done: post the next one at the back of the queue
            cur, got, ahead = min(step, size - filled), 0.0, float(queue)
    return {"price": price, "size": size, "filled": filled, "first": first, "full": full}


def _cluster_ratio(by_event, z=1.96):
    """Ratio of sums with a range (95% by default) treating each event as one unit."""
    N = sum(n for n, _ in by_event.values())
    D = sum(d for _, d in by_event.values())
    if D <= 0:
        return None, None, None
    r = N / D
    var = sum((n - r * d) ** 2 for n, d in by_event.values()) / (D * D)
    se = math.sqrt(var)
    return r, r - z * se, r + z * se


def _pc(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _rng(v, lo, hi):
    return "n/a" if v is None else f"{_pc(v)} ({_pc(lo)} to {_pc(hi)})"


SIZINGS = [
    # label, child size (None = whole order at once; "typical" = typical size at the best price),
    # whether a trade through our price fills everything we have resting
    ("A. as in the first run: whole $100 order rests; a trade through our price fills all of it", None, True),
    ("B. whole $100 order rests; a trade through our price fills only its own contracts", None, False),
    ("C. realistic: pieces no bigger than the typical size, re-queued after each fill", "typical", False),
]
REALISTIC = SIZINGS[2]


def _evaluate(gp, tapes, feebook, side, queue, wait, child, through_all):
    """Fill rate, $ filled, returns and markouts for one set of order moments under one set of rules."""
    fills, waits, marks5, marks30, dollars = [], [], [], [], []
    instant = defaultdict(lambda: [0.0, 0.0])
    filled_ev = defaultdict(lambda: [0.0, 0.0])
    placed_ev = defaultdict(lambda: [0.0, 0.0])
    for p in gp:
        sim = simulate(p, tapes.get(p["ticker"], []), queue, wait, child=child, through_fills_all=through_all)
        if sim is None:
            continue
        won = 1.0 if p["result"] == side else 0.0
        ft, fm = feebook.at(p["series_ticker"], p["t"])
        # Instant-fill benchmark: the whole order at our price, as if filled immediately.
        _, f_inst = trade_fees(ft, fm, sim["price"], sim["size"])
        instant[p["event_ticker"]][0] += sim["size"] * won - f_inst
        instant[p["event_ticker"]][1] += sim["size"] * sim["price"]
        fills.append(sim["filled"] > 0)
        dollars.append(sim["filled"] * sim["price"])
        if sim["filled"] > 0:
            _, f_m = trade_fees(ft, fm, sim["price"], sim["filled"])
            filled_ev[p["event_ticker"]][0] += sim["filled"] * won - f_m
            filled_ev[p["event_ticker"]][1] += sim["filled"] * sim["price"]
            placed_ev[p["event_ticker"]][0] += sim["filled"] * (won - sim["price"]) - f_m
            waits.append((sim["first"] - p["t"]) / 60)
            mids = {int(k): v for k, v in json.loads(p["mids"] or "{}").items()}
            for horizon, bucket in ((300, marks5), (1800, marks30)):
                later = [v for k, v in sorted(mids.items()) if k >= sim["first"] + horizon]
                if later:
                    m = later[0] if side == "yes" else 1 - later[0]
                    bucket.append(m - sim["price"])   # value of what we bought, vs. what we paid
        placed_ev[p["event_ticker"]][1] += ORDER_DOLLARS
    if not fills:
        return None
    sub1 = lambda r: tuple(None if x is None else x - 1 for x in r)
    return {"n": len(fills), "fill_rate": sum(fills) / len(fills), "dollars": statistics.mean(dollars),
            "wait": statistics.median(waits) if waits else None,
            "ret": sub1(_cluster_ratio(filled_ev)), "instant": sub1(_cluster_ratio(instant))[0],
            "per_order": _cluster_ratio(placed_ev),
            "m5": statistics.mean(marks5) if marks5 else None, "m30": statistics.mean(marks30) if marks30 else None,
            "n_marks": len(marks5)}


def _row_cells(e):
    return [f"{e['fill_rate'] * 100:.0f}%", f"${e['dollars']:.0f}",
            f"{e['wait']:.0f} min" if e["wait"] is not None else "n/a", _rng(*e["ret"]), _pc(e["instant"]),
            f"${e['per_order'][0] * ORDER_DOLLARS:+.2f}" if e["per_order"][0] is not None else "n/a"]


def build_report(conn):
    conn.executescript(SCHEMA)
    feebook = FeeBook(conn)
    queues = queue_sizes(conn)
    pts = conn.execute("SELECT * FROM rest_points WHERE done=1 AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL"
                       ).fetchall()
    tapes = defaultdict(list)
    for r in conn.execute("SELECT * FROM rest_tape ORDER BY ticker, ts"):
        tapes[r["ticker"]].append(r)
    truncated = {r[0] for r in conn.execute("SELECT ticker FROM rest_tape_done WHERE truncated=1")}

    L = ["RESTING-ORDER STUDY: CAN A ~$100 RESTING ORDER CAPTURE THE MAKER EDGE?", "=" * 72, "",
         "Read-only replay of history; no orders were placed. Each 'order' is imagined at a randomly sampled "
         "moment when the market traded in the price range below, resting at the best price on our side for "
         f"about ${ORDER_DOLLARS:.0f}. Kalshi's public trade history decides whether it would have filled: it "
         "fills only after the contracts already waiting at that price (the queue) have traded. The real queue "
         "can't be seen, so three cases are shown: 'front' (nobody ahead), 'typical' (median size at the best "
         "price) and 'long' (90th percentile).", "",
         "Sizing (realistic): $100 is worked in pieces no bigger than the typical size at the best price. Each "
         "piece joins the back of the queue, and the next is posted only after it has filled. A trade at a "
         "worse price than ours fills us only with that trade's own contracts. The last section compares this "
         "with the first run's sizing.", ""]
    rows = [[g, f"{q[0]:,.0f}", f"{q[1]:,.0f}", q[2]] for g, q in queues.items()]
    L += ["Queue sizes used (contracts waiting at the best price; the typical size is also the piece size):",
          _table(["Group", "Typical", "Long", "Source"], rows), ""]

    groups = []
    for grp, (cat, lo, hi, side) in GROUPS.items():
        gp_all = [p for p in pts if p["grp"] == grp]
        # Keep only moments where our resting price itself is in the range (the sampled trade was, but the
        # quote a minute later may not be).
        gp = [p for p in gp_all if _in_range(order_price(p), lo, hi, side)]
        if not gp:
            continue
        groups.append((grp, side, gp))
        n_mk = len({p["ticker"] for p in gp})
        n_ev = len({p["event_ticker"] for p in gp})
        tr_note = sum(1 for p in gp if p["ticker"] in truncated)
        spreads = [p["yes_ask"] - p["yes_bid"] for p in gp]
        L += [grp.upper(), f"{len(gp)} order moments in {n_mk} markets ({n_ev} events); median spread "
              f"{statistics.median(spreads) * 100:.0f}c. ({len(gp_all) - len(gp)} more were dropped because the "
              "best price had moved out of the range by the time of the order.)"
              + (f" {tr_note} moments are in markets whose trade history was too long to read in full (fills "
                 "after that point are missed, so their fill rates are understated)." if tr_note else ""), ""]
        typ, lng, _ = queues[grp]
        qmap = {"front": 0.0, "typical": typ, "long": lng}
        rows, markout_rows = [], []
        for wlabel, wait in WAITS:
            for case in CASES:
                e = _evaluate(gp, tapes, feebook, side, qmap[case], wait, typ, False)
                if e is None:
                    continue
                rows.append([wlabel, case, e["n"]] + _row_cells(e))
                if case == "typical":
                    markout_rows.append([wlabel, e["n_marks"], f"{e['m5'] * 100:+.1f}c" if e["m5"] is not None
                                         else "n/a", f"{e['m30'] * 100:+.1f}c" if e["m30"] is not None else "n/a"])
        L += [_table(["Wait", "Queue", "Orders", "Filled (any)", "Avg $ filled", "Typical wait to first fill",
                      "Return/$ on filled contracts (95% range)", "If every order filled instantly",
                      "Expected profit per $100 order placed"], rows), ""]
        L += ["Short-term adverse selection (typical queue): how the value of what we bought moved after the fill, "
              "in cents per contract (negative = the price moved against us right after we were filled):",
              _table(["Wait", "Fills with price data", "+5 min", "+30 min"], markout_rows), ""]

    L += ["HOW MUCH THE SIZING MATTERS (typical queue)"] + [label for label, _, _ in SIZINGS] + [""]
    for grp, side, gp in groups:
        typ = queues[grp][0]
        rows = []
        for wlabel, wait in WAITS:
            for label, child, through_all in SIZINGS:
                e = _evaluate(gp, tapes, feebook, side, typ, wait, typ if child == "typical" else None, through_all)
                if e is not None:
                    rows.append([wlabel, label[:2].strip(". ")] + _row_cells(e))
        L += [grp, _table(["Wait", "Sizing", "Filled (any)", "Avg $ filled", "Typical wait to first fill",
                           "Return/$ on filled contracts (95% range)", "If every order filled instantly",
                           "Expected profit per $100 order placed"], rows), ""]

    L += ["HOW TO READ THIS",
          "- 'Filled (any)': share of orders that got at least some contracts by the end of the wait. Unfilled "
          "orders are cancelled and cost nothing.",
          "- 'Avg $ filled': dollars actually invested per order, counting unfilled orders as $0 (out of $100).",
          "- 'Return/$ on filled contracts': profit per dollar at settlement, after the maker fee in effect at the "
          "time (most series charge makers nothing), on the contracts that actually filled.",
          "- 'If every order filled instantly': the same orders at the same prices, as if all filled at once. The "
          "difference between this and the filled return is the cost of adverse selection: the fills you get "
          "tend to be the ones where the market is about to move against you.",
          "- 'Expected profit per $100 order placed' includes the orders that never filled (as zero), so it is "
          "what one order is worth on average.",
          "",
          "WHAT THIS CAN'T SEE",
          "- The real queue ahead of you (others cancel and re-post); hence the three queue cases.",
          "- Fills from orders posted at the same price after yours are correctly excluded (first come, first "
          "served), but a better price posted by someone else after you would take fills from you; that is "
          "only partly captured (trades at their better price don't count for you).",
          "- Whether your own order would have changed what others did (a bigger order showing in the book can "
          "put takers off or attract them). Realistic sizing keeps the order no bigger than what usually rests.",
          "- 1-minute prices are used for the price at the moment of the order and for the after-fill moves. The "
          "order goes in at the close of the minute containing the sampled trade. Within a minute the real best "
          "price can differ from the 1-minute close, so some orders are a cent better or worse than the true best.",
          "- These price ranges were picked because the calibration study found them mispriced in this same "
          "history (January 2025 on), so the 'filled instantly' returns are in-sample and likely flatter the "
          "edge. The useful comparison is between rows (fill rates, filled vs. instant), not the level itself.",
          "- Entertainment and Mentions moments come from markets that had settled by the time of the "
          "calibration pull, so very long-dated markets are under-represented.",
          ""]
    return "\n".join(L)


# ---------- pre-registered re-test (H5, H6; see docs/preregistration-calibration-rerun.md) ----------
# Frozen on October 6, 2026. Do not change: the clean re-test depends on these exact numbers.

RERUN_GROUPS = {
    # hypothesis: (category, YES price range of the sampled trade, side, child size, typical queue, long queue)
    "H5": ("Mentions", 0.40, 0.70, "no", 24, 24, 102),
    "H6": ("Entertainment", 0.40, 0.70, "no", 200, 200, 1363),
}
RERUN_Z = 2.64          # 97.5% for two looks, split again over the three wait times
RERUN_MIN_EVENTS = 30


def sample_fresh(conn, months):
    """Every sampled fresh trade in the H5/H6 ranges becomes an order moment (at most 3 per market per
    month, chosen with a fixed seed). months: (label, start, end) of complete months from October 6, 2026."""
    conn.executescript(SCHEMA)
    for month, t0, t1 in months:
        if conn.execute("SELECT 1 FROM rest_rerun_months WHERE month=?", (month,)).fetchone():
            continue
        rng = random.Random(f"kalshi-resting-order-rerun-{month}")
        for hid, (cat, lo, hi, side, *_rest) in RERUN_GROUPS.items():
            rows = conn.execute(
                "SELECT t.ticker, t.ts, m.series_ticker, m.event_ticker, m.close_ts FROM calib_trades t "
                "JOIN calib_markets m ON m.ticker=t.ticker WHERE m.category=? AND t.yes_price>=? AND t.yes_price<? "
                "AND t.ts>=? AND t.ts<? ORDER BY t.ticker, t.ts", (cat, lo, hi, t0, t1)).fetchall()
            rng.shuffle(rows)
            per_market, picked = defaultdict(int), []
            for r in rows:
                if per_market[r["ticker"]] < MAX_PER_MARKET:
                    per_market[r["ticker"]] += 1
                    picked.append(r)
            conn.executemany(
                "INSERT INTO rest_points(grp,ticker,series_ticker,event_ticker,t,close_ts,side) VALUES(?,?,?,?,?,?,?)",
                [(f"rerun {hid}", r["ticker"], r["series_ticker"], r["event_ticker"], r["ts"], r["close_ts"], side)
                 for r in picked])
        conn.execute("INSERT INTO rest_rerun_months VALUES(?,?)", (month, int(time.time())))
        conn.commit()


def pull_rerun(conn, progress=print):
    """Quotes and trade history for fresh order moments whose market has settled (others wait)."""
    cut = parse_ts(http.kalshi.get("/historical/cutoff")["trades_created_ts"])
    todo = conn.execute(
        "SELECT p.*, m.result AS m_result, m.close_ts AS m_close FROM rest_points p JOIN calib_markets m "
        "ON m.ticker=p.ticker WHERE p.grp LIKE 'rerun %' AND p.done=0 AND m.result IN ('yes','no') "
        "ORDER BY p.ticker, p.t").fetchall()
    progress(f"Resting-order re-test: {len(todo)} settled order moments to fetch...")
    for i, p in enumerate(todo, 1):
        try:
            conn.execute("UPDATE rest_points SET result=?, close_ts=? WHERE point_id=?",
                         (p["m_result"], p["m_close"], p["point_id"]))
            p = conn.execute("SELECT * FROM rest_points WHERE point_id=?", (p["point_id"],)).fetchone()
            _tape(conn, p["ticker"], p["t"], p["close_ts"] + 60, cut)
            _quotes(conn, p)
            conn.execute("UPDATE rest_points SET done=1 WHERE point_id=? AND done=0", (p["point_id"],))
        except http.ApiError as exc:
            log.warning("Skipping %s for now: %s", p["ticker"], exc)
        conn.commit()
        if i % 100 == 0:
            progress(f"  {i}/{len(todo)}")


def rerun_results(conn, hid, data_end, z=RERUN_Z):
    """Per wait time: orders, share filled, events and effective events with fills, and the return per $1
    on filled contracts after fees with its range. Typical queue and realistic sizing (frozen)."""
    conn.executescript(SCHEMA)
    cat, lo, hi, side, child, queue, _ = RERUN_GROUPS[hid]
    feebook = FeeBook(conn)
    pts = [p for p in conn.execute(
        "SELECT * FROM rest_points WHERE grp=? AND done=1 AND t<? AND result IN ('yes','no') "
        "AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL", (f"rerun {hid}", data_end))
        if _in_range(order_price(p), lo, hi, side)]
    tapes = defaultdict(list)
    for tk in {p["ticker"] for p in pts}:
        tapes[tk] = conn.execute("SELECT * FROM rest_tape WHERE ticker=? ORDER BY ts", (tk,)).fetchall()
    out = []
    for wlabel, wait in WAITS:
        by_ev = defaultdict(lambda: [0.0, 0.0])
        n = nf = 0
        for p in pts:
            sim = simulate(p, tapes[p["ticker"]], queue, wait, child=child, through_fills_all=False)
            if sim is None:
                continue
            n += 1
            if sim["filled"] > 0:
                nf += 1
                ft, fm = feebook.at(p["series_ticker"], p["t"])
                _, fee = trade_fees(ft, fm, sim["price"], sim["filled"])
                won = 1.0 if p["result"] == side else 0.0
                by_ev[p["event_ticker"]][0] += sim["filled"] * won - fee
                by_ev[p["event_ticker"]][1] += sim["filled"] * sim["price"]
        cost = [d for _, d in by_ev.values()]
        eff = (sum(cost) ** 2 / sum(c * c for c in cost)) if cost else 0.0
        r, rlo, rhi = _cluster_ratio(by_ev, z)
        out.append({"wait": wlabel, "orders": n, "filled": nf, "events": len(by_ev), "effective": eff,
                    "ret": None if r is None else r - 1, "lo": None if rlo is None else rlo - 1,
                    "hi": None if rhi is None else rhi - 1})
    return out


def rerun_verdict(rows):
    testable = [r for r in rows if r["events"] >= RERUN_MIN_EVENTS and r["effective"] >= RERUN_MIN_EVENTS]
    if not testable:
        return "NOT YET TESTABLE"
    if any(r["lo"] is not None and r["lo"] > 0 for r in testable):
        return "SUPPORTED"
    return "NOT SUPPORTED"


def write_report(conn):
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORT_DIR / "resting_orders.txt"
    path.write_text(build_report(conn) + "\n", encoding="utf-8")
    return path
