"""FavT5 v3 coverage manifest: what was actually recorded for every BTC/ETH 15-minute window in the old live span.

Inputs (all read-only): cov_fetch.py outputs, Hermes' own 1 Hz recordings of Kalshi's cfbenchmarks_value channel
(D:/prediction-hft/data/cfb/<ASSET>.jsonl: source time, Kalshi receive time, LOCAL receive time), the v1 live/paper
ledgers, the v2 paper ledger (its T-8 'look' rows are the only locally received Kalshi quotes at T-480), and the
exchange's fills for the 68 live orders.
Outputs: reports/favt5_v3/coverage_manifest.json, reports/favt5_v3/coverage_windows.csv
Times are UTC epoch seconds unless a column says otherwise. Exchange spot proxies are NOT used anywhere here.
"""
from __future__ import annotations
import calendar, csv, glob, gzip, json, os, pathlib, statistics, sys, time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
COV = REPO / "data" / "favt5-v3" / "coverage"
REP = REPO / "reports" / "favt5_v3"
CFB = pathlib.Path("D:/prediction-hft/data/cfb")
ASSET = {"KXBTC15M": "BTC", "KXETH15M": "ETH"}
LEDGERS = {"v1_live": "D:/prediction-hft/data/fav-t5-live/fills.jsonl", "v1_paper": "D:/prediction-hft/data/fav-t5/fills.jsonl",
           "v2_paper": "D:/prediction-hft-v2/data/fav-t5-v2/fills.jsonl"}
ts = lambda s: calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
utc = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def jl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def local_index(asset: str, t0: float, t1: float) -> dict[int, tuple]:
    """{source second: (value, local receive epoch, Kalshi receive ms)} from Hermes' own recording, bisecting the file by time."""
    p = CFB / f"{asset}.jsonl"; size = p.stat().st_size; out = {}
    with open(p, "rb") as f:
        lo, hi = 0, size
        while hi - lo > 1 << 20:
            mid = (lo + hi) // 2; f.seek(mid); f.readline(); line = f.readline()
            try: t = json.loads(line)["t"]
            except Exception: t = t1
            if t < t0: lo = mid
            else: hi = mid
        f.seek(lo); f.readline()
        for line in f:
            try: d = json.loads(line)
            except Exception: continue
            if d["t"] > t1: break
            if d.get("value") is None or not d.get("value_ts_ms"): continue
            out.setdefault(int(d["value_ts_ms"]) // 1000, (d["value"], d["t"], d.get("received_at_ms"), int(d["value_ts_ms"])))
    return out


def cf_history(idx: str) -> tuple[dict[int, float], dict[int, int]]:
    """Official history via Kalshi passthrough: {second: value at the exact second tick}, {second: ticks in that second}."""
    exact, n = {}, {}
    for fp in sorted(glob.glob(str(COV / "cf_history" / idx / "*.json.gz"))):
        for x in json.load(gzip.open(fp, "rt", encoding="utf-8"))["payload"]:
            s, ms = divmod(int(x["time"]), 1000); n[s] = n.get(s, 0) + 1
            if ms == 0: exact[s] = float(x["value"])
    return exact, n


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def main() -> None:
    W = json.loads((COV / "windows.json").read_text()); mk = W["markets"]; s0, s1 = W["span"]
    candles = json.loads((COV / "candles.json").read_text())["candles"]
    led = {k: jl(v) for k, v in LEDGERS.items()}
    live_entry = {r["ticker"]: r for r in led["v1_live"] if r.get("kind") == "entry" and (r.get("qty") or 0) > 0}
    live_dec = {r["ticker"]: r for r in led["v1_live"] if r.get("kind") == "entry"}
    paper_dec = {r["ticker"]: r for r in led["v1_paper"] if r.get("kind") == "entry"}
    v2_t480 = {r["ticker"]: r for r in led["v2_paper"] if r.get("kind") == "look" and abs((r.get("secs_left") or 0) - 480) < 3}
    exch = json.loads((COV / "exchange_fills_settlements.json").read_text())
    first_fill = {}
    for f in exch["fills"]:
        t = ts(f["created_time"]) + float("0." + f["created_time"].split(".")[1].rstrip("Z")) if "." in f["created_time"] else ts(f["created_time"])
        first_fill[f["order_id"]] = min(t, first_fill.get(f["order_id"], 9e18))
    idx_local, idx_hist, idx_n = {}, {}, {}
    for ser, a in ASSET.items():
        idx_local[ser] = local_index(a, s0 - 3 * 3600, s1 + 120)
        idx_hist[ser], idx_n[ser] = cf_history(mk[next(t for t in mk if mk[t]["series"] == ser)]["index_id"])
        print(ser, "local seconds", len(idx_local[ser]), "history exact-second ticks", len(idx_hist[ser]), flush=True)
    rows = []
    for tk, m in sorted(mk.items(), key=lambda kv: (kv[1]["close_time"], kv[0])):
        ser = m["series"]; o, c = ts(m["open_time"]), ts(m["close_time"]); L, H, N = idx_local[ser], idx_hist[ser], idx_n[ser]
        secs = range(o - 60, c)                                    # strike window + whole market life
        miss_l = [s for s in secs if s not in L]; miss_h = [s for s in secs if s not in H]
        gap = lambda miss: max((b - a for a, b in zip([secs[0] - 1] + [s for s in secs if s not in miss], [s for s in secs if s not in miss] + [c])), default=0) - 1 if len(miss) < len(secs) else len(secs)
        lag = [(L[s][1] * 1000 - L[s][3]) for s in secs if s in L]                       # source -> local receipt (includes local clock error)
        klag = [(L[s][2] - L[s][3]) for s in secs if s in L and L[s][2]]                 # source -> Kalshi receipt
        mism = sum(1 for s in secs if s in L and s in H and abs(L[s][0] - H[s]) > 0.005)
        def avg(src, a, b):
            v = [src[s] if src is H else src[s][0] for s in range(a, b) if s in src]
            return (sum(v) / len(v), len(v)) if v else (None, 0)
        ev = float(m["expiration_value"]) if m.get("expiration_value") else None; K = m.get("floor_strike")
        sH, nH = avg(H, c - 60, c); sH2, _ = avg(H, c - 59, c + 1); kH, nKH = avg(H, o - 60, o); sL, nL = avg(L, c - 60, c); kL, nKL = avg(L, o - 60, o)
        d = c - 480                                                 # v3 decision instant
        seen = [s for s in range(d - 30, d + 1) if s in L and L[s][1] <= d]
        vol_n = sum(1 for s in range(d - 900, d) if s in L and L[s][1] <= d)
        cd = candles.get(tk, []); q8 = next((x for x in cd if x.get("end_period_ts") == d), None)
        v2 = v2_t480.get(tk); le = live_entry.get(tk); ld = live_dec.get(tk); pdx = paper_dec.get(tk)
        trf = COV / "trades" / f"{tk}.json.gz"
        ntr = len(json.load(gzip.open(trf, "rt", encoding="utf-8"))["trades"]) if trf.exists() else None
        rows.append({
            "ticker": tk, "series": ser, "index_id": m["index_id"], "open_utc": utc(o), "close_utc": utc(c), "decision_t480_utc": utc(d),
            "official_strike": K, "official_expiration_value": ev, "official_result": m["result"],
            "old_live_ticker": bool(le), "v1_live_decision": ("traded" if le else (ld or {}).get("reason", "no decision row (arm down or not started)")),
            "v1_paper_decision": ("traded" if pdx and (pdx.get("qty") or 0) > 0 else (pdx or {}).get("reason", "no decision row")),
            "idx_local_secs_present": len(secs) - len(miss_l), "idx_local_secs_expected": len(secs), "idx_local_max_gap_s": gap(miss_l),
            "idx_hist_secs_present": len(secs) - len(miss_h), "idx_hist_max_gap_s": gap(miss_h), "idx_hist_ticks": sum(N.get(s, 0) for s in secs),
            "idx_local_vs_hist_value_mismatch_secs": mism,
            "local_recv_lag_ms_median": round(statistics.median(lag)) if lag else None, "local_recv_lag_ms_p99": round(pct(lag, 0.99)) if lag else None, "local_recv_lag_ms_max": round(max(lag)) if lag else None,
            "kalshi_recv_lag_ms_median": round(statistics.median(klag)) if klag else None,
            "settle_avg_hist": round(sH, 4) if sH else None, "settle_avg_hist_n": nH, "settle_minus_official": round(sH - ev, 4) if sH and ev else None,
            "settle_alt_convention_minus_official": round(sH2 - ev, 4) if sH2 and ev else None,
            "settle_avg_local": round(sL, 4) if sL else None, "settle_avg_local_n": nL, "settle_local_minus_official": round(sL - ev, 4) if sL and ev else None,
            "strike_avg_hist": round(kH, 4) if kH else None, "strike_hist_minus_official": round(kH - K, 4) if kH and K else None,
            "strike_avg_local": round(kL, 4) if kL else None, "strike_avg_local_n": nKL, "strike_local_minus_official": round(kL - K, 4) if kL and K else None,
            "t480_idx_local_age_s": round(d - max(seen), 3) if seen else None, "t480_idx_last_local_recv_delay_ms": round((L[max(seen)][1] - max(seen)) * 1000) if seen else None,
            "t480_vol_history_local_secs_of_900": vol_n,
            "t480_quote_local": bool(v2), "t480_quote_local_recv_utc": utc(v2["t"]) if v2 else None, "t480_quote_local_bid": v2["bid"] if v2 else None, "t480_quote_local_ask": v2["ask"] if v2 else None,
            "t480_quote_local_side": v2["side"] if v2 else None, "t480_quote_local_age": "REST snapshot, age not measured" if v2 else None,
            "t480_candle_yes_bid": (q8 or {}).get("yes_bid", {}).get("close_dollars"), "t480_candle_yes_ask": (q8 or {}).get("yes_ask", {}).get("close_dollars"),
            "candle_minutes": len(cd), "trades": ntr, "depth_deltas_recorded": False,
            "live_order_id": (le or {}).get("order_id"), "live_decision_boundary_utc": utc(c - 300) if le else None,
            "live_exchange_first_fill_after_boundary_ms": round((first_fill[le["order_id"]] - (c - 300)) * 1000) if le and le.get("order_id") in first_fill else None,
            "live_local_ledger_row_after_boundary_ms": round((le["t"] - (c - 300)) * 1000) if le else None,
        })
    REP.mkdir(parents=True, exist_ok=True)
    with open(REP / "coverage_windows.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    # ---- summary
    exp_closes = list(range((s0 // 900 + 1) * 900, s1 + 1, 900)); have = {(r["series"], r["close_utc"]) for r in rows}
    no_market = sorted({utc(c) for c in exp_closes for s in ASSET if (s, utc(c)) not in have})
    S = {"generated_utc": utc(time.time()), "span_utc": [utc(s0), utc(s1)], "windows_expected_per_series": len(exp_closes), "windows_listed_by_exchange": len(rows),
         "closes_with_no_market_listed": no_market, "old_live_tickers": sum(r["old_live_ticker"] for r in rows), "by_series": {}}
    for ser in ASSET:
        R = [r for r in rows if r["series"] == ser]; full = lambda k: sum(1 for r in R if r[k] == r["idx_local_secs_expected"])
        holes = [(r["ticker"], r["idx_local_secs_expected"] - r["idx_local_secs_present"], r["idx_local_max_gap_s"]) for r in R if r["idx_local_secs_present"] < r["idx_local_secs_expected"]]
        dS = [abs(r["settle_minus_official"]) for r in R if r["settle_minus_official"] is not None]; dK = [abs(r["strike_hist_minus_official"]) for r in R if r["strike_hist_minus_official"] is not None]
        dS2 = [abs(r["settle_alt_convention_minus_official"]) for r in R if r["settle_alt_convention_minus_official"] is not None]
        dSL = [abs(r["settle_local_minus_official"]) for r in R if r["settle_local_minus_official"] is not None and r["settle_avg_local_n"] == 60]
        lat = [r["live_exchange_first_fill_after_boundary_ms"] for r in R if r["live_exchange_first_fill_after_boundary_ms"] is not None]
        S["by_series"][ser] = {
            "index_id": R[0]["index_id"], "windows": len(R), "old_live_tickers": sum(r["old_live_ticker"] for r in R),
            "index_local_recording": {"windows_complete": full("idx_local_secs_present"), "windows_with_holes": len(holes), "seconds_missing_total": sum(h[1] for h in holes),
                                      "largest_holes": sorted(holes, key=lambda h: -h[1])[:8],
                                      "old_live_tickers_complete": sum(1 for r in R if r["old_live_ticker"] and r["idx_local_secs_present"] == r["idx_local_secs_expected"])},
            "index_official_history_backfill": {"windows_complete": full("idx_hist_secs_present"), "windows_with_holes": sum(1 for r in R if r["idx_hist_secs_present"] < r["idx_local_secs_expected"]),
                                                "note": "source timestamps only; no local receive time exists for backfilled ticks"},
            "local_vs_history_value_mismatch_seconds": sum(r["idx_local_vs_hist_value_mismatch_secs"] for r in R),
            "index_reconciliation": {"settlement_avg_[close-60,close)_vs_official_abs_diff": {"median": statistics.median(dS), "max": max(dS), "within_1c": sum(1 for x in dS if x <= 0.0101), "n": len(dS)},
                                     "settlement_alt_(close-59..close)_abs_diff_median": statistics.median(dS2),
                                     "strike_avg_[open-60,open)_vs_official_abs_diff": {"median": statistics.median(dK), "max": max(dK), "within_2c": sum(1 for x in dK if x <= 0.0201), "n": len(dK)},
                                     "settlement_from_local_recording_abs_diff": {"median": statistics.median(dSL) if dSL else None, "max": max(dSL) if dSL else None, "n": len(dSL)}},
            "local_receive_lag_ms_source_to_this_machine": {"median_of_window_medians": statistics.median([r["local_recv_lag_ms_median"] for r in R if r["local_recv_lag_ms_median"] is not None]),
                                                            "worst_window_p99": max(r["local_recv_lag_ms_p99"] for r in R if r["local_recv_lag_ms_p99"] is not None),
                                                            "worst_single_tick": max(r["local_recv_lag_ms_max"] for r in R if r["local_recv_lag_ms_max"] is not None)},
            "t480_inputs": {"index_obs_received_within_3s": sum(1 for r in R if r["t480_idx_local_age_s"] is not None and r["t480_idx_local_age_s"] <= 3),
                            "full_900s_vol_history_received": sum(1 for r in R if r["t480_vol_history_local_secs_of_900"] >= 895),
                            "locally_received_kalshi_quote": sum(r["t480_quote_local"] for r in R), "exchange_time_candle_quote": sum(1 for r in R if r["t480_candle_yes_bid"] is not None)},
            "kalshi_quotes": {"one_minute_candles_windows": sum(1 for r in R if r["candle_minutes"] >= 15), "trade_prints_windows_fetched": sum(1 for r in R if r["trades"] is not None),
                              "trade_prints_total": sum(r["trades"] or 0 for r in R), "full_depth_or_deltas_windows": 0},
            "live_order_arrival_ms_after_decision_boundary": {"n": len(lat), "median": statistics.median(lat) if lat else None, "p90": pct(lat, 0.9), "max": max(lat) if lat else None, "min": min(lat) if lat else None},
        }
    (REP / "coverage_manifest.json").write_text(json.dumps(S, indent=1))
    print(json.dumps(S, indent=1))


if __name__ == "__main__":
    main()
