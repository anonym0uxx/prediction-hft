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


def test_arm_unfilled_and_risk_limited_are_logged_not_filled(arm):
    r = _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.80})        # best ask .90 -> no size at/below .80
    assert r["result"]["status"] == "unfilled" and r["result"]["qty"] == 0 and arm.position_for(SNAP["ticker"]) is None
    rows = [json.loads(l) for l in (arm.root / "fills.jsonl").read_text().splitlines()]; assert rows[-1]["status"] == "unfilled" and rows[-1]["best_offer"] == 0.9
    # the unfilled intent id is consumed; the same side at the same decision time is not retried, the other side still can be
    r = _act(arm, {"action": "BUY_YES", "qty": 10, "limit_price": 0.95}); assert r["result"]["status"] == "refused:intent_exists"
    # risk_limited: SOL-v1 fills the shared crypto per-close cap -> refusal logged with reason, no fill row
    R.Ledger(arm.root.parent / "sol-v1", family="sol-v1").record_open("KXSOL15M-TEST-45", "KXSOL15M", 12.0, close_ts=T, side="yes")
    n = len(rows); r = _act(arm, {"action": "BUY_NO", "qty": 10, "limit_price": 0.12})
    assert r["result"]["status"].startswith("risk_limited:") and len((arm.root / "fills.jsonl").read_text().splitlines()) == n
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
