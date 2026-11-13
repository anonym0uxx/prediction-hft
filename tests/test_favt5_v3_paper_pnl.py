"""FavT5 v3 paper P&L book: arithmetic on a hand-built ledger, plus every reconciliation check firing. No network."""
import json, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "research" / "favt5_v3"))
import paper_pnl as pp

T0 = 1794509071.0          # 2026-11-12 10:44:31 PT


def dec(arm, close, intent=None, skip=None):
    return {"t": close - 480, "kind": "decision", "key": f"{arm}:KXBTC15M:{close}", "arm": arm, "ticker": f"KXBTC15M-{close}", "close": close,
            "decision_ts": close - (300 if arm == "btc_fav_t5" else 480), "intent": intent, "skip": skip, "entry_band": "45-60c" if intent else None}


def fill(arm, close, n, cost, fee, status="full"):
    return {"t": close - 479, "kind": "fill", "key": f"{arm}:KXBTC15M:{close}", "replay": "measured", "filled": n, "cost": cost, "fee": fee, "status": status, "avg_px": cost / n if n else None}


def settle(arm, close, result, won, runner_net):
    return {"t": close + 120, "kind": "settle", "key": f"{arm}:KXBTC15M:{close}", "arm": arm, "result": result, "won": won, "replays": {"measured": {"net_exchange_fees": runner_net}}}


def make(tmp, rows):
    (tmp / "acct.json").write_text(json.dumps({"start_balance_usd": 150.0, "start_ts": T0}))
    (tmp / "l.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n{\"kind\": \"deci")       # torn last line
    return tmp / "l.jsonl", tmp / "acct.json"


C1, C2, C3, C4 = 1794537000, 1794546900, 1794600000, 1794603600      # day 1 evening PT, day 1 night, day 2, day 2 (open)
NO20 = {"side": "NO", "qty": 20, "limit": 0.47, "max_total_cost": 9.86, "modeled_probability": .568, "modeled_net_dollars": 1.5}
YES30 = {"side": "YES", "qty": 30, "limit": 0.32, "max_total_cost": 9.9, "modeled_probability": .4, "modeled_net_dollars": 2}


def test_realized_pnl_is_payout_minus_cost_minus_fees_and_rolls_up_by_day(tmp_path):
    L, A = make(tmp_path, [
        dec("btc_v3", C1, NO20), fill("btc_v3", C1, 20, 9.38, 0.1703), settle("btc_v3", C1, "no", True, 10.4497),        # won 20 -> +10.4497
        dec("btc_v3", C2, skip="modeled net edge below $0.05 per contract"),
        dec("btc_v3", C3, YES30), fill("btc_v3", C3, 12, 3.84, 0.0549, "partial"), settle("btc_v3", C3, "no", False, -3.8949),  # partial, lost
        dec("btc_v3", C4, NO20), fill("btc_v3", C4, 20, 9.4, 0.17),                                                        # open
        dec("btc_fav_t5", C1, {"side": "YES", "qty": 10, "limit": .971, "max_total_cost": 9.73}), fill("btc_fav_t5", C1, 10, 9.69, .0211), settle("btc_fav_t5", C1, "yes", True, .2889)])
    b = pp.book(L, A, now=C4 + 60); s = b["summary"]["btc_v3"]
    assert (s["executed"], s["settled"], s["open"], s["wins"], s["losses"]) == (3, 2, 1, 1, 1)
    assert s["realized_pnl"] == round(10.4497 - 3.8949, 4) and s["equity"] == round(150 + 10.4497 - 3.8949, 2)
    assert s["open_cost_at_risk"] == 9.57 and s["max_drawdown"] == 3.89 and s["windows"] == 4 and s["intents"] == 3
    d = [x for x in b["daily"] if x["arm"] == "btc_v3"]
    assert [x["realized_pnl"] for x in d] == [10.4497, -3.8949] and d[-1]["cumulative_pnl"] == 6.5548 and d[-1]["equity"] == 156.55
    assert b["summary"]["btc_fav_t5"]["realized_pnl"] == 0.2889 and b["summary"]["eth_challenge"]["executed"] == 0       # arms never mix
    assert b["checks"] == []


def test_every_inconsistency_is_reported(tmp_path):
    L, A = make(tmp_path, [
        dec("btc_v3", C1, NO20), fill("btc_v3", C1, 20, 9.38, 0.1703), settle("btc_v3", C1, "no", True, 9.99),             # runner disagrees
        dec("btc_v3", C1, NO20),                                                                                            # duplicate key
        dec("btc_v3", C2, NO20),                                                                                            # intent, no fill row
        dec("btc_v3", C3, NO20), fill("btc_v3", C3, 20, 9.4, .17)])                                                         # never settled
    c = " | ".join(pp.book(L, A, now=C3 + 7200)["checks"])
    assert "recomputed +10.4497 vs runner 9.99" in c and "duplicate decision" in c and "without a measured fill" in c and "still unsettled" in c


def test_an_unfilled_intent_is_not_a_trade(tmp_path):
    L, A = make(tmp_path, [dec("btc_v3", C1, NO20), fill("btc_v3", C1, 0, 0.0, 0.0, "none"), settle("btc_v3", C1, "no", True, 0.0)])
    s = pp.book(L, A, now=C1 + 600)["summary"]["btc_v3"]
    assert (s["intents"], s["executed"], s["realized_pnl"], s["equity"]) == (1, 0, 0, 150.0)
