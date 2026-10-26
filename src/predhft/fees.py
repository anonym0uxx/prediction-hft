"""Fee formulas (verified against primary docs 2026-10-26).

Kalshi: taker = ceil_cents(0.07 * C * p * (1-p)); maker 0 on most series (some series charge makers; combos 50% of taker).
Polymarket US: taker = Theta * C * p * (1-p), Theta = 0.0695 (0.10 table tennis); maker rebate 0 below $10M/month volume.
Both round UP to the next cent per order, which penalises small lots.
"""
from __future__ import annotations
import math

KALSHI_TAKER_THETA = 0.07
PMUS_TAKER_THETA = 0.0695

def _fee(theta: float, contracts: int, price: float, multiplier: float = 1.0) -> float:
    if not 0 < price < 1:
        raise ValueError("price must be in (0,1) dollars")
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    raw = multiplier * theta * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100  # round up to the cent, per order

def kalshi_taker_fee(contracts: int, price: float, multiplier: float = 1.0) -> float:
    return _fee(KALSHI_TAKER_THETA, contracts, price, multiplier)

def pmus_taker_fee(contracts: int, price: float, theta: float = PMUS_TAKER_THETA) -> float:
    return _fee(theta, contracts, price)

def kalshi_maker_fee(contracts: int, price: float, series_charges_makers: bool = False) -> float:
    """Most Kalshi series charge makers nothing; a few charge 0.0175 coefficient. Caller must know the series."""
    return _fee(0.0175, contracts, price) if series_charges_makers else 0.0

def breakeven_edge_cents(price: float, contracts: int = 1, taker: bool = True) -> float:
    """Minimum one-way edge (cents per contract) a strategy needs to pay the fee on this venue/price."""
    if not taker:
        return 0.0
    return kalshi_taker_fee(contracts, price) / contracts * 100
