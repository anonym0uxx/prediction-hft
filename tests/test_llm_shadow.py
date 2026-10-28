"""P23 LLM early-entry shadow tests: packet assembly from a fixture snapshot (no network), parser robustness, budget stop,
key redaction (nothing resembling the key reaches logs), decision-time scheduling, decision simulation through SOL-v1's EV path."""
from __future__ import annotations
import json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pytest
import pandas as pd
from predhft import llm_shadow as L
from predhft import sol_v1 as S

T = 1_800_000_000 - (1_800_000_000 % 900)
FAKE_KEY = "sk-nous-FAKEKEY0123456789abcdefABCDEF0123456789xyz"

SNAP = {"t": T - 600.4, "close_ts": T, "secs_left": 600.4, "K": 102.3785, "x_now": 102.62, "partial": None, "n_obs": 0, "m_rem": 60, "V": 608.5, "sigma_1s": 7.56e-05, "sigma_src": "realized", "kappa": 1.15,
        "p_model": 0.8637, "p_yes": 0.895, "blend_w": 0.0, "z": 1.097, "sd_mean": 0.2202, "yes_bid": 0.89, "yes_ask": 0.9, "mid": 0.895,
        "bids": [[0.89, 16.9], [0.88, 536.0], [0.87, 1633.8], [0.86, 525.0]], "asks": [[0.9, 54.0], [0.901, 10.0], [0.902, 22.0], [0.905, 12.0]], "book_src": "ws", "cfb_age_s": 0.3, "book_age_s": 0.5,
        "latency_s": 0.008, "decision": "abstain", "reasons": ["no_positive_ev"], "kind": "entry_eval", "ticker": "KXSOL15M-TEST-45", "version": "SOL-v1"}


def series(start, end, v=102.5, bump=0.01): return [(s, v + (bump if s % 2 else -bump)) for s in range(start, end)]
def candles(n=70, base=102.0, t_end=T - 600):
    ts = [t_end // 60 * 60 - 60 * k for k in range(n, 0, -1)]
    c = [base + 0.01 * k for k in range(n)]
    return pd.DataFrame({"ts": ts, "open": c, "high": [x + 0.05 for x in c], "low": [x - 0.05 for x in c], "close": c, "volume": [10.0] * n})


# ------------------------------------------------------------------ packet assembly (pure, no network)
def test_packet_assembly_from_fixture_is_deterministic_and_complete():
    now = T - 600.0; ser = series(T - 1300, T - 600)
    pk = L.assemble_packet(SNAP, now, ser, {"SOL": candles(), "BTC": candles(base=81000.0), "ETH": candles(base=3500.0)}, {"t": now - 2, "px": 102.69, "basis_med_bp": 6.9}, {"t": now - 60, "funding": 1e-4, "oi_usd": 3.2e8, "cb_imb_60s": 0.14}, 600.0)
    assert pk["strike_K"] == 102.3785 and pk["official_index_now"] == 102.62 and abs(pk["spot_minus_K_bp"] - 1e4 * (102.62 / 102.3785 - 1)) < 0.01
    assert pk["settlement_window"] == {"started": False, "partial_avg": None, "n_observed": 0, "n_remaining": 60}
    assert pk["kalshi_book"]["bids_top3"] == [[0.89, 16.9], [0.88, 536.0], [0.87, 1633.8]] and len(pk["kalshi_book"]["asks_top3"]) == 3
    assert pk["policy_model"]["p_model"] == 0.8637 and pk["policy_model"]["sigma_1s_bp"] == 0.756
    ip = pk["official_index_path"]; assert ip["step_s"] == 10 and len(ip["values"]) == 61 and ip["n_missing"] == 0 and ip["last"] is not None
    assert pk["candles_1m"]["SOL"]["n"] == 60 and pk["candles_1m"]["SOL"]["ret_5m_bp"] is not None and pk["comove"]["SOL_BTC"]["n"] == 15
    assert pk["perp_okx"]["age_s"] == 2.0 and pk["context"]["funding"] == 1e-4 and pk["context"]["age_s"] == 60.0
    # deterministic: same inputs -> same hash; age fields present on every optional block
    assert L.packet_hash(pk) == L.packet_hash(L.assemble_packet(SNAP, now, ser, {"SOL": candles(), "BTC": candles(base=81000.0), "ETH": candles(base=3500.0)}, {"t": now - 2, "px": 102.69, "basis_med_bp": 6.9}, {"t": now - 60, "funding": 1e-4, "oi_usd": 3.2e8, "cb_imb_60s": 0.14}, 600.0))
    msgs = L.messages_for(pk); assert msgs[0]["role"] == "system" and "60" in msgs[0]["content"] and "tie is YES" in msgs[0]["content"] and msgs[1]["content"].startswith("PACKET:")


def test_packet_marks_stale_or_missing_context():
    now = T - 300.0
    pk = L.assemble_packet(SNAP, now, [], None, {"t": now - 500, "px": 1.0}, None, 300.0)
    assert pk["perp_okx"]["stale_or_missing"] is True and pk["perp_okx"]["age_s"] == 500.0
    assert pk["context"]["stale_or_missing"] is True and pk["official_index_path"] is None and pk["candles_1m"] is None
    # candles that have not closed yet are excluded (only candles with open time <= now-60 count)
    cs = L.candle_summary(candles(t_end=int(now) + 600), now); assert cs is not None and cs["last_close_ts"] <= int(now) // 60 * 60 - 60


# ------------------------------------------------------------------ parser
@pytest.mark.parametrize("text,p,tag", [
    ('{"p_yes": 0.73, "rationale": "a. b.", "beyond_market": "none"}', 0.73, "json"),
    ('```json\n{"p_yes": "0.61", "rationale": "x"}\n```', 0.61, "json"),
    ('Sure. {"p_yes": 82, "rationale": "pct"}', 0.82, "json"),
    ('{"p_yes": 0.5, "rationale": "trailing",}', 0.5, "json"),
    ('p_yes = 0.44 because', 0.44, "regex_key"),
    ('I estimate the probability of YES at 0.91.', 0.91, "regex_prob"),
    ('Probability: 67%', 0.67, "regex_prob"),
    ('', None, "none"), ('no number here', None, "none"), ('{"p_yes": 1.7}', None, "none"),
])
def test_parse_response_robust(text, p, tag):
    r = L.parse_response(text)
    assert r["parse"] == tag
    if p is None: assert r["p_llm"] is None
    else: assert abs(r["p_llm"] - p) < 1e-9


def test_parse_keeps_rationale_and_beyond_market_fields():
    r = L.parse_response('{"p_yes": 0.2, "rationale": "Index below K with 5 min left.", "beyond_market": "used BTC co-move"}')
    assert r["rationale"].startswith("Index below") and r["beyond_market"] == "used BTC co-move"


# ------------------------------------------------------------------ budget
def test_budget_cap_persists_and_stops(tmp_path):
    b = L.Budget(tmp_path / "budget.json", cap=2); now = time.time()
    assert b.allow(now) == (True, "ok"); b.record({"prompt_tokens": 10, "completion_tokens": 5}); b.record(None)
    ok, why = b.allow(now); assert not ok and why == "daily_cap"
    b2 = L.Budget(tmp_path / "budget.json", cap=2); assert b2.state["stopped"] and b2.state["calls"] == 2 and b2.state["prompt_tokens"] == 10   # persisted
    b2.roll(now + 86400 * 2); assert not b2.state["stopped"] and b2.state["calls"] == 0                                                  # new Pacific day resets


def test_budget_stops_on_402_and_429_storm(tmp_path):
    b = L.Budget(tmp_path / "b.json", cap=100, storm=3); now = time.time()
    b.http_error(500); assert b.allow(now)[0]
    for _ in range(3): b.http_error(429)
    assert b.allow(now) == (False, "stopped:http_429_storm")
    b3 = L.Budget(tmp_path / "c.json", cap=100); b3.http_error(402); assert b3.allow(now)[1] == "stopped:http_402"


# ------------------------------------------------------------------ redaction / client
class _Transport:
    """httpx mock transport: records the request, returns a canned response (optionally raising)."""
    def __init__(self, responses): self.responses = list(responses); self.seen = []
    def __call__(self, request):
        import httpx
        self.seen.append(request); r = self.responses.pop(0)
        if isinstance(r, Exception): raise r
        status, body = r
        return httpx.Response(status, json=body)


def _client(responses, cfg_over=None):
    import httpx
    cfg = {**L.CFG, "retries": 2, "backoff_base_s": 0.0, **(cfg_over or {})}
    tr = _Transport(responses); return L.LLMClient(FAKE_KEY, "test/model", "https://example.invalid/v1", cfg, transport=httpx.MockTransport(tr)), tr


def test_key_never_reaches_logs_or_rows(tmp_path, monkeypatch):
    # the error text echoes the key (a hostile upstream) -> must be redacted everywhere we write
    client, tr = _client([(500, {"error": f"boom {FAKE_KEY} boom"}), (200, {"choices": [{"message": {"content": json.dumps({"p_yes": 0.7, "rationale": f"leak {FAKE_KEY}", "beyond_market": "none"})}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}, "model": "m"})])
    monkeypatch.setattr(L, "gather_context", lambda now, red, root, assets=("SOL", "BTC", "ETH"): ([], {}, None, None, []))
    budget = L.Budget(tmp_path / "budget.json", cap=10)
    row = L.forecast_once(client, budget, SNAP, 600.0, T - 600.0, tmp_path)
    assert row["p_llm"] == 0.7 and row["attempts"] == 2
    L._log(tmp_path, client.red, f"something {FAKE_KEY} {client.red.redact(FAKE_KEY)}")
    blob = "\n".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file())
    assert FAKE_KEY not in blob and FAKE_KEY[5:20] not in blob and "[REDACTED]" in blob
    assert tr.seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}"           # the key goes to the API header only
    assert json.loads(tr.seen[0].content)["max_tokens"] <= 400 and json.loads(tr.seen[0].content)["temperature"] == 0.0


def test_client_backoff_and_402_stop(tmp_path):
    client, tr = _client([(429, {"error": "slow"}), (200, {"choices": [{"message": {"content": '{"p_yes": 0.5}'}}], "usage": {}})])
    b = L.Budget(tmp_path / "b.json", cap=10, storm=5); res = client.complete([{"role": "user", "content": "x"}], b)
    assert res["ok"] and res["attempts"] == 2 and b.state["n429"] == 1
    client, tr = _client([(402, {"error": "pay"}), (200, {"choices": [{"message": {"content": '{"p_yes": 0.5}'}}]})])
    res = client.complete([{"role": "user", "content": "x"}], b); assert not res["ok"] and res["attempts"] == 1 and b.state["stopped"] and b.state["stop_reason"] == "http_402"
    row = L.forecast_once(client, b, SNAP, 300.0, time.time(), tmp_path)        # budget day is the Pacific day of `now`
    assert row["p_llm"] is None and "budget:stopped" in row["error"] and len(tr.seen) == 1   # no further API call once stopped


def test_exception_text_is_redacted():
    import httpx
    client, tr = _client([httpx.ConnectError(f"dns {FAKE_KEY}"), httpx.ConnectError("again")])
    res = client.complete([{"role": "user", "content": "x"}])
    assert not res["ok"] and FAKE_KEY not in res["error"] and "ConnectError" in res["error"]


# ------------------------------------------------------------------ scheduling
def test_due_decisions_fire_once_inside_tolerance_window():
    done = set(); ds = [600.0, 300.0]
    assert L.due_decisions(700.0, done, T, ds, 45.0) == []
    assert L.due_decisions(600.0, done, T, ds, 45.0) == [600.0]
    assert L.due_decisions(556.0, done, T, ds, 45.0) == [600.0]           # inside (555, 600]
    assert L.due_decisions(555.0, done, T, ds, 45.0) == []                # missed (too late) -> no stale firing
    done.add((T, 600.0)); assert L.due_decisions(590.0, done, T, ds, 45.0) == []
    assert L.due_decisions(299.0, done, T, ds, 45.0) == [300.0] and L.due_decisions(299.0, done, T + 900, ds, 45.0) == [300.0]


def test_done_set_rebuilt_from_log(tmp_path):
    p = tmp_path / "forecasts.jsonl"; p.write_text(json.dumps({"close_ts": T, "decision_secs_left": 600.0}) + "\nnot json\n" + json.dumps({"close_ts": T, "decision_secs_left": 300}) + "\n", encoding="utf-8")
    assert L.done_from_log(p) == {(T, 600.0), (T, 300.0)}


def test_latest_snapshot_reads_tail_only(tmp_path):
    p = tmp_path / "snapshots.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for i in range(2000): fh.write(json.dumps({**SNAP, "t": T - 900 + i * 0.4, "kind": "entry_eval" if i % 7 else "mark"}) + "\n")
    s = L.latest_snapshot(p, tail_bytes=5000); assert s is not None and s["kind"] == "entry_eval" and s["t"] > T - 900 + 1990 * 0.4


# ------------------------------------------------------------------ decision simulation through SOL-v1's own EV / fill path
def test_simulate_entry_uses_solv1_ev_and_fill():
    # a bullish LLM at .97 vs a .90 ask with sd_mean/z from the snapshot -> SOL-v1's entry_ev decides; abstain for p near mid
    neutral = L.simulate_entry(0.895, SNAP); assert neutral["decision"] == "abstain" and neutral["ev"]["yes"]["why"] != "ok"
    bull = L.simulate_entry(0.995, SNAP)
    assert bull["ev"]["yes"]["px_dw"] is not None
    if bull["decision"] == "enter":
        assert bull["side"] == "yes" and bull["qty"] >= S.CFG["min_lot"] and bull["px"] >= 0.9 and bull["fee"] > 0 and bull["cost"] > 0
    bear = L.simulate_entry(0.30, SNAP)                                    # NO at ~.11 ask (1-.89) with p_no .70 -> large edge
    assert bear["decision"] == "enter" and bear["side"] == "no" and bear["px"] is not None and 0.10 <= bear["px"] <= 0.13 and bear["qty"] >= S.CFG["min_lot"]
    assert L.simulate_entry(None, SNAP)["reasons"] == ["no_probability"] and L.simulate_entry(0.5, {**SNAP, "bids": [], "asks": []})["reasons"] == ["no_book"]


def test_policy_decision_capture():
    d = L.policy_decision_at({**SNAP, "decision": "enter", "side": "no", "qty": 9, "px_dw": 0.21, "limit": 0.211, "ev_pc": 0.06})
    assert d["decision"] == "enter" and d["side"] == "no" and d["qty"] == 9
    assert L.policy_decision_at({**SNAP, "snapshot_src": "rebuilt", "policy_holding": True})["decision"] == "holding"


def test_prompt_hash_is_stable_and_credentials_parser(tmp_path):
    assert len(L.PROMPT_HASH) == 16 and L.PROMPT_HASH == L.hashlib.sha256((L.VERSION + "\n" + L.SYSTEM_PROMPT).encode()).hexdigest()[:16]
    (tmp_path / ".env").write_text(f'OTHER=1\nexport NOUS_API_KEY="{FAKE_KEY}"\n', encoding="utf-8")
    (tmp_path / "config.yaml").write_text("model:\n  default: vendor/some-model\n  provider: nous\n  base_url: https://example.invalid/v1\n", encoding="utf-8")
    key, model, base = L.load_credentials(tmp_path); assert key == FAKE_KEY and model == "vendor/some-model" and base == "https://example.invalid/v1"
