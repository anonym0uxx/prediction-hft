"""Scoreboard: Brier skill of each inference family vs the market mid at decision time, once outcomes resolve.
Reads data/*/scoreboard.jsonl rows {t, family, ticker, p_model|p_market...}, caches results in data/scoreboard-results.json.
Usage: uv run python -m predhft.scoreboard            (prints per-family table; used by the daily summary)
"""
from __future__ import annotations
import json, math, pathlib, statistics as st, time
from .kalshi_public import KalshiPublic

ROOT = pathlib.Path("data"); CACHE = ROOT / "scoreboard-results.json"
def load_rows():
    rows = []
    for p in ROOT.glob("*/scoreboard.jsonl"):
        for l in p.read_text().splitlines():
            if l.strip():
                r = json.loads(l); r.setdefault("family", p.parent.name); rows.append(r)
    return rows

def results_for(tickers: set[str], k: KalshiPublic) -> dict[str, str]:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for t in tickers:
        if t in cache: continue
        try:
            m = k.get(f"/markets/{t}")["market"]
            if m.get("result") in ("yes", "no"): cache[t] = m["result"]
        except Exception: pass
    CACHE.write_text(json.dumps(cache)); return cache

def table(rows, res) -> list[str]:
    out = []
    fams = sorted({r["family"] for r in rows})
    for fam in fams:
        rs = [r for r in rows if r["family"] == fam and r["ticker"] in res and r.get("p_model") is not None and r.get("p_market") is not None]
        if not rs: out.append(f"{fam}: no resolved scored rows yet"); continue
        bm = [(r["p_model"] - (1 if res[r["ticker"]] == "yes" else 0)) ** 2 for r in rs]
        bk = [(r["p_market"] - (1 if res[r["ticker"]] == "yes" else 0)) ** 2 for r in rs]
        d = [b - a for a, b in zip(bm, bk)]  # positive = model better
        se = st.pstdev(d) / math.sqrt(len(d)) if len(d) > 1 else 0
        bss = 1 - st.fmean(bm) / st.fmean(bk) if st.fmean(bk) > 0 else float("nan")
        out.append(f"{fam}: n={len(rs)} Brier model={st.fmean(bm):.4f} market={st.fmean(bk):.4f} BSS vs market={bss:+.3f} (diff {st.fmean(d)*1e4:+.1f}±{se*1e4:.1f} e-4)")
    return out

if __name__ == "__main__":
    rows = load_rows(); k = KalshiPublic(min_interval_s=0.2)
    # only query tickers whose decision time is > 1h old, cap per run
    old = {r["ticker"] for r in rows if time.time() - r["t"] > 3600}
    res = results_for(set(list(old)[:400]), k)
    print("\n".join(table(rows, res)))
