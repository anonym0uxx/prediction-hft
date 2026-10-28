"""J1-v2 weather lock logic (src/predhft/weather_lock.py) — rules resolution, settlement-day filtering, lock table, exposure grouping,
population monitor, forward fills. All pure; no network."""
import datetime as dt, json, pathlib
import pytest
from predhft import weather_lock as WL
from predhft import weather_verify as WV
from predhft.fees import kalshi_taker_fee

RP = "If the {kind} temperature recorded at {city} (CLI{cli}) for {date}, is {phrase} fahrenheit according to The Weather Company, then the market resolves to Yes."
def mk(ticker="KXHIGHTMIN-26OCT27-T59", kind="maximum", city="Minneapolis", cli="MSP", date="Oct 27, 2026", phrase="less than 59°", strike_type="less", floor=None, cap=59, close="2026-10-28T06:00:00Z", rp=None):
    return {"ticker": ticker, "event_ticker": "-".join(ticker.split("-")[:2]), "rules_primary": rp if rp is not None else RP.format(kind=kind, city=city, cli=cli, date=date, phrase=phrase),
            "strike_type": strike_type, "floor_strike": floor, "cap_strike": cap, "close_time": close}
def U(y, m, d, h=0, mi=0): return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc).timestamp()

# ------------------------------------------------------------------------------------------------------------------ rules resolution
def test_resolve_rules_minneapolis_real_text():
    r = WL.resolve_rules(mk())
    assert (r.kind, r.cli, r.icao, r.tz, r.source, r.unit, r.strike_type, r.cap) == ("high", "MSP", "KMSP", "America/Chicago", "TWC", "fahrenheit", "less", 59)
    assert r.date == dt.date(2026, 10, 27) and r.station_day == "MSP:2026-10-27" and r.tradable
    # settlement day = local STANDARD time (CST, UTC-6) midnight-to-midnight even though Oct 27 is still CDT; Kalshi's close_time agrees
    assert (r.window_start, r.window_end) == (U(2026, 10, 27, 6), U(2026, 10, 28, 6)) and r.close_ts == r.window_end

def test_resolve_rules_flags_make_market_untradable():
    assert "date_mismatch" in WL.resolve_rules(mk(date="Oct 28, 2026")).flags[0]
    assert any("unknown_source" in f for f in WL.resolve_rules(mk(rp=RP.format(kind="maximum", city="X", cli="MSP", date="Oct 27, 2026", phrase="less than 59°").replace("The Weather Company", "AccuWeather"))).flags)
    assert any("unit" in f for f in WL.resolve_rules(mk(rp=RP.format(kind="maximum", city="X", cli="MSP", date="Oct 27, 2026", phrase="less than 15°").replace("fahrenheit", "celsius"))).flags)
    assert any("unknown_station" in f for f in WL.resolve_rules(mk(cli="ZZZ")).flags)
    assert WL.resolve_rules({"ticker": "X", "rules_primary": "no rules here"}) is None
    assert WL.resolve_rules(mk(kind="minimum", ticker="KXLOWTMIN-26OCT27-T38", strike_type="greater", floor=45, cap=None)).kind == "low"
    assert WL.resolve_rules(mk(city="New York City", cli="NYC")).icao == "KNYC"

# ------------------------------------------------------------------------------------------------------------------ settlement-day filtering
def test_window_dst_vs_standard_time_and_phoenix():
    # Chicago, during DST (Oct 27): CST day = 06Z..06Z ; after DST ends (Nov 5): still 06Z..06Z — the window never shifts with the clock
    assert WL.settlement_window(dt.date(2026, 10, 27), "America/Chicago") == (U(2026, 10, 27, 6), U(2026, 10, 28, 6))
    assert WL.settlement_window(dt.date(2026, 11, 5), "America/Chicago") == (U(2026, 11, 5, 6), U(2026, 11, 6, 6))
    assert WL.settlement_window(dt.date(2026, 7, 4), "America/New_York") == (U(2026, 7, 4, 5), U(2026, 7, 5, 5))
    assert WL.settlement_window(dt.date(2026, 10, 27), "America/Phoenix") == (U(2026, 10, 27, 7), U(2026, 10, 28, 7))
    assert WL.std_offset_hours("America/Los_Angeles", 2026) == -8.0

def test_midnight_obs_attribution_during_dst_is_explicit():
    """The old 3 wrong locks: Austin at 00:00 CDT Oct 27 (= 05:00Z) saw a 24-obs 'today' window. In LST the Oct 27 day starts 06:00Z,
    so at 05:30Z there are NO Oct 27 observations yet, and the 04:53Z/05:53Z obs belong to Oct 26."""
    w = WL.settlement_window(dt.date(2026, 10, 27), "America/Chicago")
    obs = [(U(2026, 10, 27, h, 53), 60 + h) for h in range(0, 24)] + [(U(2026, 10, 26, h, 53), 70 + h) for h in range(0, 24)]
    assert WL.filter_obs(obs, w, as_of=U(2026, 10, 27, 5, 30)) == []
    kept = WL.filter_obs(obs, w, as_of=U(2026, 10, 27, 6, 30)); assert len(kept) == 0   # 05:53Z is Oct 26 LST; 06:53Z not yet observed
    kept = WL.filter_obs(obs, w, as_of=U(2026, 10, 27, 7, 0)); assert [v for _, v in kept] == [66]
    full = WL.filter_obs(obs, w); assert len(full) == 18 and min(t for t, _ in full) >= w[0] and max(t for t, _ in full) < w[1]

def test_cross_day_window_contains_post_midnight_dst_hour():
    # 00:30 CDT Oct 28 (05:30Z) still belongs to the Oct 27 CST day: a low falling in that hour counts for Oct 27
    w = WL.settlement_window(dt.date(2026, 10, 27), "America/Chicago")
    assert WL.filter_obs([(U(2026, 10, 28, 5, 30), 40.0), (U(2026, 10, 28, 6, 0), 39.0)], w) == [(U(2026, 10, 28, 5, 30), 40.0)]

def test_running_extremes_and_metar_parse():
    ex = WL.running_extremes([(1, 50.2), (2, 61.7), (3, 55.0)])
    assert (ex["max"], ex["min"], ex["n"], ex["max2"], ex["min2"], ex["first_ts"], ex["last_ts"]) == (62, 50, 3, 55, 55, 1, 3)
    assert WL.running_extremes([])["max"] is None and WL.running_extremes([(1, 50.0)])["max2"] is None
    assert round(WL.parse_metar_temp_f("KMSP 272012Z 19007KT 6SM BR OVC009 13/12 A3002 RMK AO2 T01330122"), 2) == 55.94
    assert WL.parse_metar_temp_f("KMSP 270902Z 11012G19KT 10SM -RA SCT012 A3016 RMK AO2") is None   # missing temp group
    assert WL.parse_metar_temp_f("KDEN 010553Z 00000KT 10SM CLR M05/M08 A3030") == 23.0

# ------------------------------------------------------------------------------------------------------------------ lock table
@pytest.mark.parametrize("kind,st,floor,cap,value,slack,expect", [
    ("high", "greater", 73, None, 74, 0, 1.0), ("high", "greater", 73, None, 73, 0, None), ("high", "greater", 73, None, 74, 1, None), ("high", "greater", 73, None, 75, 1, 1.0),
    ("high", "less", None, 59, 59, 0, 0.0), ("high", "less", None, 59, 58, 0, None), ("high", "less", None, 59, 59, 1, None), ("high", "less", None, 59, 60, 1, 0.0),
    ("high", "between", 68, 69, 70, 0, 0.0), ("high", "between", 68, 69, 69, 0, None), ("high", "between", 68, 69, 68, 0, None), ("high", "between", 68, 69, 60, 0, None), ("high", "between", 68, 69, 70, 1, None),
    ("low", "greater", 45, None, 45, 0, 0.0), ("low", "greater", 45, None, 46, 0, None), ("low", "greater", 45, None, 45, 1, None), ("low", "greater", 45, None, 44, 1, 0.0),
    ("low", "less", None, 38, 37, 0, 1.0), ("low", "less", None, 38, 38, 0, None), ("low", "less", None, 38, 37, 1, None), ("low", "less", None, 38, 36, 1, 1.0),
    ("low", "between", 38, 39, 37, 0, 0.0), ("low", "between", 38, 39, 38, 0, None), ("low", "between", 38, 39, 39, 0, None), ("low", "between", 38, 39, 50, 0, None), ("low", "between", 38, 39, 37, 1, None),
    ("high", "less", None, 59, None, 0, None)])
def test_lock_table(kind, st, floor, cap, value, slack, expect):
    assert WL.lock(kind, st, floor, cap, value, slack) == expect

def test_running_extreme_never_locks_yes_for_bucket_or_wrong_direction():
    # a running max inside the bucket, or far above a 'less' cap from below, or a running min above a 'greater' floor: never YES
    for v in range(50, 70): assert WL.lock("high", "between", 68, 69, v) != 1.0
    for v in range(30, 59): assert WL.lock("high", "less", None, 59, v) is None
    for v in range(46, 80): assert WL.lock("low", "greater", 45, None, v) is None
    for v in range(38, 80): assert WL.lock("low", "less", None, 38, v) is None
    for v in range(38, 90): assert WL.lock("low", "between", 38, 39, v) != 1.0

def test_established_value_sources_and_slack():
    met = {"max": 56, "max2": 56, "min": 44, "min2": 44}
    assert WL.established_value("high", 61, met) == (None, 0, "source_disagree")      # Minneapolis 2026-10-27: CLI 61 vs METAR 56
    assert WL.established_value("high", 58, met) == (58, 0, "cli+metar")               # CLI 0-3 F above METAR: normal 5-min vs hourly gap, CLI figure used
    assert WL.established_value("high", 50, met) == (56, 1, "metar")                   # stale (earlier-issued) CLI below METAR: METAR path with rounding slack
    assert WL.established_value("high", None, met) == (56, 1, "metar")
    assert WL.established_value("high", None, {"max": 70, "max2": 60, "min": 44, "min2": 44}) == (None, 0, "metar_spike")
    assert WL.established_value("high", None, {"max": 70, "max2": None, "min": 70, "min2": None}) == (None, 0, "metar_single_obs")
    assert WL.established_value("high", 61, None) == (None, 0, "cli_only")
    assert WL.established_value("high", None, None) == (None, 0, "nodata")
    assert WL.established_value("low", 40, {"min": 44, "min2": 45}) == (None, 0, "source_disagree")
    assert WL.established_value("low", 43, {"min": 44, "min2": 45}) == (43, 0, "cli+metar")
    assert WL.established_value("low", 47, {"min": 44, "min2": 45}) == (44, 1, "metar")

def test_evaluate_minneapolis_case_is_not_a_lock():
    r = WL.resolve_rules(mk())
    ev = WL.evaluate(r, {"max": 61, "min": 44, "prelim": True}, {"max": 56, "max2": 56, "min": 44, "min2": 44, "n": 30}, 18.5)
    assert ev["locked"] is False and ev["basis"] == "source_disagree" and ev["p"] is None
    ev2 = WL.evaluate(r, {"max": 59, "min": 44, "prelim": True}, {"max": 57, "max2": 56, "min": 44, "min2": 44, "n": 30}, 18.5)
    assert ev2["locked"] and ev2["p"] == 0.0 and ev2["basis"] == "locked_cli+metar"
    ev3 = WL.evaluate(r, None, {"max": 57, "max2": 56, "min": 44, "min2": 44, "n": 10}, 15.0)  # metar 57 with slack 1 -> not >= 59
    assert not ev3["locked"] and ev3["basis"] in ("scored", "ambiguous")
    ev4 = WL.evaluate(r, None, {"max": 60, "max2": 59, "min": 44, "min2": 44, "n": 10}, 15.0)
    assert ev4["locked"] and ev4["p"] == 0.0 and ev4["basis"] == "locked_metar" and ev4["slack"] == 1

# ------------------------------------------------------------------------------------------------------------------ exposure grouping
def test_station_day_exposure_aggregates_across_series_and_sides():
    pos = [{"ticker": "KXHIGHTMIN-26OCT27-T59", "station_day": "MSP:2026-10-27", "side": "no", "cost": 5.0},
           {"ticker": "KXLOWTMIN-26OCT27-T38", "station_day": "MSP:2026-10-27", "side": "yes", "cost": 4.0},
           {"ticker": "KXHIGHCHI-26OCT27-T73", "station_day": "MDW:2026-10-27", "side": "no", "cost": 3.0},
           {"ticker": "KXHIGHTMIN-26OCT27-T59", "station_day": "MSP:2026-10-27", "side": "yes", "cost": 2.0}]   # opposite side on same ticker nets
    ex = WL.exposure_by_group(pos)
    assert ex == {"MSP:2026-10-27": 7.0, "MDW:2026-10-27": 3.0}
    assert WL.group_room(pos, "MSP:2026-10-27", 12.0) == 5.0 and WL.group_room(pos, "SEA:2026-10-27", 12.0) == 12.0 and WL.group_room(pos, "MSP:2026-10-27", 6.0) == 0.0

# ------------------------------------------------------------------------------------------------------------------ population monitor
def test_population_monitor_suspends_logic_cell_and_station(tmp_path):
    first = {"A": {"p": 0.0, "lock_src": "cli+metar", "kind": "high", "strike_type": "less", "cli": "MSP"},
             "B": {"p": 1.0, "lock_src": "metar", "kind": "low", "strike_type": "less", "cli": "AUS"},
             "C": {"p": 0.0, "lock_src": "metar", "kind": "high", "strike_type": "between", "cli": "SEA"}}
    wrong = WL.wrong_locks(first, {"A": "yes", "B": "yes", "C": "no", "D": "no"})
    assert [w["ticker"] for w in wrong] == ["A"]
    s = WL.Suspensions(tmp_path / "suspensions.json")
    assert s.suspended("cli+metar", "high", "less", "MSP") is None
    s.suspend("cli+metar", "high", "less", "MSP", "A", "detail", now=1.0)
    s2 = WL.Suspensions(tmp_path / "suspensions.json")                        # persisted and honoured by a fresh runner
    assert s2.suspended("cli+metar", "high", "less", "SEA") == "logic cli+metar|high|less"   # same logic cell, other station
    assert s2.suspended("metar", "low", "greater", "MSP") == "station MSP"                   # other logic, same station
    assert s2.suspended("metar", "low", "greater", "SEA") is None
    assert s2.d["logic"]["cli+metar|high|less"]["tickers"] == ["A"]

class _FakeK:
    def __init__(self, markets): self.m = markets; self.calls = 0
    def get(self, path): self.calls += 1; return {"market": self.m[path.split("/")[-1]]}

def test_runner_monitor_writes_suspension_alert_and_results(tmp_path, monkeypatch):
    paper = WV.Paper(tmp_path)
    (tmp_path / "first_lock_v2.json").write_text(json.dumps({
        "T1": {"t": 1, "p": 0.0, "lock_src": "cli+metar", "kind": "high", "strike_type": "less", "cli": "MSP", "station_day": "MSP:2026-10-27", "value": 61, "window_end": 100.0},
        "T2": {"t": 1, "p": 0.0, "lock_src": "metar", "kind": "high", "strike_type": "between", "cli": "SEA", "station_day": "SEA:2026-10-27", "value": 70, "window_end": 100.0},
        "T3": {"t": 1, "p": 0.0, "lock_src": "metar", "kind": "high", "strike_type": "less", "cli": "DEN", "station_day": "DEN:2026-10-27", "value": 70, "window_end": 100.0}}))
    k = _FakeK({"T1": {"result": "yes", "status": "finalized", "expiration_value": "56"}, "T2": {"result": "no", "status": "finalized"},
                "T3": {"result": "", "status": "closed", "expected_expiration_time": "1970-01-01T00:00:00Z"}})
    WV.monitor(k, paper, now=100.0 + WV.CFG["monitor_grace_s"] + 10)
    res = json.loads((tmp_path / "results_cache.json").read_text()); assert res == {"T1": "yes", "T2": "no"}
    s = WL.Suspensions(tmp_path / "suspensions.json")
    assert s.suspended("cli+metar", "high", "less", "SEA") and s.suspended("metar", "low", "greater", "MSP") and not s.suspended("metar", "high", "between", "SEA")
    kinds = sorted(json.loads(f.read_text())["kind"] for f in (tmp_path / "outbox").glob("*.json")); assert kinds == ["held_market", "wrong_lock"]
    # second pass: T1/T2 resolved (no refetch), T3 held alert not repeated
    k.calls = 0; WV.monitor(k, paper, now=100.0 + WV.CFG["monitor_grace_s"] + 10); assert k.calls == 1
    assert len(list((tmp_path / "outbox").glob("*.json"))) == 2

# ------------------------------------------------------------------------------------------------------------------ forward fills
def test_simulate_fill_uses_rechecked_book_and_orders_timestamps():
    book = {"yes_bid": 0.03, "yes_ask": 0.06, "yes_bid_size": 40.0, "yes_ask_size": 12.0}
    f = WL.simulate_fill("yes", book, 20, book_recv_t=101.0, signal_recv_t=100.0, fee_fn=kalshi_taker_fee)
    assert (f["price"], f["qty"], f["raw_offer_price"], f["raw_offer_size"], f["cost"]) == (0.07, 12, 0.06, 12.0, 12 * 0.07) and f["fee"] == kalshi_taker_fee(12, 0.07)
    assert f["recheck_latency_s"] == 1.0 and f["book_recv_t"] >= f["signal_recv_t"]
    n = WL.simulate_fill("no", book, 20, 101.0, 100.0, kalshi_taker_fee)
    assert (n["price"], n["qty"], n["raw_offer_price"]) == (0.98, 20, 0.97)
    assert WL.simulate_fill("yes", {"yes_bid": 0.5, "yes_ask": None, "yes_bid_size": 10.0, "yes_ask_size": 0.0}, 20, 101.0, 100.0, kalshi_taker_fee) is None   # no offer on that side
    assert WL.simulate_fill("yes", {"yes_bid": None, "yes_ask": 0.9, "yes_bid_size": 0.0, "yes_ask_size": 0.4}, 20, 101.0, 100.0, kalshi_taker_fee) is None   # < 1 contract displayed
    with pytest.raises(ValueError): WL.simulate_fill("yes", book, 20, book_recv_t=99.0, signal_recv_t=100.0, fee_fn=kalshi_taker_fee)   # look-ahead forbidden

# ------------------------------------------------------------------------------------------------------------------ CLI product parsing
def test_parse_cli_prelim_variants_and_final():
    prelim_a = "...THE TWIN CITIES MN CLIMATE SUMMARY FOR OCTOBER 27 2026...\nVALID TODAY AS OF 0700 PM LOCAL TIME.\n TODAY\n  MAXIMUM         61    341 AM  74\n  MINIMUM         44    454 AM  13\n"
    prelim_b = prelim_a.replace("VALID TODAY AS OF 0700 PM", "VALID AS OF 0400 PM")
    final = prelim_a.replace("VALID TODAY AS OF 0700 PM LOCAL TIME.\n", "").replace("TODAY", "YESTERDAY")
    for txt, pre in ((prelim_a, True), (prelim_b, True), (final, False)):
        d = WV.parse_cli(txt); assert (d["prelim"], d["max"], d["min"], d["for_date"]) == (pre, 61, 44, "2026-10-27")
    ps = [{"for_date": "2026-10-27", "issued": "2026-10-27T21:32:00+00:00", "max": 61}, {"for_date": "2026-10-27", "issued": "2026-10-28T06:34:00+00:00", "max": 61}, {"for_date": "2026-10-26", "issued": "2026-10-27T07:02:00+00:00", "max": 49}]
    assert WV.cli_for_date(ps, dt.date(2026, 10, 27))["issued"].startswith("2026-10-28") and WV.cli_for_date(ps, dt.date(2026, 10, 28)) is None
