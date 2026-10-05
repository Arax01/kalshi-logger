"""Settings. Defaults live here; a local .env file may override them."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _int(name, default):
    return int(os.environ.get(name, default))


def _float(name, default):
    return float(os.environ.get(name, default))


DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "kalshi.db"))
STOP_FILE = ROOT / "STOP"
PID_FILE = DATA_DIR / "logger.pid"

# Public, unauthenticated market-data API (documented as a supported production host).
KALSHI_BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
DERIBIT_BASE = "https://www.deribit.com/api/v2/public"
COINBASE_BASE = "https://api.exchange.coinbase.com"

# Kalshi's documented Basic tier is 200 read tokens/sec at 10 tokens per request (20 req/s).
# Unauthenticated limits are not published, so we stay at a small fraction of that.
KALSHI_MAX_RPS = _float("KALSHI_MAX_RPS", 2.0)

SCAN_INTERVAL_SEC = _int("SCAN_INTERVAL_SEC", 20 * 60)
CRYPTO_INTERVAL_SEC = _int("CRYPTO_INTERVAL_SEC", 2 * 60)
INGAME_INTERVAL_SEC = _int("INGAME_INTERVAL_SEC", 60)
RESULTS_INTERVAL_SEC = _int("RESULTS_INTERVAL_SEC", 60 * 60)
REPORT_CHECK_INTERVAL_SEC = _int("REPORT_CHECK_INTERVAL_SEC", 15 * 60)

# A job that has not succeeded for this many intervals is recorded as a data gap.
GAP_FACTOR = _float("GAP_FACTOR", 2.5)

# Combo (multi-leg) markets: how many of the most-traded ones get a quote snapshot per scan.
COMBO_SAMPLE_SIZE = _int("COMBO_SAMPLE_SIZE", 300)
# Safety cap on pages of the public trade feed read per scan (1000 trades per page).
TRADE_FEED_MAX_PAGES = _int("TRADE_FEED_MAX_PAGES", 400)

INGAME_LEAGUES = [x.strip() for x in os.environ.get("INGAME_LEAGUES", "NFL,NBA,MLB,NHL,NCAAFB").split(",") if x.strip()]

# Crypto fair value: which Kalshi series to model (terminal-price markets only).
CRYPTO_SERIES = {
    "KXBTCD": "BTC", "KXBTC": "BTC", "KXBTC15M": "BTC", "KXBTCY": "BTC",
    "KXETHD": "ETH", "KXETH": "ETH", "KXETH15M": "ETH", "KXETHY": "ETH",
}
# Don't compute a fair value inside the final settlement-averaging window.
CRYPTO_MIN_SECONDS_TO_CLOSE = _int("CRYPTO_MIN_SECONDS_TO_CLOSE", 120)
# A crypto "gap" for the daily report: edge net of fees larger than this (dollars per contract).
CRYPTO_GAP_THRESHOLD = _float("CRYPTO_GAP_THRESHOLD", 0.03)

# Weekly report: a market counts as having "meaningful volume" above this many contracts in the week.
MEANINGFUL_WEEKLY_VOLUME = _int("MEANINGFUL_WEEKLY_VOLUME", 500)

USER_AGENT = "kalshi-logger/1.0 (read-only research)"

# Order-book snapshots (books job): which markets, how often, how deep.
BOOKS_INTERVAL_SEC = _int("BOOKS_INTERVAL_SEC", 180)
BOOKS_CATEGORIES = [x.strip() for x in os.environ.get("BOOKS_CATEGORIES", "Entertainment,Mentions").split(",")
                    if x.strip()]
BOOKS_MIN_VOLUME_24H = _int("BOOKS_MIN_VOLUME_24H", 100)
BOOKS_MAX_MARKETS = _int("BOOKS_MAX_MARKETS", 1000)
BOOKS_LEVELS = 3   # fixed: the table has columns for 3 levels per side
