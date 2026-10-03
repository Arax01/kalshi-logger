"""Priority 3: in-game sports logging.

About every 60 seconds, for each game in progress in the chosen leagues, logs the Kalshi
game-winner prices next to the current score, period and clock.

Score source: Kalshi's own public live-data feed (provider: Stats Perform). Kalshi links each
game ("milestone") directly to its market tickers, so no team-name matching is needed, which is
why college football is included. Its delay versus the real game is not published; we store the
feed's own update timestamp (live_updated_ts) next to our fetch time (ts) so the lag can be
measured afterwards. Treat it as seconds to tens of seconds behind live play.
"""
import json
import logging
import time

from . import catalog, config, db, http
from .util import num, parse_ts, quote

log = logging.getLogger(__name__)

GAME_TYPES = ["football_game", "basketball_game", "baseball_game", "hockey_match"]
REFRESH_SEC = 10 * 60
PRE_GAME = {"scheduled", "not_started", "created", "pre", ""}
FINISHED = {"closed", "final", "complete", "completed", "finished", "cancelled", "canceled", "postponed"}
MAX_GAME_HOURS = 8
_last_refresh = 0
_last_details = {}


def refresh_games(conn):
    """Find today's games in the chosen leagues and their game-winner market tickers."""
    now = int(time.time())
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 12 * 3600))
    new = {}
    for gtype in GAME_TYPES:
        for page in http.kalshi.paginate(
            "/milestones", {"type": gtype, "minimum_start_date": since, "limit": 200}, "milestones", max_pages=5
        ):
            for ms in page:
                d = ms.get("details") or {}
                start = parse_ts(ms.get("start_date"))
                event = d.get("main_game_event_ticker")
                if d.get("league") not in config.INGAME_LEAGUES or not event or not start or start > now + 15 * 60:
                    continue
                if conn.execute("SELECT 1 FROM games WHERE milestone_id=?", (ms["id"],)).fetchone():
                    continue
                new[event] = (ms, d, start)
    events = list(new)
    for i in range(0, len(events), 50):
        data = http.kalshi.get("/events", {"tickers": ",".join(events[i: i + 50]), "with_nested_markets": "true"})
        for e in data.get("events") or []:
            if e.get("event_ticker") not in new:
                continue
            ms, d, start = new[e["event_ticker"]]
            markets = e.get("markets") or []
            for m in markets:
                m.setdefault("event_ticker", e["event_ticker"])
                catalog.ensure_market(conn, m)
            conn.execute(
                "INSERT OR IGNORE INTO games(milestone_id,league,title,event_ticker,start_ts,home_team_id,"
                "away_team_id,market_tickers,finished,updated_ts) VALUES(?,?,?,?,?,?,?,?,0,?)",
                (ms["id"], d.get("league"), ms.get("title"), e["event_ticker"], start, d.get("home_team_id"),
                 d.get("away_team_id"), json.dumps([m["ticker"] for m in markets]), now),
            )
        conn.commit()
    if new:
        log.info("In-game: %d new games found", len(new))


def _period_clock(gtype, d):
    if gtype == "baseball_game":
        inning = d.get("inning")
        half = {1: "Top", 2: "Bot"}.get(d.get("inning_half"), "")
        period = f"{half} {inning}".strip() if inning else None
        clock = f"{d.get('outs')} out" if d.get("outs") is not None else None
        return period, clock
    period = d.get("quarter") or d.get("period")
    clock = d.get("clock") or d.get("period_remaining_time")
    return (str(period) if period is not None else None), clock


def run(after_gap=False):
    global _last_refresh
    conn = db.connect()
    if time.time() - _last_refresh > REFRESH_SEC:
        catalog.refresh(conn)
        refresh_games(conn)
        _last_refresh = time.time()
    now = int(time.time())
    games = conn.execute(
        "SELECT * FROM games WHERE finished=0 AND start_ts <= ?", (now + 60,)
    ).fetchall()
    if not games:
        return

    live = {}
    ids = [g["milestone_id"] for g in games]
    for i in range(0, len(ids), 50):
        data = http.kalshi.get("/live_data/batch", {"milestone_ids": ids[i: i + 50]})
        for item in data.get("live_datas") or []:
            live[item.get("milestone_id")] = item

    tickers = []
    in_play = []
    for g in games:
        item = live.get(g["milestone_id"])
        d = (item or {}).get("details") or {}
        status = (d.get("status") or "").lower()
        if now - g["start_ts"] > MAX_GAME_HOURS * 3600:
            conn.execute("UPDATE games SET finished=1 WHERE milestone_id=?", (g["milestone_id"],))
            continue
        if item is None or status in PRE_GAME:
            continue
        if status in FINISHED and not conn.execute(
            "SELECT 1 FROM game_snapshots WHERE milestone_id=? LIMIT 1", (g["milestone_id"],)
        ).fetchone():
            # Already over before we first saw it: nothing in-game to record.
            conn.execute("UPDATE games SET finished=1 WHERE milestone_id=?", (g["milestone_id"],))
            continue
        in_play.append((g, item, d, status))
        tickers.extend(json.loads(g["market_tickers"] or "[]"))

    quotes = {}
    for i in range(0, len(tickers), 100):
        data = http.kalshi.get("/markets", {"tickers": ",".join(tickers[i: i + 100]), "limit": 1000})
        for m in data.get("markets") or []:
            quotes[m["ticker"]] = m
    ts = int(time.time())

    rows = 0
    for g, item, d, status in in_play:
        period, clock = _period_clock(item.get("type"), d)
        last_play = d.get("last_play")
        if isinstance(last_play, dict):
            last_play = last_play.get("description")
        details_json = json.dumps(d, separators=(",", ":"))
        store_details = _last_details.get(g["milestone_id"]) != details_json
        _last_details[g["milestone_id"]] = details_json
        live_ts = d.get("last_updated_ts") or d.get("score_last_updated_ts")
        for ticker in json.loads(g["market_tickers"] or "[]"):
            m = quotes.get(ticker)
            if m is None:
                continue
            mid, _ = catalog.ensure_market(conn, m)
            bid, ask, bid_size, ask_size = quote(m)
            conn.execute(
                "INSERT INTO game_snapshots(ts,market_id,league,milestone_id,yes_bid,yes_ask,bid_size,ask_size,"
                "last_price,volume,game_status,home_points,away_points,period,clock,live_updated_ts,last_play,"
                "details) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, mid, g["league"], g["milestone_id"], bid, ask, bid_size, ask_size,
                 num(m.get("last_price_dollars")), num(m.get("volume_fp")), status, d.get("home_points"),
                 d.get("away_points"), period, clock, live_ts, (last_play or "")[:300] or None,
                 details_json if store_details else None),
            )
            store_details = False
            rows += 1
        if status in FINISHED:
            conn.execute("UPDATE games SET finished=1, updated_ts=? WHERE milestone_id=?", (ts, g["milestone_id"]))
    conn.commit()
    log.info("In-game: %d games in play, %d rows", len(in_play), rows)
