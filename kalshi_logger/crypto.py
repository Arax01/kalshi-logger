"""Priority 2: crypto fair-value baseline (the control group).

Every couple of minutes, for each open BTC/ETH terminal-price market on Kalshi (above/below,
range, 15-minute up/down, end-of-year range):

    fair probability = chance the settlement price lands in the YES region, assuming moves are
    random with the size implied by Deribit options (lognormal, no drift).

Settlement detail from Kalshi's market rules: the settlement value is the simple average of the
60 seconds of CF Benchmarks' Real-Time Index before the close. Averaging the last minute
removes about 40 seconds' worth of variance, so we use (time to close - 40 s). We don't compute a
fair value in the final 2 minutes, when part of the average is already fixed.

Path-dependent markets ("will BTC hit X at any point") are not modelled.
"""
import logging
import math
import time

from . import catalog, config, db, fees, http, volsurface
from .util import parse_ts, quote

log = logging.getLogger(__name__)

AVERAGING_ADJ_SEC = 40
CURRENCIES = {"BTC": "btc_usd", "ETH": "eth_usd"}
_surface_cache = {}
SURFACE_MAX_AGE = 110


def _surface(asset):
    s = _surface_cache.get(asset)
    if s is None or time.time() - s.fetched_ts > SURFACE_MAX_AGE:
        s = volsurface.fetch_surface(asset)
        _surface_cache[asset] = s
    return s


def _deribit_spot(asset):
    return http.deribit.get("/get_index_price", {"index_name": CURRENCIES[asset]})["result"]["index_price"]


def _reference(asset, event_ticker):
    """Coinbase mid, Kalshi's settlement-index chart, and realised vol measured from that chart.

    Kalshi's public chart gives the 60-second average of the CF Benchmarks index every second for
    the last 3 hours. Realised vol uses 5-minute changes; for a 60 s moving average the variance of
    an h-second change is sigma^2 * (h - 20 s), which we correct for.
    """
    cb = kref = rv = None
    try:
        t = http.coinbase.get(f"/products/{asset}-USD/ticker")
        cb = (float(t["bid"]) + float(t["ask"])) / 2
    except Exception as exc:
        log.debug("Coinbase spot unavailable: %s", exc)
    if event_ticker:
        try:
            d = http.kalshi.get(f"/live_data/events/{event_ticker}", {"range": "3h"})
            series = d["live_data"]["details"].get("timeseries") or []
            if series and time.time() - series[-1]["t"] / 1000 < 120:
                kref = series[-1]["v"]
            rv = _realised_vol(series)
        except Exception as exc:
            log.debug("Kalshi reference price unavailable: %s", exc)
    return cb, kref, rv


def _realised_vol(series, step=300):
    v = {p["t"] // 1000: p["v"] for p in series if p.get("v")}
    if len(v) < 3600:
        return None
    start, end = min(v), max(v)
    moves = [math.log(v[t + step] / v[t]) for t in range(start, end - step + 1, step) if t in v and t + step in v]
    if len(moves) < 10:
        return None
    var_per_sec = sum(x * x for x in moves) / len(moves) / (step - 20)
    return math.sqrt(var_per_sec * volsurface.YEAR_SEC)


def fair_value(m, spot, surface, now, vol_override=None):
    """Returns (fair_yes, vol, method, lo_expiry, hi_expiry, vol_lo, vol_hi) or None if not modelled.

    With vol_override, that volatility is used instead of the options-implied one.
    """
    close_ts = parse_ts(m.get("close_time"))
    if not close_ts:
        return None
    secs = close_ts - now
    if secs < config.CRYPTO_MIN_SECONDS_TO_CLOSE:
        return None
    tau = (secs - AVERAGING_ADJ_SEC) / volsurface.YEAR_SEC
    st, lo_k, hi_k = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
    if st in ("greater", "greater_or_equal") and lo_k:
        ref_k = lo_k
    elif st in ("less", "less_or_equal") and hi_k:
        ref_k = hi_k
    elif st == "between" and lo_k and hi_k:
        ref_k = (lo_k + hi_k) / 2   # ranges are narrow, so one vol at the middle of the range
    else:
        return None
    if vol_override:
        vol, method, e_lo, e_hi, v_lo, v_hi = vol_override, "realised", None, None, None, None
    else:
        vol, method, e_lo, e_hi, v_lo, v_hi = surface.vol(ref_k, close_ts, now)
    if vol is None:
        return None
    if st in ("greater", "greater_or_equal"):
        p = volsurface.prob_above(spot, lo_k, vol, tau)
    elif st in ("less", "less_or_equal"):
        p = 1.0 - volsurface.prob_above(spot, hi_k, vol, tau)
    else:
        p = volsurface.prob_above(spot, lo_k, vol, tau) - volsurface.prob_above(spot, hi_k, vol, tau)
    return max(0.0, min(1.0, p)), vol, method, e_lo, e_hi, v_lo, v_hi


def _worth_logging(fair, edge_yes, edge_no):
    """Skip far-out strikes where nothing interesting can happen, to keep the database small."""
    if 0.02 <= fair <= 0.98:
        return True
    return max(e for e in (edge_yes, edge_no, -1.0) if e is not None) > -0.05


def run(after_gap=False):
    conn = db.connect()
    catalog.refresh(conn)
    written = 0
    series_by_asset = {}
    for series_ticker, asset in config.CRYPTO_SERIES.items():
        series_by_asset.setdefault(asset, []).append(series_ticker)

    for asset, series_list in series_by_asset.items():
        surface = _surface(asset)
        ref = None
        for series_ticker in series_list:
            # Spot is read immediately before and after the quotes so the two line up in time.
            spot_before = _deribit_spot(asset)
            data = http.kalshi.get("/markets", {"series_ticker": series_ticker, "status": "open", "limit": 1000})
            spot_after = _deribit_spot(asset)
            now = int(time.time())
            markets = data.get("markets") or []
            if not markets:
                continue
            spot = (spot_before + spot_after) / 2
            notes = []
            if after_gap:
                notes.append("after_gap")
            if abs(spot_after - spot_before) / spot > 0.0005:
                notes.append(f"spot moved {spot_after - spot_before:+.2f} while reading quotes")
            if ref is None:
                nearest = min(markets, key=lambda x: parse_ts(x.get("close_time")) or math.inf)
                ref = _reference(asset, nearest.get("event_ticker"))
            spot_cb, spot_k, rv = ref
            s = catalog.get(conn, series_ticker)
            for m in markets:
                fv = fair_value(m, spot, surface, now)
                if fv is None:
                    continue
                fair, vol, method, e_lo, e_hi, v_lo, v_hi = fv
                fair_rv = fair_value(m, spot, surface, now, vol_override=rv)[0] if rv else None
                bid, ask, bid_size, ask_size = quote(m)
                fee_yes = fees.taker_fee_per_contract(ask, ask_size, s.get("fee_type"), s.get("fee_multiplier")) \
                    if ask is not None else None
                fee_no = fees.taker_fee_per_contract(1 - bid, bid_size, s.get("fee_type"), s.get("fee_multiplier")) \
                    if bid is not None else None
                edge_yes = fair - ask - fee_yes if ask is not None and fee_yes is not None else None
                edge_no = bid - fair - fee_no if bid is not None and fee_no is not None else None
                if not _worth_logging(fair, edge_yes, edge_no):
                    continue
                mid, _ = catalog.ensure_market(conn, m)
                conn.execute(
                    "INSERT INTO crypto_fv(ts,market_id,asset,seconds_to_close,spot,spot_coinbase,spot_kalshi_ref,"
                    "vol,vol_method,expiry_lo_ts,expiry_hi_ts,vol_lo,vol_hi,fair_yes,vol_realised,fair_yes_realised,"
                    "yes_bid,yes_ask,bid_size,ask_size,fee_buy_yes,fee_buy_no,edge_buy_yes,edge_buy_no,note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, mid, asset, parse_ts(m.get("close_time")) - now, spot, spot_cb, spot_k, vol, method,
                     e_lo, e_hi, v_lo, v_hi, fair, rv, fair_rv, bid, ask, bid_size, ask_size, fee_yes, fee_no,
                     edge_yes, edge_no, "; ".join(notes) or None),
                )
                written += 1
            conn.commit()
    log.info("Crypto: %d fair values logged", written)
