"""P31 Kalshi Liquidity Incentive Program (LIP) recorder - READ-ONLY public endpoints, no credentials, no orders.

Every cycle (default 10 min):
  * pages /incentive_programs?status=active (hourly full snapshot -> data/lip/programs/YYYYMMDDHH.jsonl.gz)
  * picks the top-N liquidity programs by reward per target contract
  * records each picked market's order book (depth 10) and the trades since the previous cycle
Output: data/lip/books/YYYYMMDD.jsonl, data/lip/trades/YYYYMMDD.jsonl, data/lip/recorder.log.
Purpose: a paper replay of "quote at the reference price for LIP credit" with real fill (adverse selection) data
before any capital decision. Rate: ~1 req/0.4 s, well under Kalshi's ~3 req/s public limit.
"""
from __future__ import annotations
import argparse, gzip, json, pathlib, time, urllib.parse, urllib.request
from datetime import datetime, timezone

API = "https://api.elections.kalshi.com/trade-api/v2"
ROOT = pathlib.Path("data/lip")
UA = {"User-Agent": "predhft-lip-recorder/1 (read-only research)"}


def _get(path: str, params: dict | None = None, tries: int = 4) -> dict:
    url = f"{API}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
    for i in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(2 + 3 * i); continue
            if e.code == 404: return {}
            if i == tries - 1: raise
        except Exception:
            if i == tries - 1: raise
        time.sleep(1 + i)
    return {}


def active_programs() -> list[dict]:
    out, cur = [], None
    while True:
        p = {"status": "active", "limit": 1000}
        if cur: p["cursor"] = cur
        d = _get("/incentive_programs", p)
        out += d.get("incentive_programs", [])
        cur = d.get("next_cursor") or d.get("cursor")
        if not cur or not d.get("incentive_programs"): break
        time.sleep(0.4)
    return out


def pick(programs: list[dict], n: int) -> list[dict]:
    liq = [p for p in programs if p.get("incentive_type") == "liquidity" and p.get("market_ticker")]
    def score(p):
        try: return float(p.get("period_reward") or 0) / max(1.0, float(p.get("target_size_fp") or 1))
        except Exception: return 0.0
    best = {}
    for p in sorted(liq, key=score, reverse=True):
        best.setdefault(p["market_ticker"], p)
        if len(best) >= n: break
    return list(best.values())


def _append(path: pathlib.Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f: f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def _log(msg: str) -> None:
    _append(ROOT / "recorder.log.jsonl", {"t": time.time(), "msg": msg})


def cycle(n: int, state: dict) -> dict:
    t0 = time.time(); day = datetime.now(timezone.utc).strftime("%Y%m%d"); hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    progs = active_programs()
    snap = ROOT / "programs" / f"{hour}.jsonl.gz"
    if not snap.exists():
        snap.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(snap, "wt", encoding="utf-8") as f:
            for p in progs: f.write(json.dumps(p, separators=(",", ":")) + "\n")
    sel = pick(progs, n); nb = nt = 0
    last = state.setdefault("last_trade_ts", {})
    for p in sel:
        tk = p["market_ticker"]
        ob = _get(f"/markets/{tk}/orderbook", {"depth": 10}); time.sleep(0.4)
        _append(ROOT / "books" / f"{day}.jsonl", {"t": time.time(), "ticker": tk, "program_id": p.get("id"), "period_reward": p.get("period_reward"),
                                                   "target_size_fp": p.get("target_size_fp"), "discount_factor_bps": p.get("discount_factor_bps"),
                                                   "end_date": p.get("end_date"), "book": ob.get("orderbook_fp") or ob.get("orderbook")}); nb += 1
        min_ts = int(last.get(tk, t0 - 600))
        tr = _get("/markets/trades", {"ticker": tk, "min_ts": min_ts, "limit": 1000}).get("trades", []); time.sleep(0.4)
        for x in tr: _append(ROOT / "trades" / f"{day}.jsonl", {"t_rec": time.time(), **x}); nt += 1
        last[tk] = int(t0)
    _log(f"cycle programs={len(progs)} picked={len(sel)} books={nb} trades={nt} secs={time.time() - t0:.1f}")
    return state


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=150); ap.add_argument("--every", type=float, default=600.0)
    ap.add_argument("--once", action="store_true"); a = ap.parse_args()
    st_p = ROOT / "state.json"; state = json.loads(st_p.read_text()) if st_p.exists() else {}
    _log(f"start n={a.n} every={a.every}")
    while True:
        t = time.time()
        try: state = cycle(a.n, state); st_p.write_text(json.dumps(state))
        except Exception as e: _log(f"error {type(e).__name__}: {e}")
        if a.once: break
        time.sleep(max(5.0, a.every - (time.time() - t)))


if __name__ == "__main__":
    main()
