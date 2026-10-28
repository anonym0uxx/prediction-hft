"""Tail readers for the 1 Hz feed files (data/cfb/<ASSET>.jsonl, data/replica/<ASSET>.jsonl). No heavy imports (safe inside
the runner loop and in pure tests): seek to the last `max_bytes`, parse complete JSON lines, return rows with t >= since."""
from __future__ import annotations
import json, math, pathlib

CFB = pathlib.Path("data/cfb"); REPLICA = pathlib.Path("data/replica")

def tail_rows(path: pathlib.Path, since: float, max_bytes: int = 64_000) -> list[dict]:
    """Rows with row['t'] >= since from the end of a jsonl file (partial first line dropped). [] if the file is missing."""
    path = pathlib.Path(path)
    if not path.exists(): return []
    out = []
    with path.open("rb") as fh:
        fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - max_bytes)); chunk = fh.read()
    lines = chunk.split(b"\n")
    if size > max_bytes: lines = lines[1:]
    for l in lines:
        if not l.strip(): continue
        try: r = json.loads(l)
        except Exception: continue
        if isinstance(r, dict) and r.get("t") is not None and r["t"] >= since: out.append(r)
    return out

def cfb_recent(asset: str, seconds: float, now: float, root: pathlib.Path = CFB) -> list[dict]:
    return tail_rows(root / f"{asset}.jsonl", now - seconds, max_bytes=int(400 * (seconds + 10)))

def move_bp(rows: list[dict], key: str, now: float, horizon_s: float) -> float | None:
    """Log move (bp) of `key` from the first row at/after now-horizon to the last row. None if < 2 usable rows or span < 0.5*horizon."""
    xs = [(r["t"], r[key]) for r in rows if r.get(key) and r["t"] >= now - horizon_s]
    if len(xs) < 2: return None
    (t0, v0), (t1, v1) = xs[0], xs[-1]
    if t1 - t0 < 0.5 * horizon_s or v0 <= 0 or v1 <= 0: return None
    return 1e4 * math.log(v1 / v0)

def sigma_1m_from_rows(rows: list[dict], key: str = "value", min_minutes: int = 30) -> float | None:
    """Trailing per-minute log-return sd from 1 Hz rows (bucketed to minute closes). None if fewer than min_minutes minutes."""
    by_min: dict[int, float] = {}
    for r in rows:
        v = r.get(key)
        if v: by_min[int(r["t"] // 60)] = float(v)
    ks = sorted(by_min)
    rets = [math.log(by_min[b] / by_min[a]) for a, b in zip(ks, ks[1:]) if b - a == 1 and by_min[a] > 0]
    if len(rets) < min_minutes: return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((x - m) ** 2 for x in rets) / len(rets))
