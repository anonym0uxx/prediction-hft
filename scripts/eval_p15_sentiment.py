"""P15 sentiment-leg evaluation (docs/SENTIMENT_DESIGN_2026-10-27.md §4, §5 item 5). Scored-only, no-lookahead, stdlib only.

  uv run python scripts/eval_p15_sentiment.py [--out reports/p15_sentiment_<date>.md] [--assets BTC,ETH,SOL]

Test A (hourly Brier skill): P10 family books rows at T-3 joined to the latest social feature row with as_of <= t (age <= 900 s,
lex_version present) and to the official result (data/scoreboard-results.json + trades). Expanding-window 1-parameter logistic
overlay logit(p) = logit(p_mid) + b*x, out-of-sample Brier vs p_mid, hour-clustered SE, MDE = 2.8 sd/sqrt(n), sign stability over halves.
Test B (P13 pre-filter): contested trades (fav .6-.95): hit rate with vs without the sentiment veto (|x| > trailing 7-day 70th pct of |x|,
sign against the side), plus the sign-only disagreement fraction.
Test C (attention regime): Spearman of z_posts / z_msgs vs |realised hourly z| (proxy: next T-3 spot vs this T-3 spot, in sigma units),
P13 supply per hour, and 15-min pinned fraction; hour-cluster bootstrap CI; quintile means.
Features are read from data/context/social_features.jsonl (not the books ctx) so pre-wiring rows can still be joined causally."""
from __future__ import annotations
import argparse, bisect, dataclasses, datetime as dt, json, math, pathlib, random, statistics as st, sys

ROOT = pathlib.Path("data"); CTX = ROOT / "context"
FEATURE_PATHS = {"rd_z_posts_60m": ("rd", "z_posts_60m"), "rd_dir_ewma_120": ("rd", "dir_ewma_120"), "rd_dir_180m": ("rd", "dir_180m"), "rd_n_dir_180m": ("rd", "n_dir_180m"),
                 "st_bull_ratio_60m": ("st", "bull_ratio_60m"), "st_z_msgs_60m": ("st", "z_msgs_60m"), "st_dir_ewma_30": ("st", "dir_ewma_30"),
                 "st_disag_60m": ("st", "disag_60m"), "st_users_60m": ("st", "users_60m"), "st_z_users_60m": ("st", "z_users_60m"), "st_dir_lex_60m": ("st", "dir_lex_60m")}
DIR_FEATURES = ("rd_dir_ewma_120", "st_dir_ewma_30", "st_bull_ratio_60m")      # pre-registered direction features (§4.1)
ATT_FEATURES = ("rd_z_posts_60m", "st_z_msgs_60m", "st_z_users_60m")
FAMILY = {"BTC": "s7p10-KXBTCD", "ETH": "s7p10-KXETHD", "SOL": "s7p10-KXSOLD"}
P13 = {"BTC": ("s7p13-KXBTCD", "s7p13-KXBTC15M"), "ETH": ("s7p13-KXETH15M",), "SOL": ("s7p13-KXSOL15M",)}
MAX_AGE = 900.0


@dataclasses.dataclass
class Row:
    asset: str
    close_ts: float
    t: float
    p_mid: float
    y: int | None
    feats: dict
    sent_age_s: float | None
    lex_version: str | None
    cluster: int           # hour bucket for clustering
    spot: float | None = None
    sigma1: float | None = None
    mins_left: float | None = None
    ticker: str | None = None


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists(): return []
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            try: out.append(json.loads(line))
            except Exception: continue
    return out


def load_features(path: pathlib.Path = CTX / "social_features.jsonl") -> list[dict]:
    rows = [r for r in _jsonl(path) if "as_of" in r and "by_asset" in r]
    rows.sort(key=lambda r: r["as_of"]); return rows


def feature_at(feats: list[dict], t: float, asset: str, max_age: float = MAX_AGE) -> tuple[dict, float | None, str | None]:
    """Latest feature row with as_of <= t (causal). Returns (flat features, age, lex_version); empty if none/stale/unversioned."""
    keys = [r["as_of"] for r in feats]; i = bisect.bisect_right(keys, t) - 1
    if i < 0: return {}, None, None
    r = feats[i]; age = t - r["as_of"]
    if age > max_age or not r.get("lex_version"): return {}, age, r.get("lex_version")
    b = r["by_asset"].get(asset) or {}
    flat = {name: (b.get(blk) or {}).get(key) for name, (blk, key) in FEATURE_PATHS.items()}
    flat["news_flag"] = b.get("news_flag")
    return flat, age, r.get("lex_version")


def load_results() -> dict[str, str]:
    res = {}
    try: res.update(json.loads((ROOT / "scoreboard-results.json").read_text(encoding="utf-8")))
    except Exception: pass
    for fam in ROOT.glob("s7*"):
        for t in _jsonl(fam / "trades.jsonl"):
            if t.get("result") in ("yes", "no"): res[t["ticker"]] = t["result"]
    return res


def join_hourly(asset: str, family_dir: pathlib.Path, feats: list[dict], t_minus_min: float = 3.0) -> list[Row]:
    """One row per close_ts: the books snapshot nearest T-3 for the strike nearest spot (the contested one), mid = (bid+ask)/2."""
    books = _jsonl(family_dir / "books.jsonl"); res = load_results(); by_close: dict[float, list[dict]] = {}
    for b in books:
        if b.get("mins_left") is None or b.get("yes_bid") is None or b.get("yes_ask") is None or b.get("spot") is None: continue
        by_close.setdefault(b["close_ts"], []).append(b)
    rows = []
    for close_ts, bs in sorted(by_close.items()):
        bs.sort(key=lambda b: (abs(b["mins_left"] - t_minus_min), abs(b["K"] - b["spot"])))
        snap_t = bs[0]["t"]; cands = [b for b in bs if abs(b["t"] - snap_t) < 1.0]
        b = min(cands, key=lambda x: abs(x["K"] - x["spot"]))
        mid = (b["yes_bid"] + b["yes_ask"]) / 2
        if not (0.02 <= mid <= 0.98): continue
        f, age, lv = feature_at(feats, b["t"], asset)
        y = res.get(b["ticker"]); y = (1 if y == "yes" else 0 if y == "no" else None)
        rows.append(Row(asset, close_ts, b["t"], mid, y, f, age, lv, int(close_ts // 3600), b["spot"], b.get("sigma1"), b["mins_left"], b["ticker"]))
    return rows


# ---------------------------------------------------------------- Test A
def _logit(p): p = min(max(p, 1e-4), 1 - 1e-4); return math.log(p / (1 - p))
def _sig(x): return 1 / (1 + math.exp(-x)) if x > -700 else 0.0


def fit_beta(xs: list[float], offs: list[float], ys: list[int], l2: float = 1.0, iters: int = 25) -> float:
    """1-parameter logistic MLE with offset and ridge l2 (Newton)."""
    b = 0.0
    for _ in range(iters):
        g = -l2 * b; h = -l2
        for x, o, y in zip(xs, offs, ys):
            p = _sig(o + b * x); g += x * (y - p); h -= x * x * p * (1 - p)
        if abs(h) < 1e-12: break
        step = g / h; b -= step
        if abs(step) < 1e-8: break
    return b


def cluster_se(d: list[float], keys: list) -> float:
    by: dict = {}
    for v, k in zip(d, keys): by.setdefault(k, []).append(v)
    sums = [sum(v) for v in by.values()]; G = len(sums); n = len(d)
    if G < 2 or n == 0: return float("nan")
    mean = sum(d) / n; return math.sqrt(sum((s - len(v) * mean) ** 2 for s, v in zip(sums, by.values())) * G / (G - 1)) / n


def brier_skill(rows: list[Row], feature: str, burn_in: int = 48) -> dict:
    rs = sorted([r for r in rows if r.y is not None and r.feats.get(feature) is not None], key=lambda r: r.t)
    out = {"feature": feature, "n_total": len(rs), "n_oos": 0}
    if len(rs) <= burn_in: out["note"] = f"n={len(rs)} <= burn_in={burn_in}"; return out
    deltas, keys, betas = [], [], []
    for i in range(burn_in, len(rs)):
        hist = rs[:i]; b = fit_beta([r.feats[feature] for r in hist], [_logit(r.p_mid) for r in hist], [r.y for r in hist])
        r = rs[i]; p = _sig(_logit(r.p_mid) + b * r.feats[feature])
        deltas.append((r.p_mid - r.y) ** 2 - (p - r.y) ** 2); keys.append(r.cluster); betas.append(b)
    n = len(deltas); mean = sum(deltas) / n; se = cluster_se(deltas, keys); sd = st.pstdev(deltas) if n > 1 else float("nan")
    half = n // 2; m1 = sum(deltas[:half]) / max(half, 1); m2 = sum(deltas[half:]) / max(n - half, 1)
    out.update({"n_oos": n, "delta_brier": round(mean, 5), "se_cluster": (round(se, 5) if se == se else None), "ci95": [round(mean - 1.96 * se, 5), round(mean + 1.96 * se, 5)] if se == se else None,
                "mde_95_80": (round(2.8 * sd / math.sqrt(n), 5) if sd == sd else None), "beta_final": round(betas[-1], 4), "halves": [round(m1, 5), round(m2, 5)],
                "sign_stable": (m1 > 0) == (m2 > 0), "pass": bool(se == se and mean - 1.96 * se > 0 and n >= 150)})
    return out


# ---------------------------------------------------------------- Test B
def contested_filter(asset: str, trades: list[dict], feats: list[dict], feature: str = "st_dir_ewma_30", pct: float = 0.70, lo: float = 0.6, hi: float = 0.95) -> dict:
    cs = [t for t in trades if t.get("result") in ("yes", "no") and t.get("fav_at_decision") is not None and lo <= t["fav_at_decision"] <= hi]
    cs.sort(key=lambda t: t["t"]); hist: list[tuple[float, float]] = []; kept = []; all_hits = []; disagree = 0; n_feat = 0
    for t in cs:
        f, age, lv = feature_at(feats, t["t"], asset); x = f.get(feature)
        hit = int(t["result"] == t["side"]); all_hits.append(hit)
        if x is None: kept.append(hit); continue
        n_feat += 1
        trail = [abs(v) for ts, v in hist if t["t"] - ts <= 7 * 86400]
        thr = sorted(trail)[int(pct * (len(trail) - 1))] if len(trail) >= 10 else None
        sign_x = 1 if x > 0 else -1 if x < 0 else 0; side_sign = 1 if t["side"] == "yes" else -1
        if sign_x and sign_x != side_sign: disagree += 1
        veto = thr is not None and abs(x) > thr and sign_x and sign_x != side_sign
        if not veto: kept.append(hit)
        hist.append((t["t"], x))
    n_all = len(all_hits); n_kept = len(kept)
    return {"asset": asset, "feature": feature, "n_contested": n_all, "n_with_feature": n_feat, "hit_all": (round(sum(all_hits) / n_all, 4) if n_all else None),
            "hit_filtered": (round(sum(kept) / n_kept, 4) if n_kept else None), "n_vetoed": n_all - n_kept, "sign_disagree_frac": (round(disagree / n_feat, 4) if n_feat else None),
            "pass": bool(n_all >= 40 and n_kept and n_all and sum(kept) / n_kept - sum(all_hits) / n_all >= 0.03)}


# ---------------------------------------------------------------- Test C
def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i]); r = [0.0] * len(xs); i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]: j += 1
        for k in range(i, j + 1): r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def _r3(v): return round(v, 3) if v is not None else None


def spearman(x, y):
    if len(x) < 3: return None
    rx, ry = _rank(x), _rank(y); mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry)); den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def _boot_ci(pairs: list[tuple[float, float, int]], B: int = 400, seed: int = 7):
    by: dict = {}
    for x, y, c in pairs: by.setdefault(c, []).append((x, y))
    clusters = list(by.values()); rng = random.Random(seed); vals = []
    for _ in range(B):
        samp = [p for c in (rng.choice(clusters) for _ in clusters) for p in c]
        r = spearman([p[0] for p in samp], [p[1] for p in samp])
        if r is not None: vals.append(r)
    if len(vals) < 20: return None
    vals.sort(); return [round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals)) - 1], 3)]


def _quintiles(pairs):
    ps = sorted(pairs); n = len(ps)
    if n < 5: return None
    return [round(sum(y for _, y in ps[i * n // 5:(i + 1) * n // 5]) / max(len(ps[i * n // 5:(i + 1) * n // 5]), 1), 4) for i in range(5)]


def attention_regime(asset: str, feats: list[dict], hourly: list[Row], supply: list[dict], feature: str = "rd_z_posts_60m", books15: list[dict] | None = None) -> dict:
    out = {"asset": asset, "feature": feature}
    # Y1: |realised hourly move| in sigma units between consecutive T-3 snapshots (proxy for the P10 realised |z|)
    hs = sorted([r for r in hourly if r.feats.get(feature) is not None and r.spot and r.sigma1], key=lambda r: r.close_ts)
    by_close = {r.close_ts: r for r in sorted(hourly, key=lambda r: r.close_ts) if r.spot}
    pairs = []
    for r in hs:
        nxt = by_close.get(r.close_ts + 3600)
        if not nxt: continue
        z = abs(math.log(nxt.spot / r.spot)) / (r.sigma1 * math.sqrt(60))
        pairs.append((r.feats[feature], z, r.cluster))
    out["y1_realised_z"] = {"n": len(pairs), "spearman": _r3(spearman([p[0] for p in pairs], [p[1] for p in pairs])),
                            "ci95_boot": _boot_ci(pairs) if len(pairs) >= 10 else None, "quintile_means": _quintiles([(p[0], p[1]) for p in pairs])}
    # Y2: P13 supply per close hour vs the feature at the start of the hour
    sup: dict[int, int] = {}
    for s in supply:
        if s.get("traded") is not None: sup[int(s["close_ts"] // 3600)] = sup.get(int(s["close_ts"] // 3600), 0) + int(bool(s.get("traded")))
    pairs2 = []
    for r in hs:
        h = int(r.close_ts // 3600)
        if h in sup: pairs2.append((r.feats[feature], sup[h], r.cluster))
    out["y2_p13_supply"] = {"n": len(pairs2), "spearman": _r3(spearman([p[0] for p in pairs2], [p[1] for p in pairs2])),
                            "ci95_boot": _boot_ci(pairs2) if len(pairs2) >= 10 else None, "quintile_means": _quintiles([(p[0], p[1]) for p in pairs2])}
    # Y3: 15-min pinned fraction (supply.jsonl pinned_at_T55) per hour
    pin: dict[int, list[int]] = {}
    for s in supply:
        if "pinned_at_T55" in s: pin.setdefault(int(s["close_ts"] // 3600), []).append(int(bool(s["pinned_at_T55"])))
    pairs3 = [(r.feats[feature], sum(pin[h]) / len(pin[h]), r.cluster) for r in hs for h in [int(r.close_ts // 3600)] if h in pin]
    out["y3_pinned_frac"] = {"n": len(pairs3), "spearman": _r3(spearman([p[0] for p in pairs3], [p[1] for p in pairs3])),
                             "ci95_boot": _boot_ci(pairs3) if len(pairs3) >= 10 else None, "quintile_means": _quintiles([(p[0], p[1]) for p in pairs3])}
    return out


# ---------------------------------------------------------------- coverage / census
def coverage(rows: list[Row]) -> dict:
    n = len(rows); ages = [r.sent_age_s for r in rows if r.sent_age_s is not None]
    nulls = {f: sum(1 for r in rows if r.feats.get(f) is None) for f in DIR_FEATURES + ATT_FEATURES}
    return {"n_hours": n, "n_settled": sum(1 for r in rows if r.y is not None), "n_with_features": sum(1 for r in rows if r.feats),
            "mean_sent_age_s": (round(sum(ages) / len(ages), 1) if ages else None), "null_rate": {f: (round(v / n, 3) if n else None) for f, v in nulls.items()},
            "lex_versions": sorted({r.lex_version for r in rows if r.lex_version})}


def census_summary(store: pathlib.Path = CTX / "social_posts.jsonl") -> dict:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    from predhft.sentiment_lex import load_lexicon, census
    docs = _jsonl(store)
    if not docs: return {"n_docs": 0}
    rd = [d for d in docs if d.get("src") == "reddit"]
    c = census(rd[-200:], load_lexicon()); c.pop("scored", None); c.pop("per_term", None)
    c["store_docs"] = len(docs); c["reddit_docs"] = len(rd); c["stocktwits_docs"] = len(docs) - len(rd)
    c["dup_rate"] = round(sum(1 for d in docs if d.get("dup_of")) / len(docs), 4); c["sticky_rate_reddit"] = round(sum(1 for d in rd if d.get("is_sticky")) / len(rd), 4) if rd else None
    c["n_val_ge1_rate_reddit"] = round(sum(1 for d in rd if d.get("n_val", 0) >= 1) / len(rd), 4) if rd else None
    return c


def report(out: pathlib.Path, assets=("BTC", "ETH", "SOL")) -> str:
    feats = load_features(); lines = [f"# P15 sentiment leg — evaluation {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%MZ}", "",
                                      f"feature rows: {len(feats)} (data/context/social_features.jsonl); scored-only; no-lookahead (as_of <= t, age <= {MAX_AGE:.0f} s)", ""]
    lines += ["## Lexicon census (store)", "```", json.dumps(census_summary(), indent=1, ensure_ascii=False), "```", ""]
    for a in assets:
        rows = join_hourly(a, ROOT / FAMILY[a], feats)
        lines += [f"## {a}", "", "coverage: `" + json.dumps(coverage(rows)) + "`", ""]
        lines += ["### Test A — hourly Brier skill (P10 rows, T-3)"]
        for f in DIR_FEATURES + ATT_FEATURES: lines.append("- `" + json.dumps(brier_skill(rows, f)) + "`")
        trades = [t for fam in P13[a] for t in _jsonl(ROOT / fam / "trades.jsonl")]
        lines += ["", "### Test B — P13 contested pre-filter (fav .6-.95)"]
        for f in DIR_FEATURES: lines.append("- `" + json.dumps(contested_filter(a, trades, feats, f)) + "`")
        supply = [s for fam in P13[a] for s in _jsonl(ROOT / fam / "supply.jsonl")]
        lines += ["", "### Test C — attention regime"]
        for f in ATT_FEATURES: lines.append("- `" + json.dumps(attention_regime(a, feats, rows, supply, f)) + "`")
        lines.append("")
    text = "\n".join(lines); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(text, encoding="utf-8"); return text


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default=f"reports/p15_sentiment_{dt.date.today():%Y-%m-%d}.md"); ap.add_argument("--assets", default="BTC,ETH,SOL")
    a = ap.parse_args(); print(report(pathlib.Path(a.out), tuple(a.assets.split(","))))
