from datetime import datetime, timezone


def parse_ts(value):
    """ISO-8601 string -> Unix seconds (int), or None."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def num(value):
    """Kalshi fixed-point strings ('0.5600', '31.33') -> float, or None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_cc(dollars):
    """Dollars -> integer centi-cents (1 = $0.0001)."""
    return None if dollars is None else int(round(dollars * 10000))


def quote(market):
    """Best YES bid/ask in dollars and their sizes. A side with zero size counts as no quote."""
    bid, ask = num(market.get("yes_bid_dollars")), num(market.get("yes_ask_dollars"))
    bid_size, ask_size = num(market.get("yes_bid_size_fp")) or 0.0, num(market.get("yes_ask_size_fp")) or 0.0
    if bid_size <= 0:
        bid = None
    if ask_size <= 0:
        ask = None
    return bid, ask, bid_size, ask_size


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
