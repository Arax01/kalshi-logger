"""SQLite storage. All timestamps are Unix seconds (UTC). Prices are dollars per contract (0-1)
except in scan_snapshots, where they are stored compactly as integer centi-cents (1 = $0.0001)."""
import sqlite3
import threading

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS job_state (
    job TEXT PRIMARY KEY,
    last_ok_ts INTEGER,
    last_attempt_ts INTEGER,
    last_error TEXT
);

-- Periods where a job did not collect data (laptop asleep, offline, logger stopped, API down).
CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS gaps_job_ts ON gaps(job, start_ts);

CREATE TABLE IF NOT EXISTS series (
    series_ticker TEXT PRIMARY KEY,
    title TEXT,
    category TEXT,
    tags TEXT,
    fee_type TEXT,
    fee_multiplier REAL,
    updated_ts INTEGER
);

CREATE TABLE IF NOT EXISTS markets (
    market_id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    event_ticker TEXT,
    series_ticker TEXT,
    title TEXT,
    subtitle TEXT,
    category TEXT,
    market_group TEXT,      -- game_winner, player_prop, game_line, sports_other, combo, crypto, other
    sport TEXT,
    league TEXT,
    stat_type TEXT,         -- player props only: points, rebounds, passing_yards, ...
    is_combo INTEGER DEFAULT 0,
    combo_legs INTEGER,
    combo_detail TEXT,      -- JSON list of legs for combos
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    open_ts INTEGER,
    close_ts INTEGER,
    first_seen_ts INTEGER,
    -- last written snapshot, used to compute "change since last snapshot" across restarts
    last_snap_ts INTEGER,
    last_bid_cc INTEGER,
    last_ask_cc INTEGER,
    last_bid_size REAL,
    last_ask_size REAL,
    last_volume REAL,
    -- final outcome
    status TEXT,
    result TEXT,
    settlement_value REAL,
    settled_ts INTEGER,
    result_checked_ts INTEGER
);
CREATE INDEX IF NOT EXISTS markets_series ON markets(series_ticker);
CREATE INDEX IF NOT EXISTS markets_close ON markets(close_ts);

CREATE TABLE IF NOT EXISTS scans (
    scan_id INTEGER PRIMARY KEY,
    started_ts INTEGER,
    finished_ts INTEGER,
    status TEXT,            -- ok, partial, failed
    markets_seen INTEGER,
    rows_written INTEGER,
    combos_traded INTEGER,  -- distinct combo markets that traded in the window
    combo_trade_count INTEGER,
    combo_contracts REAL,
    combo_trade_pages INTEGER,
    combo_feed_truncated INTEGER,
    after_gap INTEGER,      -- 1 if the previous scan was missed (sleep/offline)
    notes TEXT
);

-- One row per market per scan, written when anything changed since the market's last row
-- (or at least every 2 hours). A market missing from a scan was unchanged.
CREATE TABLE IF NOT EXISTS scan_snapshots (
    scan_id INTEGER NOT NULL,
    market_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    bid_cc INTEGER,         -- best YES bid, centi-cents
    ask_cc INTEGER,         -- best YES ask, centi-cents
    bid_size REAL,
    ask_size REAL,
    last_cc INTEGER,
    volume REAL,            -- lifetime contracts
    volume_24h REAL,
    open_interest REAL,
    prev_ts INTEGER,        -- time of this market's previous row (NULL if none or across a gap)
    d_mid_cc INTEGER,       -- change in mid price since previous row (NULL across a data gap)
    d_volume REAL,          -- contracts traded since previous row (NULL across a data gap)
    PRIMARY KEY (scan_id, market_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS scan_snapshots_market ON scan_snapshots(market_id, ts);

-- Combo trades aggregated from the public trade feed, per scan window.
CREATE TABLE IF NOT EXISTS combo_trades (
    scan_id INTEGER NOT NULL,
    market_id INTEGER NOT NULL,
    window_start_ts INTEGER,
    window_end_ts INTEGER,
    n_trades INTEGER,
    contracts REAL,
    vwap_yes REAL,
    min_yes REAL,
    max_yes REAL,
    PRIMARY KEY (scan_id, market_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS crypto_fv (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    market_id INTEGER NOT NULL,
    asset TEXT,
    seconds_to_close INTEGER,
    spot REAL,              -- Deribit index (used in the model)
    spot_coinbase REAL,     -- cross-check
    spot_kalshi_ref REAL,   -- Kalshi's own chart of the settlement index (cross-check)
    vol REAL,               -- annualised implied volatility used
    vol_method TEXT,        -- how it was interpolated
    expiry_lo_ts INTEGER,   -- Deribit expiries bracketing the Kalshi close
    expiry_hi_ts INTEGER,
    vol_lo REAL,            -- implied vol at this strike for each bracketing expiry
    vol_hi REAL,
    fair_yes REAL,          -- our fair probability (options-implied vol): the approved model
    vol_realised REAL,      -- audit: vol measured from the settlement index over the last 3 hours
    fair_yes_realised REAL, -- audit: same model using vol_realised instead
    yes_bid REAL,
    yes_ask REAL,
    bid_size REAL,
    ask_size REAL,
    fee_buy_yes REAL,       -- taker fee per contract buying YES at the ask (for the full ask size)
    fee_buy_no REAL,        -- taker fee per contract buying NO (= selling YES at the bid)
    edge_buy_yes REAL,      -- fair - ask - fee  (positive = YES looks cheap)
    edge_buy_no REAL,       -- bid - fair - fee  (positive = YES looks expensive)
    note TEXT
);
CREATE INDEX IF NOT EXISTS crypto_fv_ts ON crypto_fv(ts);
CREATE INDEX IF NOT EXISTS crypto_fv_market ON crypto_fv(market_id, ts);

CREATE TABLE IF NOT EXISTS game_snapshots (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    market_id INTEGER NOT NULL,
    league TEXT,
    milestone_id TEXT,
    yes_bid REAL,
    yes_ask REAL,
    bid_size REAL,
    ask_size REAL,
    last_price REAL,
    volume REAL,
    game_status TEXT,
    home_points INTEGER,
    away_points INTEGER,
    period TEXT,            -- quarter / inning / period
    clock TEXT,
    live_updated_ts INTEGER, -- when the score feed says it last updated
    last_play TEXT,
    details TEXT            -- full score-feed payload (JSON), for later analysis
);
CREATE INDEX IF NOT EXISTS game_snapshots_market ON game_snapshots(market_id, ts);

-- Far-away crypto strikes (fair value under 2% or over 98%), every 30 minutes, for the longshot report.
CREATE TABLE IF NOT EXISTS crypto_far (
    ts INTEGER NOT NULL,
    market_id INTEGER NOT NULL,
    seconds_to_close INTEGER,
    spot REAL,
    vol REAL,
    fair_yes REAL,
    yes_bid REAL,
    yes_ask REAL,
    bid_size REAL,
    ask_size REAL,
    PRIMARY KEY (ts, market_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS games (
    milestone_id TEXT PRIMARY KEY,
    league TEXT,
    title TEXT,
    event_ticker TEXT,
    start_ts INTEGER,
    home_team_id TEXT,
    away_team_id TEXT,
    market_tickers TEXT,    -- JSON list of game-winner market tickers
    finished INTEGER DEFAULT 0,
    updated_ts INTEGER
);

-- ===== Backfilled football data (one-time historical pull; separate from live logging) =====
-- Games found for the backfill and how far each one got.
CREATE TABLE IF NOT EXISTS bf_games (
    milestone_id TEXT PRIMARY KEY,
    league TEXT,            -- NFL or NCAAFB
    season_type TEXT,       -- PRE (preseason) or REG
    title TEXT,
    event_ticker TEXT,
    start_ts INTEGER,
    home_team_id TEXT,
    away_team_id TEXT,
    status TEXT,            -- done, no_timestamps, no_markets, no_candles, error
    n_plays INTEGER,
    n_timed_plays INTEGER,
    note TEXT,
    fetched_ts INTEGER
);

-- Game-winner markets of backfilled games; side says which team the YES side is.
CREATE TABLE IF NOT EXISTS bf_markets (
    ticker TEXT PRIMARY KEY,
    milestone_id TEXT,
    side TEXT,              -- home or away
    team_name TEXT,
    result TEXT
);

-- Every play, with Kalshi's timestamp of when its feed first saw it.
CREATE TABLE IF NOT EXISTS bf_plays (
    milestone_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    play_id TEXT,
    wall_ts INTEGER,        -- Unix seconds the play was first seen (NULL if not timestamped)
    period INTEGER,
    clock TEXT,
    home_points INTEGER,
    away_points INTEGER,
    play_type TEXT,
    turnover TEXT,
    description TEXT,
    PRIMARY KEY (milestone_id, sequence)
) WITHOUT ROWID;

-- One row per market per minute, from the first to the last 1-minute candle.
-- observed = 1: Kalshi had a candle for that minute (real prices).
-- observed = 0: no candle that minute; prices are carried forward from the last real candle
--               and must not be treated as new information.
CREATE TABLE IF NOT EXISTS bf_minutes (
    ticker TEXT NOT NULL,
    minute_ts INTEGER NOT NULL,   -- end of the minute (Unix seconds)
    observed INTEGER NOT NULL,
    yes_bid REAL,                 -- best bid at the end of the minute
    yes_ask REAL,                 -- best ask at the end of the minute
    last_price REAL,              -- last trade price in the minute (NULL if no trade)
    volume REAL,                  -- contracts traded in the minute (0 if no candle)
    PRIMARY KEY (ticker, minute_ts)
) WITHOUT ROWID;

-- Each time the logger runs: start, last heartbeat (every minute), end, and whether it stopped
-- cleanly (stop.bat) or was killed (window closed, crash, power loss).
CREATE TABLE IF NOT EXISTS process_runs (
    run_id INTEGER PRIMARY KEY,
    started_ts INTEGER,
    heartbeat_ts INTEGER,
    ended_ts INTEGER,
    clean INTEGER
);

CREATE TABLE IF NOT EXISTS reports_done (
    report_type TEXT,
    period_key TEXT,
    generated_ts INTEGER,
    path TEXT,
    PRIMARY KEY (report_type, period_key)
);
"""

_local = threading.local()


def connect():
    """One connection per thread (SQLite connections must not be shared across threads)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.commit()


def _add_missing_columns(conn):
    """Upgrade an older database in place: add any columns that newer code expects."""
    ref = sqlite3.connect(":memory:")
    ref.executescript(SCHEMA.replace("PRAGMA journal_mode=WAL;", ""))
    tables = [r[0] for r in ref.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for _, name, ctype, *_ in ref.execute(f"PRAGMA table_info({table})"):
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}")
    ref.close()


def market_id(conn, ticker, fields):
    """Return the id for a market ticker, inserting or refreshing its descriptive fields."""
    row = conn.execute("SELECT market_id FROM markets WHERE ticker=?", (ticker,)).fetchone()
    if row:
        return row[0]
    cols = ["ticker"] + list(fields)
    cur = conn.execute(
        f"INSERT INTO markets ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [ticker] + list(fields.values()),
    )
    return cur.lastrowid
