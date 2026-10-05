"""Read-only HTTP client.

Only GET requests exist in this module. There is no code anywhere in this project
that can place, modify or cancel orders, and no API key is used.
"""
import logging
import threading
import time

import requests

from . import config

log = logging.getLogger(__name__)


class RateLimiter:
    """Thread-safe pacing: at most `rate` requests per second, shared by all jobs."""

    def __init__(self, rate):
        self.interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.penalty_until = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot, self.penalty_until)
            self.next_slot = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def back_off(self, seconds):
        """After a 429, slow every job down for a while, not just the caller."""
        with self.lock:
            self.penalty_until = max(self.penalty_until, time.monotonic() + seconds)


class ReadOnlyClient:
    def __init__(self, base_url, rate, name):
        self.base = base_url.rstrip("/")
        self.limiter = RateLimiter(rate)
        self.name = name
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.session.headers["Accept"] = "application/json"
        self.stats_lock = threading.Lock()
        self.request_count = 0
        self.rate_limited_count = 0

    def get(self, path, params=None, max_attempts=6, timeout=30):
        url = path if path.startswith("http") else f"{self.base}{path}"
        delay = 2.0
        last_error = None
        for attempt in range(1, max_attempts + 1):
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("%s GET %s failed (attempt %d): %s", self.name, path, attempt, exc)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            with self.stats_lock:
                self.request_count += 1
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                last_error = "HTTP 429 (rate limited)"
                with self.stats_lock:
                    self.rate_limited_count += 1
                log.info("%s rate limited on %s; backing off %.0fs", self.name, path, delay)
                self.limiter.back_off(delay)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                log.warning("%s GET %s -> %s (attempt %d)", self.name, path, resp.status_code, attempt)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            # 4xx other than 429: the request itself is wrong; retrying will not help.
            raise ApiError(f"{self.name} GET {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        raise ApiError(f"{self.name} GET {path} failed after {max_attempts} attempts: {last_error}")

    def paginate(self, path, params, list_key, max_pages=None):
        """Yield each page's list for Kalshi's cursor-based pagination."""
        params = dict(params)
        pages = 0
        while True:
            data = self.get(path, params)
            pages += 1
            yield data.get(list_key) or []
            cursor = data.get("cursor")
            if not cursor or (max_pages and pages >= max_pages):
                return
            params["cursor"] = cursor


class ApiError(Exception):
    pass


kalshi = ReadOnlyClient(config.KALSHI_BASE, config.KALSHI_MAX_RPS, "kalshi")
deribit = ReadOnlyClient(config.DERIBIT_BASE, 5.0, "deribit")
coinbase = ReadOnlyClient(config.COINBASE_BASE, 3.0, "coinbase")
