"""Calibration / favorite-longshot helpers. Inputs: list of (implied_prob, outcome01)."""
from __future__ import annotations
import math
from statistics import fmean

DEFAULT_BINS = [(0, .05), (.05, .15), (.15, .3), (.3, .5), (.5, .7), (.7, .85), (.85, .95), (.95, 1.0001)]

def brier(points): return fmean((p - y) ** 2 for p, y in points)

def log_loss(points, eps=1e-6):
    return fmean(-(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps))) for p, y in points)

def reliability(points, bins=DEFAULT_BINS):
    out = []
    for lo, hi in bins:
        b = [(p, y) for p, y in points if lo <= p < hi]
        if b:
            out.append({"lo": lo, "hi": hi, "n": len(b), "implied": fmean(p for p, _ in b), "realized": fmean(y for _, y in b)})
    return out

def flb_return_cents(points, bins=DEFAULT_BINS, taker_fee_cents=None):
    """Expected return per $1 contract bought at implied price, by bin: realized - implied (- fee)."""
    rows = []
    for r in reliability(points, bins):
        ret = (r["realized"] - r["implied"]) * 100
        if taker_fee_cents is not None: ret -= taker_fee_cents(r["implied"])
        rows.append({**r, "return_cents": ret})
    return rows
