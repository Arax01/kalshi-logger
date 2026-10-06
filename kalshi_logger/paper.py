"""Forward paper trading: simulated resting NO orders on live Mentions and Entertainment markets.

Read-only. Nothing is sent to Kalshi; orders exist only as rows in paper_orders.

Placing (every few minutes, from the order-book snapshots in books.py): in each Mentions or
Entertainment market the books job watches, when the best NO bid is 30-60c, a simulated order is
placed at that NO bid. It is one piece of realistic size: the smaller of $100 and the typical size
at the best price (24 contracts in Mentions, 200 in Entertainment). Everything showing at that price
is assumed to be ahead of us. A market gets a new piece once its last one has filled, or after an hour.

Filling (every 15 minutes, from Kalshi's public trades for each market with open orders): a taker
buying YES at 1 - our NO price trades first with the queue ahead, then with us. A taker buying YES
higher than that means our price was used up, so it clears the queue and fills us with its own
contracts. Two cases are kept:
* pessimistic: the queue ahead shrinks only through trades;
* optimistic: it also shrinks when the logged book shows fewer contracts at our price.
Orders are followed until the market closes. The report works out what each would have earned if
cancelled after 5 minutes, after 1 hour, or left until close, using the settled result.
"""
import logging
import math
import time
from collections import defaultdict

from . import config, db, http
from .util import num, parse_ts, to_cc

log = logging.getLogger(__name__)

MAX_TRADE_PAGES = 20
LAG_SEC = 60   # read trades up to a minute ago, so late-arriving trades aren't skipped


# ---------- placing ----------

def best(side):
    """(price, size) of the best level of one side of a Kalshi order book (listed worst to best)."""
    for price, size in reversed(side or []):
        p, s = num(price), num(size)
        if p is not None and s:
            return p, s
    return None, None


def place(conn, books, ts):
    """books: [(market_id, category, orderbook_fp dict)] just fetched by the books job."""
    placed = 0
    for mid, cat, book in books:
        if cat not in config.PAPER_PIECE:
            continue
        q, q_size = best(book.get("no_dollars"))
        if q is None or not (config.PAPER_NO_MIN - 1e-9 <= q <= config.PAPER_NO_MAX + 1e-9):
            continue
        busy = conn.execute(
            "SELECT 1 FROM paper_orders WHERE market_id=? AND placed_ts>? AND filled_pess<size LIMIT 1",
            (mid, ts - config.PAPER_REORDER_SEC)).fetchone()
        if busy:
            continue
        yb, _ = best(book.get("yes_dollars"))
        size = min(config.PAPER_PIECE[cat], math.floor(config.PAPER_ORDER_DOLLARS / q))
        conn.execute(
            "INSERT INTO paper_orders(market_id,category,placed_ts,no_price,size,queue,yes_bid,yes_ask,"
            "ahead_pess,ahead_opt) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, cat, ts, q, size, q_size, yb, round(1 - q, 4), q_size, q_size))
        # Start reading this market's trades from now, unless older orders there still need earlier trades.
        if not conn.execute("SELECT 1 FROM paper_orders WHERE market_id=? AND done=0 AND order_id<?",
                            (mid, conn.execute("SELECT last_insert_rowid()").fetchone()[0])).fetchone():
            conn.execute("INSERT OR REPLACE INTO paper_markets VALUES(?,?)", (mid, ts))
        placed += 1
    return placed


# ---------- filling ----------

def _level_size(row, price_cc):
    """Contracts showing at our NO price in a book snapshot, 0 if that price is now empty, None if unknown."""
    levels = [(row[f"nb{i}_cc"], row[f"nb{i}_size"]) for i in (1, 2, 3)]
    for cc, size in levels:
        if cc == price_cc:
            return size or 0.0
    if levels[0][0] is None or levels[0][0] < price_cc:
        return 0.0      # the best NO bid is below ours: nobody is left at our price
    return None


def apply_trade(order, tr):
    """Update one order (a dict) for one public trade. Returns {'pess': qty, 'opt': qty} filled."""
    level = round(1 - order["no_price"], 4)
    if tr["taker_side"] != "yes" or tr["yes_price"] is None:
        return {}
    hits = abs(tr["yes_price"] - level) < 1e-9
    through = tr["yes_price"] > level + 1e-9
    if not (hits or through):
        return {}
    out = {}
    for sc in ("pess", "opt"):
        ahead, filled = order[f"ahead_{sc}"], order[f"filled_{sc}"]
        vol = tr["count"] or 0.0
        if through:
            ahead = 0.0
        take = min(max(vol - ahead, 0.0), order["size"] - filled)
        order[f"ahead_{sc}"] = max(ahead - vol, 0.0)
        if take > 0:
            order[f"filled_{sc}"] = filled + take
            out[sc] = take
    return out


def apply_book(order, row):
    seen = _level_size(row, to_cc(order["no_price"]))
    if seen is not None:
        order["ahead_opt"] = min(order["ahead_opt"], seen)


def _trades(ticker, t0, t1):
    out = []
    for page in http.kalshi.paginate("/markets/trades", {"ticker": ticker, "min_ts": t0, "max_ts": t1,
                                                         "limit": 1000}, "trades", max_pages=MAX_TRADE_PAGES):
        out += [{"ts": parse_ts(x.get("created_time")), "yes_price": num(x.get("yes_price_dollars")),
                 "count": num(x.get("count_fp")), "taker_side": x.get("taker_side")} for x in page]
    return sorted((t for t in out if t["ts"] is not None), key=lambda t: t["ts"])


COLS = ("order_id", "placed_ts", "no_price", "size", "ahead_pess", "ahead_opt", "filled_pess", "filled_opt")


def run(after_gap=False):
    conn = db.connect()
    now = int(time.time())
    upto = now - LAG_SEC
    mk = conn.execute(
        "SELECT DISTINCT o.market_id, m.ticker, m.close_ts, p.trades_read_to FROM paper_orders o "
        "JOIN markets m USING(market_id) JOIN paper_markets p USING(market_id) WHERE o.done=0").fetchall()
    n_fills = n_read = 0
    for r in mk:
        t0 = r["trades_read_to"] + 1
        if t0 > upto:
            continue
        try:
            trades = _trades(r["ticker"], t0, upto)
        except http.ApiError as exc:
            # Leave this and later markets for the next run; it picks up from where each one was read to.
            log.warning("Paper trading: stopped this round at %s: %s", r["ticker"], exc)
            break
        books = conn.execute("SELECT * FROM book_snapshots WHERE market_id=? AND ts>=? AND ts<=? ORDER BY ts",
                             (r["market_id"], t0, upto)).fetchall()
        orders = [dict(zip(COLS, o)) for o in conn.execute(
            f"SELECT {','.join(COLS)} FROM paper_orders WHERE market_id=? AND done=0", (r["market_id"],))]
        # Trades and book snapshots in time order (a trade before a snapshot taken in the same second).
        events = [(t["ts"], 0, t) for t in trades] + [(b["ts"], 1, b) for b in books]
        for ts, kind, ev in sorted(events, key=lambda e: (e[0], e[1])):
            for o in orders:
                if ts <= o["placed_ts"]:
                    continue
                if kind == 1:
                    apply_book(o, ev)
                    continue
                for sc, qty in apply_trade(o, ev).items():
                    conn.execute("INSERT INTO paper_fills VALUES(?,?,?,?)", (o["order_id"], ts, sc, qty))
                    n_fills += 1
        closed = r["close_ts"] is not None and r["close_ts"] <= upto
        for o in orders:
            done = closed or (o["filled_pess"] >= o["size"] and o["filled_opt"] >= o["size"])
            conn.execute("UPDATE paper_orders SET ahead_pess=?, ahead_opt=?, filled_pess=?, filled_opt=?, done=? "
                         "WHERE order_id=?", (o["ahead_pess"], o["ahead_opt"], o["filled_pess"], o["filled_opt"],
                                              int(done), o["order_id"]))
        conn.execute("UPDATE paper_markets SET trades_read_to=? WHERE market_id=?", (upto, r["market_id"]))
        conn.commit()
        n_read += 1
    log.info("Paper trading: %d of %d markets read, %d simulated fills recorded", n_read, len(mk), n_fills)


# ---------- weekly report section ----------

WAITS = [("5 minutes", 300), ("1 hour", 3600), ("until close", None)]


def _ratio(by_event, z=1.96):
    """Return per $1 (profit / cost) with a range treating each event as one unit."""
    P = sum(p for p, _ in by_event.values())
    C = sum(c for _, c in by_event.values())
    if C <= 0:
        return None, None, None
    r = P / C
    se = math.sqrt(sum((p - r * c) ** 2 for p, c in by_event.values())) / C
    return r, r - z * se, r + z * se


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _rng(r):
    return "n/a" if r[0] is None else f"{_pct(r[0])} ({_pct(r[1])} to {_pct(r[2])})"


def _orders(conn, where, args):
    rows = conn.execute(
        "SELECT o.*, m.event_ticker, m.result, m.series_ticker, s.fee_type, s.fee_multiplier FROM paper_orders o "
        "JOIN markets m USING(market_id) LEFT JOIN series s ON s.series_ticker=m.series_ticker WHERE " + where,
        args).fetchall()
    fills = defaultdict(list)
    if rows:
        for f in conn.execute("SELECT * FROM paper_fills WHERE order_id IN (%s)" % ",".join("?" * len(rows)),
                              [r["order_id"] for r in rows]):
            fills[f["order_id"]].append(f)
    return rows, fills


def _filled(o, fl, sc, wait):
    end = math.inf if wait is None else o["placed_ts"] + wait
    return sum(f["qty"] for f in fl if f["scenario"] == sc and f["ts"] <= end)


def _no_value_after(conn, market_id, t):
    r = conn.execute("SELECT yb1_cc, nb1_cc FROM book_snapshots WHERE market_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                     (market_id, t)).fetchone()
    if not r or r["yb1_cc"] is None or r["nb1_cc"] is None:
        return None
    mid_yes = (r["yb1_cc"] + (10000 - r["nb1_cc"])) / 2 / 10000
    return 1 - mid_yes


def report_section(conn, start, end):
    from .calib_report import trade_fees, _table
    L = ["FORWARD PAPER TRADING (simulated resting NO orders; nothing was sent to Kalshi)", "",
         "Orders: one piece at the best NO bid when NO costs 30-60c, in Mentions and Entertainment markets the "
         "logger watches (up to 24 / 200 contracts, under $100). Everything showing at that price is assumed "
         "ahead of us. Two cases: pessimistic (the queue ahead shrinks only through trades) and optimistic (it "
         "also shrinks when the book shows fewer contracts at our price). Each order is scored three ways: "
         "cancelled after 5 minutes, after 1 hour, or left until close.", ""]
    rows, fills = _orders(conn, "o.placed_ts>=? AND o.placed_ts<?", (start, end))
    if not rows:
        L += ["No simulated orders were placed this week.", ""]
    else:
        t = []
        for cat in config.PAPER_CATEGORIES:
            os_ = [o for o in rows if o["category"] == cat]
            if not os_:
                continue
            cells = [cat, len(os_), len({o["market_id"] for o in os_})]
            for _, w in WAITS:
                p = sum(_filled(o, fills[o["order_id"]], "pess", w) > 0 for o in os_) / len(os_)
                q = sum(_filled(o, fills[o["order_id"]], "opt", w) > 0 for o in os_) / len(os_)
                cells.append(f"{p * 100:.0f}-{q * 100:.0f}%")
            t.append(cells)
        L += ["THIS WEEK: orders placed and how many got any fill (pessimistic-optimistic; 'until close' is so far, "
              "since many markets are still open)",
              _table(["Category", "Orders", "Markets", "Filled in 5 min", "Filled in 1 hour", "Filled until close"],
                     t), ""]
    # Since the start: settled orders only.
    rows, fills = _orders(conn, "o.placed_ts<? AND m.result IN ('yes','no')", (end,))
    first = conn.execute("SELECT MIN(placed_ts) FROM paper_orders").fetchone()[0]
    L += [f"SINCE PAPER TRADING STARTED: {len(rows):,} orders whose market has settled. Profit after the maker "
          "fee, return per $1 on the contracts that filled (95% range, clustered by event)."]
    if not rows:
        L += ["None yet.", ""]
        return L
    t, marks = [], []
    for cat in config.PAPER_CATEGORIES:
        os_ = [o for o in rows if o["category"] == cat]
        if not os_:
            continue
        for wlabel, w in WAITS:
            cells = [cat, wlabel, len(os_)]
            for sc in ("pess", "opt"):
                by_ev = defaultdict(lambda: [0.0, 0.0])
                n_f, profit = 0, 0.0
                for o in os_:
                    qty = _filled(o, fills[o["order_id"]], sc, w)
                    if qty <= 0:
                        continue
                    n_f += 1
                    won = 1.0 if o["result"] == "no" else 0.0
                    _, fee = trade_fees(o["fee_type"] or "quadratic",
                                        1.0 if o["fee_multiplier"] is None else o["fee_multiplier"],
                                        o["no_price"], qty)
                    pnl = qty * (won - o["no_price"]) - fee
                    profit += pnl
                    by_ev[o["event_ticker"]][0] += pnl
                    by_ev[o["event_ticker"]][1] += qty * o["no_price"]
                cells += [f"{n_f / len(os_) * 100:.0f}%", f"${profit:+,.2f}", _rng(_ratio(by_ev))]
            t.append(cells)
        m5, m30 = [], []
        for o in os_:
            fl = sorted(f["ts"] for f in fills[o["order_id"]] if f["scenario"] == "pess")
            if not fl:
                continue
            for h, bucket in ((300, m5), (1800, m30)):
                v = _no_value_after(conn, o["market_id"], fl[0] + h) if fl[0] + h <= end else None
                if v is not None:
                    bucket.append(v - o["no_price"])
        marks.append([cat, len(m5), f"{sum(m5) / len(m5) * 100:+.1f}c" if m5 else "n/a",
                      f"{sum(m30) / len(m30) * 100:+.1f}c" if m30 else "n/a"])
    L += [_table(["Category", "Wait", "Orders", "Filled (pess.)", "Profit (pess.)", "Return/$ filled (pess.)",
                  "Filled (opt.)", "Profit (opt.)", "Return/$ filled (opt.)"], t), "",
          "Price after the first fill (pessimistic case): how the value of the NO we bought moved, in cents per "
          "contract (negative = against us):",
          _table(["Category", "Fills with price data", "+5 min", "+30 min"], marks), "",
          f"Paper trading started {time.strftime('%b %d, %Y', time.gmtime(first))}. Its period overlaps the "
          "pre-registered re-test (H5, H6), but it is a separate measurement. These numbers must not be used to "
          "change the re-test's rules.", ""]
    return L
