"""P19 quote path: orderbook parsing on fixtures (ordering, cents vs dollars, empty sides, depth), bucketing, scoring on synthetic rows, feed tail helpers."""
import json, math, pathlib
import pytest
from predhft import open_quotes as OQ, quote_path as QP, feed_tail as FT

# ---------------------------------------------------------------------------------------------------------- orderbook parsing
FP_ASC = {"orderbook_fp": {"yes_dollars": [["0.4000", "10"], ["0.5000", "20"], ["0.5100", "5"]], "no_dollars": [["0.4500", "7"], ["0.4700", "30"], ["0.4800", "12"]]}}

def test_parse_book_best_levels_do_not_depend_on_order():
    b = OQ.parse_book(FP_ASC)
    assert b["yes_bid"] == 0.51 and b["yes_bid_size"] == 5 and b["yes_ask"] == pytest.approx(0.52) and b["yes_ask_size"] == 12   # ask = 1 - best NO bid
    rev = {"orderbook_fp": {k: list(reversed(v)) for k, v in FP_ASC["orderbook_fp"].items()}}
    b2 = OQ.parse_book(rev); assert (b2["yes_bid"], b2["yes_ask"], b2["yes_bid_size"], b2["yes_ask_size"]) == (b["yes_bid"], b["yes_ask"], b["yes_bid_size"], b["yes_ask_size"])
    shuffled = {"orderbook_fp": {"yes_dollars": [["0.5000", "20"], ["0.4000", "10"], ["0.5100", "5"]], "no_dollars": [["0.4800", "12"], ["0.4500", "7"], ["0.4700", "30"]]}}
    assert OQ.parse_book(shuffled)["bids"] == b["bids"] and OQ.parse_book(shuffled)["asks"] == b["asks"]

def test_parse_book_depth_and_ordering_of_levels():
    b = OQ.parse_book(FP_ASC)
    assert b["bids"] == [(0.51, 5.0), (0.5, 20.0), (0.4, 10.0)] and b["asks"] == [(0.52, 12.0), (0.53, 30.0), (0.55, 7.0)]
    assert b["bid_depth"] == 35.0 and b["ask_depth"] == 49.0 and b["ok"]
    deep = {"orderbook_fp": {"yes_dollars": [[f"{p/100:.4f}", "1"] for p in range(10, 60)], "no_dollars": []}}
    d = OQ.parse_book(deep, depth=3); assert len(d["bids"]) == 3 and d["bids"][0][0] == 0.59 and d["asks"] == [] and d["yes_ask"] is None

def test_parse_book_cents_fallback_and_empty_and_garbage():
    c = {"orderbook": {"yes": [[1, 100], [40, 10]], "no": [[55, 20]]}}      # 1 cent must NOT be read as 1.0 dollars
    b = OQ.parse_book(c); assert b["yes_bid"] == 0.40 and b["yes_ask"] == pytest.approx(0.45) and b["bids"][-1] == (0.01, 100.0)
    e = OQ.parse_book({"orderbook_fp": {"yes_dollars": None, "no_dollars": None}}); assert e["yes_bid"] is None and e["yes_ask"] is None and not e["ok"]
    assert not OQ.parse_book({})["ok"]
    g = OQ.parse_book({"orderbook_fp": {"yes_dollars": [["x", "1"], ["0.3000"], ["0.2000", "4"]], "no_dollars": [["1.0000", "5"], ["0.0000", "5"]]}})
    assert g["bids"] == [(0.2, 4.0)] and g["asks"] == []                     # garbage levels skipped, 0/1 prices ignored

def test_close_ts_and_sample_skips_final_minute(monkeypatch, tmp_path):
    assert OQ.close_ts_of({"close_time": "2026-10-28T04:45:00Z"}) == 1793162700.0 and OQ.close_ts_of({"close_time": "bad"}) is None
    class K:                                              # no network: any call would fail the test
        def markets(self, **kw): raise AssertionError("must not list markets inside the final minute")
        def orderbook(self, tk): raise AssertionError("must not fetch books inside the final minute")
    rec = OQ.Recorder(k=K(), out=tmp_path / "q.jsonl")
    assert rec.sample(now=1793162700 - 30) == 0 and not (tmp_path / "q.jsonl").exists()

# ------------------------------------------------------------------------------------------------------------------ buckets
def test_bucket_of_minutes_left():
    assert QP.bucket_of(870) == 15 and QP.bucket_of(840) == 14 and QP.bucket_of(839.9) == 14 and QP.bucket_of(61) == 2 and QP.bucket_of(60) == 1 and QP.bucket_of(5) == 1

def test_p_ref_direction_and_horizon():
    base = {"strike": 100.0, "cfb_value": 100.0 * math.exp(5e-4), "secs_left": 600, "sigma1": 0.0003, "asset": "BTC"}
    p = QP.p_ref(base); assert 0.5 < p < 1
    assert QP.p_ref({**base, "cfb_value": 100.0 * math.exp(-5e-4)}) == pytest.approx(1 - p)
    assert QP.p_ref({**base, "secs_left": 120}) > p                          # same lead, less time left -> more certain
    assert QP.p_ref({**base, "cfb_value": None, "replica": 100.0 * math.exp(5e-4)}) == pytest.approx(p)   # replica fallback
    assert QP.p_ref({**base, "strike": None}) is None

# ------------------------------------------------------------------------------------------------------------------ scoring
def mk(ticker, close_ts, secs_left, yb, ya, asset="BTC", strike=100.0, x=100.0):
    return {"v": 2, "ticker": ticker, "series": f"KX{asset}15M", "asset": asset, "close_ts": close_ts, "secs_left": secs_left, "yes_bid": yb, "yes_ask": ya, "strike": strike, "cfb_value": x, "sigma1": 0.0003}

def test_score_calibration_brier_and_availability_on_synthetic():
    rows, res = [], {}
    # 20 windows: 10 YES winners with YES ask .70 at b=14 and .89 at b=5; 10 NO winners with YES bid .30 (NO ask .70) at b=14 and YES bid .05 at b=5
    for i in range(10):
        cl = 1000 * (i + 1); tk = f"W{i}"; res[tk] = {"result": "yes"}
        rows += [mk(tk, cl, 14 * 60 - 30, 0.68, 0.70, x=100.02), mk(tk, cl, 5 * 60 - 30, 0.88, 0.89, x=100.05)]
    for i in range(10):
        cl = 1000 * (i + 11); tk = f"L{i}"; res[tk] = {"result": "no"}
        rows += [mk(tk, cl, 14 * 60 - 30, 0.30, 0.32, x=99.98), mk(tk, cl, 5 * 60 - 30, 0.05, 0.07, x=99.95)]
    sc = QP.score(rows, res)
    assert sc["n_joined"] == 40 and set(sc["buckets"]) == {("BTC", 14), ("BTC", 5)}
    b14, b5 = sc["buckets"][("BTC", 14)], sc["buckets"][("BTC", 5)]
    assert b14["n"] == 20 and b14["closes"] == 20 and b14["avail"][0.95] == 1.0 and b14["avail"][0.90] == 1.0 and b14["avail"][0.80] == 1.0   # win side at .70 everywhere
    assert b5["avail"][0.95] == 1.0 and b5["avail"][0.90] == 0.5 and b5["avail"][0.80] == 0.0                                           # YES winners cost .89, NO winners .95
    assert b14["brier_mkt"] == pytest.approx((1 - 0.69) ** 2, abs=1e-9) and b5["brier_mkt"] == pytest.approx(0.5 * (1 - 0.885) ** 2 + 0.5 * 0.06 ** 2, abs=1e-9)
    assert b14["dbrier_mkt_minus_ref"] is not None and b14["dbrier_se"] is not None and b14["mkt_p_win_mean"] == pytest.approx(0.69)
    cal = {c["bin_lo"]: c for c in sc["calibration"]["BTC"]}
    assert cal[0.6]["n"] == 10 and cal[0.6]["freq"] == 1.0 and cal[0.3]["freq"] == 0.0 and cal[0.0]["n"] == 10
    assert sc["first_available"]["BTC"][0.80] == [14] * 20 and sorted(sc["first_available"]["BTC"][0.95]) == [14] * 20
    rep = QP.fmt_report(sc); assert "| BTC | 14 | 20 |" in rep and "Calibration" in rep

def test_score_ignores_unsettled_and_v1_rows_are_readable(tmp_path):
    p = tmp_path / "q.jsonl"
    v1 = {"t": 1.0, "series": "KXBTC15M", "ticker": "V1", "close_ts": 900.0, "mins_left": 14.5, "strike": 100.0, "yes_bid": 0.5, "yes_ask": 0.51, "yes_bid_qty": 10, "no_bid_qty": 5, "cfb_value": 100.0, "cfb_partial": None, "cfb_age_s": 0.1, "orderbook_error": None}
    p.write_text(json.dumps(v1) + "\n" + json.dumps({"v": 2, "series": "KXBTC15M", "close_ts": 900, "error": "markets: boom"}) + "\n" + json.dumps(mk("U", 1800, 500, 0.4, 0.42)) + "\n")
    rows = QP.load_rows(p); assert len(rows) == 2 and rows[0]["asset"] == "BTC" and rows[0]["secs_left"] == pytest.approx(870)
    sc = QP.score(rows, {"V1": {"result": "yes"}}); assert sc["n_joined"] == 1 and ("BTC", 15) in sc["buckets"]

def test_cluster_mean_se():
    m, se, G = QP.cluster_mean_se([1, 3, 10], [1, 1, 2]); assert m == 6.0 and G == 2 and se == pytest.approx(math.sqrt(((2 - 6) ** 2 + (10 - 6) ** 2) / 1 / 2))
    assert QP.cluster_mean_se([5.0], [1]) == (5.0, None, 1)

# ---------------------------------------------------------------------------------------------------------------- feed tail
def test_feed_tail_rows_move_and_sigma(tmp_path):
    p = tmp_path / "BTC.jsonl"
    with p.open("w") as f:
        for i in range(400): f.write(json.dumps({"t": 1000 + i, "value": 100.0 * math.exp(0.0001 * (i % 7))}) + "\n")
    rows = FT.tail_rows(p, since=1000 + 360); assert len(rows) == 40 and rows[0]["t"] == 1360
    assert FT.move_bp(rows, "value", now=1400, horizon_s=30) == pytest.approx(1e4 * math.log(rows[-1]["value"] / [r for r in rows if r["t"] >= 1370][0]["value"]))
    assert FT.move_bp(rows[-1:], "value", now=1400, horizon_s=30) is None
    s = FT.sigma_1m_from_rows([{"t": 60 * k, "value": 100.0 * (1.001 if k % 2 else 1.0)} for k in range(80)], min_minutes=30); assert s == pytest.approx(math.log(1.001), rel=1e-3)
    assert FT.sigma_1m_from_rows([], min_minutes=30) is None
    assert FT.tail_rows(tmp_path / "missing.jsonl", 0) == []
