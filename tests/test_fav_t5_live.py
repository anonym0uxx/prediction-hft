import json, os, subprocess, sys, time

os.environ["PREDHFT_MODE"] = "live"          # module refuses to import otherwise
from predhft import fav_t5_live as fl

def test_order_payload_yes_favourite_is_bid_at_max_px():
    p = fl.order_payload("KXBTC15M-26NOV090000-00", "yes", 40, 0.99)
    assert p["side"] == "bid" and p["price"] == "0.9900" and p["count"] == "40.00"
    assert p["time_in_force"] == "immediate_or_cancel" and p["post_only"] is False
    assert p["cancel_order_on_pause"] is True and p["self_trade_prevention_type"] == "taker_at_cross"
    assert p["ticker"].startswith("KXBTC15M") and p["client_order_id"]

def test_order_payload_no_favourite_is_ask_at_one_minus_max_px():
    p = fl.order_payload("KXETH15M-26NOV090000-00", "no", 40, 0.99)
    assert p["side"] == "ask" and p["price"] == "0.0100"          # buy NO = sell YES at 1-0.99

def test_execute_converts_yes_leg_fill_to_favourite_leg(monkeypatch):
    def fake_req(method, path, body=None, timeout=15):
        assert method == "POST" and path == fl.ORDERS_PATH and body["side"] == "ask"
        return 201, {"order_id": "oid", "fill_count": "37.00", "remaining_count": "0.00",
                     "average_fill_price": "0.9830", "average_fee_paid": "0.0011", "ts_ms": 1}
    monkeypatch.setattr(fl, "_req", fake_req)
    x = fl.execute("T", "no", 37, 0.99)
    assert x["fill_count"] == 37 and abs(x["avg_px"] - 0.017) < 1e-9      # 1 - 0.983 YES-leg
    assert abs(x["fee"] - 0.0011 * 37) < 1e-9                              # per-contract fee x fills

def test_execute_records_partial_and_zero_fills(monkeypatch):
    outs = iter([(201, {"order_id": "o", "fill_count": "12.00", "remaining_count": "28.00",
                         "average_fill_price": "0.9800", "average_fee_paid": "0.0001"}),
                 (400, {"code": "bad", "message": "nope"})])
    monkeypatch.setattr(fl, "_req", lambda *a, **k: next(outs))
    x = fl.execute("T", "yes", 40, 0.99)
    assert x["fill_count"] == 12 and x["avg_px"] == 0.98
    x2 = fl.execute("T", "yes", 40, 0.99)
    assert "error" in x2 and "HTTP 400" in x2["error"]

def test_paper_counterpart_finds_entry(tmp_path):
    fake = tmp_path / "fills.jsonl"
    rows = [{"kind": "entry", "t": 1, "ticker": "TK1", "qty": 40, "avg_px": 0.98, "cost": 39.2},
            {"kind": "entry", "t": 2, "ticker": "TK2", "qty": 0}]
    fake.write_text("\n".join(json.dumps(r) for r in rows))
    pp = fl.paper_counterpart("TK1", str(fake))
    assert pp and pp["qty"] == 40 and pp["avg_px"] == 0.98
    assert fl.paper_counterpart("TK2", str(fake)) is None and fl.paper_counterpart("TKX", str(fake)) is None

def test_parity_row_fields():
    from predhft.fav_t5 import equity_at
    pp = {"qty": 40, "avg_px": 0.98, "cost": 39.20}
    live = {"qty": 37, "avg_px": 0.983, "cost": 36.40}
    row = {"paper_qty": pp["qty"], "live_qty": live["qty"], "delta_qty": live["qty"] - pp["qty"],
           "delta_cost": round(live["cost"] - pp["cost"], 4)}
    assert row["delta_qty"] == -3 and abs(row["delta_cost"] + 2.8) < 1e-9
    eq, held = equity_at([{"kind": "entry", "t": 10, "ticker": "A", "qty": 5, "cost": 4.9},
                          {"kind": "settle", "t": 20, "ticker": "A", "pnl": 0.1}], epoch=5.0, bankroll=100.0)
    assert abs(eq - 100.1) < 1e-9

def test_module_refuses_without_live_mode():
    code = ("import sys; sys.path.insert(0, 'src'); import os\n"
            "os.environ.pop('PREDHFT_MODE', None)\n"
            "try:\n"
            "    import predhft.fav_t5_live; print('FAIL: imported')\n"
            "except RuntimeError as e:\n"
            "    print('guard ok:', e)\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=".", capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "PREDHFT_MODE"})
    assert "guard ok" in r.stdout, r.stdout + r.stderr
