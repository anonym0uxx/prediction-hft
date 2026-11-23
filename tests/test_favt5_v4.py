"""Rule-unit tests for the frozen v4 mechanical mirror (side, band, sizing, fee, fills, keying)."""
import math

import predhft.favt5_v4_shadow as v4


def test_exch_fee_rounding():
    assert v4.exch_fee(1, 0.5) == 0.0175                    # 0.07*0.25 = 0.0175 exactly
    expected = math.ceil(0.07 * 215 * 0.21 * 0.79 * 1e4 - 1e-9) / 1e4
    assert v4.exch_fee(215, 0.21) == 2.4968 == expected     # per-fill exchange rounding, up to 1e-4
    assert v4.exch_fee(0, 0.3) == 0.0


def test_less_likely_side():
    # YES cheap -> YES is less likely -> buy YES
    assert v4.less_likely_side(0.30, 0.31) == "YES"
    # YES dear -> NO is less likely -> buy NO
    assert v4.less_likely_side(0.70, 0.71) == "NO"
    # exact tie -> NO (recorded convention)
    assert v4.less_likely_side(0.495, 0.505) == "NO"


def test_band_edges():
    # in-band offers -> intent exists with limit = offer + 0.01
    i = v4.intent_for(0.24, 0.25, [(0.25, 500.0)])          # YES less likely, ask 0.25
    assert i is not None and i["side"] == "YES" and i["offer"] == 0.25 and abs(i["limit"] - 0.26) < 1e-9
    i20 = v4.intent_for(0.19, 0.20, [(0.20, 500.0)])
    assert i20 is not None and abs(i20["offer"] - 0.20) < 1e-9
    i35 = v4.intent_for(0.34, 0.35, [(0.35, 500.0)])
    assert i35 is not None and abs(i35["offer"] - 0.35) < 1e-9
    # out of band -> no intent
    assert v4.intent_for(0.19, 0.199, [(0.199, 500.0)]) is None
    assert v4.intent_for(0.35, 0.351, [(0.351, 500.0)]) is None
    # empty book -> no intent
    assert v4.intent_for(0.24, 0.25, []) is None


def test_sizing_50_budget():
    i = v4.intent_for(0.24, 0.25, [(0.25, 10**6)])
    limit = 0.26; unit = limit + 0.07 * limit * (1 - limit)   # 0.273468
    assert i["qty"] == math.floor(50.0 / unit) == 182
    assert i["max_total_cost"] == round(math.ceil(unit * 182 * 100 - 1e-8) / 100, 2)
    assert i["max_total_cost"] <= 50.0
    assert abs(i["modeled_fee"] - (i["max_total_cost"] - 182 * limit)) < 0.02


def test_no_side_preference_flip():
    # NO side less likely: side NO, offer from YES bids (1 - yes_bid)
    i = v4.intent_for(0.75, 0.76, [(0.25, 500.0)])           # NO ask 0.25 -> in band
    assert i["side"] == "NO"
    i2 = v4.intent_for(0.75, 0.76, [(0.40, 500.0)])          # NO ask 0.40 -> out
    assert i2 is None


def test_fill_against_partial_and_none():
    f = v4.fill_against([(0.25, 100.0), (0.26, 50.0)], 182, 0.26)
    assert f["filled"] == 150                                 # partial, depth-limited
    assert f["filled"] < 182
    assert abs(f["cost"] - (100 * 0.25 + 50 * 0.26)) < 1e-9
    assert abs(f["fee"] - (v4.exch_fee(100, 0.25) + v4.exch_fee(50, 0.26))) < 1e-9
    none = v4.fill_against([(0.27, 100.0)], 182, 0.26)
    assert none["filled"] == 0 and none["avg_px"] is None


def test_key_and_clean():
    assert v4.key("early_t300", "KXBTC15M", 1800000000) == "early_t300:KXBTC15M:1800000000"
    row = v4.clean({"a": float("nan"), "b": [float("inf"), 1.0], "c": {"d": 2.0}})
    assert row == {"a": None, "b": [None, 1.0], "c": {"d": 2.0}}


def test_book_offers_and_top():
    b = v4.Book()
    snap = {"yes_dollars_fp": [["0.72", 400.0], ["0.70", 100.0]], "no_dollars_fp": [["0.25", 300.0], ["0.27", 60.0]]}
    b.snapshot(snap, 100.0, 100.0)
    assert b.top() == (0.72, 0.73)                            # ask = 1 - best no bid = 1 - 0.27
    offs = b.offers("NO")                                     # NO ask = 1 - yes bid: 0.28, 0.30
    assert offs[0][0] == 0.28
    offs_y = b.offers("YES")                                  # YES ask = 1 - no bid: 0.73, 0.75
    assert offs_y[0][0] == 0.73


def test_runner_restart_keys(tmp_path):
    import json
    root = tmp_path / "od"
    root.mkdir()
    rows = [
        {"kind": "decision", "key": "early_t300:KXBTC15M:111", "intent": None},
        {"kind": "decision", "key": "late_t090:KXBTC15M:111", "intent": {"side": "YES", "qty": 5, "limit": 0.26}},
        {"kind": "settle", "key": "late_t090:KXBTC15M:111"},
    ]
    (root / "fills.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    import predhft.favt5_v4_shadow as m
    old_root = m.ROOT
    try:
        m.ROOT = root
        r1 = m.Runner()
        assert r1.keys == {"early_t300:KXBTC15M:111", "late_t090:KXBTC15M:111"}
        assert r1.open == {}                                    # settled decision is closed
        r2 = m.Runner()
        assert r2.keys == r1.keys                               # restart keeps the dedupe set
    finally:
        m.ROOT = old_root
