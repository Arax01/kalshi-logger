"""Priority 1: broad market scanner.

Each scan:
1. Pages through every open non-combo market (about 130k, 1000 per request) and stores a
   snapshot for each market whose quote, size or volume changed since its last stored row
   (and at least every 2 hours). Unchanged markets are skipped to keep the database small.
2. Reads the public trade feed since the previous scan to find combo (multi-leg) markets that
   actually traded, stores trade stats for the most-traded ones, and snapshots a capped sample.
"""
import logging
import time
from collections import defaultdict

from . import catalog, config, db, http
from .runner import stop_event
from .util import num, quote, to_cc

log = logging.getLogger(__name__)

KEEPALIVE_SEC = 2 * 3600
COMBO_TRADE_ROWS = 2000      # most-traded combos whose trade stats are stored each scan
TICKERS_PER_REQUEST = 100


class StopRequested(Exception):
    pass


def _check_stop():
    if stop_event.is_set():
        raise StopRequested()


def _load_gaps(conn, since):
    rows = conn.execute(
        "SELECT start_ts, end_ts FROM gaps WHERE job='scanner' AND end_ts >= ?", (since,)
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _crosses_gap(prev_ts, ts, gaps):
    return any(prev_ts < g_end and ts > g_start for g_start, g_end in gaps)


def write_snapshot(conn, scan_id, m, mid, prev, ts, gaps, force=False):
    """Store one market snapshot if it changed. Returns True if a row was written."""
    bid, ask, bid_size, ask_size = quote(m)
    bid_cc, ask_cc = to_cc(bid), to_cc(ask)
    volume = num(m.get("volume_fp"))
    if prev is not None and prev["last_snap_ts"] is not None and not force:
        unchanged = (
            prev["last_bid_cc"] == bid_cc and prev["last_ask_cc"] == ask_cc
            and prev["last_bid_size"] == bid_size and prev["last_ask_size"] == ask_size
            and prev["last_volume"] == volume
        )
        if unchanged and ts - prev["last_snap_ts"] < KEEPALIVE_SEC:
            return False
    prev_ts = d_mid = d_volume = None
    if prev is not None and prev["last_snap_ts"] is not None:
        prev_ts = prev["last_snap_ts"]
        if not _crosses_gap(prev_ts, ts, gaps):
            if None not in (bid_cc, ask_cc, prev["last_bid_cc"], prev["last_ask_cc"]):
                d_mid = int(round((bid_cc + ask_cc - prev["last_bid_cc"] - prev["last_ask_cc"]) / 2))
            if volume is not None and prev["last_volume"] is not None:
                d_volume = volume - prev["last_volume"]
    conn.execute(
        "INSERT OR REPLACE INTO scan_snapshots(scan_id,market_id,ts,bid_cc,ask_cc,bid_size,ask_size,"
        "last_cc,volume,volume_24h,open_interest,prev_ts,d_mid_cc,d_volume) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (scan_id, mid, ts, bid_cc, ask_cc, bid_size, ask_size, to_cc(num(m.get("last_price_dollars"))),
         volume, num(m.get("volume_24h_fp")), num(m.get("open_interest_fp")), prev_ts, d_mid, d_volume),
    )
    conn.execute(
        "UPDATE markets SET last_snap_ts=?, last_bid_cc=?, last_ask_cc=?, last_bid_size=?, "
        "last_ask_size=?, last_volume=?, status=? WHERE market_id=?",
        (ts, bid_cc, ask_cc, bid_size, ask_size, volume, m.get("status"), mid),
    )
    return True


def run_scan(after_gap=False):
    conn = db.connect()
    catalog.refresh(conn)
    started = int(time.time())
    prev = conn.execute(
        "SELECT started_ts FROM scans WHERE status IN ('ok','partial') ORDER BY scan_id DESC LIMIT 1"
    ).fetchone()
    scan_id = conn.execute(
        "INSERT INTO scans(started_ts, status, after_gap) VALUES(?, 'running', ?)", (started, int(after_gap))
    ).lastrowid
    conn.commit()

    gaps = _load_gaps(conn, started - 7 * 24 * 3600)
    if after_gap:
        last_ok = conn.execute("SELECT last_ok_ts FROM job_state WHERE job='scanner'").fetchone()
        if last_ok and last_ok[0]:
            gaps.append((last_ok[0], started))

    seen = written = 0
    status, notes = "ok", []
    try:
        for page in http.kalshi.paginate(
            "/markets", {"status": "open", "limit": 1000, "mve_filter": "exclude"}, "markets"
        ):
            _check_stop()
            ts = int(time.time())
            for m in page:
                mid, prev_row = catalog.ensure_market(conn, m)
                seen += 1
                if write_snapshot(conn, scan_id, m, mid, prev_row, ts, gaps):
                    written += 1
            conn.commit()
    except StopRequested:
        status = "partial"
        notes.append("stopped during market pages")
    except http.ApiError as exc:
        status = "partial"
        notes.append(f"market pages: {exc}")
        log.warning("Scan %d incomplete: %s", scan_id, exc)

    combo_stats = {}
    if status == "ok":
        window_start = prev[0] if prev and not after_gap else started - config.SCAN_INTERVAL_SEC
        window_start = max(window_start, started - 2 * config.SCAN_INTERVAL_SEC)
        try:
            combo_stats = scan_combos(conn, scan_id, window_start, started, gaps)
        except StopRequested:
            status = "partial"
            notes.append("stopped during combo step")
        except http.ApiError as exc:
            status = "partial"
            notes.append(f"combos: {exc}")
            log.warning("Combo step failed: %s", exc)

    conn.execute(
        "UPDATE scans SET finished_ts=?, status=?, markets_seen=?, rows_written=?, combos_traded=?, "
        "combo_trade_count=?, combo_contracts=?, combo_trade_pages=?, combo_feed_truncated=?, notes=? "
        "WHERE scan_id=?",
        (int(time.time()), status, seen, written + combo_stats.get("written", 0),
         combo_stats.get("tickers"), combo_stats.get("trades"), combo_stats.get("contracts"),
         combo_stats.get("pages"), combo_stats.get("truncated"), "; ".join(notes) or None, scan_id),
    )
    conn.commit()
    log.info("Scan %d %s: %d markets seen, %d rows written, %s combos traded",
             scan_id, status, seen, written, combo_stats.get("tickers"))
    if status != "ok" and not stop_event.is_set():
        raise RuntimeError(f"scan {scan_id} incomplete: {'; '.join(notes)}")


def scan_combos(conn, scan_id, window_start, window_end, gaps):
    """Aggregate combo trades from the public trade feed, then snapshot the most-traded ones."""
    agg = defaultdict(lambda: {"n": 0, "contracts": 0.0, "notional": 0.0, "min": None, "max": None})
    pages = 0
    truncated = 0
    for trades in http.kalshi.paginate(
        "/markets/trades", {"limit": 1000, "min_ts": window_start, "max_ts": window_end}, "trades",
        max_pages=config.TRADE_FEED_MAX_PAGES,
    ):
        _check_stop()
        pages += 1
        for t in trades:
            if not t["ticker"].startswith("KXMVE"):
                continue
            price, count = num(t.get("yes_price_dollars")), num(t.get("count_fp")) or 0.0
            a = agg[t["ticker"]]
            a["n"] += 1
            a["contracts"] += count
            if price is not None:
                a["notional"] += price * count
                a["min"] = price if a["min"] is None else min(a["min"], price)
                a["max"] = price if a["max"] is None else max(a["max"], price)
    if pages >= config.TRADE_FEED_MAX_PAGES:
        truncated = 1
        log.info("Trade feed capped at %d pages; combo stats cover the most recent trades only", pages)

    ranked = sorted(agg.items(), key=lambda kv: kv[1]["contracts"], reverse=True)
    total_contracts = sum(a["contracts"] for _, a in ranked)
    total_trades = sum(a["n"] for _, a in ranked)

    # Snapshot a capped sample of the most-traded combos (also records their legs).
    sample = [t for t, _ in ranked[: config.COMBO_SAMPLE_SIZE]]
    written = 0
    ids = {}
    for i in range(0, len(sample), TICKERS_PER_REQUEST):
        _check_stop()
        chunk = sample[i: i + TICKERS_PER_REQUEST]
        data = http.kalshi.get("/markets", {"tickers": ",".join(chunk), "limit": 1000})
        ts = int(time.time())
        for m in data.get("markets") or []:
            mid, prev_row = catalog.ensure_market(conn, m)
            ids[m["ticker"]] = mid
            if write_snapshot(conn, scan_id, m, mid, prev_row, ts, gaps, force=True):
                written += 1
        conn.commit()

    for ticker, a in ranked[:COMBO_TRADE_ROWS]:
        mid = ids.get(ticker)
        if mid is None:
            mid, _ = catalog.ensure_market(conn, {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0],
                                                  "mve_collection_ticker": "unknown"})
        vwap = a["notional"] / a["contracts"] if a["contracts"] else None
        conn.execute(
            "INSERT OR REPLACE INTO combo_trades(scan_id,market_id,window_start_ts,window_end_ts,n_trades,"
            "contracts,vwap_yes,min_yes,max_yes) VALUES(?,?,?,?,?,?,?,?,?)",
            (scan_id, mid, window_start, window_end, a["n"], a["contracts"], vwap, a["min"], a["max"]),
        )
    conn.commit()
    return {"tickers": len(agg), "pages": pages, "truncated": truncated, "written": written,
            "trades": total_trades, "contracts": total_contracts}
