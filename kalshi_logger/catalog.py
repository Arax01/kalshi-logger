"""Series metadata (category, tags, fees) and registration of markets in the database."""
import json
import logging
import threading
import time

from . import classify, http
from .util import parse_ts

log = logging.getLogger(__name__)

_lock = threading.Lock()
_series = {}          # series_ticker -> dict(category, tags, fee_type, fee_multiplier, title)
_loaded_ts = 0
REFRESH_SEC = 12 * 3600


def _store(conn, s):
    info = {
        "title": s.get("title"),
        "category": s.get("category"),
        "tags": s.get("tags") or [],
        "fee_type": s.get("fee_type"),
        "fee_multiplier": s.get("fee_multiplier"),
    }
    conn.execute(
        "INSERT INTO series(series_ticker,title,category,tags,fee_type,fee_multiplier,updated_ts) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(series_ticker) DO UPDATE SET title=excluded.title, "
        "category=excluded.category, tags=excluded.tags, fee_type=excluded.fee_type, "
        "fee_multiplier=excluded.fee_multiplier, updated_ts=excluded.updated_ts",
        (s["ticker"], info["title"], info["category"], json.dumps(info["tags"]),
         info["fee_type"], info["fee_multiplier"], int(time.time())),
    )
    return info


def refresh(conn, force=False):
    """Load every series once (a single request returns all of them); refresh twice a day."""
    global _loaded_ts
    with _lock:
        if not force and _series and time.time() - _loaded_ts < REFRESH_SEC:
            return
        data = http.kalshi.get("/series")
        items = data.get("series") or []
        for s in items:
            _series[s["ticker"]] = _store(conn, s)
        conn.commit()
        _loaded_ts = time.time()
        log.info("Loaded %d series", len(items))


def get(conn, series_ticker):
    info = _series.get(series_ticker)
    if info is not None:
        return info
    row = conn.execute("SELECT * FROM series WHERE series_ticker=?", (series_ticker,)).fetchone()
    if row:
        info = {"title": row["title"], "category": row["category"], "tags": json.loads(row["tags"] or "[]"),
                "fee_type": row["fee_type"], "fee_multiplier": row["fee_multiplier"]}
    else:
        try:
            s = http.kalshi.get(f"/series/{series_ticker}").get("series")
            info = _store(conn, s) if s else None
        except http.ApiError:
            info = None
        if info is None:
            info = {"title": None, "category": None, "tags": [], "fee_type": None, "fee_multiplier": None}
    _series[series_ticker] = info
    return info


def ensure_market(conn, m):
    """Insert a market (from an API market object) if new. Returns (market_id, row_or_None).

    The returned row holds the market's previous snapshot state for change calculations.
    """
    row = conn.execute(
        "SELECT market_id, close_ts, last_snap_ts, last_bid_cc, last_ask_cc, last_bid_size, "
        "last_ask_size, last_volume FROM markets WHERE ticker=?",
        (m["ticker"],),
    ).fetchone()
    close_ts = parse_ts(m.get("close_time"))
    if row:
        if close_ts and row["close_ts"] != close_ts:
            conn.execute("UPDATE markets SET close_ts=? WHERE market_id=?", (close_ts, row["market_id"]))
        return row["market_id"], row
    series_ticker = classify.series_from_event(m.get("event_ticker"))
    is_combo = bool(m.get("mve_collection_ticker")) or m["ticker"].startswith("KXMVE")
    legs = m.get("mve_selected_legs") or []
    s = get(conn, series_ticker) if not is_combo else {"category": "Combo", "tags": []}
    c = classify.classify(series_ticker, s.get("category"), s.get("tags"), is_combo=is_combo)
    combo_detail = None
    if is_combo and legs:
        combo_detail = json.dumps([{"market": leg.get("market_ticker"), "side": leg.get("side")} for leg in legs])
        leagues = sorted({_leg_league(conn, leg.get("event_ticker")) for leg in legs})
        c["league"] = "+".join(x for x in leagues if x)[:200] or None
    cur = conn.execute(
        "INSERT INTO markets(ticker,event_ticker,series_ticker,title,subtitle,category,market_group,"
        "sport,league,stat_type,is_combo,combo_legs,combo_detail,strike_type,floor_strike,cap_strike,"
        "open_ts,close_ts,first_seen_ts,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (m["ticker"], m.get("event_ticker"), series_ticker, m.get("title"),
         m.get("yes_sub_title") or m.get("subtitle"), s.get("category"), c["market_group"], c["sport"],
         c["league"], c["stat_type"], int(is_combo), len(legs) or None, combo_detail,
         m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"),
         parse_ts(m.get("open_time")), close_ts, int(time.time()), m.get("status")),
    )
    return cur.lastrowid, None


def _leg_league(conn, event_ticker):
    series_ticker = classify.series_from_event(event_ticker)
    info = _series.get(series_ticker)
    if info is None:
        return series_ticker
    c = classify.classify(series_ticker, info.get("category"), info.get("tags"))
    return c["league"] or c["sport"] or info.get("category") or series_ticker
