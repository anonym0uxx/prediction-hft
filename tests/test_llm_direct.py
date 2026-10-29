"""P24 LLM direct trade-decision arm tests: decision-block parser (robust + malformed -> no-op with error), hard-limit clamps and
refusals (no edge gate anywhere), paper fill at the model's limit (full / partial / no size), EXIT path (full + partial), settlement
from the exchange result with feed cross-check, 60-s marks, cohort tagging (prompt_hash + model), and the same-call integration
with the P23 probability arm (one API call, one budget count, both arms logged). No network, no real ledger (risk.GLOBAL -> tmp)."""
from __future__ import annotations
import json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pytest
from predhft import llm_direct as D
from predhft import llm_shadow as L
from predhft import sol_v1 as S
from predhft import risk as R
from predhft.fees import kalshi_taker_fee

T = 1_800_000_000 - (1_800_000_000 % 900)
SNAP = {"t": T - 600.4, "close_ts": T, "secs_left": 600.4, "K": 102.3785, "x_now": 102.62, "partial": None, "n_obs": 0, "m_rem": 60, "V": 608.5, "sigma_1s": 7.56e-05, "sigma_src": "realized", "kappa": 1.15,
        "p_model": 0.8637, "p_yes": 0.895, "blend_w": 0.0, "z": 1.097, "sd_mean": 0.2202, "yes_bid": 0.89, "yes_ask": 0.9, "mid": 0.895,
        "bids": [[0.89, 16.9], [0.88, 536.0], [0.87, 1633.8], [0.86, 525.0]], "asks": [[0.9, 54.0], [0.901, 10.0], [0.902, 22.0], [0.905, 12.0]], "book_src": "ws", "cfb_age_s": 0.3, "book_age_s": 0.5,
        "latency_s": 0.008, "decision": "abstain", "reasons": ["no_positive_ev"], "kind": "entry_eval", "ticker": "KXSOL15M-TEST-45", "version": "SOL-v1"}
BOOK = {"yes_bid": 0.89, "yes_ask": 0.9, "bids": SNAP["bids"], "asks": SNAP["asks"], "recv_t": T - 600.0, "fetch_s": 0.0, "src": "ws"}


@pytest.fixture
def arm(tmp_path, monkeypatch):
    """A DirectArm wired to tmp files, a fixed book, a controllable exchange result and NO network; the global risk ledger is redirected."""
    monkeypatch.setattr(R, "GLOBAL", tmp_path / "risk" / "events.jsonl")
    state = {"book": dict(BOOK), "result": None}
    a = D.DirectArm(tmp_path / "llm-direct", tmp_path / "sol-v1", book_fn=lambda tk, snap=None: state["book"], result_fn=lambda tk: state["result"], series_fn=lambda now: [])
    a._state = state
    return a


# ------------------------------------------------------------------ parser
@pytest.mark.parametrize("raw,holding,action,qty,px,err", [
    ({"action": "BUY_YES", "qty": 10, "limit_price": 0.91, "hold_plan": "HOLD_TO_SETTLEMENT", "reason": "r"}, False, "BUY_YES", 10, 0.91, None),
    ({"action": "buy no", "qty": "15", "limit_price": "0.12"}, False, "BUY_NO", 15, 0.12, None),
    ({"action": "Buy-Yes", "qty": 7.0, "limit_price": "45c"}, False, "BUY_YES", 7, 0.45, None),
    ({"action": "BUY_YES", "qty": 8, "limit_price": 62}, False, "BUY_YES", 8, 0.62, None),              # plain cents
    ({"action": "NO_TRADE", "qty": 0, "limit_price": None}, False, "NO_TRADE", 0, None, None),
    ({"action": "pass"}, False, "NO_TRADE", 0, None, None),
    ("NO_TRADE", False, "NO_TRADE", 0, None, None),
    ('{"action": "BUY_YES", "qty": 5, "limit_price": 0.5}', False, "BUY_YES", 5, 0.5, None),            # block arrived as a JSON string
    ({"action": "HOLD"}, True, "HOLD", 0, None, None),
    ({"action": "exit", "reason": "take profit"}, True, "EXIT", 0, None, None),
    ({"action": "sell"}, True, "EXIT", 0, None, None),
    # malformed / not allowed -> no-op with error
    (None, False, "NO_TRADE", 0, None, "decision_missing"),
    (None, True, "HOLD", 0, None, "decision_missing"),
    ("garbage text", False, "NO_TRADE", 0, None, "decision_not_object"),
    ([1, 2], False, "NO_TRADE", 0, None, "decision_not_object"),
    ({"qty": 10, "limit_price": 0.5}, False, "NO_TRADE", 0, None, "action_missing"),
    ({"action": "BUY_YES", "qty": "ten", "limit_price": 0.5}, False, "NO_TRADE", 0, None, "qty_invalid"),
    ({"action": "BUY_YES", "qty": 10}, False, "NO_TRADE", 0, None, "limit_price_invalid"),
    ({"action": "BUY_YES", "qty": 10, "limit_price": 1.7}, False, "NO_TRADE", 0, None, "limit_price_invalid"),
    ({"action": "BUY_YES", "qty": 10, "limit_price": 0.0}, False, "NO_TRADE", 0, None, "limit_price_invalid"),
    ({"action": "EXIT"}, False, "NO_TRADE", 0, None, "action_not_allowed:EXIT"),                        # flat: cannot exit
    ({"action": "BUY_YES", "qty": 10, "limit_price": 0.5}, True, "HOLD", 0, None, "action_not_allowed:BUY_YES"),  # holding: cannot add
    ({"action": "SHORT"}, False, "NO_TRADE", 0, None, "action_not_allowed:SHORT"),
])
def test_parse_decision(raw, holding, action, qty, px, err):
    d = D.parse_decision(raw, holding)
    assert d["action"] == action and d["qty"] == qty and d["error"] == err
    if px is None: assert d["limit_price"] is None
    else: assert abs(d["limit_price"] - px) < 1e-9
    if action in ("BUY_YES", "BUY_NO"): assert d["hold_plan"] in D.HOLD_PLANS


def test_parse_response_carries_decision_block_and_probability_unchanged():
    txt = '```json\n{"p_yes": 0.61, "rationale": "x", "beyond_market": "none", "decision": {"action": "BUY_NO", "qty": 10, "limit_price": 0.40, "hold_plan": "REVIEW_AT_T5", "reason": "r"}}\n```'
    pr = L.parse_response(txt); assert pr["p_llm"] == 0.61 and pr["parse"] == "json" and pr["decision_raw"]["action"] == "BUY_NO"
    pr2 = L.parse_response('{"p_yes": 0.4, "rationale": "old style"}'); assert pr2["p_llm"] == 0.4 and pr2["decision_raw"] is None
    assert D.parse_decision(pr2["decision_raw"], False)["error"] == "decision_missing"
    pr3 = L.parse_response('{"p_yes": 0.4, "decision": {"action": "BUY_YES", "qty": 5, "limit_price": 0.5,},}')     # trailing commas in nested object
    assert pr3["p_llm"] == 0.4 and pr3["decision_raw"]["qty"] == 5


# ------------------------------------------------------------------ hard limits (the only filter)
def test_hard_limits_clamp_and_refuse_without_any_edge_logic(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "GLOBAL", tmp_path / "events.jsonl")
    led = R.Ledger(tmp_path / "llm-direct", family="llm-direct")
    # a terrible trade (buy YES at .95 with no edge whatsoever) passes: there is no probability input at all
    hl = D.hard_limits("BUY_YES", 10, 0.95, led, T); assert hl["ok"] and hl["qty"] == 10 and hl["clamps"] == []
    assert D.hard_limits("BUY_YES", 50, 0.50, led, T)["qty"] == 20 and D.hard_limits("BUY_NO", 2, 0.50, led, T)["qty"] == 5     # lot bounds
    hl = D.hard_limits("BUY_YES", 20, 0.90, led, T)                            # $15 per market -> 16, then the shared $12 crypto per-close cap -> 13
    assert hl["qty"] == 13 and any("market_cost_cap_16" in c for c in hl["clamps"]) and any(c.endswith("_13") and "settlement_ts" in c for c in hl["clamps"])
    assert D.hard_limits("BUY_YES", 10, 1.2, led, T)["reason"] == "risk_limited:limit_out_of_bounds"
    assert D.hard_limits("NO_TRADE", 0, None, led, T)["reason"] == "not_an_entry"
    # shared per-close exposure: SOL-v1 already holds $9 on this close -> crypto cap $12 leaves $3 -> 6 contracts at .50 fit, 20 don't
    led_sol = R.Ledger(tmp_path / "sol-v1", family="sol-v1"); led_sol.record_open("KXSOL15M-X", "KXSOL15M", 9.0, close_ts=T, side="yes")
    hl = D.hard_limits("BUY_YES", 20, 0.50, led, T); assert hl["ok"] and hl["qty"] == 6 and any("crypto_close" in c for c in hl["clamps"])
    hl = D.hard_limits("BUY_YES", 20, 0.80, led, T); assert not hl["ok"] and hl["reason"].startswith("risk_limited:") and hl["qty"] == 0   # 3/.8 = 3 < min lot
    # loss halt refuses (enforced for this arm even in paper mode)
    led.record_settle("KXSOL15M-L", -16.0)
    hl = D.hard_limits("BUY_YES", 10, 0.50, led, T + 900); assert not hl["ok"] and hl["reason"].startswith("risk_limited:halt:")


# ------------------------------------------------------------------ fills at the model's limit
def test_fill_entry_full_partial_none():
    f = D.fill_entry("BUY_YES", 10, 0.90, BOOK); assert f["status"] == "filled" and f["qty"] == 10 and abs(f["px"] - 0.901) < 1e-9 and f["fee"] == kalshi_taker_fee(10, 0.901)
    f = D.fill_entry("BUY_YES", 20, 0.901, {**BOOK, "asks": [[0.9, 4.0], [0.901, 3.0], [0.95, 100.0]]}); assert f["status"] == "partial" and f["qty"] == 7 and f["depth_within_limit"] == 7.0
    f = D.fill_entry("BUY_YES", 10, 0.85, BOOK); assert f["status"] == "unfilled" and f["qty"] == 0 and f["px"] is None and f["best_offer"] == 0.9
    f = D.fill_entry("BUY_NO", 10, 0.12, BOOK); assert f["side"] == "no" and f["status"] == "filled" and abs(f["px"] - (0.11 + S.TICK)) < 1e-9     # NO at 1 - .89 (+1 tick)


# ------------------------------------------------------------------ the arm end to end (no network)
def _act(arm, raw, dsl=600.0, snap=SNAP, ph="ph1", model="m/x"):
    return arm.act(snap, dsl, raw, arm.block(snap, dsl, snap["close_ts"] - dsl), snap["close_ts"] - dsl + 3, prompt_hash=ph, model_name=model)


def test_arm_entry_then_hold_then_settle_with_cohort_tags(arm):
    blk = arm.block(SNAP, 600.0, T - 600); assert blk["holding"] is False and blk["allowed_actions"] == ["BUY_YES", "BUY_NO", "NO_TRADE"]
    r = _act(arm, {"action": "BUY_NO", "qty": 40, "limit_price": 0.12, "reason": "cheap NO"})
    assert r["result"]["status"] == "filled" and r["result"]["qty"] == 20 and r["result"]["side"] == "no" and r["decision"]["action"] == "BUY_NO"
    assert arm.position_for("KXSOL15M-TEST-45") is not None and arm.pos.has_close(T)
    # second BUY on the same market/close is refused (one position per market); T-5 block shows the position and allows HOLD/EXIT
    r2 = _act(arm, {"action": "BUY_YES", "qty": 5, "limit_price": 0.95}, dsl=300.0)
    assert r2["result"]["status"] == "no_action" or r2["result"]["status"].startswith("refused") or r2["decision"]["error"].startswith("action_not_allowed")
    blk5 = arm.block(SNAP, 300.0, T - 300); assert blk5["holding"] and blk5["allowed_actions"] == ["HOLD", "EXIT"] and blk5["position"]["side"] == "no" and blk5["position"]["qty"] == 20
    assert blk5["position"]["current_bid"] == 0.1 and blk5["position"]["current_ask"] == 0.11 and blk5["position"]["unrealised"] < 0        # paid .111+fee, bid .10
    r3 = _act(arm, {"action": "HOLD", "reason": "keep"}, dsl=300.0); assert r3["result"]["status"] == "hold"
    # malformed at T-5 while holding -> HOLD with error logged
    r4 = _act(arm, "???", dsl=300.0); assert r4["decision"]["action"] == "HOLD" and r4["decision"]["error"] == "decision_not_object"
    # settlement: not before close+90 s; then exchange result NO -> win for the NO side; feed cross-check recorded
    arm.tick(T + 10); assert arm.pos.open
    arm._state["result"] = {"result": "no", "expiration_value": "102.3000"}
    arm._series_fn = lambda now: [(s, 102.30) for s in range(T - 60, T)]
    arm.tick(T + 100); assert not arm.pos.open
    rows = [json.loads(l) for l in (arm.root / "fills.jsonl").read_text().splitlines()]
    st = [r for r in rows if r["kind"] == "settle"][0]; fill = [r for r in rows if r["kind"] == "fill"][0]
    assert st["win"] and st["feed_result"] == "no" and st["feed_disagrees"] is False and abs(st["pnl"] - (20 - fill["cost"] - fill["fee"])) < 1e-9
    assert fill["prompt_hash"] == "ph1" and fill["model_name"] == "m/x" and st["prompt_hash"] == "ph1" and st["model_name"] == "m/x" and fill["version"] == D.VERSION
    assert fill["book"]["bids"][0] == [0.89, 16.9] and fill["limit"] == 0.12 and fill["model_qty"] == 40 and fill["intended_qty"] == 20 and "qty_40->max_lot_20" in fill["risk_clamps"]
    decs = [json.loads(l) for l in (arm.root / "decisions.jsonl").read_text().splitlines()]
    assert len(decs) == 4 and decs[0]["parsed"]["action"] == "BUY_NO" and decs[0]["result"]["status"] == "filled" and decs[3]["parse_error"] == "decision_not_object"
    # the global ledger saw the open and the settle under family llm-direct (shared caps), realised matches
    ev = R.Ledger.events(0); assert {e["kind"] for e in ev} >= {"open", "settle"} and all(e["family"] == "llm-direct" for e in ev)
    assert abs(arm.realized - st["pnl"]) < 1e-9
    # marks: 60-s cadence, includes idle, equity = bankroll + realised when flat
    arm.tick(T + 200); marks = [json.loads(l) for l in (arm.root / "marks.jsonl").read_text().splitlines()]
    assert marks and marks[-1]["n_open"] == 0 and abs(marks[-1]["equity"] - (250 + arm.realized)) < 1e-9


def test_arm_unfilled_now_rests_and_risk_limited_is_logged_not_filled(arm):
    r = _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.80})        # best ask .90 -> no size at/below .80 -> exec model v2: the order RESTS, no position
    assert r["result"]["status"] == "resting" and r["result"]["qty"] == 0 and r["result"]["resting_qty"] == 10 and arm.position_for(SNAP["ticker"]) is None and arm.resting_for(SNAP["ticker"])["qty_remaining"] == 10
    rows = [json.loads(l) for l in (arm.root / "fills.jsonl").read_text().splitlines()]
    assert rows[-2]["kind"] == "fill" and rows[-2]["status"] == "unfilled" and rows[-2]["best_offer"] == 0.9 and rows[-2]["exec_model"] == "rest_v2" and rows[-1]["kind"] == "rest_placed" and rows[-1]["book"]["asks"][0] == [0.9, 54.0]
    # the intent id is consumed; the same side at the same decision time is not retried
    r = _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.95}); assert r["result"]["status"] == "refused:intent_exists" and arm.resting_for(SNAP["ticker"]) is not None
    # risk_limited: SOL-v1 fills the shared crypto per-close cap -> refusal logged with reason, no fill row, resting order untouched
    R.Ledger(arm.root.parent / "sol-v1", family="sol-v1").record_open("KXSOL15M-TEST-45", "KXSOL15M", 12.0, close_ts=T, side="yes")
    n = len(rows); r = _act(arm, {"action": "BUY_NO", "qty": 10, "limit_price": 0.12})
    assert r["result"]["status"].startswith("risk_limited:") and len((arm.root / "fills.jsonl").read_text().splitlines()) == n and arm.resting_for(SNAP["ticker"])["side"] == "yes"
    decs = [json.loads(l) for l in (arm.root / "decisions.jsonl").read_text().splitlines()]; assert decs[-1]["risk"]["ok"] is False and decs[-1]["result"]["status"].startswith("risk_limited:")


def test_arm_exit_full_and_partial(arm):
    _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.91})
    held = arm.position_for(SNAP["ticker"]); assert held["qty"] == 10 and abs(held["px"] - 0.901) < 1e-9
    # full exit at the bid (.89, 16.9 displayed) -> depth-weighted .89, taker fee again
    r = _act(arm, {"action": "EXIT", "reason": "flip"}, dsl=300.0)
    assert r["result"]["status"] == "exited" and r["result"]["qty"] == 10 and abs(r["result"]["exit_px"] - 0.89) < 1e-9 and r["result"]["exit_fee"] == kalshi_taker_fee(10, 0.89)
    assert abs(r["result"]["pnl"] - (10 * 0.89 - kalshi_taker_fee(10, 0.89) - held["cost"] - held["fee"])) < 1e-9 and arm.position_for(SNAP["ticker"]) is None
    # partial exit: only 4 contracts bid -> 4 sold, 6 remain open under a derived intent; positions survive a reload from fills.jsonl
    snap2 = {**SNAP, "ticker": "KXSOL15M-TEST-46", "close_ts": T + 900}
    arm._state["book"] = {**BOOK, "bids": [[0.89, 4.0]], "asks": [[0.9, 54.0]]}
    _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.91}, snap=snap2)
    r = _act(arm, {"action": "EXIT"}, dsl=300.0, snap=snap2)
    assert r["result"]["status"] == "exited_partial" and r["result"]["qty"] == 4 and arm.position_for(snap2["ticker"])["qty"] == 6
    reloaded = S.Positions(arm.root); assert reloaded.open[snap2["ticker"]]["qty"] == 6 and snap2["ticker"] in {p["ticker"] for p in reloaded.open.values()}
    # exit with no bid at all -> exit_unfilled, position kept
    arm._state["book"] = {**BOOK, "bids": [], "asks": [[0.9, 54.0]], "yes_bid": None}
    r = _act(arm, {"action": "EXIT"}, dsl=300.0, snap=snap2); assert r["result"]["status"] == "exit_unfilled" and arm.position_for(snap2["ticker"])["qty"] == 6


def test_arm_refuses_non_paper_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MODE", "live")
    with pytest.raises(RuntimeError): D.DirectArm(tmp_path / "x", tmp_path / "y", book_fn=lambda tk, snap=None: BOOK)


# ------------------------------------------------------------------ same call, both arms, one budget count
class _Transport:
    def __init__(self, responses): self.responses = list(responses); self.seen = []
    def __call__(self, request):
        import httpx
        self.seen.append(request); status, body = self.responses.pop(0); return httpx.Response(status, json=body)


def test_one_call_feeds_both_arms_and_counts_budget_once(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(R, "GLOBAL", tmp_path / "events.jsonl")
    monkeypatch.setattr(L, "gather_context", lambda now, red, root, assets=("SOL", "BTC", "ETH"): ([], {}, None, None, []))
    answer = {"p_yes": 0.30, "rationale": "r", "beyond_market": "none", "decision": {"action": "BUY_NO", "qty": 10, "limit_price": 0.12, "hold_plan": "REVIEW_AT_T5", "reason": "NO is cheap"}}
    tr = _Transport([(200, {"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "model": "m"})])
    client = L.LLMClient("k-test", "test/model", "https://example.invalid/v1", {**L.CFG, "retries": 1, "backoff_base_s": 0.0}, transport=httpx.MockTransport(tr))
    arm = D.DirectArm(tmp_path / "llm-direct", tmp_path / "sol-v1", red=client.red, book_fn=lambda tk, snap=None: BOOK, result_fn=lambda tk: None, series_fn=lambda now: [])
    budget = L.Budget(tmp_path / "budget.json", cap=10)
    row = L.forecast_once(client, budget, SNAP, 600.0, T - 600.0, tmp_path / "llm-shadow", direct=arm)
    assert len(tr.seen) == 1 and budget.state["calls"] == 1                                   # ONE call for both arms
    assert row["p_llm"] == 0.30 and row["llm_decision"]["decision"] in ("enter", "abstain")  # P23 arm unchanged (gated simulation)
    assert row["direct_arm"]["allowed_actions"] == ["BUY_YES", "BUY_NO", "NO_TRADE"] and row["direct_decision"]["action"] == "BUY_NO" and row["direct_result"]["status"] == "filled" and row["direct_result"]["qty"] == 10
    assert row["prompt_hash"] == L.PROMPT_HASH and arm.position_for(SNAP["ticker"])["prompt_hash"] == L.PROMPT_HASH and arm.position_for(SNAP["ticker"])["model_name"] == "test/model"
    sent = json.loads(tr.seen[0].content); assert "direct_arm" in sent["messages"][1]["content"] and "decision" in sent["messages"][0]["content"] and "NO_TRADE" in sent["messages"][0]["content"]
    # T-5 on the same market: the packet carries the position and the allowed actions become HOLD/EXIT
    tr.responses.append((200, {"choices": [{"message": {"content": json.dumps({"p_yes": 0.35, "decision": {"action": "HOLD", "reason": "wait"}})}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}))
    row5 = L.forecast_once(client, budget, {**SNAP, "t": T - 300.2}, 300.0, T - 300.0, tmp_path / "llm-shadow", direct=arm)
    assert budget.state["calls"] == 2 and row5["direct_arm"]["holding"] and row5["direct_arm"]["position"]["qty"] == 10 and row5["direct_decision"]["action"] == "HOLD" and row5["direct_result"]["status"] == "hold"
    sent5 = json.loads(tr.seen[1].content); assert '"holding":true' in sent5["messages"][1]["content"].replace(" ", "") and "EXIT" in sent5["messages"][1]["content"]


def test_prompt_hash_changed_for_v2_and_prompt_states_hard_facts():
    sp = L.SYSTEM_PROMPT
    for must in ("$250", "5..20", "0.07 x qty x price x (1 - price)", "pays $1.00", "0.01..0.99", "NO_TRADE freely", "HOLD", "EXIT", "no edge threshold"): assert must in sp
    assert L.PROMPT_HASH != "b9b52b5e795277e7" and L.VERSION == "P23-llm-shadow-v2"      # P23-v1/v2 cohort hash (Sonnet / Opus rows) is retired by the prompt change


# ------------------------------------------------------------------ scorer pieces (scripts/llm_direct_score.py)
def test_scorer_realised_trades_and_sharpe_per_15min_mark():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    import llm_direct_score as SC
    tag = {"prompt_hash": "h", "model_name": "m"}
    fills = [{"kind": "fill", "status": "open", "intent_id": "a", "ticker": "X1", "close_ts": T, "K": 1.0, "side": "yes", "qty": 10, "px": 0.5, "cost": 5.0, "fee": 0.18, "t": T - 600, **tag},
             {"kind": "settle", "intent_id": "a", "ticker": "X1", "close_ts": T, "side": "yes", "qty": 10, "pnl": 4.82, "win": True, "fee": 0.18, "t": T + 100, **tag},
             {"kind": "fill", "status": "open", "intent_id": "b", "ticker": "X2", "close_ts": T + 900, "K": 1.0, "side": "no", "qty": 5, "px": 0.3, "cost": 1.5, "fee": 0.08, "t": T + 300, **tag},
             {"kind": "exit", "intent_id": "b", "ticker": "X2", "close_ts": T + 900, "side": "no", "qty": 5, "pnl": -0.4, "exit_fee": 0.07, "fee": 0.08, "t": T + 600, **tag},
             {"kind": "fill", "status": "open", "intent_id": "c", "ticker": "X3", "close_ts": T + 1800, "K": 1.0, "side": "yes", "qty": 5, "px": 0.4, "cost": 2.0, "fee": 0.09, "t": T + 1200, **tag}]
    series = [(s, 1.5) for s in range(T + 1800 - 60, T + 1800)]                       # X3 settles YES on the feed (open position settled by the scorer)
    tr = SC.realised_trades(fills, series, {})
    assert [t["kind"] for t in tr] == ["settle", "exit", "settle"] and tr[2]["scorer_settled"] and abs(tr[2]["pnl"] - (5 - 2.0 - 0.09)) < 1e-9 and tr[0]["cohort"] == "h|m"
    s = SC.summarize(tr); assert s["trades"] == 3 and s["closes"] == 3 and abs(s["net_usd"] - (4.82 - 0.4 + 2.91)) < 1e-9 and s["se_c_per_ct_close"] is not None and s["max_drawdown_usd"] >= 0.4
    marks = [{"t": T + 60 * i, "equity": 250.0 + (0.0 if i < 20 else 4.82) - (1.0 if 40 <= i < 50 else 0.0)} for i in range(0, 90)]   # 90 minutes of 60-s marks incl. idle
    sh = SC.sharpe_15m(marks); assert sh["n_intervals"] == 4 and sh["sd_usd"] > 0 and sh["sharpe"] is not None
    assert SC.sharpe_15m(marks[:5])["n_intervals"] == 0


# ------------------------------------------------------------------ exec model v2: resting limit orders (crossing-only)
def _bk(bids, asks, recv_t):
    return {"yes_bid": (bids[0][0] if bids else None), "yes_ask": (asks[0][0] if asks else None), "bids": bids, "asks": asks, "recv_t": recv_t, "fetch_s": 0.0, "src": "ws"}

def _order(side="BUY_YES", qty=10, limit=0.28, t_placed=T - 600.0, cfg=None):
    return D.new_order("llm-direct:X:yes:T600", "X", T, 100.0, side, qty, limit, t_placed, {**D.CFG, **(cfg or {})}, prompt_hash="h", model_name="m", decision_secs_left=600.0)


def test_crossing_only_fill_requires_the_offer_at_or_below_the_limit():
    o = _order()                                                                  # BUY_YES 10 @ .28
    # bid touches/exceeds the limit but the ask stays above it -> NO fill (we never assume queue priority / maker fills)
    assert D.step_order(o, _bk([[0.28, 500.0], [0.27, 100.0]], [[0.30, 200.0]], T - 599.0), T - 599.0) == (None, None)
    assert D.step_order(o, _bk([[0.29, 500.0]], [[0.29, 200.0]], T - 598.0), T - 598.0) == (None, None)          # ask 1 tick above the limit
    ev, f = D.step_order(o, _bk([[0.26, 500.0]], [[0.28, 200.0]], T - 597.0), T - 597.0)                           # offer AT the limit crosses
    assert ev == "filled" and f["qty"] == 10 and abs(f["px"] - (0.28 + S.TICK)) < 1e-9 and f["fee"] == kalshi_taker_fee(10, 0.281)
    # BUY_NO: the NO offer is 1 - yes_bid; a yes_ask at/below (1 - limit) is irrelevant
    on = _order("BUY_NO", 10, 0.061)
    assert D.step_order(on, _bk([[0.936, 8.0]], [[0.938, 149.0]], T - 599.0), T - 599.0) == (None, None)           # NO ask = .064 > .061
    assert D.step_order(on, _bk([[0.93, 8.0]], [[0.939, 149.0]], T - 598.0), T - 598.0) == (None, None)            # yes_ask irrelevant; NO ask .070
    ev, f = D.step_order(on, _bk([[0.94, 8.0], [0.938, 50.0]], [[0.942, 149.0]], T - 597.0), T - 597.0)             # NO ask .060 <= .061 (8 displayed), next .062 above -> partial
    assert ev == "partial" and f["qty"] == 8 and abs(f["px"] - (0.06 + S.TICK)) < 1e-9


def test_partial_fills_accumulate_only_against_new_liquidity():
    o = _order(qty=10, limit=0.28)
    b1 = _bk([[0.26, 10.0]], [[0.28, 4.0], [0.29, 300.0]], T - 599.0); ev, f = D.step_order(o, b1, T - 599.0); assert ev == "partial" and f["qty"] == 4
    D.apply_fill(o, f, T - 599.0, b1); assert o["qty_remaining"] == 6 and o["qty_filled"] == 4 and len(o["fills"]) == 1
    assert D.step_order(o, b1, T - 598.0) == (None, None)                                                           # same book -> no second helping
    b1b = _bk([[0.25, 10.0]], [[0.28, 4.0], [0.29, 300.0]], T - 597.5); assert D.step_order(o, b1b, T - 597.5) == (None, None)   # book ticked elsewhere, same displayed size at the limit
    b2 = _bk([[0.26, 10.0]], [[0.27, 3.0], [0.28, 1.0], [0.29, 300.0]], T - 597.0); ev, f = D.step_order(o, b2, T - 597.0)    # fresh size: 3 @ .27 + 1 @ .28
    assert ev == "partial" and f["qty"] == 4 and abs(f["px"] - (0.2725 + S.TICK)) < 1e-9; D.apply_fill(o, f, T - 597.0, b2)
    b3 = _bk([[0.26, 10.0]], [[0.28, 50.0]], T - 596.0); ev, f = D.step_order(o, b3, T - 596.0); assert ev == "filled" and f["qty"] == 2; D.apply_fill(o, f, T - 596.0, b3)
    assert o["qty_remaining"] == 0 and o["qty_filled"] == 10 and abs(o["cost"] - (4 * 0.281 + 4 * 0.2735 + 2 * 0.281)) < 1e-6 and o["fee"] == kalshi_taker_fee(4, 0.281) + kalshi_taker_fee(4, 0.2735) + kalshi_taker_fee(2, 0.281)


def test_ttl_expiry_and_final_minute_cancel_come_before_any_fill():
    o = _order(t_placed=T - 600.0)                                                # ttl 60 s -> expires T-540; cancel_at T-60
    cross = lambda t: _bk([[0.26, 10.0]], [[0.27, 500.0]], t)
    assert o["expires_t"] == T - 540.0 and o["cancel_at_t"] == T - 60.0
    assert D.step_order(o, cross(T - 541.0), T - 541.0)[0] == "filled"
    assert D.step_order(o, cross(T - 540.0), T - 540.0) == ("expired", None)      # crossing book at expiry instant: time wins
    o2 = _order(t_placed=T - 100.0, cfg={"rest_ttl_s": 600.0})                   # long TTL but placed inside T-100 -> cancel at T-60
    assert D.step_order(o2, cross(T - 61.0), T - 61.0)[0] == "filled" and D.step_order(o2, cross(T - 60.0), T - 60.0) == ("cancelled_final_minute", None)
    assert D.step_order(o2, None, T - 59.0) == ("cancelled_final_minute", None)  # no book needed to cancel


def test_arm_resting_order_fills_later_and_position_exits_at_t5(arm):
    arm.now_fn = lambda: T - 597.0
    r = _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.85})         # ask .90 -> rests
    assert r["result"]["status"] == "resting" and arm.has_resting() and arm.sleep_s(5.0) == 1.0 and json.loads((arm.root / "open_orders.json").read_text())["orders"][0]["qty_remaining"] == 10
    # monitor: bid touches .85 but ask stays .90 -> nothing; then 4 offered at .85 -> partial; then the rest at .84
    evs = arm.monitor(T - 596.0, book_for={SNAP["ticker"]: _bk([[0.85, 100.0]], [[0.9, 54.0]], T - 596.0)}); assert evs == [] and arm.position_for(SNAP["ticker"]) is None
    evs = arm.monitor(T - 595.0, book_for={SNAP["ticker"]: _bk([[0.84, 100.0]], [[0.85, 4.0], [0.9, 54.0]], T - 595.0)})
    assert len(evs) == 1 and evs[0]["kind"] == "rest_partial" and evs[0]["qty_filled"] == 4 and evs[0]["qty_remaining"] == 6 and evs[0]["book"]["asks"][0] == [0.85, 4.0]
    held = arm.position_for(SNAP["ticker"]); assert held["qty"] == 4 and held["acquired_via"] == "rest" and held["status"] == "open" and abs(held["px"] - 0.851) < 1e-9
    blk = arm.block(SNAP, 300.0, T - 300); assert blk["holding"] and blk["position"]["qty"] == 4 and blk["position"]["resting_remainder"]["qty_remaining"] == 6 and blk["allowed_actions"] == ["HOLD", "EXIT"]
    evs = arm.monitor(T - 594.0, book_for={SNAP["ticker"]: _bk([[0.83, 100.0]], [[0.84, 50.0]], T - 594.0)})
    assert evs[0]["kind"] == "rest_filled" and not arm.has_resting() and arm.sleep_s(5.0) == 5.0 and json.loads((arm.root / "open_orders.json").read_text())["orders"] == []
    held = arm.position_for(SNAP["ticker"]); assert held["qty"] == 10 and held["n_rest_fills"] == 2 and abs(held["cost"] - (4 * 0.851 + 6 * 0.841)) < 1e-6 and abs(held["fee"] - (kalshi_taker_fee(4, 0.851) + kalshi_taker_fee(6, 0.841))) < 1e-9
    assert S.Positions(arm.root).open[SNAP["ticker"]]["qty"] == 10                # fills.jsonl reload sees the accumulated position
    assert sum(e["cost"] for e in R.Ledger.events(0) if e["kind"] == "open") == pytest.approx(held["cost"])      # shared ledger saw both increments
    # EXIT at T-5 on the resting-acquired position: sells into the bids, fee again
    arm._state["book"] = _bk([[0.89, 16.9]], [[0.9, 54.0]], T - 300.0); arm.now_fn = lambda: T - 300.0
    r = _act(arm, {"action": "EXIT"}, dsl=300.0); assert r["result"]["status"] == "exited" and r["result"]["qty"] == 10 and abs(r["result"]["pnl"] - (10 * 0.89 - kalshi_taker_fee(10, 0.89) - held["cost"] - held["fee"])) < 1e-9
    rows = [json.loads(l) for l in (arm.root / "fills.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["fill", "rest_placed", "fill", "rest_partial", "fill", "rest_filled", "exit"] and rows[-1]["acquired_via"] == "rest"
    decs = [json.loads(l) for l in (arm.root / "decisions.jsonl").read_text().splitlines()]; assert decs[0]["result"]["resting_intent"] == rows[1]["intent_id"] and decs[-1]["resting_intent"] is None


def test_arm_ttl_expiry_and_final_minute_cancel_events(arm):
    arm.now_fn = lambda: T - 597.0
    _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.85})
    evs = arm.monitor(T - 536.0, book_for={SNAP["ticker"]: _bk([[0.84, 100.0]], [[0.85, 50.0]], T - 536.0)})     # crossing book, but 61 s after placement -> TTL first
    assert evs[0]["kind"] == "rest_expired" and evs[0]["qty_filled"] == 0 and not arm.has_resting() and arm.position_for(SNAP["ticker"]) is None
    # placed at T-100 with a long TTL: cancelled at T-60 whatever the book
    arm.cfg["rest_ttl_s"] = 900.0; arm.now_fn = lambda: T - 100.0
    r = _act(arm, {"action": "BUY_NO", "qty": 10, "limit_price": 0.05}, dsl=300.0); assert r["result"]["status"] == "resting"
    assert arm.monitor(T - 61.0, book_for={SNAP["ticker"]: BOOK}) == [] and arm.has_resting()
    evs = arm.monitor(T - 60.0, book_for={SNAP["ticker"]: _bk([[0.96, 100.0]], [[0.97, 50.0]], T - 60.0)})       # NO offered at .04 <= .05 but it is T-60: cancel
    assert evs[0]["kind"] == "rest_cancelled_final_minute" and not arm.has_resting()
    # inside the final minute a new BUY that does not fill immediately is NOT rested
    arm.now_fn = lambda: T - 30.0; r = _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.85}, dsl=30.0); assert r["result"]["status"] == "unfilled" and r["result"]["resting_qty"] == 0 and not arm.has_resting()


def test_arm_t5_decision_governs_resting_order(arm):
    arm.now_fn = lambda: T - 597.0
    # flat + resting, T-5 says NO_TRADE -> cancelled
    _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.85}); assert arm.has_resting()
    r = _act(arm, {"action": "NO_TRADE"}, dsl=300.0); assert r["result"]["status"] == "no_trade" and r["result"]["cancelled_intent"].endswith(":yes:T600") and not arm.has_resting()
    rows = [json.loads(l) for l in (arm.root / "fills.jsonl").read_text().splitlines()]; assert rows[-1]["kind"] == "rest_cancelled_decision"
    # flat + resting, T-5 BUY of the OTHER side -> old cancelled (replaced), new order rests on the new side
    snap2 = {**SNAP, "ticker": "KXSOL15M-TEST-46", "close_ts": T + 900}
    _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.85}, snap=snap2)
    r = _act(arm, {"action": "BUY_NO", "qty": 5, "limit_price": 0.05}, dsl=300.0, snap=snap2)
    assert r["result"]["status"] == "resting" and r["result"]["replaced_intent"].endswith(":yes:T600") and arm.resting_for(snap2["ticker"])["side"] == "no" and arm.resting_for(snap2["ticker"])["intent_id"].endswith(":no:T300")
    # flat + resting, T-5 BUY of the SAME side -> the latest decision's qty/limit govern (old replaced, one order rests)
    snap3 = {**SNAP, "ticker": "KXSOL15M-TEST-47", "close_ts": T + 1800}
    _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.85}, snap=snap3)
    r = _act(arm, {"action": "BUY_YES", "qty": 7, "limit_price": 0.86}, dsl=300.0, snap=snap3)
    assert r["result"]["status"] == "resting" and arm.resting_for(snap3["ticker"])["limit"] == 0.86 and arm.resting_for(snap3["ticker"])["qty_remaining"] == 7 and len(arm.orders) == 2
    # a malformed T-5 answer leaves the resting order alone (no-op), HOLD while partially filled keeps it, EXIT cancels it
    r = _act(arm, "???", dsl=300.0, snap=snap3); assert r["decision"]["action"] == "NO_TRADE" and arm.resting_for(snap3["ticker"]) is not None
    arm.monitor(T - 590.0, book_for={snap3["ticker"]: _bk([[0.85, 10.0]], [[0.86, 3.0], [0.9, 50.0]], T - 590.0)}); assert arm.position_for(snap3["ticker"])["qty"] == 3 and arm.resting_for(snap3["ticker"])["qty_remaining"] == 4
    r = _act(arm, {"action": "HOLD"}, dsl=300.0, snap=snap3); assert r["result"]["status"] == "hold" and r["result"]["resting_kept"] and arm.has_resting()
    arm._state["book"] = _bk([[0.85, 10.0]], [[0.9, 50.0]], T - 299.0)
    r = _act(arm, {"action": "EXIT"}, dsl=300.0, snap=snap3); assert r["result"]["status"] == "exited" and r["result"]["qty"] == 3 and r["result"]["cancelled_intent"].endswith(":yes:T300") and arm.resting_for(snap3["ticker"]) is None
    assert [x["kind"] for x in map(json.loads, (arm.root / "fills.jsonl").read_text().splitlines())][-2:] == ["rest_cancelled_exit", "exit"]


def test_restart_recovers_open_orders_and_positions(arm, tmp_path, monkeypatch):
    arm.now_fn = lambda: T - 597.0
    _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.85})
    arm.monitor(T - 595.0, book_for={SNAP["ticker"]: _bk([[0.84, 100.0]], [[0.85, 4.0], [0.9, 54.0]], T - 595.0)})
    state = arm._state
    arm2 = D.DirectArm(arm.root, arm.sol_root, book_fn=lambda tk, snap=None: state["book"], result_fn=lambda tk: None, series_fn=lambda now: [])
    o = arm2.resting_for(SNAP["ticker"]); assert o is not None and o["qty_remaining"] == 6 and o["qty_filled"] == 4 and o["exec_model"] == "rest_v2" and o["intent_id"] in arm2.pos.intents
    assert arm2.position_for(SNAP["ticker"])["qty"] == 4 and arm2.has_resting()
    # the recovered order keeps filling / expiring on its original clock
    evs = arm2.monitor(T - 594.0, book_for={SNAP["ticker"]: _bk([[0.84, 100.0]], [[0.84, 50.0]], T - 594.0)}); assert evs[0]["kind"] == "rest_filled" and arm2.position_for(SNAP["ticker"])["qty"] == 10
    arm3 = D.DirectArm(arm.root, arm.sol_root, book_fn=lambda tk, snap=None: state["book"], result_fn=lambda tk: None, series_fn=lambda now: []); assert not arm3.has_resting() and arm3.position_for(SNAP["ticker"])["qty"] == 10
    # an order whose TTL passed while the runner was down is expired on the first monitor pass (no fill from a stale book)
    arm3.now_fn = lambda: T - 400.0; _act(arm3, {"action": "BUY_NO", "qty": 5, "limit_price": 0.05}, dsl=300.0, snap={**SNAP, "ticker": "KXSOL15M-TEST-46", "close_ts": T + 900})
    arm4 = D.DirectArm(arm.root, arm.sol_root, book_fn=lambda tk, snap=None: state["book"], result_fn=lambda tk: None, series_fn=lambda now: []); assert arm4.has_resting()
    evs = arm4.monitor(T - 300.0, book_for={"KXSOL15M-TEST-46": _bk([[0.97, 100.0]], [[0.98, 50.0]], T - 300.0)}); assert evs[0]["kind"] == "rest_expired" and not arm4.has_resting()


def test_replay_resting_on_tape_is_causal_and_crossing_only():
    o = _order(qty=10, limit=0.28, t_placed=T - 600.0)
    tape = [{"recv_t": T - 600.5, "bids": [[0.27, 100.0]], "asks": [[0.30, 50.0]]},                                 # before placement: not crossing anyway
            {"recv_t": T - 598.2, "bids": [[0.28, 100.0]], "asks": [[0.29, 50.0]]},                                 # bid at the limit, ask above -> nothing
            {"recv_t": T - 596.7, "bids": [[0.26, 100.0]], "asks": [[0.28, 3.0], [0.29, 50.0]]},                    # 3 offered at .28
            {"recv_t": T - 594.1, "bids": [[0.26, 100.0]], "asks": [[0.27, 20.0]]}]                                 # the rest at .27
    r = D.replay_resting(o, tape, poll_s=1.0)
    assert r["status"] == "filled" and r["qty_filled"] == 10 and len(r["fills"]) == 2 and r["fills"][0]["qty"] == 3 and abs(r["fills"][0]["px"] - 0.281) < 1e-9 and abs(r["fills"][1]["px"] - 0.271) < 1e-9
    assert r["fills"][0]["t"] >= T - 596.7 and r["time_to_first_fill_s"] >= 3.3 and r["time_to_last_fill_s"] >= 5.9 and r["avg_px"] == pytest.approx((3 * 0.281 + 7 * 0.271) / 10)
    r2 = D.replay_resting(_order(qty=10, limit=0.28, t_placed=T - 600.0), tape[:2], poll_s=1.0); assert r2["status"] == "expired" and r2["qty_filled"] == 0


def test_scorer_splits_exec_models_and_replays_ioc_unfilled_on_tape(tmp_path):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    import llm_direct_score as SC
    tag = {"prompt_hash": "h", "model_name": "m"}
    fills = [{"kind": "fill", "status": "unfilled", "fill_status": "unfilled", "intent_id": "i1", "ticker": "X1", "close_ts": T, "K": 1.0, "side": "yes", "qty": 0, "intended_qty": 10, "limit": 0.28, "best_offer": 0.32, "t": T - 600.0, "decision_secs_left": 600.0, **tag},   # v1 IOC row (no exec_model)
             {"kind": "fill", "status": "open", "fill_status": "partial", "intent_id": "i2", "ticker": "X2", "close_ts": T + 900, "K": 1.0, "side": "no", "qty": 4, "intended_qty": 10, "resting_qty": 6, "limit": 0.3, "px": 0.301, "cost": 1.204, "fee": 0.06, "t": T + 300, "exec_model": "rest_v2", **tag},
             {"kind": "rest_placed", "intent_id": "i2", "ticker": "X2", "close_ts": T + 900, "side": "no", "qty_filled": 4, "qty_remaining": 6, "resting_for_s": 0.0, "t": T + 300, "exec_model": "rest_v2", **tag},
             {"kind": "rest_filled", "intent_id": "i2", "ticker": "X2", "close_ts": T + 900, "side": "no", "qty_filled": 10, "qty_remaining": 0, "resting_for_s": 7.5, "t": T + 307.5, "exec_model": "rest_v2", **tag},
             {"kind": "fill", "status": "open", "intent_id": "i2", "ticker": "X2", "close_ts": T + 900, "K": 1.0, "side": "no", "qty": 10, "px": 0.3, "cost": 3.0, "fee": 0.15, "t": T + 307.5, "note": "resting fill accumulated", "exec_model": "rest_v2", **tag},
             {"kind": "settle", "intent_id": "i2", "ticker": "X2", "close_ts": T + 900, "side": "no", "qty": 10, "pnl": 6.85, "win": True, "fee": 0.15, "t": T + 1000, "exec_model": "rest_v2", **tag}]
    tr = SC.realised_trades(fills, [], {}); assert [t["exec_model"] for t in tr] == ["rest_v2"]
    rs = SC.resting_stats(fills); assert rs["placed"] == 1 and rs["filled_fully"] == 1 and rs["still_resting"] == 0 and rs["time_to_full_fill_s"]["median"] == 7.5
    # replay of the IOC-unfilled intent over a synthetic ws tape: the offer comes to the limit 4 s later -> would have filled; settlement from the feed; no pnl key
    sol = tmp_path / "sol-v1"; (sol / "book").mkdir(parents=True)
    day = SC.dt.datetime.fromtimestamp(T - 600.0, SC.dt.timezone.utc).strftime("%Y%m%d")
    tape = [{"t": T - 599.0, "recv_t": T - 599.0, "ticker": "X1", "yes_bid": 0.28, "yes_ask": 0.30, "bids": [[0.28, 100.0]], "asks": [[0.30, 50.0]]},
            {"t": T - 596.2, "recv_t": T - 596.2, "ticker": "X1", "yes_bid": 0.26, "yes_ask": 0.28, "bids": [[0.26, 100.0]], "asks": [[0.28, 6.0], [0.29, 50.0]]},
            {"t": T - 590.0, "recv_t": T - 590.0, "ticker": "X1", "yes_bid": 0.26, "yes_ask": 0.27, "bids": [[0.26, 100.0]], "asks": [[0.27, 60.0]]},
            {"t": T - 598.0, "recv_t": T - 598.0, "ticker": "OTHER", "yes_bid": 0.1, "yes_ask": 0.2, "bids": [[0.1, 1.0]], "asks": [[0.2, 1.0]]}]
    (sol / "book" / f"{day}.jsonl").write_text("\n".join(json.dumps(r) for r in tape) + "\n", encoding="utf-8")
    series = [(s, 1.5) for s in range(T - 60, T)]
    rp = SC.replay_ioc_unfilled(fills, sol, series, {})
    assert len(rp) == 1 and rp[0]["intent_id"] == "i1" and rp[0]["would_have_filled"] == "filled" and rp[0]["qty_filled"] == 10 and rp[0]["n_fills"] == 2 and rp[0]["time_to_first_fill_s"] == 4.0 and rp[0]["time_to_last_fill_s"] == 10.0
    assert rp[0]["avg_px"] == pytest.approx((6 * 0.281 + 4 * 0.271) / 10) and rp[0]["settlement"] == "yes" and rp[0]["side_would_have_won"] is True and rp[0]["tape_rows"] == 3 and "pnl" not in rp[0]

