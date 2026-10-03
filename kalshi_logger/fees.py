"""Kalshi trading fees, as documented by Kalshi.

Sources (checked 2026-10-03):
* docs.kalshi.com "Fee Rounding": worked example of a 1-contract buy at $0.055 with a model fee of
  $0.00363825 = 0.07 x 0.055 x (1 - 0.055), i.e. taker fee = 0.07 x contracts x P x (1 - P).
* Series API field `fee_type` ('quadratic', 'quadratic_with_maker_fees',
  'quadratic_with_combo_maker_fees', 'flat') and `fee_multiplier`, which scales the formula.
  Some series have fee_multiplier 0 (no fee).
* API changelog: standard maker fee multiplier 0.25 (0.5 for combo quoters).
* Rounding: the fee is rounded up to $0.000001, then the balance change is rounded to the
  account's precision ($0.01 for non-direct members, $0.0001 for direct members). We assume the
  conservative case: total fee for an order rounded UP to the next whole cent.

Not verified: the 'flat' fee table lives in a PDF on kalshi.com that blocks automated access,
so markets in 'flat' series get no fee estimate (stored as NULL) rather than a guess.
"""
import math

TAKER_RATE = 0.07


def taker_fee_total(price, contracts, fee_type="quadratic", fee_multiplier=1.0):
    """Total taker fee in dollars for buying `contracts` at `price` (dollars, 0-1).

    Returns None if the fee structure is unknown.
    """
    if price is None or contracts is None:
        return None
    if fee_type not in ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"):
        return None
    if fee_multiplier is None:
        fee_multiplier = 1.0
    contracts = max(float(contracts), 1.0)
    raw = TAKER_RATE * fee_multiplier * contracts * price * (1.0 - price)
    raw = math.ceil(round(raw * 1e6, 6)) / 1e6
    return math.ceil(round(raw * 100, 6)) / 100.0


def taker_fee_per_contract(price, contracts, fee_type="quadratic", fee_multiplier=1.0):
    total = taker_fee_total(price, contracts, fee_type, fee_multiplier)
    if total is None:
        return None
    return total / max(float(contracts or 1), 1.0)
