"""AUDIT_ADDENDUM2_2026-10-28 §1 — P22 execution model v2: raw offer preserved, no cap, explicit out-of-range status; regression tests
for the two v1 defects (truncated haircut at 0.985, below-offer booking at 0.995)."""
import math
import pytest
from predhft import p22_exec as X
from predhft.fees import kalshi_taker_fee


def test_v1_defect_truncated_haircut_at_0_985():
    v1 = X.legacy_v1_price(0.985)
    assert math.isclose(v1, 0.99) and math.isclose(v1 - 0.985, 0.005)          # haircut truncated to half a tick
    e = X.exec_price(0.985)
    assert e["raw_offer"] == 0.985 and math.isclose(e["exec_price"], 0.995) and e["status"] == X.STATUS_OUTSIDE


def test_v1_defect_below_offer_booking_at_0_995():
    v1 = X.legacy_v1_price(0.995)
    assert v1 < 0.995                                                            # paper price BELOW the displayed offer
    e = X.exec_price(0.995)
    assert e["exec_price"] > 0.995 and e["status"] == X.STATUS_OUTSIDE


def test_inside_range_is_plain_plus_one_tick_and_boundary_is_0_98():
    assert X.exec_price(0.47)["exec_price"] == pytest.approx(0.48) and X.exec_price(0.47)["status"] == X.STATUS_OK
    assert X.exec_price(0.98)["status"] == X.STATUS_OK and X.exec_price(0.98)["exec_price"] == pytest.approx(0.99)
    assert X.exec_price(0.981)["status"] == X.STATUS_OUTSIDE
    for bad in (None, 0.0, 1.0, 1.2, -0.1): assert X.exec_price(bad)["status"] == X.STATUS_UNAVAILABLE and X.exec_price(bad)["exec_price"] is None


def test_outside_range_records_are_excluded_from_edge_denominators_not_clipped():
    b = X.book_record(0.995, p_side=0.999, win=1.0)
    assert b["exec_status"] == X.STATUS_OUTSIDE and b["edge_c"] is None and b["real_c"] is None and b["fee_per_contract"] is None
    assert b["exec_price"] == pytest.approx(1.005) and b["raw_offer"] == 0.995 and b["measurement_version"] == "p22-v2"


def test_ok_record_edge_and_realised_match_definition():
    b = X.book_record(0.60, p_side=0.75, win=1.0, lot=10)
    fee = kalshi_taker_fee(10, 0.61) / 10
    assert b["edge_c"] == pytest.approx(100 * (0.75 - 0.61 - fee), abs=1e-3) and b["real_c"] == pytest.approx(100 * (1 - 0.61 - fee), abs=1e-3)


def test_recompute_record_preserves_raw_and_v1_values_and_sets_status():
    rec = {"book_src": "books", "offer_off": 0.995, "p_off": 0.999, "side_off": "yes", "y": 1.0, "edge_c_off": 0.0, "real_c_off": 0.9}
    r = X.recompute_record(rec, "off")
    assert r["offer_off"] == 0.995 and r["v1_real_c_off"] == 0.9 and r["exec_status_off"] == X.STATUS_OUTSIDE and r["real_c_off"] is None
    assert X.recompute_record({"book_src": None}, "off")["exec_status_off"] == "no_book"
    assert X.recompute_record({"book_src": "books", "offer_off": None}, "off")["exec_status_off"] == "no_offer"
    ok = X.recompute_record({"book_src": "books", "offer_off": 0.30, "p_off": 0.2, "side_off": "no", "y": 0.0}, "off")
    assert ok["exec_status_off"] == X.STATUS_OK and ok["exec_price_off"] == pytest.approx(0.31) and ok["real_c_off"] > 0
