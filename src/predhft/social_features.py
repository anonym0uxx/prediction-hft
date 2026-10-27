"""Dedup store + causal feature builder + 7-day same-hour baselines for the P15 sentiment leg
(docs/SENTIMENT_DESIGN_2026-10-27.md §3.3-3.5, §5 item 3). Stdlib only.

Store: data/context/social_posts.jsonl, one normalised document per line (schema §3.3; every doc carries fetched_ts).
Features: data/context/social_features.jsonl + social_features.latest.json {as_of, lex_version, by_asset:{BTC:{rd:{...}, st:{...}, news_flag}}}.
Baselines: data/context/social_baselines.json (per (src, asset, feature, hour-of-day) sum/sumsq/n over the trailing 7 days) and
data/lexicon/sub_priors.json (per sub/symbol posts/h and s_doc mean/sd) — produced here, never hand-set.
Causality: features(as_of) only use docs with fetched_ts <= as_of; a doc's created_ts is never used to back-fill a window it was not
visible for. Baselines (distributions, not decisions) use created_ts so they can be bootstrapped from a limit=100 listing."""
from __future__ import annotations
import json, math, pathlib, re, time
from .sentiment_lex import load_lexicon

ROOT = pathlib.Path("data/context")
STORE = ROOT / "social_posts.jsonl"
FEATS = ROOT / "social_features.jsonl"
LATEST = ROOT / "social_features.latest.json"
BASELINES = ROOT / "social_baselines.json"
FETCH_STATE = ROOT / "social_fetch_state.json"
SUB_PRIORS = pathlib.Path("data/lexicon/sub_priors.json")
ASSETS = ("BTC", "ETH", "SOL")
SUBS_FOR = {"BTC": ("Bitcoin", "CryptoCurrency"), "ETH": ("ethereum", "CryptoCurrency"), "SOL": ("solana", "CryptoCurrency")}
SYMBOL_FOR = {"BTC": "BTC.X", "ETH": "ETH.X", "SOL": "SOL.X"}
HL_15M, HL_1H = 900.0, 3600.0
DUP_JACCARD, DUP_WINDOW = 0.8, 86400.0
WINDOW_S = 4 * 3600.0
_TOK = re.compile(r"[a-z0-9$]+")


def _title_set(title: str) -> frozenset:
    return frozenset(_TOK.findall((title or "").lower()))


def read_store(store: pathlib.Path = STORE) -> list[dict]:
    if not store.exists(): return []
    out = []
    with store.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: out.append(json.loads(line))
            except Exception: continue
    return out


def ingest(docs: list[dict], store: pathlib.Path | None = None) -> int:
    """Append new docs (by id); flag near-duplicates (title Jaccard >= .8 within 24 h, across subs) with dup_of. Returns lines written."""
    store = store or STORE; store.parent.mkdir(parents=True, exist_ok=True)
    existing = read_store(store); ids = {d["id"] for d in existing}
    newest = max((d.get("created_ts") or 0) for d in existing) if existing else 0
    recent = [(d["id"], d.get("created_ts") or 0, _title_set(d.get("title") or "")) for d in existing if (d.get("created_ts") or 0) >= newest - 2 * DUP_WINDOW and d.get("dup_of") is None]
    written = 0
    with store.open("a", encoding="utf-8") as f:
        for d in sorted(docs, key=lambda x: x.get("created_ts") or 0):
            if d["id"] in ids: continue
            ts = d.get("created_ts") or 0; ts_set = _title_set(d.get("title") or ""); dup = None
            if len(ts_set) >= 3:
                for oid, ots, oset in recent:
                    if abs(ts - ots) <= DUP_WINDOW and oset:
                        inter = len(ts_set & oset); uni = len(ts_set | oset)
                        if uni and inter / uni >= DUP_JACCARD: dup = oid; break
            row = dict(d); row["dup_of"] = dup
            f.write(json.dumps(row, ensure_ascii=False) + "\n"); written += 1; ids.add(d["id"])
            if dup is None: recent.append((d["id"], ts, ts_set))
    return written


def load_window(store: pathlib.Path, as_of: float, max_age_s: float = WINDOW_S) -> list[dict]:
    """Docs visible at as_of (fetched_ts <= as_of) created within max_age_s before as_of."""
    lo = as_of - max_age_s
    return [d for d in read_store(store) if (d.get("fetched_ts") or 0) <= as_of and lo < (d.get("created_ts") or 0) <= as_of + 60]


def load_fetch_state(path: pathlib.Path | None = None) -> dict:
    path = path or FETCH_STATE
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return {}


def mark_fetched(src: str, target: str, ts: float, path: pathlib.Path | None = None) -> None:
    path = path or FETCH_STATE; st = load_fetch_state(path); st.setdefault(src, {})[target] = ts
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(st), encoding="utf-8")


# ---------------------------------------------------------------- helpers
def _mean(xs):
    xs = list(xs); return sum(xs) / len(xs) if xs else None


def _author_mean(docs: list[dict]) -> tuple[float | None, int]:
    by = {}
    for d in docs: by.setdefault(d.get("author") or d["id"], []).append(d["s_doc"])
    n = len(by)
    return (round(_mean(_mean(v) for v in by.values()), 4) if n >= 3 else None), n


def _ewma(docs: list[dict], as_of: float, hl: float) -> float | None:
    if not docs: return None
    w = [2 ** (-(as_of - d["created_ts"]) / hl) for d in docs]
    sw, sw2 = sum(w), sum(x * x for x in w)
    if sw <= 0 or (sw * sw / sw2) < 3: return None
    return round(sum(wi * d["s_doc"] for wi, d in zip(w, docs)) / sw, 4)


def _disag(docs: list[dict], use_label: bool = False) -> float | None:
    pos = neg = 0
    for d in docs:
        lab = d.get("label") if use_label else None
        if lab == "Bullish" or (lab is None and d["s_doc"] > 0.15): pos += 1
        elif lab == "Bearish" or (lab is None and d["s_doc"] < -0.15): neg += 1
    return round(min(pos, neg) / (pos + neg), 4) if pos + neg >= 3 else None


def _dir_docs(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("n_val", 0) >= 1 and not d.get("is_question") and d.get("dup_of") is None and not d.get("is_sticky")]


def _z(baselines: dict | None, src: str, asset: str, feat: str, as_of: float, x: float | None, log: bool = False) -> float | None:
    if x is None or not baselines: return None
    h = int(time.gmtime(as_of).tm_hour); s = ss = 0.0; n = 0
    for hh in (h - 1, h, h + 1):
        b = baselines.get("stats", {}).get(f"{src}|{asset}|{feat}|h{hh % 24}")
        if b: s += b["sum"]; ss += b["sumsq"]; n += b["n"]
    if n < 20: return None
    mu = s / n; var = max(ss / n - mu * mu, 0.0); sd = math.sqrt(var)
    v = math.log1p(x) if log else x
    if sd < 1e-9: return 0.0 if abs(v - mu) < 1e-9 else None
    return round((v - mu) / sd, 3)


def _in(d: dict, asset: str, as_of: float, w: float, src: str) -> bool:
    return d.get("src") == src and asset in (d.get("assets") or []) and as_of - w < d["created_ts"] <= as_of + 60 and (d.get("fetched_ts") or 0) <= as_of


def features(asset: str, as_of: float, docs: list[dict], baselines: dict | None, hl_15m: float = HL_15M, hl_1h: float = HL_1H, fetch_state: dict | None = None) -> dict:
    """§3.4 definitions; direction features exclude stickies, duplicates, questions and docs with n_val == 0."""
    fs = fetch_state if fetch_state is not None else load_fetch_state()
    rd_all = [d for d in docs if d.get("src") == "reddit" and (d.get("fetched_ts") or 0) <= as_of]
    rd = lambda w: [d for d in rd_all if _in(d, asset, as_of, w, "reddit") and not d.get("is_sticky") and d.get("dup_of") is None]
    r60, r180, r240 = rd(3600), rd(10800), rd(WINDOW_S)
    dir60, n_a60 = _author_mean(_dir_docs(r60)); dir180, n_a180 = _author_mean(_dir_docs(r180))
    rd_fetch = fs.get("reddit", {}); subs_fresh = [s for s, t in rd_fetch.items() if as_of - t <= 300]
    rd_stale = (as_of - max(rd_fetch.values())) if rd_fetch else None
    rd_block = {"posts_60m": len(r60), "posts_180m": len(r180), "authors_60m": len({d.get("author") for d in r60}),
                "dir_60m": dir60, "dir_180m": dir180, "n_dir_180m": n_a180, "dir_ewma_120": _ewma(_dir_docs(r240), as_of, hl_1h),
                "disag_180m": _disag(_dir_docs(r180)), "z_posts_60m": _z(baselines, "reddit", asset, "posts_60m", as_of, len(r60), log=True),
                "stale_s": (round(rd_stale) if rd_stale is not None else None), "subs_fresh": sorted(subs_fresh),
                "newest_age_s": (round(as_of - max(d["created_ts"] for d in rd_all)) if rd_all else None)}
    st_all = [d for d in docs if d.get("src") == "stocktwits" and (d.get("fetched_ts") or 0) <= as_of]
    st = lambda w: [d for d in st_all if _in(d, asset, as_of, w, "stocktwits") and d.get("dup_of") is None]
    s15, s60 = st(900), st(3600)
    lab_bull = sum(1 for d in s60 if d.get("label") == "Bullish"); lab_bear = sum(1 for d in s60 if d.get("label") == "Bearish")
    ratio = round(lab_bull / (lab_bull + lab_bear), 4) if lab_bull + lab_bear >= 5 else None
    dir_lex, _ = _author_mean(_dir_docs(s60))
    st_fetch = fs.get("stocktwits", {}); sym = SYMBOL_FOR.get(asset, asset)
    st_stale = (as_of - st_fetch[sym]) if sym in st_fetch else None
    users60 = len({d.get("author") for d in s60})
    st_block = {"msgs_15m": len(s15), "msgs_60m": len(s60), "users_60m": users60, "lab_bull_60m": lab_bull, "lab_bear_60m": lab_bear,
                "bull_ratio_60m": ratio, "dir_lex_60m": dir_lex, "dir_ewma_30": _ewma(_dir_docs(st(WINDOW_S)), as_of, hl_15m),
                "disag_60m": _disag(s60, use_label=True), "z_msgs_60m": _z(baselines, "stocktwits", asset, "msgs_60m", as_of, len(s60), log=True),
                "z_users_60m": _z(baselines, "stocktwits", asset, "users_60m", as_of, users60, log=True),
                "z_bull_ratio_60m": _z(baselines, "stocktwits", asset, "bull_ratio_60m", as_of, ratio),
                "stale_s": (round(st_stale) if st_stale is not None else None),
                "newest_age_s": (round(as_of - max(d["created_ts"] for d in st_all)) if st_all else None)}
    news = any(d.get("is_news") for d in r60 if d.get("fetched_ts", 0) <= as_of) or any(d.get("is_news") for d in s15)
    return {"asset": asset, "as_of": as_of, "rd": rd_block, "st": st_block, "news_flag": bool(news)}


def emit(as_of: float | None = None, store: pathlib.Path | None = None, root: pathlib.Path | None = None, baselines: dict | None = None) -> dict:
    """Compute features for all assets at as_of from the store, append to social_features.jsonl, write latest. No network."""
    as_of = as_of or time.time(); store = store or STORE; root = root or ROOT; root.mkdir(parents=True, exist_ok=True)
    if baselines is None:
        try: baselines = json.loads((root / BASELINES.name).read_text(encoding="utf-8"))
        except Exception: baselines = None
    docs = load_window(store, as_of)
    lex = load_lexicon()
    row = {"as_of": as_of, "lex_version": lex.version, "by_asset": {a: features(a, as_of, docs, baselines) for a in ASSETS}}
    with (root / FEATS.name).open("a", encoding="utf-8") as f: f.write(json.dumps(row) + "\n")
    (root / LATEST.name).write_text(json.dumps(row), encoding="utf-8")
    return row


def _flat(b: dict, as_of: float, lex_version: str | None, now: float) -> dict:
    rd, st = b.get("rd", {}), b.get("st", {})
    return {"rd_z_posts_60m": rd.get("z_posts_60m"), "rd_dir_ewma_120": rd.get("dir_ewma_120"), "rd_n_dir_180m": rd.get("n_dir_180m"),
            "st_bull_ratio_60m": st.get("bull_ratio_60m"), "st_z_msgs_60m": st.get("z_msgs_60m"), "st_dir_ewma_30": st.get("dir_ewma_30"),
            "st_disag_60m": st.get("disag_60m"), "st_users_60m": st.get("users_60m"), "st_msgs_15m": st.get("msgs_15m"),
            "rd_newest_age_s": rd.get("newest_age_s"), "st_newest_age_s": st.get("newest_age_s"), "rd_stale_s": rd.get("stale_s"), "st_stale_s": st.get("stale_s"),
            "sent_as_of": as_of, "sent_age_s": round(now - as_of, 1), "lex_version": lex_version, "news_flag": b.get("news_flag"), "rd": rd, "st": st}

def features_now(asset: str, as_of: float | None = None, root: pathlib.Path | None = None) -> dict | None:
    """Decision-time features computed directly from the store at as_of (default now), so the newest fetched posts are always used
    rather than the last emitted snapshot (which can be up to one feed cycle old). Causal: only docs with fetched_ts <= as_of."""
    root = root or ROOT; store = root / STORE.name; as_of = as_of or time.time()
    if not store.exists(): return None
    try:
        docs = load_window(store, as_of)
        bl = json.loads((root / BASELINES.name).read_text(encoding="utf-8")) if (root / BASELINES.name).exists() else None
        b = features(asset, as_of, docs, bl)
        try: lv = (root.parent / "lexicon" / "VERSION").read_text(encoding="utf-8").strip().split("=")[-1]
        except Exception: lv = None
        return _flat(b, as_of, lv, time.time())
    except Exception: return None

def latest(asset: str, root: pathlib.Path | None = None, max_age: float = 900.0) -> dict | None:
    """Flat dict of the s7_live ctx fields for `asset` (plus as_of, sent_age_s, lex_version) or None if missing/stale."""
    root = root or ROOT; p = root / LATEST.name
    if not p.exists(): return None
    try: d = json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None
    now = time.time(); age = now - d["as_of"]
    if age > max_age: return None
    b = d.get("by_asset", {}).get(asset)
    if not b: return None
    rd, st = b.get("rd", {}), b.get("st", {})
    return {"rd_z_posts_60m": rd.get("z_posts_60m"), "rd_dir_ewma_120": rd.get("dir_ewma_120"), "rd_n_dir_180m": rd.get("n_dir_180m"),
            "st_bull_ratio_60m": st.get("bull_ratio_60m"), "st_z_msgs_60m": st.get("z_msgs_60m"), "st_dir_ewma_30": st.get("dir_ewma_30"),
            "st_disag_60m": st.get("disag_60m"), "st_users_60m": st.get("users_60m"), "sent_as_of": d["as_of"], "sent_age_s": round(age, 1),
            "lex_version": d.get("lex_version"), "news_flag": b.get("news_flag"), "rd": rd, "st": st}


# ---------------------------------------------------------------- baselines
def rebuild_baselines(store: pathlib.Path | None = None, as_of: float | None = None, days: int = 7, step_s: float = 900.0, out: pathlib.Path | None = BASELINES, priors_out: pathlib.Path | None = SUB_PRIORS) -> dict:
    """Per (src, asset, feature, hour-of-day) sum/sumsq/n of the raw features sampled on a 15-min grid over the trailing `days`
    (counts stored as log1p). Uses created_ts only (bootstrappable from a limit=100 listing). Also writes sub_priors.json."""
    as_of = as_of or time.time(); store = store or STORE; lo = as_of - days * 86400
    docs = [d for d in read_store(store) if lo - WINDOW_S < (d.get("created_ts") or 0) <= as_of and d.get("dup_of") is None]
    for d in docs: d["fetched_ts"] = 0  # distribution only: ignore visibility for baselines
    stats: dict[str, dict] = {}
    def add(key, v):
        if v is None: return
        b = stats.setdefault(key, {"sum": 0.0, "sumsq": 0.0, "n": 0}); b["sum"] += v; b["sumsq"] += v * v; b["n"] += 1
    t = lo + ((as_of - lo) % step_s)
    first_doc = min((d["created_ts"] for d in docs), default=as_of)
    first_src = {src: min((d["created_ts"] for d in docs if d["src"] == src), default=None) for src in ("reddit", "stocktwits")}
    while t <= as_of:
        if t >= first_doc + 3600:  # only grid points inside the covered span (per source: a 1-h-old source does not get 7 days of zeros)
            h = time.gmtime(t).tm_hour; win = [d for d in docs if t - WINDOW_S < d["created_ts"] <= t]
            ok_rd = first_src["reddit"] is not None and t >= first_src["reddit"] + 3600
            ok_st = first_src["stocktwits"] is not None and t >= first_src["stocktwits"] + 3600
            for a in ASSETS:
                f = features(a, t, win, None, fetch_state={})
                if ok_rd:
                    add(f"reddit|{a}|posts_60m|h{h}", math.log1p(f["rd"]["posts_60m"]))
                    add(f"reddit|{a}|dir_180m|h{h}", f["rd"]["dir_180m"])
                if ok_st:
                    add(f"stocktwits|{a}|msgs_60m|h{h}", math.log1p(f["st"]["msgs_60m"]))
                    add(f"stocktwits|{a}|users_60m|h{h}", math.log1p(f["st"]["users_60m"]))
                    add(f"stocktwits|{a}|bull_ratio_60m|h{h}", f["st"]["bull_ratio_60m"])
        t += step_s
    span_h = max((as_of - first_doc) / 3600, 1e-9) if docs else 0
    pri: dict[str, dict] = {}
    for d in docs:
        p = pri.setdefault(f"{d['src']}:{d['sub']}", {"n": 0, "s_sum": 0.0, "s_sq": 0.0, "n_val": 0, "first": d["created_ts"], "last": d["created_ts"]})
        p["n"] += 1; p["first"] = min(p["first"], d["created_ts"]); p["last"] = max(p["last"], d["created_ts"])
        if d.get("n_val", 0) >= 1 and not d.get("is_sticky"): p["n_val"] += 1; p["s_sum"] += d["s_doc"]; p["s_sq"] += d["s_doc"] ** 2
    priors = {}
    for k, p in pri.items():
        hrs = max((p["last"] - p["first"]) / 3600, 1 / 60); m = p["s_sum"] / p["n_val"] if p["n_val"] else None
        priors[k] = {"n": p["n"], "span_h": round(hrs, 2), "posts_per_h": round(p["n"] / hrs, 3), "s_doc_mean": (round(m, 4) if m is not None else None),
                     "s_doc_sd": (round(math.sqrt(max(p["s_sq"] / p["n_val"] - m * m, 0)), 4) if p["n_val"] else None), "n_val": p["n_val"]}
    base = {"built_at": as_of, "days": days, "step_s": step_s, "n_docs": len(docs), "stats": stats}
    if out is not None: out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(base), encoding="utf-8")
    if priors_out is not None:
        priors_out.parent.mkdir(parents=True, exist_ok=True)
        priors_out.write_text(json.dumps({"built_at": as_of, "days": days, "lex_version": load_lexicon().version, "priors": priors}, indent=1), encoding="utf-8")
    return base


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "baselines": b = rebuild_baselines(); print(json.dumps({k: v for k, v in b.items() if k != "stats"}), len(b["stats"]), "keys")
    else: print(json.dumps(emit(), indent=1))
