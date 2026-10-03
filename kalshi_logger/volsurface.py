"""Implied-volatility surface from Deribit's free public options data.

For each Deribit expiry we build a volatility "smile": implied vol as a function of how far the
strike is from that expiry's forward price (log-moneyness), using out-of-the-money options.
To get a vol for a Kalshi close time that falls between two Deribit expiries, we interpolate
*total variance* (vol squared x time) linearly in time at the same moneyness, which is the
standard way to interpolate between expiries without creating impossible prices.
"""
import math
import time
from bisect import bisect_left
from datetime import datetime, timezone

from . import http

YEAR_SEC = 365.0 * 24 * 3600
MIN_NODE_SEC = 3600          # ignore expiries less than an hour away (their quotes are noisy)
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def parse_expiry(code):
    """'4OCT26' -> Unix seconds of 08:00 UTC that day (Deribit's expiry time)."""
    day, mon, yr = int(code[:-5]), MONTHS[code[-5:-2]], 2000 + int(code[-2:])
    return int(datetime(yr, mon, day, 8, 0, tzinfo=timezone.utc).timestamp())


class Smile:
    def __init__(self, expiry_ts, forward, points):
        self.expiry_ts = expiry_ts
        self.forward = forward
        pts = sorted(points)
        self.k = [p[0] for p in pts]
        self.iv = [p[1] for p in pts]

    def vol(self, strike):
        """Implied vol at a strike: linear in log-moneyness, flat beyond the quoted strikes."""
        k = math.log(strike / self.forward)
        if k <= self.k[0]:
            return self.iv[0]
        if k >= self.k[-1]:
            return self.iv[-1]
        i = bisect_left(self.k, k)
        k0, k1, v0, v1 = self.k[i - 1], self.k[i], self.iv[i - 1], self.iv[i]
        return v0 + (v1 - v0) * (k - k0) / (k1 - k0)


class Surface:
    def __init__(self, currency, smiles, fetched_ts):
        self.currency = currency
        self.smiles = sorted(smiles, key=lambda s: s.expiry_ts)
        self.fetched_ts = fetched_ts

    def vol(self, strike, target_ts, now=None):
        """Returns (vol, method, lo_expiry, hi_expiry, vol_lo, vol_hi)."""
        now = now or time.time()
        tau = (target_ts - now) / YEAR_SEC
        nodes = [s for s in self.smiles if s.expiry_ts - now >= MIN_NODE_SEC]
        if not nodes or tau <= 0:
            return None, "no_data", None, None, None, None
        lo = [s for s in nodes if s.expiry_ts <= target_ts]
        hi = [s for s in nodes if s.expiry_ts >= target_ts]
        if lo and hi:
            s1, s2 = lo[-1], hi[0]
            v1, v2 = s1.vol(strike), s2.vol(strike)
            if s1.expiry_ts == s2.expiry_ts:
                return v1, "exact_expiry", s1.expiry_ts, s2.expiry_ts, v1, v2
            t1, t2 = (s1.expiry_ts - now) / YEAR_SEC, (s2.expiry_ts - now) / YEAR_SEC
            w = v1 * v1 * t1 + (v2 * v2 * t2 - v1 * v1 * t1) * (tau - t1) / (t2 - t1)
            return math.sqrt(max(w, 1e-12) / tau), "interpolated", s1.expiry_ts, s2.expiry_ts, v1, v2
        if hi:
            # Before the first usable Deribit expiry: interpolate between zero variance now and
            # the first expiry, which means using that expiry's vol.
            s2 = hi[0]
            v2 = s2.vol(strike)
            return v2, "before_first_expiry", None, s2.expiry_ts, None, v2
        s1 = lo[-1]
        v1 = s1.vol(strike)
        return v1, "after_last_expiry", s1.expiry_ts, None, v1, None


def fetch_surface(currency):
    data = http.deribit.get("/get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    by_expiry = {}
    for o in data.get("result") or []:
        try:
            _, exp_code, strike, cp = o["instrument_name"].split("-")
            strike = float(strike)
        except (ValueError, KeyError):
            continue
        iv, fwd = o.get("mark_iv"), o.get("underlying_price")
        if not iv or not fwd or iv <= 0:
            continue
        # Use out-of-the-money options only (calls above the forward, puts below): they are the
        # liquid side and avoid double-counting each strike.
        if (cp == "C" and strike < fwd) or (cp == "P" and strike >= fwd):
            continue
        e = by_expiry.setdefault(exp_code, {"fwd": fwd, "pts": {}})
        e["pts"][math.log(strike / fwd)] = iv / 100.0
    smiles = []
    for code, e in by_expiry.items():
        if len(e["pts"]) >= 3:
            smiles.append(Smile(parse_expiry(code), e["fwd"], list(e["pts"].items())))
    return Surface(currency, smiles, time.time())


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot, strike, vol, tau_years):
    """Probability the price finishes above `strike`, for lognormal moves with zero drift."""
    if tau_years <= 0 or vol <= 0:
        return 1.0 if spot > strike else 0.0
    sd = vol * math.sqrt(tau_years)
    d2 = (math.log(spot / strike) - 0.5 * sd * sd) / sd
    return norm_cdf(d2)
