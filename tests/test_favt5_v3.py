"""FavT5 v3: the reference scorer is reproduced exactly, and the shadow machinery around it cannot repeat or invent an order.
No network. Fixtures are copied unchanged from FavT5_v3_Stress_Test.zip (sha256 39e6d27c...)."""
import csv, hashlib, json, math, pathlib

import pytest

from predhft import favt5_v3_index as ref
from predhft import favt5_v3_shadow as sh

FX = pathlib.Path(__file__).parent / "fixtures" / "favt5_v3"
SNAP = json.loads((FX / "example_snapshot.json").read_text())


# ---- the frozen rule -------------------------------------------------------------------------------------------------
def test_scorer_file_is_the_archived_reference_byte_for_byte():
    assert hashlib.sha256(pathlib.Path(ref.__file__).read_bytes()).hexdigest() == "c2db8eb208e4fab44a3c2619d856095c90d91766496b48f577cb8f598950fad0"


def test_example_snapshot_reproduces_the_archived_intent():
    assert ref.signal(SNAP) == json.loads((FX / "example_intent.json").read_text())


def test_archived_order_export_is_reproduced_from_the_panel():
    """All 5,315 T-480 rows scored; exactly the 77 archived intents come out, same side, size, limit and cost."""
    orders = {r["ticker"]: r for r in csv.DictReader((FX / "tick_leader_orders.csv").open())}
    scored = made = 0
    for r in csv.DictReader((FX / "neighbor_h480.csv").open()):
        bid, ask, target = float(r["bid"]), float(r["ask"]), float(r["target"]); scored += 1
        p = .75 * ref.t5_cdf((float(r["lag_mean"]) - target) / math.hypot(float(r["lag_sigma"]), target * .000035)) + .25 * (bid + ask) / 2
        plan = ref.select(p, bid, ask)
        assert bool(plan) == (r["ticker"] in orders), r["ticker"]
        if plan:
            o = orders[r["ticker"]]; made += 1
            assert (plan["side"], plan["qty"], plan["limit"], plan["max_total_cost"]) == (o["side"], float(o["qty"]), float(o["limit"]), float(o["cost"]))
            best = max((x for x in sh.side_table(p, bid, ask) if x["qualifies"]), key=lambda x: x["modeled_net_dollars"])
            assert (best["side"], best["qty"], best["limit"]) == (plan["side"], plan["qty"], plan["limit"])       # the log table agrees with the scorer
        else:
            assert not any(x["qualifies"] for x in sh.side_table(p, bid, ask))
    assert (scored, made, len(orders)) == (5315, 77, 77)


@pytest.mark.parametrize("change, reason", [
    (lambda s: s["quote"].update(received=s["close"] - 480 + .5), "no quote received by the decision time"),
    (lambda s: s["quote"].update(event=s["close"] - 480 - 3.5), "quote older than 3 s"),
    (lambda s: s.update(quoted_strike=s["official_strike"] + 1), "strike difference over $0.02"),
    (lambda s: s.update(official_strike=float("nan")), "strike could not be confirmed from the received index"),
    (lambda s: s.update(strike_received=s["close"] - 479), "strike not published by the decision time"),
    (lambda s: s.update(brti=[]), "index or volatility history missing (3 s lag)"),
    (lambda s: s["quote"].update(bid=s["quote"]["bid"] - .07), "spread above 6 cents"),
])
def test_bad_inputs_are_rejected_and_the_reason_matches_the_reference(change, reason):
    s = json.loads(json.dumps(SNAP)); change(s)
    assert ref.signal(s) is None
    assert sh.diagnose(s)[0] == reason


def test_diagnosis_is_silent_exactly_when_the_reference_trades():
    why, info = sh.diagnose(SNAP)
    assert why is None and info["sides"][1]["qualifies"] and abs(info["fair_yes"] - (1 - 0.25358980671852693)) < 1e-12
    assert ref.signal(dict(SNAP, series="KXETH15M")) is None                    # the BTC rule never fires on another series


# ---- shadow machinery ------------------------------------------------------------------------------------------------
def book():
    b = sh.Book(); b.snapshot({"yes_dollars_fp": [["0.4000", "30.00"], ["0.3900", "100.00"]], "no_dollars_fp": [["0.5800", "12.50"], ["0.5700", "40.00"]]}, 100.0, 100.1)
    return b


def test_book_top_and_offers_come_from_the_opposite_side():
    b = book()
    assert b.top() == (0.40, 0.42)                                                # YES ask = 1 - best NO bid
    assert b.offers("YES") == [(0.42, 12.5), (0.43, 40.0)] and b.offers("NO")[0] == (0.60, 30.0)
    b.delta({"side": "no", "price_dollars": "0.5800", "delta_fp": "-12.50"}, 101.0, 101.1)
    assert b.top() == (0.40, 0.43) and (b.event, b.recv) == (101.0, 101.1)


def test_fill_takes_only_displayed_size_at_or_under_the_limit():
    f = sh.fill_against(book().offers("YES"), 23, 0.43)
    assert f["filled"] == 23 and f["cost"] == pytest.approx(12 * .42 + 11 * .43) and f["shown_at_limit"] == 52.5   # 12.5 shown -> 12 whole contracts
    assert sh.fill_against(book().offers("YES"), 23, 0.42)["filled"] == 12                                        # partial, the rest is not assumed
    assert sh.fill_against(book().offers("YES"), 23, 0.41)["filled"] == 0
    assert sh.fill_against([], 23, 0.43) == {"filled": 0, "shown_at_limit": 0.0, "cost": 0.0, "fee": 0.0, "avg_px": None}


def test_exchange_fee_rounds_each_fill_up_to_a_hundredth_of_a_cent():
    assert sh.exch_fee(77, 0.988) == 0.064 and sh.exch_fee(5, 0.974) == 0.0089     # both are real fills from the live ledger


def test_an_invalid_book_has_no_quote():
    b = book(); b.valid = False
    assert b.top() is None


def test_favourite_control_matches_the_archive_definition():
    f = sh.favourite(0.02, 0.03)                                                  # NO offered at 0.98
    assert f == {"side": "NO", "offer": 0.98, "limit": 0.981, "qty": 10, "max_total_cost": 9.83}
    assert sh.favourite(0.50, 0.51) is None and sh.favourite(0.90, 0.97) is None  # nothing at 0.965-0.990; spread too wide


def test_rebuilt_strike_needs_all_sixty_seconds():
    ticks = [{"event": float(s), "received": s + .1, "value": 100.0 + (s % 2)} for s in range(940, 1000)]
    assert sh.rebuilt_strike(ticks, 1000, 1500) == pytest.approx(100.5)
    assert math.isnan(sh.rebuilt_strike(ticks[:-1], 1000, 1500))
    assert math.isnan(sh.rebuilt_strike(ticks, 1000, 990))                         # values not yet received do not count


def test_one_decision_per_series_and_expiry_even_across_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "ROOT", tmp_path); calls = []
    monkeypatch.setattr(sh.Runner, "decide", lambda self, arm, tk, m, asset, D, t: (calls.append(arm), sh._write({"kind": "decision", "key": sh.key(arm, m["series"], m["close"]), "intent": None}))[0])
    r = sh.Runner(); r.mk["KXBTC15M-X"] = {"series": "KXBTC15M", "close": 10_000, "strike": 1.0, "strike_recv": 9_200.0}
    r.due(9_519.9); assert calls == []                                            # before T-480 nothing happens
    r.due(9_520.01); r.due(9_520.02); r.due(9_521.0); assert calls == ["btc_v3"]  # once, however often the clock ticks
    r.due(9_700.005); assert calls == ["btc_v3", "btc_fav_t5"]                    # the control arm has its own key at T-300
    again = sh.Runner(); again.mk = dict(r.mk); again.due(9_700.5)                # restart: keys reload from the ledger
    assert calls == ["btc_v3", "btc_fav_t5"] and sh.key("btc_v3", "KXBTC15M", 10_000) in again.keys


def test_a_boundary_missed_through_downtime_is_skipped_not_made_up(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "ROOT", tmp_path)
    monkeypatch.setattr(sh.Runner, "decide", lambda *a, **k: pytest.fail("a late decision must not be scored"))
    r = sh.Runner(); r.mk["KXBTC15M-X"] = {"series": "KXBTC15M", "close": 10_000, "strike": 1.0, "strike_recv": 9_200.0}
    r.due(9_522.0)                                                                # two seconds late
    rows = [json.loads(l) for l in (tmp_path / "fills.jsonl").open()]
    assert [x["skip"][:15] for x in rows] == ["missed boundary"] and rows[0]["intent"] is None


def test_shadow_has_no_order_route():
    src = pathlib.Path(sh.__file__).read_text(encoding="utf-8")
    assert "/portfolio" not in src and "method=\"POST\"" not in src and "create_order" not in src and "kalshi_auth import headers" in src
    assert src.count("headers(") == 1                                             # the only signed call is the websocket handshake
