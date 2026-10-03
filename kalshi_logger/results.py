"""Records each logged market's final result, so fair values and price moves can be checked later.

Hourly: every market we have logged whose close time has passed and has no final result yet is
looked up (100 per request). Markets are re-checked every 3 hours until Kalshi marks them
'finalized' (or for up to 30 days, e.g. for disputed outcomes). Markets older than Kalshi's
historical cutoff (about two months) are looked up on the historical endpoint instead.
"""
import logging
import time

from . import db, http
from .runner import stop_event
from .util import num, parse_ts

log = logging.getLogger(__name__)

RECHECK_SEC = 3 * 3600
GIVE_UP_SEC = 30 * 24 * 3600
BATCH = 100
MAX_PER_RUN = 20000
FINAL_STATUSES = {"finalized"}


def _apply(conn, m, now):
    status = m.get("status")
    final = status in FINAL_STATUSES and m.get("result") is not None
    conn.execute(
        "UPDATE markets SET status=?, result=?, settlement_value=?, settled_ts=?, result_checked_ts=? "
        "WHERE ticker=?",
        (status, (m.get("result") or None) if final else None, num(m.get("settlement_value_dollars")),
         parse_ts(m.get("settlement_ts")), now, m["ticker"]),
    )


def run(after_gap=False):
    conn = db.connect()
    now = int(time.time())
    rows = conn.execute(
        "SELECT ticker FROM markets WHERE result IS NULL AND close_ts IS NOT NULL AND close_ts < ? "
        "AND close_ts > ? AND (result_checked_ts IS NULL OR result_checked_ts < ?) "
        "ORDER BY close_ts LIMIT ?",
        (now - 300, now - GIVE_UP_SEC, now - RECHECK_SEC, MAX_PER_RUN),
    ).fetchall()
    tickers = [r[0] for r in rows]
    found = 0
    for i in range(0, len(tickers), BATCH):
        if stop_event.is_set():
            break
        chunk = tickers[i: i + BATCH]
        data = http.kalshi.get("/markets", {"tickers": ",".join(chunk), "limit": 1000})
        got = {m["ticker"]: m for m in data.get("markets") or []}
        missing = [t for t in chunk if t not in got]
        if missing:
            hist = http.kalshi.get("/historical/markets", {"tickers": ",".join(missing), "limit": 1000})
            got.update({m["ticker"]: m for m in hist.get("markets") or []})
        for t in chunk:
            if t in got:
                _apply(conn, got[t], now)
                found += 1 if got[t].get("status") in FINAL_STATUSES else 0
            else:
                conn.execute("UPDATE markets SET result_checked_ts=? WHERE ticker=?", (now, t))
        conn.commit()
    if tickers:
        log.info("Results: checked %d closed markets, %d have final results", len(tickers), found)
