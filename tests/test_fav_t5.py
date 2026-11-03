from predhft.fav_t5 import book_sides, walk, decide

def ob(yes, no):
    return {"orderbook_fp": {"yes_dollars": [[str(p), str(q)] for p, q in yes], "no_dollars": [[str(p), str(q)] for p, q in no]}}

def test_book_sides_converts_bids_to_asks():
    ya, na = book_sides(ob([(0.97, 100)], [(0.01, 50), (0.02, 40)]))
    assert ya == [(0.98, 40.0), (0.99, 50.0)]
    assert na == [(0.03, 100.0)]

def test_walk_respects_max_px_and_lot():
    assert walk([(0.98, 40), (0.99, 50), (0.995, 999)], 300, 0.99) == (90, 40 * 0.98 + 50 * 0.99)
    assert walk([(0.98, 400)], 300, 0.99)[0] == 300

def test_decide_yes_favourite():
    d = decide({}, ob([(0.97, 100)], [(0.01, 50), (0.02, 40)]))
    assert d["side"] == "yes" and d["qty"] == 90 and d["fee"] > 0

def test_decide_skips_coinflip_and_no_depth():
    assert decide({}, ob([(0.50, 10)], [(0.48, 10)])) is None
    d = decide({}, ob([(0.995, 10)], [(0.004, 10)]))
    assert d["qty"] == 0

def test_decide_no_favourite():
    d = decide({}, ob([(0.01, 70)], [(0.975, 10)]))
    assert d["side"] == "no" and d["qty"] == 70 and abs(d["avg_px"] - 0.99) < 1e-9
