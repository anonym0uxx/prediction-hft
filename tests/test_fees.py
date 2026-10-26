from predhft.fees import kalshi_taker_fee, pmus_taker_fee, kalshi_maker_fee, breakeven_edge_cents
import pytest

def test_kalshi_taker_100_lot_at_50c_is_1_75():
    assert kalshi_taker_fee(100, 0.50) == pytest.approx(1.75)

def test_kalshi_taker_rounds_up_per_order_small_lot():
    # 1 contract at 50c: 0.0175 -> rounds up to 2 cents
    assert kalshi_taker_fee(1, 0.50) == pytest.approx(0.02)

def test_pmus_matches_published_max():
    assert pmus_taker_fee(100, 0.50) == pytest.approx(1.74)

def test_maker_zero_by_default():
    assert kalshi_maker_fee(100, 0.5) == 0.0

def test_breakeven_edge_taker_vs_maker():
    assert breakeven_edge_cents(0.5, 100, taker=True) == pytest.approx(1.75)
    assert breakeven_edge_cents(0.5, 100, taker=False) == 0.0

def test_invalid_price():
    with pytest.raises(ValueError): kalshi_taker_fee(1, 1.0)
