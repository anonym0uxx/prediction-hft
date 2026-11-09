import json
from predhft.fees import kalshi_taker_fee
from predhft import fav_t5 as ft
from predhft.fav_t5 import book_sides, walk, size_trade, equity_at, decide, CFG

def ob(yes, no):
    return {"orderbook_fp": {"yes_dollars": [[str(p), str(q)] for p, q in yes], "no_dollars": [[str(p), str(q)] for p, q in no]}}

def test_book_sides_converts_bids_to_asks():
    ya, na = book_sides(ob([(0.97, 100)], [(0.01, 50), (0.02, 40)]))
    assert ya == [(0.98, 40.0), (0.99, 50.0)]
    assert na == [(0.03, 100.0)]

def test_walk_respects_max_px_and_qty():
    assert walk([(0.98, 40), (0.99, 50), (0.995, 999)], 300, 0.99) == (90, 40 * 0.98 + 50 * 0.99)
    assert walk([(0.98, 400)], 300, 0.99)[0] == 300

def test_size_trade_fits_budget_with_fee():
    asks = [(0.98, 1000)]
    q, cost = size_trade(asks, 40.0, 0.99)
    assert q >= 1 and cost + kalshi_taker_fee(q, cost / q) <= 40.0
    assert size_trade(asks, 0.9, 0.99) == (0, 0.0)          # cannot afford a single contract

def test_decide_yes_favourite():
    d = decide({}, ob([(0.97, 100)], [(0.01, 50), (0.02, 40)]), budget=1000)
    assert d["side"] == "yes" and d["qty"] == 90 and d["fee"] > 0 and d["budget"] == 1000
    d40 = decide({}, ob([(0.97, 1000)], [(0.01, 1000)]), budget=40)   # ~98c asks, $40 budget
    assert d40["qty"] <= 40 and d40["cost"] + d40["fee"] <= 40

def test_decide_skips_coinflip_and_no_depth():
    d0 = decide({}, ob([(0.50, 10)], [(0.48, 10)]))
    assert d0["qty"] == 0 and d0["side"] is None and 0.4 < d0["mid"] < 0.6
    d = decide({}, ob([(0.995, 10)], [(0.004, 10)]))
    assert d["qty"] == 0

def test_decide_no_favourite():
    d = decide({}, ob([(0.01, 70)], [(0.975, 10)]), budget=1000)
    assert d["side"] == "no" and d["qty"] == 70 and abs(d["avg_px"] - 0.99) < 1e-9

def test_sizing_policy_is_40pct_of_105_bankroll():
    assert CFG["bankroll"] == 105.0 and CFG["stake_frac"] == 0.40
    assert abs(CFG["bankroll"] * CFG["stake_frac"] - 42.0) < 1e-9

def test_paper_epoch_syncs_bankroll_to_live_equity(tmp_path):
    live = tmp_path / "fav-t5-live" / "fills.jsonl"
    live.parent.mkdir()
    live.write_text(json.dumps({"kind": "start", "bankroll": 105.0}) + "\n"
                    + json.dumps({"kind": "settle", "pnl": 0.567}) + "\n", encoding="utf-8")
    orig = ft.ROOT
    try:
        ft.ROOT = tmp_path / "fav-t5"                # ROOT.parent -> tmp_path, where the live ledger lives
        assert abs((ft._live_equity() or 0) - 105.567) < 1e-6
        ft.ROOT = tmp_path / "sub" / "fav-t5"       # parent dir has no live ledger -> fallback None
        assert ft._live_equity() is None
    finally:
        ft.ROOT = orig


def test_equity_at_epoch_accounting():
    epoch = 1000.0
    rows = [
        {"kind": "entry", "t": 900.0, "ticker": "A", "qty": 5, "cost": 4.9},     # pre-epoch: settles to ledger only
        {"kind": "settle", "t": 1100.0, "ticker": "A", "pnl": 0.1},
        {"kind": "entry", "t": 1001.0, "ticker": "B", "qty": 6, "cost": 5.88},  # in-epoch win
        {"kind": "settle", "t": 1101.0, "ticker": "B", "pnl": 0.12},
        {"kind": "entry", "t": 1002.0, "ticker": "C", "qty": 7, "cost": 6.86},  # in-epoch, still open
    ]
    eq, held = equity_at(rows, epoch, bankroll=100.0)
    assert abs(eq - 100.12) < 1e-9        # pre-epoch settle ignored, B's pnl counted
    assert abs(held - 6.86) < 1e-9        # C's capital is working
    eq0, held0 = equity_at([], epoch, bankroll=100.0)
    assert eq0 == 100.0 and held0 == 0.0
