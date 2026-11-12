"""FavT5 v3 offline comparisons. Research only; reads files, writes reports/favt5_v3/offline_comparisons.json.

A. Archive check: recompute the frozen v3 rule and the same-budget favourite from the archive's tick panel with our own
   code and compare with the archive's published numbers (so the comparison below is not taken on trust).
B. The owner's three comparisons on the archive (the only sample large enough to say anything):
   1 same-budget BTC T-5 favourite at $0.965-$0.990, paired by day; 2 entry-band attribution of the same v3 intents;
   3 ETH: the archive has no ETH native-index ticks, so ETH can only be scored on our own span (part C).
C. Old live span (2026-11-09..12): the same scorer on our own recordings, BTC and ETH native index. Quotes here are the
   exchange's one-minute candle close at the decision second (exchange time, NO local receipt time), and a fill is
   accepted only when a public print shows our side trading at or under the limit within 5 s of the replay arrival.
   Three days is far too short to rank anything; it is a plumbing check on real data.
Frozen replay accounting throughout: full size at the limit, cash rounded up to a whole cent, hold to settlement."""
from __future__ import annotations
import bisect, calendar, gzip, json, math, pathlib, sys, time
import numpy as np, pandas as pd

W = pathlib.Path(__file__).resolve().parents[2]; sys.path.insert(0, str(W / "src"))
from predhft import favt5_v3_index as ref, favt5_v3_shadow as sh
REF = W / "data/favt5-v3/reference/deep"; COV = W / "data/favt5-v3/coverage"; OUT = W / "reports/favt5_v3"
V3 = "tick|480|index_lag|0.15|0.6|0.05"; FAV = "tick|300|favorite|0.965|0.99|None"


def total(p, n): return math.ceil((p + .07 * p * (1 - p)) * n * 100 - 1e-8) / 100
def qty_at(p): return math.floor(10 / (p + .07 * p * (1 - p)) + 1e-10)


def plans(g: pd.DataFrame, prob, lo: float, hi: float, edge: float | None) -> pd.DataFrame:
    """Port of the archive's stress.evaluate for the tick panel: one plan per expiry, then 1 s / 2 s / 5 s fills."""
    rows = []
    for r, p in zip(g.itertuples(index=False), prob):
        best = None
        for yes in (True, False):
            price = round(r.ask if yes else 1 - r.bid, 4); limit = sh.bump(price); q = qty_at(limit); cost = total(limit, q); fair = p if yes else 1 - p; score = q * fair - cost
            ok = lo <= price <= hi and r.ask - r.bid <= .06 and limit < 1 and q > 0 and cost <= 10 and math.isfinite(score) and (edge is None or score >= edge * q)
            rank = (score if edge is not None else price) if ok else -math.inf
            if best is None or rank > best[0]: best = (rank, yes, price, limit, q, cost)
        rank, yes, price, limit, q, cost = best
        if rank == -math.inf: continue
        won = bool(r.yes) == yes; o = {"day": r.day, "ticker": r.ticker, "close_ts": r.end, "side": "YES" if yes else "NO", "offer": price, "limit": limit, "qty": q, "cost": cost, "won": won, "vol_bps": r.main_vbps}
        for name, d in (("pnl", 1), ("stress", 2), ("late", 5)):
            b, a = getattr(r, f"bid{d}"), getattr(r, f"ask{d}"); offer = a if yes else 1 - b
            valid = bool(b >= 0 and a <= 1 and a >= b and 0 < offer < 1 and offer <= limit + 1e-9)
            px = limit if name != "stress" else min(sh.bump(limit), .999)
            o[name] = (qty_at(px) * won - total(px, qty_at(px))) if valid else 0.0
            if name == "pnl": o["filled"] = valid
        rows.append(o)
    return pd.DataFrame(rows)


def stats(l: pd.DataFrame, days: list[str]) -> dict:
    part = l[l.day.isin(days)]; f = part[part.filled]; daily = part.groupby("day").pnl.sum().reindex(days, fill_value=0.0)
    cum = np.r_[0, part.sort_values("close_ts").pnl.cumsum()]; wins = f[f.pnl > 0].pnl; losses = f[f.pnl < 0].pnl
    return {"intents": len(part), "fills": len(f), "fill_rate": round(len(f) / len(part), 4) if len(part) else None, "days": len(days), "active_days": int(f.day.nunique()),
            "net": round(float(daily.sum()), 2), "net_per_fill": round(float(f.pnl.mean()), 3) if len(f) else None, "net_per_day": round(float(daily.mean()), 3),
            "daily_sharpe_fixed_capital": round(float(daily.mean() / daily.std()), 4) if daily.std() > 0 else 0.0,
            "wins": len(wins), "avg_win": round(float(wins.mean()), 2) if len(wins) else None, "losses": len(losses), "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
            "max_drawdown": round(float((np.maximum.accumulate(cum) - cum).max()), 2), "net_without_top_two_winners": round(float(daily.sum() - f.pnl.nlargest(2).sum()), 2),
            "net_2s_stress": round(float(part.stress.sum()), 2), "net_5s_late": round(float(part.late.sum()), 2)}


def block_bootstrap(x: np.ndarray, mean_block: float = 3, reps: int = 40000, seed: int = 77) -> tuple[float, float]:
    """Stationary bootstrap of the daily mean (geometric blocks, wrap-around)."""
    rng = np.random.default_rng(seed); n = len(x); idx = np.empty((reps, n), dtype=np.int64); idx[:, 0] = rng.integers(0, n, reps)
    for j in range(1, n):
        new = rng.random(reps) < 1 / mean_block
        idx[:, j] = np.where(new, rng.integers(0, n, reps), (idx[:, j - 1] + 1) % n)
    m = x[idx].mean(1); return float(np.quantile(m, .025)), float(np.quantile(m, .975))


def archive() -> dict:
    t = pd.read_parquet(REF / "tick_panel.parquet"); t = t[t.main_mean.notna() & (abs(t.target - t.official_target) <= .02)].copy()
    sel = json.loads((REF / "results/tick_selection.json").read_text()); dev, conf = sel["development_days"], sel["confirmation_days"]
    g = t[t.h == 480].sort_values("end"); p = [.25 * (r.ask + r.bid) / 2 + .75 * ref.t5_cdf((r.lag_mean - r.target) / math.hypot(r.lag_sigma, r.target * .000035)) if math.isfinite(r.lag_mean) else math.nan for r in g.itertuples()]
    v3 = plans(g, p, .15, .60, .05); f = t[t.h == 300].sort_values("end"); fav = plans(f, (f.ask + f.bid) / 2, .965, .99, None)
    out = {"source": "FavT5_v3_Stress_Test.zip deep/tick_panel.parquet (archived BTC quotes + BRTI, 2026-06-22..2026-10-17)", "development_days": len(dev), "confirmation_days": len(conf)}
    for name, l in (("v3", v3), ("favourite_t5", fav)): out[name] = {"development": stats(l, dev), "confirmation": stats(l, conf)}
    pub = {i: {ph: pd.read_csv(REF / f"results/tick_{ph}.csv").set_index("id").loc[i] for ph in ("development", "confirmation")} for i in (V3, FAV)}
    out["matches_archive"] = {n: {ph: {"ours": out[n][ph]["net"], "archive": round(float(pub[i][ph].pnl), 2), "fills_ours": out[n][ph]["fills"], "fills_archive": int(pub[i][ph].n)} for ph in ("development", "confirmation")} for n, i in (("v3", V3), ("favourite_t5", FAV))}
    for ph, days in (("development", dev), ("confirmation", conf)):
        a = v3[v3.day.isin(days)].groupby("day").pnl.sum().reindex(days, fill_value=0.0); b = fav[fav.day.isin(days)].groupby("day").pnl.sum().reindex(days, fill_value=0.0); d = (a - b).to_numpy()
        lo, hi = block_bootstrap(d); lo3, hi3 = block_bootstrap(a.to_numpy(), seed=44)
        out.setdefault("paired_v3_minus_favourite", {})[ph] = {"total": round(float(d.sum()), 2), "per_day": round(float(d.mean()), 3), "three_day_block_95": [round(lo, 3), round(hi, 3)], "days_v3_ahead": int((d > 0).sum()), "days_favourite_ahead": int((d < 0).sum()),
                                                                 "v3_alone_per_day_three_day_block_95": [round(lo3, 3), round(hi3, 3)]}
    fl = v3[v3.filled].copy(); fl["band"] = [sh.band(x) for x in fl.offer]; fl["phase"] = np.where(fl.day.isin(dev), "development", "confirmation")
    out["entry_band_attribution"] = {ph: {b: {"fills": int(len(x)), "wins": int(x.won.sum()), "net": round(float(x.pnl.sum()), 2), "cost": round(float(x.cost.sum()), 2)} for b, x in fl[fl.phase == ph].groupby("band")} for ph in ("development", "confirmation")}
    fl["vol"] = np.where(fl.vol_bps >= sh.VOL_SPLIT_VBPS, "higher", "lower")
    out["confirmation_by_vol_regime"] = {k: {"fills": int(len(x)), "wins": int(x.won.sum()), "net": round(float(x.pnl.sum()), 2)} for k, x in fl[fl.phase == "confirmation"].groupby("vol")}
    out["archive_published"] = {"paired_btc_control": json.loads((REF / "results/paired_btc_control.json").read_text()), "day_uncertainty": pd.read_csv(REF / "results/day_uncertainty.csv").to_dict("records"),
                                "selection_risk": pd.read_csv(REF / "results/selection_risk.csv").to_dict("records")}
    return out


# ---- old live span on our own recordings ----------------------------------------------------------------------------
def local_ticks(asset: str, t0: float, t1: float) -> list[dict]:
    p = pathlib.Path("D:/prediction-hft/data/cfb") / f"{asset}.jsonl"; out = []
    with p.open("rb") as f:
        lo, hi = 0, p.stat().st_size
        while hi - lo > 1 << 20:                                   # bisect to the first row near t0 (rows are in time order)
            mid = (lo + hi) // 2; f.seek(mid); f.readline(); line = f.readline()
            try: tt = json.loads(line)["t"]
            except Exception: tt = 0
            lo, hi = (mid, hi) if tt < t0 else (lo, mid)
        f.seek(lo); f.readline()
        for line in f:
            try: r = json.loads(line)
            except Exception: continue
            if r["t"] > t1: break
            if r.get("value") and r.get("value_ts_ms"): out.append({"event": r["value_ts_ms"] / 1000, "received": r["t"], "value": float(r["value"])})
    out.sort(key=lambda x: x["event"]); return out


def print_fill(ticker: str, side: str, limit: float, at: float) -> str:
    """'filled' if the first public print of our side within 5 s of arrival traded at or under the limit."""
    d = json.load(gzip.open(COV / "trades" / f"{ticker}.json.gz", "rt"))["trades"]; best = None
    for x in d:
        ts = calendar.timegm(time.strptime(x["created_time"][:19], "%Y-%m-%dT%H:%M:%S")) + float("0." + x["created_time"][20:-1] if "." in x["created_time"] else 0)
        if at <= ts < at + 5 and x["taker_side"] == side.lower() and (best is None or ts < best[0]): best = (ts, float(x[f"{side.lower()}_price_dollars"]))
    return "no print of our side within 5 s" if best is None else ("filled" if best[1] <= limit + 1e-9 else f"offer moved to {best[1]:.2f}")


def span() -> dict:
    w = json.loads((COV / "windows.json").read_text()); candles = json.loads((COV / "candles.json").read_text())["candles"]; t0, t1 = w["span"]
    ticks = {a: local_ticks(a, t0 - 2000, t1 + 100) for a in ("BTC", "ETH")}; ev = {a: [x["event"] for x in ticks[a]] for a in ticks}; out = {}
    for arm, (ser, asset, h) in sh.ARMS.items():
        res = {"windows": 0, "skips": {}, "intents": []}
        for tk, m in sorted(w["markets"].items(), key=lambda kv: kv[1]["close_time"]):
            if not tk.startswith(ser) or m.get("result") not in ("yes", "no"): continue
            close = calendar.timegm(time.strptime(m["close_time"], "%Y-%m-%dT%H:%M:%SZ")); D = close - h; res["windows"] += 1
            c = next((x for x in candles.get(tk, []) if x["end_period_ts"] == D), None)
            if not c or not c["yes_bid"].get("close_dollars") or not c["yes_ask"].get("close_dollars"): res["skips"]["no exchange quote at the decision second"] = res["skips"].get("no exchange quote at the decision second", 0) + 1; continue
            bid, ask = float(c["yes_bid"]["close_dollars"]), float(c["yes_ask"]["close_dollars"]); i0 = bisect.bisect_left(ev[asset], close - 900 - 61); i1 = bisect.bisect_right(ev[asset], D); tt = ticks[asset][i0:i1]
            snap = {"series": "KXBTC15M", "close": close, "quote": {"event": D, "received": D, "bid": bid, "ask": ask}, "strike_received": close - 900, "quoted_strike": float(m["floor_strike"]),
                    "official_strike": sh.rebuilt_strike(tt, close - 900, D), "brti": tt}
            if arm == "btc_fav_t5": intent = sh.favourite(bid, ask) if ref.index_features(tt, D, 1) is not None else None; why = None if intent else "no side offered at $0.965-$0.990 (or no index)"
            else: intent = ref.signal(snap); why = sh.diagnose(snap)[0]
            if not intent: res["skips"][why] = res["skips"].get(why, 0) + 1; continue
            won = (m["result"] == "yes") == (intent["side"] == "YES"); row = {"ticker": tk, "day": time.strftime("%Y-%m-%d", time.gmtime(close)), "side": intent["side"], "qty": intent["qty"], "limit": intent["limit"], "cost": intent["max_total_cost"], "won": won,
                                                                               "modeled_probability": intent.get("modeled_probability"), "fills": {}}
            for d in (1, 2, 5, 15, 30):
                st = print_fill(tk, intent["side"], intent["limit"], D + d); row["fills"][f"+{d}s"] = {"status": st, "net": round(intent["qty"] * won - intent["max_total_cost"], 2) if st == "filled" else 0.0}
            res["intents"].append(row)
        days = sorted({time.strftime("%Y-%m-%d", time.gmtime(calendar.timegm(time.strptime(m["close_time"], "%Y-%m-%dT%H:%M:%SZ")))) for m in w["markets"].values()})
        res["by_replay"] = {k: {"fills": sum(1 for r in res["intents"] if r["fills"][k]["status"] == "filled"), "net": round(sum(r["fills"][k]["net"] for r in res["intents"]), 2)} for k in ("+1s", "+2s", "+5s", "+15s", "+30s")}
        res["daily_net_1s"] = {d: round(sum(r["fills"]["+1s"]["net"] for r in res["intents"] if r["day"] == d), 2) for d in days}
        out[arm] = res
    out["quote_source"] = "exchange one-minute candle close at the decision second: exchange time only, no local receipt time, so 'aged at most 3 s on receipt' is NOT tested here"
    return out


if __name__ == "__main__":
    R = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "archive": archive(), "old_live_span": span()}
    OUT.mkdir(parents=True, exist_ok=True); (OUT / "offline_comparisons.json").write_text(json.dumps(R, indent=1, default=str))
    a = R["archive"]; print(json.dumps(a["matches_archive"], indent=1)); print(json.dumps({k: a[k] for k in ("v3", "favourite_t5", "paired_v3_minus_favourite", "entry_band_attribution", "confirmation_by_vol_regime")}, indent=1))
    for arm, r in R["old_live_span"].items():
        if isinstance(r, dict): print(arm, "windows", r["windows"], "intents", len(r["intents"]), r["by_replay"], "skips", r["skips"])
