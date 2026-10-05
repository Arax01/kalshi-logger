"""One-time backfill of 2026 NFL and college football games from Kalshi's public API.

For every finished game Kalshi links to markets, it stores:
* the game-winner markets and which team each YES side is (bf_markets),
* every play with the time Kalshi's feed first saw it (bf_plays),
* a minute-by-minute price grid built from 1-minute candlesticks (bf_minutes). Minutes with no
  candle are kept but flagged observed = 0, with prices carried forward from the last real candle.

Everything goes into bf_* tables, separate from live logging. The backfill can be re-run: games
already done are skipped, and newly finished games are added.
"""
import logging
import time

from . import catalog, db, http
from .util import num, parse_ts

log = logging.getLogger(__name__)

SEASON_START = "2026-07-01T00:00:00Z"
LEAGUES = ("NFL", "NCAAFB")
CANDLE_BEFORE_SEC = 30 * 60
CANDLE_AFTER_SEC = 7 * 3600
FINISHED_AFTER_SEC = 5 * 3600     # treat a game as finished this long after kick-off


def _walk_plays(game_stats):
    """Yield every play dict in a football play-by-play payload, with its period number."""
    for period in ((game_stats or {}).get("pbp") or {}).get("periods") or []:
        for drive in period.get("events") or []:
            for play in drive.get("plays") or []:
                yield period.get("period_number"), play


def _candle_value(c, field, part):
    """Candle fields are e.g. yes_bid.close_dollars on live endpoints, yes_bid.close on historical."""
    d = c.get(field) or {}
    return num(d.get(f"{part}_dollars", d.get(part)))


def minute_grid(candles, start_minute=None, end_minute=None):
    """Turn sparse 1-minute candles into one row per minute.

    Minutes without a candle are flagged observed=0 and carry the last real bid/ask forward
    (last_price None, volume 0), so analysis can tell real prices from carried-forward ones.
    """
    by_ts = {c["end_period_ts"]: c for c in candles}
    if not by_ts:
        return []
    first = start_minute or min(by_ts)
    last = end_minute or max(by_ts)
    rows, bid, ask = [], None, None
    for ts in range(first, last + 1, 60):
        c = by_ts.get(ts)
        if c is not None:
            b, a = _candle_value(c, "yes_bid", "close"), _candle_value(c, "yes_ask", "close")
            bid = b if b is not None else bid
            ask = a if a is not None else ask
            vol = num(c.get("volume_fp", c.get("volume"))) or 0.0
            rows.append((ts, 1, bid, ask, _candle_value(c, "price", "close"), vol))
        else:
            rows.append((ts, 0, bid, ask, None, 0.0))
    return rows


def discover(conn):
    """Find finished 2026 NFL/college games that Kalshi links to a game-winner event."""
    now = time.time()
    found = 0
    for page in http.kalshi.paginate(
        "/milestones", {"type": "football_game", "minimum_start_date": SEASON_START, "limit": 200},
        "milestones", max_pages=50,
    ):
        for m in page:
            d = m.get("details") or {}
            start = parse_ts(m.get("start_date"))
            if d.get("league") not in LEAGUES or not start or start > now - FINISHED_AFTER_SEC:
                continue
            event = d.get("main_game_event_ticker") or next(
                (t for t in m.get("primary_event_tickers") or [] if "GAME-" in t), None)
            if not event:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO bf_games(milestone_id,league,season_type,title,event_ticker,start_ts,"
                "home_team_id,away_team_id,status) VALUES(?,?,?,?,?,?,?,?,NULL)",
                (m["id"], d.get("league"), (d.get("season") or {}).get("type"), m.get("title"), event, start,
                 d.get("home_team_id"), d.get("away_team_id")))
            found += cur.rowcount
    conn.commit()
    return found


def _candles(series, ticker, start, end):
    params = {"start_ts": start, "end_ts": end, "period_interval": 1}
    try:
        data = http.kalshi.get(f"/series/{series}/markets/{ticker}/candlesticks", params)
        if data.get("candlesticks"):
            return data["candlesticks"]
    except http.ApiError:
        pass
    # Markets settled before Kalshi's historical cutoff (about two months back) live here instead.
    try:
        return http.kalshi.get(f"/historical/markets/{ticker}/candlesticks", params).get("candlesticks") or []
    except http.ApiError:
        return []


def backfill_game(conn, g, markets):
    """Fetch plays and candles for one game. `markets` is the event's nested market list."""
    mid = g["milestone_id"]
    plays = list(_walk_plays(http.kalshi.get(f"/live_data/milestone/{mid}/game_stats")))
    timed = sum(1 for _, p in plays if p.get("wall_clock"))
    conn.execute("DELETE FROM bf_plays WHERE milestone_id=?", (mid,))
    for period, p in plays:
        if p.get("sequence") is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO bf_plays(milestone_id,sequence,play_id,wall_ts,period,clock,home_points,"
            "away_points,play_type,turnover,description) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mid, p["sequence"], p.get("play_id"), p.get("wall_clock"), period, p.get("clock"),
             p.get("home_points"), p.get("away_points"), p.get("play_type"), p.get("turnover") or None,
             (p.get("description") or "")[:300]))
    status, note = "done", None
    if not plays:
        status = "no_plays"
    elif timed == 0:
        status = "no_timestamps"
    if not markets:
        status, note = "no_markets", "Kalshi returned no game-winner markets"

    series = g["event_ticker"].split("-")[0]
    n_candles = 0
    for m in markets:
        team = (m.get("custom_strike") or {}).get("football_team")
        side = "home" if team and team == g["home_team_id"] else "away" if team and team == g["away_team_id"] else None
        conn.execute(
            "INSERT OR REPLACE INTO bf_markets(ticker,milestone_id,side,team_name,result) VALUES(?,?,?,?,?)",
            (m["ticker"], mid, side, m.get("yes_sub_title"), m.get("result") or None))
        if status != "done":
            continue
        candles = _candles(series, m["ticker"], g["start_ts"] - CANDLE_BEFORE_SEC, g["start_ts"] + CANDLE_AFTER_SEC)
        n_candles += len(candles)
        conn.execute("DELETE FROM bf_minutes WHERE ticker=?", (m["ticker"],))
        conn.executemany(
            "INSERT INTO bf_minutes(ticker,minute_ts,observed,yes_bid,yes_ask,last_price,volume) "
            "VALUES(?,?,?,?,?,?,?)", [(m["ticker"], *row) for row in minute_grid(candles)])
    if status == "done" and n_candles == 0:
        status = "no_candles"
    conn.execute(
        "UPDATE bf_games SET status=?, n_plays=?, n_timed_plays=?, note=?, fetched_ts=? WHERE milestone_id=?",
        (status, len(plays), timed, note, int(time.time()), mid))
    conn.commit()
    return status


def run(progress=print):
    conn = db.connect()
    for series in ("KXNFLGAME", "KXNCAAFGAME"):
        catalog.get(conn, series)   # stores the series' fee settings for the analysis
    conn.commit()
    new = discover(conn)
    todo = conn.execute(
        "SELECT * FROM bf_games WHERE status IS NULL OR status='error' ORDER BY start_ts").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM bf_games").fetchone()[0]
    progress(f"Found {total} finished games ({new} new). {len(todo)} to download.")
    t0 = time.time()
    for i in range(0, len(todo), 40):
        chunk = todo[i: i + 40]
        events = {}
        data = http.kalshi.get("/events", {"tickers": ",".join(g["event_ticker"] for g in chunk),
                                           "with_nested_markets": "true"})
        for e in data.get("events") or []:
            events[e["event_ticker"]] = e.get("markets") or []
        for j, g in enumerate(chunk, start=i + 1):
            try:
                backfill_game(conn, g, events.get(g["event_ticker"], []))
            except http.ApiError as exc:
                conn.rollback()
                conn.execute("UPDATE bf_games SET status='error', note=? WHERE milestone_id=?",
                             (str(exc)[:300], g["milestone_id"]))
                conn.commit()
                log.warning("Backfill failed for %s: %s", g["title"], exc)
            if j % 25 == 0 or j == len(todo):
                left = (time.time() - t0) / j * (len(todo) - j)
                progress(f"  {j}/{len(todo)} games downloaded (about {left / 60:.0f} min left)")
    summary = conn.execute(
        "SELECT league, season_type, status, COUNT(*) n FROM bf_games GROUP BY 1,2,3 ORDER BY 1,2,3").fetchall()
    progress("Backfill status by league:")
    for r in summary:
        progress(f"  {r['league']:<7} {r['season_type'] or '?':<4} {r['status']:<14} {r['n']}")
    return summary
