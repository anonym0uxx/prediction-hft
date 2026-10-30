"""P26 external feeds: parsing, staleness, packet section, prompt-variant isolation (docs/EXTERNAL_SIGNALS_2026-10-29.md). No network."""
from __future__ import annotations
import json, time
import pytest
from predhft import ext_feeds as X
from predhft.ext_feeds import polymarket as PM, hyperliquid as HL, binance_flow as BN, events as EV
from predhft import llm_shadow as L


def test_polymarket_slug_and_book_parse():
    assert PM.slug_for(1793333700) == "sol-updown-15m-1793332800"
    assert PM.slug_for(1793333700, 300) == "sol-updown-5m-1793333400"
    book = {"bids": [{"price": "0.10", "size": "50"}, {"price": "0.13", "size": "100"}, {"price": "0.12", "size": "20"}], "asks": [{"price": "0.20", "size": "5"}, {"price": "0.14", "size": "70"}], "timestamp": "1793333009515"}
    b = PM.parse_book(book)
    assert b["up_bid"] == 0.13 and b["up_ask"] == 0.14 and b["up_mid"] == 0.135 and b["spread_c"] == 1.0
    assert b["up_bid_qty_top3"] == 170.0 and b["up_ask_qty_top3"] == 75.0 and b["book_ts_ms"] == 1793333009515
    e = PM.parse_book({"bids": [], "asks": []}); assert e["up_mid"] is None and e["spread_c"] is None


def test_polymarket_market_parse_and_price_at():
    ev = {"slug": "sol-updown-15m-1", "closed": True, "markets": [{"conditionId": "0xc", "clobTokenIds": json.dumps(["UP", "DOWN"]), "outcomes": json.dumps(["Up", "Down"]), "eventStartTime": "s", "endDate": "e", "volume": "12.5", "outcomePrices": json.dumps(["1", "0"])}]}
    m = PM.parse_market(ev); assert m["up_token"] == "UP" and m["down_token"] == "DOWN" and m["volume_usd"] == 12.5 and m["closed"]
    ev["markets"][0]["outcomes"] = json.dumps(["Down", "Up"]); assert PM.parse_market(ev)["up_token"] == "DOWN"      # outcome order is not assumed
    assert PM.parse_market({"markets": []}) is None
    hist = [(100, 0.4), (160, 0.45), (220, 0.5)]
    assert PM.price_at(hist, 200) == (0.45, 40.0) and PM.price_at(hist, 99) == (None, None) and PM.price_at(hist, 1000) == (None, None)


def test_hyperliquid_ctx_parse():
    payload = [{"universe": [{"name": "BTC"}, {"name": "SOL"}]}, [{}, {"funding": "0.0000125", "openInterest": "1000", "prevDayPx": "1", "dayNtlVlm": "5e8", "premium": "-0.0005", "oraclePx": "100", "markPx": "99.95", "midPx": "99.96"}]]
    c = HL.parse_ctx(payload)
    assert c["funding_hourly"] == 1.25e-5 and c["funding_annualised_pct"] == pytest.approx(10.95) and c["premium_bp"] == -5.0 and c["oi_usd"] == 99950 and c["mark_px"] == 99.95
    with pytest.raises(ValueError): HL.parse_ctx(payload, "XYZ")


def test_binance_flow_stats_maker_flag():
    now_ms = 1_000_000_000
    tr = [{"p": "100", "q": "2", "T": now_ms - 10_000, "m": False},      # taker bought 200
          {"p": "100", "q": "1", "T": now_ms - 50_000, "m": True},       # taker sold 100
          {"p": "100", "q": "5", "T": now_ms - 200_000, "m": True}]      # outside 60 s, inside 300 s
    s = BN.flow_stats(tr, now_ms)
    assert s["imb_60s"] == pytest.approx((200 - 100) / 300, abs=1e-3) and s["cvd_60s_usd"] == 100 and s["n_trades_60s"] == 2
    assert s["imb_300s"] == pytest.approx((200 - 600) / 800, abs=1e-3) and s["n_trades_300s"] == 3 and s["oldest_trade_age_s"] == 200.0


def test_events_calendar_and_window_flags():
    items = [{"title": "FOMC Statement", "country": "USD", "impact": "High", "date": "2026-10-29T14:00:00-04:00"},
             {"title": "German ifo", "country": "EUR", "impact": "High", "date": "2026-10-29T05:00:00-04:00"},
             {"title": "Unemployment Claims", "country": "USD", "impact": "Medium", "date": "2026-10-29T08:30:00-04:00"}]
    cal = EV.parse_calendar(items); assert len(cal) == 1 and cal[0][1] == "FOMC Statement"
    fomc = cal[0][0]                                                         # 18:00Z
    f = EV.window_flags(cal, fomc - 600, close_ts=int(fomc) + 300)           # window 17:50-18:05Z contains the release
    assert f["high_usd_in_window"] and not f["high_usd_within_30m_before"] and f["next_high_usd_in_min"] == 10.0
    f2 = EV.window_flags(cal, fomc + 1200, close_ts=int(fomc) + 1800)        # window 18:15-18:30: release 15 min before it opened
    assert not f2["high_usd_in_window"] and f2["high_usd_within_30m_before"] and f2["next_high_usd_title"] is None
    assert EV.status_indicator({"status": {"indicator": "minor", "description": "Partially Degraded Service"}}) == "minor:Partially Degraded Service"
    assert EV.status_indicator({}) is None


def test_packet_block_ages_and_stale(tmp_path):
    now = time.time()
    X.write_row("polymarket", {"t": now - 5, "close_ts": 1793333700, "slug": "sol-updown-15m-1793332800", "up_mid": 0.135, "up_bid": 0.13, "up_ask": 0.14, "secret_field": 1}, root=tmp_path)
    X.write_row("hyperliquid", {"t": now - 500, "funding_hourly": 1e-5, "premium_bp": -5.0}, root=tmp_path)
    X.write_row("events", {"t": now - 100, "high_usd_in_window": False, "kraken_status": "none:ok"}, root=tmp_path)
    blk = X.packet_block(now, close_ts=1793333700, root=tmp_path)
    pm = blk["polymarket_sol15m"]; assert pm["up_mid"] == 0.135 and pm["stale"] is False and 4.5 <= pm["age_s"] <= 6 and pm["matched_window"] is True and "secret_field" not in pm
    assert X.packet_block(now, close_ts=1793334600, root=tmp_path)["polymarket_sol15m"]["matched_window"] is False
    hl = blk["hyperliquid_sol_perp"]; assert hl["stale"] is True and hl["funding_hourly"] == 1e-5
    bn = blk["binance_spot_flow"]; assert bn["stale"] is True and bn["age_s"] is None
    assert blk["events"]["stale"] is False
    # jsonl append + latest + tail_rows
    X.write_row("polymarket", {"t": now, "up_mid": 0.2}, root=tmp_path)
    assert X.latest("polymarket", tmp_path)["up_mid"] == 0.2 and len(X.tail_rows("polymarket", now - 10, tmp_path)) == 2 and X.tail_rows("polymarket", now - 1, tmp_path)[0]["up_mid"] == 0.2
    assert json.dumps(blk)  # serialisable


def test_v2c_is_v2b_plus_one_line_and_hashes_are_isolated():
    assert L.prompt_hash_for("v2b") == "55bd678581f7293b" and L.prompt_hash_for("v2a") == "d1a3852ffee0b21a"      # P25 cohorts untouched
    assert L.prompt_hash_for("v2c") == "ee484a634efe25a7"
    assert L.SYSTEM_PROMPT_V2C.replace(L._V2C_EXT_LINE, "", 1) == L.SYSTEM_PROMPT_V2B                                # the only textual difference
    assert L._V2C_EXT_LINE.count("\n") == 1 and "external_signals" in L._V2C_EXT_LINE


def _snap():
    return {"ticker": "KXSOL15M-T", "t": 1793333099.0, "close_ts": 1793333700, "K": 100.0, "x_now": 100.2, "sigma_1s": 2e-4, "V": 300.0, "n_obs": 0, "m_rem": 60, "partial": None, "mid": 0.6, "yes_bid": 0.59, "yes_ask": 0.61, "bids": [[0.59, 100]], "asks": [[0.61, 100]], "p_model": 0.62}


def test_external_section_only_in_v2c_packet():
    ext = {"version": "x", "polymarket_sol15m": {"up_mid": 0.5, "stale": False, "age_s": 3}}
    now = 1793333100.0
    for v in ("v2a", "v2b"):
        pk = L.build_packet(_snap(), now, v, external=ext, decision_secs_left=600.0); assert "external_signals" not in pk
    pk = L.build_packet(_snap(), now, "v2c", external=ext, decision_secs_left=600.0)
    assert pk["external_signals"] == ext and "other_forecasters" not in pk
    body = L.messages_for(pk, "v2c")[1]["content"]; assert '"external_signals"' in body and body.index('"executable_now"') < body.index('"external_signals"')
    assert L.messages_for(pk, "v2c")[0]["content"] == L.SYSTEM_PROMPT_V2C
