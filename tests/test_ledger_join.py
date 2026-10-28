"""AUDIT_ADDENDUM_2026-10-28: joined decision/fill ledger (predhft.ledger_join) — join, portfolio replay, decomposition identity,
regime tags, null-with-limitation handling. Synthetic data roots only (no repo data read)."""
import datetime as dt, json, pathlib
import pytest
from predhft import ledger_join as LJ

CLOSE = int(dt.datetime(2026, 10, 28, 3, 15, tzinfo=LJ.PT).timestamp())   # 03:15 PT close (overnight block), post-freeze


def _w(p: pathlib.Path, rows):
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def make_root(tmp_path: pathlib.Path):
    fam = tmp_path / "s7p15-KXBTC15M"; t_fill = CLOSE - 56.0; t_skip = CLOSE - 40.0
    _w(fam / "trades.jsonl", [{"t": t_fill, "ticker": "KXBTC15M-T1", "close_ts": CLOSE, "side": "no", "price": 0.24, "qty": 9, "avail_at_touch": 30.0, "slippage": 0.01, "decision_latency_s": 0.45, "cost": 2.16, "fee": 0.12,
                                "p_model": 0.62, "p_market": 0.72, "spot": 100.0, "K": 100.01, "mins_left": 0.93, "status": "settled", "series": "KXBTC15M", "sigma1": 0.0004, "result": "yes", "pnl": -2.28, "spot_src": "cfb", "partial_n": 6}])
    _w(fam / "books.jsonl", [{"t": t_fill, "mins_left": 0.93, "ticker": "KXBTC15M-T1", "K": 100.01, "spot": 100.0, "spot_src": "cfb", "partial_n": 6, "sigma1": 0.0004, "p_model": 0.62, "yes_bid": 0.70, "yes_ask": 0.74, "bid_size": 1109.3, "ask_size": 497.0, "close_ts": CLOSE, "cfb_age_s": 0.4},
                             {"t": t_skip - 1.0, "mins_left": 0.68, "ticker": "KXBTC15M-T1", "K": 100.01, "spot": 100.0, "spot_src": "cfb", "partial_n": 20, "sigma1": 0.0006, "p_model": 0.55, "yes_bid": 0.60, "yes_ask": 0.66, "bid_size": 50.0, "ask_size": 40.0, "close_ts": CLOSE, "cfb_age_s": 0.5,
                              "yes_bid_present": True, "yes_ask_present": True, "bids": [[0.60, 50.0]], "asks": [[0.66, 40.0]]},
                             {"t": t_skip - 1.0, "ticker": "KXBTC15M-T1", "post_fill_check": True}])
    iso = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")
    (fam / "runner.log").write_text("\n".join([f"{iso(CLOSE - 3600)} start KXBTC15M/BTC-USD period=900s thr=0.05 window=T-1.0..T-0.1min lot=10 scored_only=False proj_gate=False",
                                                 f"{iso(t_fill)} PAPER BUY NO 9@0.24 KXBTC15M-T1 p_model=0.62 avail=30 T-0.9m lat=0.45s",
                                                 f"{iso(t_skip)} SKIP KXBTC15M-T1 yes below min lot (kelly=3 cap=9 mult=1.00 avail=40)",
                                                 f"{iso(t_skip + 2)} STALE KXBTC15M-T1 yes touch moved before fill (latency 0.61s)"]) + "\n", encoding="utf-8")
    day0 = int(t_fill // 86400) * 86400
    _w(tmp_path / "risk" / "events.jsonl", [{"kind": "settle", "ticker": "X1", "pnl": -26.0, "t": day0 + 10, "family": "other"},                      # halt crossed earlier this UTC day
                                           {"kind": "open", "ticker": "KXBTC15M-T1", "series": "KXBTC15M", "cost": 2.16, "close_ts": CLOSE, "t": t_fill + 0.01, "family": "s7p15-KXBTC15M"},
                                           {"kind": "open", "ticker": "OLD", "series": "KXHIGHX", "cost": 7.2, "close_ts": CLOSE - 86400, "t": CLOSE - 90000, "family": "s1-maker"},   # never settled: must drop after close+120
                                           {"kind": "settle", "ticker": "KXBTC15M-T1", "pnl": -2.28, "t": CLOSE + 120, "family": "s7p15-KXBTC15M"}])
    (tmp_path / "RESET.json").write_text(json.dumps({"reset_at": day0}))
    return tmp_path, {"KXBTC15M-T1": {"result": "yes", "expiration_value": "100.02", "floor_strike": 100.01}}


def test_build_rows_joins_fill_and_unfilled_decisions_with_config_snapshot_and_outcome(tmp_path):
    data, mk = make_root(tmp_path); rows = LJ.build_rows(data, mk)
    assert [r["row_kind"] for r in rows] == ["fill", "skip", "stale"] and all(set(r) == set(LJ.SCHEMA) for r in rows)
    f = rows[0]
    assert f["config_flags"]["proj_gate"] == "False" and f["version_tag"].startswith("P12") and f["era_pre_post_freeze"] == "post" and f["era_pre_post_suspension"] == "pre"
    assert f["quoted_touch"] == 0.23 and f["booked_price"] == 0.24 and f["execution_penalty"] == 0.09 and f["snap_age_s"] == 0.0 and f["snap_yes_ask"] == 0.74
    assert f["result"] == "yes" and f["proceeds"] == 0.0 and f["net_pnl"] == -2.28 and f["expiration_value"] == "100.02"
    assert f["snap_bids_top3"] is None and "top-3 depth" in f["limitation"]            # legacy snapshot: explicit null + limitation
    s = rows[1]
    assert s["qty"] == 0 and s["net_pnl"] == 0.0 and s["reject_reason"].startswith("skip: below min lot") and s["quoted_touch"] == 0.66 and s["avail_at_touch"] == 40.0 and s["snap_asks_top3"] == [[0.66, 40.0]]
    assert rows[2]["decision_latency_s"] == 0.61 and rows[2]["reject_reason"].startswith("stale")


def test_portfolio_state_replays_halt_open_cost_and_releases_unsettled_opens(tmp_path):
    data, mk = make_root(tmp_path); rows = LJ.build_rows(data, mk)
    f = rows[0]
    assert f["halt_state"] == "shadow_halt:global_daily" and f["portfolio_realized_utc_day"] == -26.0       # the fill was taken while shadow-halted
    assert f["portfolio_open_cost"] == 0.0 and f["portfolio_open_cost_s7"] == 0.0                           # s1-maker open past close+120 released; own open not yet booked
    assert rows[1]["portfolio_open_cost_s7"] == 2.16 and rows[1]["portfolio_open_cost"] == 2.16
    assert f["portfolio_equity"] == 250.0 - 26.0


def test_decomposition_identity_and_regime_tags(tmp_path):
    data, mk = make_root(tmp_path); rows = LJ.build_rows(data, mk); d = LJ.decomposition(rows)
    assert d == {"n": 1, "contracts": 9, "proceeds": 0.0, "quoted_cost": 2.07, "gross_vs_quoted": -2.07, "fees": 0.12, "execution_penalty": 0.09, "net": -2.28, "identity_residual": 0.0}
    assert rows[0]["regime_tod_block"] == "00-06 PT overnight" and rows[0]["regime_fomc_window"] is False
    assert LJ.fomc_window(dt.datetime(2026, 10, 28, 11, 0, tzinfo=LJ.PT).timestamp()) and not LJ.fomc_window(dt.datetime(2026, 10, 28, 9, 0, tzinfo=LJ.PT).timestamp())
    assert LJ.vol_tercile(0.0001, (0.0003, 0.0005)) == "low" and LJ.vol_tercile(0.0009, (0.0003, 0.0005)) == "high" and LJ.vol_tercile(None, (0.0003, 0.0005)) is None


def test_write_ledger_emits_jsonl_csv_and_schema(tmp_path):
    data, mk = make_root(tmp_path); rows = LJ.build_rows(data, mk); pj, pc = LJ.write_ledger(rows, tmp_path / "ledger")
    back = [json.loads(l) for l in pj.read_text(encoding="utf-8").splitlines()]
    assert len(back) == 3 and back[0]["row_id"] == "00000" and pc.read_text(encoding="utf-8").splitlines()[0].split(",") == LJ.SCHEMA
    schema = json.loads((tmp_path / "ledger" / "SCHEMA.json").read_text()); assert schema["fields"] == LJ.SCHEMA and "p13_suspension" in schema["boundaries_pt"]


def test_version_tag_boundaries():
    assert LJ.version_tag("s7p13-KXBTC15M", LJ.SUSPEND_TS) == "P16+P18+P20 | scored-only (suspended)"
    assert LJ.version_tag("s7p13-KXBTC15M", LJ.SUSPEND_TS - 1) == "P16+P18+P20 | official feed, gate on"
    assert LJ.version_tag("s7p15-KXETH15M", LJ.FREEZE_TS) == "P12 15M model-rule | official feed (FROZEN eval)"
    assert LJ.version_tag("s7p15-KXETH15M", LJ.FREEZE_TS - 1) == "P12 15M model-rule | replica, post-rebase"
