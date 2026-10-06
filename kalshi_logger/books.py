"""Order-book snapshots for Entertainment and Mentions markets with recent volume.

Every few minutes, for each open Entertainment or Mentions market that traded at least
BOOKS_MIN_VOLUME_24H contracts in the last 24 hours (per the latest scan), this stores the top
BOOKS_LEVELS price levels on each side: YES bids and NO bids, with the contracts resting at each.
A NO bid at price q is the same as a YES ask at 1 - q.

A row is written only when those levels changed since the market's last row, or at least hourly,
to keep the database small. Uses Kalshi's public batch order-book endpoint (100 markets per request).
"""
import logging
import time

from . import config, db, http
from .util import num, to_cc

log = logging.getLogger(__name__)

KEEPALIVE_SEC = 3600
_last = {}   # market_id -> (levels tuple, ts)


def targets(conn, now):
    cats = config.BOOKS_CATEGORIES
    return conn.execute(
        f"SELECT m.market_id, m.ticker FROM markets m JOIN scan_snapshots s "
        f"ON s.market_id=m.market_id AND s.ts=m.last_snap_ts "
        f"WHERE m.category IN ({','.join('?' * len(cats))}) AND s.volume_24h >= ? AND m.close_ts > ? "
        f"ORDER BY s.volume_24h DESC LIMIT ?",
        (*cats, config.BOOKS_MIN_VOLUME_24H, now, config.BOOKS_MAX_MARKETS)).fetchall()


def top_levels(side, n):
    """Best n levels of one side. Kalshi lists each side from worst price to best, so read from the end."""
    out = []
    for price, size in reversed(side or []):
        out += [to_cc(num(price)), num(size)]
        if len(out) == 2 * n:
            break
    return out + [None] * (2 * n - len(out))


def run(after_gap=False):
    conn = db.connect()
    now = int(time.time())
    mk = targets(conn, now)
    if not mk:
        return
    ids = {r["ticker"]: r["market_id"] for r in mk}
    tickers = list(ids)
    n = config.BOOKS_LEVELS
    written = 0
    for i in range(0, len(tickers), 100):
        data = http.kalshi.get("/markets/orderbooks", {"tickers": tickers[i: i + 100]})
        ts = int(time.time())
        for ob in data.get("orderbooks") or []:
            mid = ids.get(ob.get("ticker"))
            if mid is None:
                continue
            book = ob.get("orderbook_fp") or {}
            levels = tuple(top_levels(book.get("yes_dollars"), n) + top_levels(book.get("no_dollars"), n))
            prev = _last.get(mid)
            if prev and prev[0] == levels and ts - prev[1] < KEEPALIVE_SEC:
                continue
            conn.execute(
                f"INSERT OR REPLACE INTO book_snapshots VALUES ({','.join('?' * (2 + len(levels)))})",
                (mid, ts, *levels))
            _last[mid] = (levels, ts)
            written += 1
        conn.commit()
    log.info("Books: %d markets checked, %d snapshots stored", len(tickers), written)
