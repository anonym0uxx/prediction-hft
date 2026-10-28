"""P22 as a PAIRED prospective experiment (AUDIT_2026-10-28 §4). Offline, deterministic, reads only recorded data:

  data/cfb/<A>.jsonl       official CF Benchmarks ticks with LOCAL receipt time t (what the live process could have known)
  data/replica/<A>.jsonl   1 Hz replica index rows (t = local sample time)
  data/<fam>/books.jsonl   final-minute book snapshots (s7p13 + s7p15 families; new rows carry top-3 depth, old rows top-of-book)
  data/open_quotes/quotes.jsonl  quote-path rows (T-15 min .. T-60 s), used for the T-60 s book when no runner row is near
  data/p22/markets_cache.json    settled market results + expiration_value (re-fetched by scripts/audit_2026_10_28 helpers)

For EVERY settled 15M market in the official-feed era (not only the ones P13 considered), at the predetermined horizons
T-60/-45/-30/-20/-10 s, using only information RECEIVED by that timestamp, it records: the replica-based forecast, the official-feed
forecast, the market mid, and the contemporaneous executable book; then scores
  (a) forecast quality: Brier, calibration (reliability by decile), settlement-value error in bp (projection vs expiration_value);
  (b) trading value: net edge vs the executable price for the intended qty (lot 10, +1 tick as the paper runner books it, fee once),
      realised net per contract on candidates with edge >= 5c, survival of the offer after the measured latency (next snapshot).
Uncertainty: SE clustered by settlement close (BTC/ETH/SOL share a close) AND by hour block (4 closes). Alternative timing windows
are the horizons themselves (shadow); nothing is tuned.

Usage: uv run python scripts/p22_paired.py [--since "2026-10-27 16:00"] [--lot 10] [--edge-min-c 5]
"""
from __future__ import annotations
import argparse, bisect, collections, datetime as dt, json, math, pathlib, statistics, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft.reject_taxonomy import p_cal, side_present
from predhft.ts_levers import BASIS_BP, BASIS_NOISE_BP
from predhft.fees import kalshi_taker_fee
from predhft.feed_tail import sigma_1m_from_rows

PT = dt.timezone(dt.timedelta(hours=-7)); DATA = pathlib.Path("data"); HORIZONS = (60, 45, 30, 20, 10); ASSETS = ("BTC", "ETH", "SOL")

def load_jsonl(p: pathlib.Path, keep):
    out = []
    if not p.exists(): return out
    with p.open(encoding="utf-8") as f:
        for l in f:
            if not l.strip(): continue
            try: r = json.loads(l)
            except Exception: continue
            if keep(r): out.append(r)
    return out

class Feed:
    """Time-indexed 1 Hz feed; `upto(t)` = rows received by local time t (bisect on t)."""
    def __init__(self, rows: list[dict], vkey: str, tskey: str | None):
        rows.sort(key=lambda r: r["t"]); self.rows = rows; self.ts = [r["t"] for r in rows]; self.vkey = vkey; self.tskey = tskey
    def idx_upto(self, t: float) -> int: return bisect.bisect_right(self.ts, t)
    def last_value(self, t: float):
        i = self.idx_upto(t) - 1
        while i >= 0:
            v = self.rows[i].get(self.vkey)
            if v: return float(v), self.rows[i]["t"]
            i -= 1
        return None, None
    def window_mean(self, t: float, w0: float, w1: float):
        """Mean of samples received by t whose SAMPLE time (value_ts for cfb, local t for replica) lies in [w0, w1)."""
        i = self.idx_upto(t); vals = []
        j = i - 1
        while j >= 0 and self.rows[j]["t"] >= w0 - 5:
            r = self.rows[j]; st = (r[self.tskey] / 1000.0) if self.tskey else r["t"]
            if w0 <= st < w1 and r.get(self.vkey): vals.append(float(r[self.vkey]))
            j -= 1
        return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)
    def sigma1(self, t: float) -> float | None:
        i = self.idx_upto(t); j = bisect.bisect_left(self.ts, t - 7200)
        return sigma_1m_from_rows(self.rows[j:i], key=self.vkey, min_minutes=30)

def forecast(feed: Feed, t_h: float, close: float, K: float, basis_bp: float, noise_bp: float, sigma_fallback: float = 0.0005):
    v, tv = feed.last_value(t_h)
    if v is None: return None
    b = 1 + basis_bp * 1e-4; v *= b
    partial, n = feed.window_mean(t_h, close - 60, min(t_h, close)); partial = partial * b if partial else None
    r = (60 - n) / 60.0 if n else min((close - t_h) / 60.0, 1.0)
    proj = (1 - r) * partial + r * v if (partial and n >= 3) else v
    sig = feed.sigma1(t_h) or sigma_fallback; d_bp = 1e4 * (proj / K - 1)
    return {"proj": proj, "d_bp": d_bp, "p": p_cal(d_bp, sig, max(r, 0.02), noise_bp), "n": n, "age_s": t_h - tv, "sigma1": sig}

def book_at(rows_by_tk: dict, quotes_by_tk: dict, tk: str, t_h: float):
    """Latest book row for tk with t in [t_h-6, t_h]; falls back to a quote-path row within 20 s. Returns (row, source)."""
    S = rows_by_tk.get(tk, [])
    best = None
    for r in S:
        if r["t"] <= t_h and r["t"] >= t_h - 6 and (best is None or r["t"] > best["t"]): best = r
    if best is not None: return best, "books"
    for q in quotes_by_tk.get(tk, []):
        if q["t"] <= t_h and q["t"] >= t_h - 20 and (best is None or q["t"] > best["t"]): best = q
    if best is not None:
        b = {"t": best["t"], "yes_bid": best.get("yes_bid"), "yes_ask": best.get("yes_ask"), "bid_size": best.get("yes_bid_qty") or 0.0, "ask_size": best.get("no_bid_qty") or 0.0}
        return b, "quotes"
    return None, None

def offer_for(row: dict, side: str, lot: int):
    """Executable price for `lot` contracts on `side` from top-3 depth when present (new rows) else top-of-book; None if not available."""
    bid_ok, ask_ok = side_present(row)
    levels = None
    if side == "yes" and ask_ok: levels = row.get("asks") or [(float(row["yes_ask"]), float(row.get("ask_size") or 0))]
    if side == "no" and bid_ok: levels = [(round(1 - float(p), 4), float(q)) for p, q in (row.get("bids") or [(float(row["yes_bid"]), float(row.get("bid_size") or 0))])]
    if not levels: return None, 0.0
    need = lot; cost = 0.0; got = 0.0
    for px, q in levels:
        take = min(q, need - got); cost += take * px; got += take
        if got >= need - 1e-9: break
    depth = sum(q for _, q in levels)
    return (cost / got if got >= need - 1e-9 else None), depth

def next_snapshot(rows_by_tk: dict, tk: str, after: float, within: float = 4.0):
    cands = [r for r in rows_by_tk.get(tk, []) if after < r["t"] <= after + within]
    return min(cands, key=lambda r: r["t"]) if cands else None

def cluster_se(vals: list[float], clusters: list) -> float | None:
    """Cluster-robust SE of the mean (Liang-Zeger, G/(G-1) small-sample factor)."""
    n = len(vals)
    if n < 2: return None
    m = sum(vals) / n; by = collections.defaultdict(float)
    for v, c in zip(vals, clusters): by[c] += v - m
    G = len(by)
    if G < 2: return None
    return math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--since", default="2026-10-27 16:00"); ap.add_argument("--lot", type=int, default=10); ap.add_argument("--edge-min-c", type=float, default=5.0)
    ap.add_argument("--latency-s", type=float, default=1.0, help="assumed decision->order latency for the survival test (measured re-check latency p50 0.45 s)")
    a = ap.parse_args(); since = dt.datetime.strptime(a.since, "%Y-%m-%d %H:%M").replace(tzinfo=PT).timestamp(); lot = a.lot
    mk = json.loads((DATA / "p22" / "markets_cache.json").read_text())
    feeds = {A: Feed(load_jsonl(DATA / "cfb" / f"{A}.jsonl", lambda r: r.get("value") and r["t"] >= since - 7300), "value", "value_ts_ms") for A in ASSETS}
    reps = {A: Feed(load_jsonl(DATA / "replica" / f"{A}.jsonl", lambda r: r.get("replica") and r["t"] >= since - 7300), "replica", None) for A in ASSETS}
    rows_by_tk = collections.defaultdict(list)
    for fam in list(DATA.glob("s7p13-KX*15M")) + list(DATA.glob("s7p15-KX*15M")):
        for r in load_jsonl(fam / "books.jsonl", lambda r: r.get("mins_left") is not None and r["t"] >= since and r.get("close_ts") and r["close_ts"] - r["t"] <= 130): rows_by_tk[r["ticker"]].append(r)
    quotes_by_tk = collections.defaultdict(list)
    for q in load_jsonl(DATA / "open_quotes" / "quotes.jsonl", lambda r: r.get("ticker") and r["t"] >= since and r.get("close_ts") and r["close_ts"] - r["t"] <= 130): quotes_by_tk[q["ticker"]].append(q)
    recs = []
    for tk, m in mk.items():
        if m.get("result") not in ("yes", "no") or not m.get("expiration_value") or not m.get("floor_strike"): continue
        A = tk[2:5]
        if A not in ASSETS: continue
        close = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
        if close < since + 60: continue
        K = float(m["floor_strike"]); ev = float(m["expiration_value"]); y = 1.0 if m["result"] == "yes" else 0.0
        for h in HORIZONS:
            t_h = close - h
            f_off = forecast(feeds[A], t_h, close, K, 0.0, 0.0); f_rep = forecast(reps[A], t_h, close, K, BASIS_BP[A], BASIS_NOISE_BP)
            if f_off is None or f_rep is None: continue
            bk, src = book_at(rows_by_tk, quotes_by_tk, tk, t_h)
            rec = {"ticker": tk, "asset": A, "close_ts": int(close), "hour_block": int(close // 3600), "h": h, "y": y, "ev": ev, "K": K,
                   "p_off": f_off["p"], "p_rep": f_rep["p"], "err_off_bp": 1e4 * (f_off["proj"] - ev) / ev, "err_rep_bp": 1e4 * (f_rep["proj"] - ev) / ev,
                   "n_off": f_off["n"], "n_rep": f_rep["n"], "age_off_s": round(f_off["age_s"], 2), "age_rep_s": round(f_rep["age_s"], 2), "book_src": src, "book_age_s": (round(t_h - bk["t"], 2) if bk else None)}
            if bk is not None:
                bid_ok, ask_ok = side_present(bk)
                rec["mid"] = (float(bk["yes_bid"]) + float(bk["yes_ask"])) / 2 if (bid_ok and ask_ok) else (float(bk["yes_bid"]) if bid_ok else (float(bk["yes_ask"]) if ask_ok else None))
                rec["two_sided"] = bid_ok and ask_ok
                for tag, f in (("off", f_off), ("rep", f_rep)):
                    side = "yes" if f["d_bp"] > 0 else "no"; px, depth = offer_for(bk, side, lot)
                    rec[f"side_{tag}"] = side; rec[f"offer_{tag}"] = px; rec[f"depth_{tag}"] = depth
                    if px is not None and 0 < px < 1:
                        price = min(px + 0.01, 0.99); fee = kalshi_taker_fee(lot, price) / lot; p_side = f["p"] if side == "yes" else 1 - f["p"]; win = y if side == "yes" else 1 - y
                        rec[f"edge_c_{tag}"] = 100 * (p_side - price - fee); rec[f"real_c_{tag}"] = 100 * (win - price - fee)
                        nxt = next_snapshot(rows_by_tk, tk, t_h + a.latency_s)
                        if nxt is not None:
                            px2, _ = offer_for(nxt, side, lot); rec[f"survives_{tag}"] = (px2 is not None and px2 <= px + 1e-9)
            recs.append(rec)
    out_p = DATA / "p22" / f"p22_paired_{dt.datetime.now(PT).strftime('%Y-%m-%d')}.jsonl"
    out_p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    # ---------------------------------------------------------------------------------------------------- scoring
    print(f"# P22 paired — {len(recs)} (market, horizon) records, {len({r['ticker'] for r in recs})} markets since {a.since} PT; rows -> {out_p}\n")
    print("## Forecast quality by asset x horizon (Brier lower is better; err = projection - settlement value, bp)\n")
    print("| asset | T-h s | n | Brier off | Brier rep | Brier mid | Brier off (same 2-sided subset) | n(mid) | |err| off bp | |err| rep bp | sd err off | sd err rep | off n samples | book src |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    def mean(x): return sum(x) / len(x) if x else float("nan")
    for A in ASSETS + ("ALL",):
        for h in HORIZONS:
            R = [r for r in recs if r["h"] == h and (A == "ALL" or r["asset"] == A)]
            if not R: continue
            bo = mean([(r["p_off"] - r["y"]) ** 2 for r in R]); br = mean([(r["p_rep"] - r["y"]) ** 2 for r in R])
            M = [r for r in R if r.get("mid") is not None and r.get("two_sided")]; bm = mean([(r["mid"] - r["y"]) ** 2 for r in M]) if M else float("nan"); bo2 = mean([(r["p_off"] - r["y"]) ** 2 for r in M]) if M else float("nan")
            eo = [r["err_off_bp"] for r in R]; er = [r["err_rep_bp"] for r in R]
            src = collections.Counter(r["book_src"] for r in R)
            print(f"| {A} | {h} | {len(R)} | {bo:.4f} | {br:.4f} | {bm:.4f} | {bo2:.4f} | {len(M)} | {mean([abs(x) for x in eo]):.2f} | {mean([abs(x) for x in er]):.2f} | {statistics.pstdev(eo) if len(eo) > 1 else 0:.2f} | {statistics.pstdev(er) if len(er) > 1 else 0:.2f} | {mean([r['n_off'] for r in R]):.0f} | {dict(src)} |")
    print("\n## Calibration (reliability) of p_off by decile, pooled over horizons\n")
    print("| decile | n | mean p_off | hit rate |"); print("|---|---|---|---|")
    for d in range(10):
        R = [r for r in recs if d / 10 <= r["p_off"] < (d + 1) / 10 + (1e-9 if d == 9 else 0)]
        if R: print(f"| {d/10:.1f}-{(d+1)/10:.1f} | {len(R)} | {mean([r['p_off'] for r in R]):.3f} | {mean([r['y'] for r in R]):.3f} |")
    print("\n## Trading value by asset x horizon (lot 10, paper price = executable + 1 tick, fee once). cand = edge >= %.0fc on the executable book\n" % a.edge_min_c)
    print("| asset | T-h s | n with book | two-sided | offered(off side) | depth>=lot | cand off | real net c/ct (cand off) | SE close | SE hour | survive after latency | cand rep | real net c/ct (cand rep) | mean edge off (all offered) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for A in ASSETS + ("ALL",):
        for h in HORIZONS:
            R = [r for r in recs if r["h"] == h and (A == "ALL" or r["asset"] == A) and r.get("book_src")]
            if not R: continue
            two = sum(1 for r in R if r.get("two_sided")); off = [r for r in R if r.get("offer_off") is not None]
            co = [r for r in off if r["edge_c_off"] >= a.edge_min_c]; cr = [r for r in R if r.get("offer_rep") is not None and r["edge_c_rep"] >= a.edge_min_c]
            rv = [r["real_c_off"] for r in co]; se_c = cluster_se(rv, [r["close_ts"] for r in co]); se_h = cluster_se(rv, [r["hour_block"] for r in co])
            surv = [r.get("survives_off") for r in co if r.get("survives_off") is not None]
            print(f"| {A} | {h} | {len(R)} | {two} | {len(off)} | {sum(1 for r in off if r['depth_off'] >= lot)} | {len(co)} | {mean(rv):+.1f} | {('%.1f' % se_c) if se_c else '-'} | {('%.1f' % se_h) if se_h else '-'} | {sum(surv)}/{len(surv)} | {len(cr)} | {mean([r['real_c_rep'] for r in cr]):+.1f} | {mean([r['edge_c_off'] for r in off]):+.1f} |")
    # paired difference official - replica (same market, same horizon)
    print("\n## Paired information test (same market & horizon): official minus replica\n")
    print("| T-h s | n | mean Brier diff (off-rep) | SE close | SE hour | mean |err| diff bp (off-rep) | share |d_off|>|d_rep| |"); print("|---|---|---|---|---|---|---|")
    for h in HORIZONS:
        R = [r for r in recs if r["h"] == h]
        if not R: continue
        bd = [(r["p_off"] - r["y"]) ** 2 - (r["p_rep"] - r["y"]) ** 2 for r in R]; ed = [abs(r["err_off_bp"]) - abs(r["err_rep_bp"]) for r in R]
        print(f"| {h} | {len(R)} | {mean(bd):+.4f} | {cluster_se(bd, [r['close_ts'] for r in R]):.4f} | {cluster_se(bd, [r['hour_block'] for r in R]):.4f} | {mean(ed):+.2f} | {mean([1.0 if abs(r['err_off_bp']) < abs(r['err_rep_bp']) else 0.0 for r in R]):.2f} |")

if __name__ == "__main__":
    main()
