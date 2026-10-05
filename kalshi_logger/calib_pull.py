"""Download a random sample of historical Kalshi trades plus each market's final result.

Design (agreed in advance):
* January 2025 to now, 150 random time windows per month, each sized to hold about 1,000 trades
  (windows are longer in quiet months), so every month is represented, not just busy 2026.
* For every sampled trade's market: its final result, event and category.
* Thin-category top-up: any category with fewer than 200 settled events gets extra events sampled
  directly from its busiest series, with their trades.
* Fee history (when each series' fee type changed) so each trade gets the fee in effect at the time.

Everything is stored in calib_* tables, separate from live logging, and the pull can be resumed.
"""
import calendar
import logging
import random
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import classify, http
from .util import num, parse_ts

log = logging.getLogger(__name__)

START = (2025, 1)
WINDOWS_PER_MONTH = 150
TARGET_TRADES_PER_WINDOW = 1000
MIN_EVENTS = 200
TOPUP_MARKETS_PER_EVENT = 6
TOPUP_SERIES_PER_CATEGORY = 30


def category_of(series_ticker, info, is_combo):
    if is_combo:
        return "Combos"
    cat = (info or {}).get("category")
    c = classify.classify(series_ticker, cat, (info or {}).get("tags"))
    if cat == "Sports":
        return {"game_winner": "Game winners", "player_prop": "Player props",
                "game_line": "Game lines (spreads, totals)"}.get(c["market_group"], "Other sports")
    return {"Crypto": "Crypto", "Climate and Weather": "Weather", "Economics": "Economics",
            "Financials": "Financials", "Entertainment": "Entertainment", "Politics": "Politics & elections",
            "Elections": "Politics & elections", "Mentions": "Mentions", "Companies": "Companies",
            "Science and Technology": "Science & tech", "Commodities": "Commodities"}.get(cat, "Other")


def _months(now):
    y, m = START
    out = []
    while (y, m) <= (now.year, now.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _trades_page(cut_trades, t0, t1, ticker=None):
    ep = "/historical/trades" if t1 <= cut_trades else "/markets/trades"
    params = {"min_ts": t0, "max_ts": t1, "limit": 1000}
    if ticker:
        params["ticker"] = ticker
    return http.kalshi.get(ep, params)


def _rate(cut_trades, t0, t1, rng):
    """Trades per second in a month, from three 60-second probes."""
    rates = []
    for _ in range(3):
        t = rng.randint(t0, t1 - 60)
        tr = _trades_page(cut_trades, t, t + 60).get("trades") or []
        if len(tr) >= 1000:
            ts = [parse_ts(x["created_time"]) for x in tr]
            span = max(max(ts) - min(ts), 1)
            rates.append(len(tr) / span)
        else:
            rates.append(len(tr) / 60)
    return max(statistics.median(rates), 1e-4)


def plan_windows(conn, cut_trades, progress):
    now = datetime.now(timezone.utc)
    end_limit = int(time.time()) - 2 * 86400
    for y, m in _months(now):
        month = f"{y}-{m:02d}"
        if conn.execute("SELECT 1 FROM calib_windows WHERE month=? AND source='random' LIMIT 1", (month,)).fetchone():
            continue
        rng = random.Random(f"kalshi-calibration-{month}")   # fixed seed: same windows every run
        t0 = calendar.timegm((y, m, 1, 0, 0, 0))
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        t1 = min(calendar.timegm((ny, nm, 1, 0, 0, 0)), end_limit)
        if t1 - t0 < 86400:
            continue
        rate = _rate(cut_trades, t0, t1, rng)
        length = int(min(max(TARGET_TRADES_PER_WINDOW / rate, 2), 4 * 3600))
        starts = sorted(rng.randint(t0, t1 - length) for _ in range(WINDOWS_PER_MONTH))
        if cut_trades:  # don't let a window straddle the live/historical boundary
            starts = [s if not (s < cut_trades < s + length) else cut_trades for s in starts]
        conn.executemany("INSERT INTO calib_windows(month,start_ts,length_sec,source) VALUES(?,?,?,'random')",
                         [(month, s, length) for s in starts])
        conn.commit()
        progress(f"  {month}: ~{rate * 86400 / 1e6:.2f}M trades/day, windows of {length}s")


def _store_trades(conn, window_id, trades):
    rows = []
    for x in trades:
        p, c = num(x.get("yes_price_dollars")), num(x.get("count_fp"))
        if p is None or not c:
            continue
        rows.append((window_id, x["ticker"], parse_ts(x["created_time"]), p, c, x.get("taker_side")))
    conn.executemany("INSERT INTO calib_trades(window_id,ticker,ts,yes_price,count,taker_side) VALUES(?,?,?,?,?,?)",
                     rows)
    return len(rows)


def fetch_windows(conn, cut_trades, progress):
    todo = conn.execute("SELECT * FROM calib_windows WHERE done=0 AND source='random' ORDER BY start_ts").fetchall()
    progress(f"Downloading {len(todo)} trade windows...")
    t0 = time.time()
    for i, w in enumerate(todo, 1):
        data = _trades_page(cut_trades, w["start_ts"], w["start_ts"] + w["length_sec"])
        n = _store_trades(conn, w["window_id"], data.get("trades") or [])
        conn.execute("UPDATE calib_windows SET n_trades=?, full=?, done=1 WHERE window_id=?",
                     (n, int(bool(data.get("cursor"))), w["window_id"]))
        conn.commit()
        if i % 200 == 0 or i == len(todo):
            progress(f"  {i}/{len(todo)} windows (about {(time.time() - t0) / i * (len(todo) - i) / 60:.0f} min left)")


def _series_map():
    return {s["ticker"]: s for s in http.kalshi.get("/series").get("series") or []}


def _apply_market(conn, m, series):
    st = classify.series_from_event(m.get("event_ticker"))
    is_combo = bool(m.get("mve_collection_ticker")) or m["ticker"].startswith("KXMVE")
    res = m.get("result")
    status = m.get("status")
    final = status in ("finalized", "settled") and res in ("yes", "no")
    conn.execute(
        "INSERT OR REPLACE INTO calib_markets(ticker,event_ticker,series_ticker,category,status,result,close_ts,"
        "volume,looked_up) VALUES(?,?,?,?,?,?,?,?,1)",
        (m["ticker"], m.get("event_ticker"), st, category_of(st, series.get(st), is_combo), status,
         res if final else ("other" if status in ("finalized", "settled") else None), parse_ts(m.get("close_time")),
         num(m.get("volume_fp", m.get("volume")))))


MAX_TICKER_CHARS = 2500   # keep request URLs short; long combo tickers made 100 per request too long


def _chunks(tickers):
    """Batches of up to 100 tickers whose combined length keeps the request URL short."""
    batch, size = [], 0
    for t in tickers:
        if batch and (len(batch) >= 100 or size + len(t) + 3 > MAX_TICKER_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += len(t) + 3   # each comma becomes %2C
    if batch:
        yield batch


def _fetch_markets(endpoint, tickers, patience=5):
    """Markets by ticker. If the server says the URL is too long, split the batch in half; if it keeps
    rate-limiting us, pause a minute and try again (a long pull shouldn't die on a temporary slowdown)."""
    for attempt in range(patience):
        try:
            return http.kalshi.get(endpoint, {"tickers": ",".join(tickers), "limit": 1000}).get("markets") or []
        except http.ApiError as exc:
            if "HTTP 414" in str(exc) and len(tickers) > 1:
                half = len(tickers) // 2
                return _fetch_markets(endpoint, tickers[:half]) + _fetch_markets(endpoint, tickers[half:])
            if "429" not in str(exc) or attempt == patience - 1:
                raise
            log.warning("Rate limited repeatedly; pausing 60 s before retrying")
            time.sleep(60)


def lookup_markets(conn, cut_markets, series, progress):
    rows = conn.execute(
        "SELECT t.ticker, MAX(t.ts) last_ts FROM calib_trades t LEFT JOIN calib_markets m ON m.ticker=t.ticker "
        "WHERE m.ticker IS NULL OR m.looked_up=0 GROUP BY t.ticker").fetchall()
    old = [r["ticker"] for r in rows if r["last_ts"] < cut_markets]
    new = [r["ticker"] for r in rows if r["last_ts"] >= cut_markets]
    progress(f"Looking up results for {len(rows):,} markets...")
    t0, done = time.time(), 0
    for first, second, tickers in (("/historical/markets", "/markets", old), ("/markets", "/historical/markets", new)):
        for n_batch, chunk in enumerate(_chunks(tickers)):
            try:
                got = {m["ticker"]: m for m in _fetch_markets(first, chunk)}
                missing = [t for t in chunk if t not in got]
                if missing:
                    got.update({m["ticker"]: m for m in _fetch_markets(second, missing)})
            except http.ApiError as exc:
                # Leave these unlooked-up; the next run picks them up.
                log.warning("Skipping a batch of %d markets for now: %s", len(chunk), exc)
                continue
            for t in chunk:
                if t in got:
                    _apply_market(conn, got[t], series)
                else:
                    conn.execute("INSERT OR REPLACE INTO calib_markets(ticker,looked_up) VALUES(?,1)", (t,))
            conn.commit()
            done += len(chunk)
            if n_batch % 50 == 0:
                left = (time.time() - t0) / done * (len(rows) - done)
                progress(f"  {done:,}/{len(rows):,} markets (about {left / 60:.0f} min left)")


def _settled_events_by_category(conn):
    out = defaultdict(int)
    for r in conn.execute(
            "SELECT m.category, COUNT(DISTINCT m.event_ticker) n FROM calib_markets m WHERE m.result IN ('yes','no') "
            "AND EXISTS (SELECT 1 FROM calib_trades t WHERE t.ticker=m.ticker) GROUP BY m.category"):
        out[r["category"]] = r["n"]
    return out


def topup(conn, cut_markets, cut_trades, series, progress):
    """Sample extra settled events for categories with fewer than MIN_EVENTS events."""
    counts = _settled_events_by_category(conn)
    study_start = calendar.timegm((START[0], START[1], 1, 0, 0, 0))
    wanted_cats = {"Weather", "Economics", "Entertainment", "Crypto", "Player props", "Game winners", "Combos",
                   "Financials", "Politics & elections", "Mentions", "Companies", "Science & tech", "Commodities"}
    for cat in sorted(wanted_cats):
        have = counts.get(cat, 0)
        if have >= MIN_EVENTS or cat == "Combos":
            continue   # combos come in plenty from the random sample; they can't be sampled by series anyway
        if conn.execute("SELECT 1 FROM calib_windows WHERE source=? LIMIT 1", (f"topup:{cat}",)).fetchone():
            continue
        cands = [s for s in series.values() if category_of(s["ticker"], s, False) == cat]
        if not cands:
            continue
        rng = random.Random(f"kalshi-calibration-topup-{cat}")
        rng.shuffle(cands)
        events = defaultdict(list)
        for s in cands[:TOPUP_SERIES_PER_CATEGORY]:
            for ep, extra in (("/historical/markets", {}), ("/markets", {"status": "settled"})):
                try:
                    for page in http.kalshi.paginate(ep, {"series_ticker": s["ticker"], "limit": 1000, **extra},
                                                     "markets", max_pages=3):
                        for m in page:
                            ct = parse_ts(m.get("close_time"))
                            if m.get("result") in ("yes", "no") and ct and ct >= study_start and \
                                    num(m.get("volume_fp")):
                                events[m["event_ticker"]].append(m)
                except http.ApiError:
                    continue
        picks = list(events)
        rng.shuffle(picks)
        need = MIN_EVENTS - have
        progress(f"Top-up {cat}: have {have} events, adding up to {need} from {len(picks)} candidates")
        added = 0
        for ev in picks:
            if added >= need:
                break
            markets = sorted(events[ev], key=lambda m: -(num(m.get("volume_fp")) or 0))[:TOPUP_MARKETS_PER_EVENT]
            wid = conn.execute("INSERT INTO calib_windows(month,start_ts,length_sec,source,done) VALUES(?,?,?,?,1)",
                               (None, None, None, f"topup:{cat}")).lastrowid
            n = 0
            for m in markets:
                _apply_market(conn, m, series)
                ct = parse_ts(m.get("close_time"))
                tr = _trades_page(cut_trades, study_start, ct + 3600, ticker=m["ticker"]).get("trades") or []
                n += _store_trades(conn, wid, tr)
            conn.execute("UPDATE calib_windows SET n_trades=? WHERE window_id=?", (n, wid))
            conn.commit()
            added += 1 if n else 0


def load_fee_changes(conn):
    conn.execute("DELETE FROM calib_fee_changes")
    data = http.kalshi.get("/series/fee_changes", {"show_historical": "true"})
    for f in data.get("series_fee_change_arr") or []:
        conn.execute("INSERT INTO calib_fee_changes VALUES(?,?,?,?)",
                     (f["series_ticker"], parse_ts(f["scheduled_ts"]), f["fee_type"], f["fee_multiplier"]))
    conn.commit()


def run(conn, progress=print):
    cut = http.kalshi.get("/historical/cutoff")
    cut_trades, cut_markets = parse_ts(cut["trades_created_ts"]), parse_ts(cut["market_settled_ts"])
    series = _series_map()
    for s in series.values():   # current fee settings, used when a series has no fee-change history
        conn.execute("INSERT OR REPLACE INTO series(series_ticker,title,category,tags,fee_type,fee_multiplier,"
                     "updated_ts) VALUES(?,?,?,?,?,?,?)",
                     (s["ticker"], s.get("title"), s.get("category"), None, s.get("fee_type"),
                      s.get("fee_multiplier"), int(time.time())))
    conn.commit()
    load_fee_changes(conn)
    progress("Planning sample windows (estimating each month's trading rate)...")
    plan_windows(conn, cut_trades, progress)
    fetch_windows(conn, cut_trades, progress)
    lookup_markets(conn, cut_markets, series, progress)
    topup(conn, cut_markets, cut_trades, series, progress)
    counts = _settled_events_by_category(conn)
    progress("Settled events per category: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
