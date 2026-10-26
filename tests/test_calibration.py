from predhft.calibration import brier, log_loss, reliability, flb_return_cents
from predhft.kalshi_public import mid_from_candle
import math

def test_brier_logloss_perfect_and_uniform():
    pts = [(0.999999, 1), (0.000001, 0)]
    assert brier(pts) < 1e-6
    assert abs(log_loss([(0.5, 1), (0.5, 0)]) - math.log(2)) < 1e-9

def test_reliability_bins_and_flb_return():
    pts = [(0.10, 0)] * 9 + [(0.10, 1)]
    r = reliability(pts); assert r[0]["n"] == 10 and abs(r[0]["realized"] - 0.1) < 1e-9
    rows = flb_return_cents(pts); assert abs(rows[0]["return_cents"]) < 1e-9

def test_mid_from_candle_handles_cents_and_dollars():
    assert abs(mid_from_candle({"yes_bid": {"close": 40}, "yes_ask": {"close": 44}}) - 0.42) < 1e-9
    assert abs(mid_from_candle({"yes_bid": {"close_dollars": "0.40"}, "yes_ask": {"close_dollars": "0.44"}}) - 0.42) < 1e-9
    assert abs(mid_from_candle({"price": {"close": 7}}) - 0.07) < 1e-9
    assert mid_from_candle({}) is None
