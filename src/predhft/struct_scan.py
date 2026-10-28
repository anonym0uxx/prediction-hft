"""P21 structural-edge scanner (scored-only, zero-cost, no orders). docs/LAYERX_LESSONS_2026-10-27.md §4.

Every --period seconds (default 60) it pulls the open-market lists of a few Kalshi crypto series that settle on the SAME number
(the 60-s CF Benchmarks average at a given close) and looks for portfolios whose payoff is CONSTANT across every settlement value
but whose cost at the asks is below that payoff after taker fees:
  (a) yes_no      : YES ask + NO ask < 1 on one market (impossible on Kalshi's unified book unless crossed; kept as a sanity check)
  (b) ladder      : nested events (above K2 => above K1, 15M-up => above K when K < open reference, ...): bid(inner) > ask(outer)
  (c) crosshz     : identical / complementary events across series or horizons (KXBTC 'greater K' == KXBTCD 'above K',
                    KXBTC 'less K' == NOT KXBTCD 'above K-0.01', KXBTC15M at an :00 close vs the hourly ladder), disjoint-union
                    triples (bucket [a,b) + above b == above a) and the full range partition (sum of YES asks < 1, sum of NO asks < n-1)
  (d) reslag      : windows whose outcome is already LOCKED by the official feed (remaining 60-s-average move needed exceeds
                    1.5x the trailing-24h empirical envelope of the official feed's forward s-second average) while the favoured side is still offered < .99.
                    Measured from the P13 runners' final-minute book snapshots (zero extra requests) and the data/cfb feed.
Depth-aware sizing: when a top-of-book candidate has gross margin >= 0 the scanner fetches the legs' order books in one batched
request (/markets/orderbooks?tickers=...), walks the asks level by level and reports the largest size whose MARGINAL cost + fee is
still below the payoff (executable size), gross and net profit at that size, and top-3 depth per leg.
Each hit is appended to data/struct/hits.jsonl with a stable hit_id and first_seen so persistence = last_seen - first_seen + period.
Rate: own client min_interval 1.0 s + the shared cross-process limiter (never the priority lane) -> <= 1 req/s from this process.

Usage: uv run python -m predhft.struct_scan --loop            # runner
       uv run python -m predhft.struct_scan --once --verbose   # one cycle
       uv run python -m predhft.struct_scan --report [--since-hours 1]
       uv run python -m predhft.struct_scan --offline-lag      # (d) over the whole recorded P13 tape
"""
from __future__ import annotations
import argparse, collections, datetime as dt, glob, json, math, pathlib, statistics as st, time
from .fees import KALSHI_TAKER_THETA

ROOT = pathlib.Path("data/struct"); INF = 10 ** 12
SERIES = {"BTC": ["KXBTCD", "KXBTC", "KXBTC15M"], "ETH": ["KXETHD", "KXETH", "KXETH15M"]}
P13_BOOKS = {"BTC": "data/s7p13-KXBTC15M/books.jsonl", "ETH": "data/s7p13-KXETH15M/books.jsonl", "SOL": "data/s7p13-KXSOL15M/books.jsonl"}
LOCK_MULT = 1.5          # locked iff the required move of the remaining average > LOCK_MULT x empirical envelope (trailing 24 h) at that horizon
LAG_ASK_MAX = 0.99       # (d) hit iff the favoured side is still offered below this

# ---------------------------------------------------------------- fees (Kalshi fee schedule, effective 2026-07-07; docs.kalshi.com fee_rounding)
def fee_exact(contracts: float, price: float, theta: float = KALSHI_TAKER_THETA, precision: float = 0.0001) -> float:
    """Taker fee in dollars for `contracts` at `price`: theta*C*P*(1-P), trade fee rounded UP to $0.000001, then the balance is
    aligned to `precision` ($0.0001 direct members, $0.01 non-direct) by a rounding fee. Upper bound: ceil to `precision`."""
    if contracts <= 0 or not (0 < price < 1): return 0.0
    raw = theta * contracts * price * (1 - price)
    return math.ceil(raw / precision - 1e-9) * precision

def fee_pc(price: float) -> float:
    """Per-contract fee without rounding (used for marginal-unit decisions)."""
    return KALSHI_TAKER_THETA * price * (1 - price) if 0 < price < 1 else 0.0

# ---------------------------------------------------------------- events on the settlement value (integer cents)
def cents(x) -> int: return int(round(float(x) * 100))

def interval(m: dict) -> tuple[int, int] | None:
    """Half-open [lo, hi) in cents of the settlement average for which the market resolves YES. Rules (universe.db, rules_primary):
    greater K: A > K; less K: A < K; between a,b: a <= A <= b; greater_or_equal R (15M): A >= R. The index is published to 2 dp."""
    stt = m.get("strike_type"); fl, cp = m.get("floor_strike"), m.get("cap_strike")
    if stt == "greater" and fl is not None: return (cents(fl) + 1, INF)
    if stt == "greater_or_equal" and fl is not None: return (cents(fl), INF)
    if stt == "less" and cp is not None: return (-INF, cents(cp))
    if stt == "less_or_equal" and cp is not None: return (-INF, cents(cp) + 1)
    if stt == "between" and fl is not None and cp is not None: return (cents(fl), cents(cp) + 1)
    return None

def touch(m: dict) -> tuple[float, float]:
    """(yes_bid, yes_ask) in dollars from a market list row; missing side -> 0 / 1."""
    def fv(k, d):
        try: return float(m.get(k)) if m.get(k) not in (None, "") else d
        except (TypeError, ValueError): return d
    return fv("yes_bid_dollars", 0.0), fv("yes_ask_dollars", 1.0)

def leg_ask(m: dict, side: str) -> float:
    yb, ya = touch(m); return ya if side == "yes" else 1 - yb

# ---------------------------------------------------------------- portfolio enumeration (payoff constant >= 1 by construction)
def _rel(a: tuple, b: tuple) -> str:
    if a == b: return "equal"
    if a[0] <= b[0] and b[1] <= a[1]: return "a_contains_b"
    if b[0] <= a[0] and a[1] <= b[1]: return "b_contains_a"
    if a[1] <= b[0] or b[1] <= a[0]: return "disjoint"
    return "overlap"

def _covers_all(a: tuple, b: tuple) -> bool:
    lo, hi = (a, b) if a[0] <= b[0] else (b, a)
    return lo[0] == -INF and hi[1] == INF and hi[0] <= lo[1]

def candidates(markets: list[dict]) -> list[dict]:
    """All constant-payoff portfolios among markets sharing one settlement value. Returns dicts with legs [(ticker, side)], payoff,
    cost (top-of-book asks), fee, net_pc (per contract), kind."""
    ms = [(m, iv) for m in markets if (iv := interval(m))]
    out = []
    def add(kind, legs, payoff):
        cost = sum(leg_ask(m, s) for m, s in legs); fee = sum(fee_pc(leg_ask(m, s)) for m, s in legs)
        out.append({"kind": kind, "legs": [(m["ticker"], s) for m, s in legs], "payoff": payoff, "cost": round(cost, 4), "fee_pc": round(fee, 5), "gross_pc": round(payoff - cost, 4), "net_pc": round(payoff - cost - fee, 5)})
    for m, iv in ms:                                                        # (a) same market YES + NO
        add("yes_no", [(m, "yes"), (m, "no")], 1.0)
    n = len(ms)
    for i in range(n):
        mi, a = ms[i]
        for j in range(i + 1, n):
            mj, b = ms[j]; r = _rel(a, b)
            if r == "equal":
                add("crosshz_equal", [(mi, "yes"), (mj, "no")], 1.0); add("crosshz_equal", [(mi, "no"), (mj, "yes")], 1.0)
            elif r == "a_contains_b": add("ladder", [(mi, "yes"), (mj, "no")], 1.0)        # outer YES + inner NO: payoff 1 or 2
            elif r == "b_contains_a": add("ladder", [(mj, "yes"), (mi, "no")], 1.0)
            if r == "disjoint":
                add("crosshz_disjoint", [(mi, "no"), (mj, "no")], 1.0)                      # at most one can be YES
                if _covers_all(a, b): add("crosshz_complement", [(mi, "yes"), (mj, "yes")], 1.0)
    by_iv: dict[tuple, list] = collections.defaultdict(list)
    for m, iv in ms: by_iv[iv].append(m)
    by_lo: dict[int, list] = collections.defaultdict(list)
    for m, iv in ms: by_lo[iv[0]].append((m, iv))
    for mx, x in ms:                                                        # disjoint union: X + Y == Z
        for my, y in by_lo.get(x[1], []):
            for mz in by_iv.get((x[0], y[1]), []):
                add("crosshz_union", [(mx, "yes"), (my, "yes"), (mz, "no")], 1.0)
                add("crosshz_union", [(mx, "no"), (my, "no"), (mz, "yes")], 2.0)
    groups: dict[tuple, list] = collections.defaultdict(list)             # full partition per (series, close): KXBTC less/between/greater
    for m, iv in ms: groups[(m["ticker"].split("-")[0], m.get("close_time"))].append((m, iv))
    for key, g in groups.items():
        g.sort(key=lambda t: t[1][0])
        if len(g) >= 3 and g[0][1][0] == -INF and g[-1][1][1] == INF and all(g[k][1][1] == g[k + 1][1][0] for k in range(len(g) - 1)):
            add("partition_yes", [(m, "yes") for m, _ in g], 1.0); add("partition_no", [(m, "no") for m, _ in g], float(len(g) - 1))
    return out

# ---------------------------------------------------------------- depth-aware execution
def asks_from_book(ob: dict, side: str) -> list[tuple[float, float]]:
    """Ascending (price, size) asks for buying `side`: YES asks = 1 - NO bids; NO asks = 1 - YES bids (unified Kalshi book)."""
    o = ob.get("orderbook_fp") or ob
    bids = o.get("no_dollars" if side == "yes" else "yes_dollars") or []
    lv = sorted(((round(1 - float(p), 4), float(q)) for p, q in bids if float(q) > 0))
    return lv

def executable(legs_levels: list[list[tuple[float, float]]], payoff: float, max_levels: int = 3, max_units: int = 100000) -> dict:
    """Walk the legs' asks unit by unit (top `max_levels` per leg); executable size = largest q whose MARGINAL cost + fee < payoff.
    Returns size, gross, net ($), vwap per leg, depth per leg (top-3 total)."""
    legs = [lv[:max_levels] for lv in legs_levels]
    if any(not lv for lv in legs): return {"size": 0, "gross": 0.0, "net": 0.0, "vwap": [], "depth": [sum(q for _, q in lv) for lv in legs]}
    cap = min(sum(q for _, q in lv) for lv in legs); cap = min(int(cap), max_units)
    idx = [0] * len(legs); used = [0.0] * len(legs); size = 0; gross = net = 0.0; spent = [0.0] * len(legs)
    while size < cap:
        prices = []
        for k, lv in enumerate(legs):
            while idx[k] < len(lv) and used[k] >= lv[idx[k]][1]: used[k] -= lv[idx[k]][1]; idx[k] += 1
            if idx[k] >= len(lv): prices = None; break
            prices.append(lv[idx[k]][0])
        if prices is None: break
        c = sum(prices); f = sum(fee_pc(p) for p in prices)
        if c + f >= payoff: break
        size += 1; gross += payoff - c; net += payoff - c - f
        for k, p in enumerate(prices): used[k] += 1; spent[k] += p
    return {"size": size, "gross": round(gross, 4), "net": round(net, 4), "vwap": [round(s / size, 4) if size else None for s in spent],
            "depth": [sum(q for _, q in lv) for lv in legs]}

# ---------------------------------------------------------------- (d) resolution lag from the P13 tape + official feed
ENV_S = (1, 3, 5, 10, 15, 20, 30, 45, 60)
def envelope_bp(asset: str, hours: float = 24.0, root: pathlib.Path = pathlib.Path("data/cfb")) -> dict[int, float] | None:
    """Empirical envelope of the official index: for each horizon s, the maximum |log(mean of the NEXT s 1-Hz ticks / current tick)| in bp
    over the trailing `hours` (gap-free windows only). This is what the remaining average has ever done in s seconds."""
    p = root / f"{asset}.jsonl"
    if not p.exists(): return None
    cut = time.time() - hours * 3600; secs = []; px = []
    with p.open(encoding="utf-8") as fh:
        for l in fh:
            try: r = json.loads(l)
            except Exception: continue
            if r.get("t", 0) >= cut and r.get("value") and r.get("value_ts_ms"): secs.append(r["value_ts_ms"] // 1000); px.append(r["value"])
    n = len(px)
    if n < 600: return None
    pre = [0.0] * (n + 1)
    for i, v in enumerate(px): pre[i + 1] = pre[i] + v
    env = {}
    for s in ENV_S:
        mx = 0.0
        for i in range(0, n - s):
            if secs[i + s] - secs[i] != s: continue
            mx = max(mx, abs(math.log(((pre[i + s + 1] - pre[i + 1]) / s) / px[i])) * 1e4)
        env[s] = round(mx, 3)
    return env

def env_at(env: dict[int, float], s_left: float) -> float:
    """Envelope at horizon s_left (next tabulated horizon up, i.e. conservative)."""
    for s in ENV_S:
        if s >= s_left: return env.get(s, max(env.values()))
    return env.get(60, max(env.values()))

def lock_state(snap: dict, env: dict[int, float], mult: float = LOCK_MULT) -> dict | None:
    """Given a final-minute snapshot (spot, K, partial_twap, partial_n, mins_left) decide whether the outcome is locked:
    the remaining s-second average would have to move by req_bp relative to the current index; the official feed has never
    moved its s-second forward average by more than env(s) bp; locked iff req_bp > mult x env(s)."""
    pt, pn, K, spot = snap.get("partial_twap"), snap.get("partial_n") or 0, snap.get("K"), snap.get("spot")
    if not (pt and K and spot and pn >= 3): return None
    s_left = max(min(snap["mins_left"] * 60.0, 60.0), 0.0); r = max(s_left / 60.0, 1e-3)
    k_eff = (K - (1 - r) * pt) / r
    if k_eff <= 0: req_bp = 1e9; side = "yes"
    else: req_bp = abs(math.log(spot / k_eff)) * 1e4; side = "yes" if spot > k_eff else "no"
    bound_bp = mult * env_at(env, s_left)
    return {"side": side, "req_bp": round(req_bp, 2), "bound_bp": round(bound_bp, 2), "locked": req_bp > bound_bp, "secs_left": round(s_left, 1)}

def lag_hits_from_snaps(snaps: list[dict], env: dict[int, float], lot: int = 10) -> list[dict]:
    """One hit per (ticker) lock episode: first locked snapshot where the favoured side's ask < LAG_ASK_MAX, persistence until the last."""
    by_tk: dict[str, list] = collections.defaultdict(list)
    for s in snaps:
        if s.get("mins_left") is None or s["mins_left"] >= 1.0 or s.get("spot_src") != "cfb": continue
        by_tk[s["ticker"]].append(s)
    hits = []
    for tk, ss in by_tk.items():
        ss.sort(key=lambda s: s["t"]); ep = None
        for s in ss:
            L = lock_state(s, env)
            ask = (s["yes_ask"] if L and L["side"] == "yes" else (1 - s["yes_bid"] if s.get("yes_bid", 0) > 0 else 1.0)) if L else None
            size = (s["ask_size"] if L and L["side"] == "yes" else s.get("bid_size", 0)) if L else 0
            on = bool(L and L["locked"] and ask is not None and 0 < ask < LAG_ASK_MAX and size >= lot)
            if on and ep is None:
                ep = {"kind": "reslag", "ticker": tk, "side": L["side"], "first_seen": s["t"], "last_seen": s["t"], "secs_left_first": L["secs_left"], "ask_first": round(ask, 4), "size_first": size,
                      "req_bp": L["req_bp"], "bound_bp": L["bound_bp"], "partial_n": s.get("partial_n"), "n_obs": 1, "min_ask": round(ask, 4), "close_ts": s.get("close_ts")}
            elif on: ep["last_seen"] = s["t"]; ep["n_obs"] += 1; ep["min_ask"] = min(ep["min_ask"], round(ask, 4)); ep["secs_left_last"] = L["secs_left"]
            elif ep is not None: hits.append(ep); ep = None
        if ep is not None: hits.append(ep)
    for h in hits:
        h["persist_s"] = round(h["last_seen"] - h["first_seen"], 2); h["gross_pc"] = round(1 - h["ask_first"], 4); h["net_pc"] = round(1 - h["ask_first"] - fee_pc(h["ask_first"]), 4)
    return hits

def tail_snaps(path: str, since: float) -> list[dict]:
    p = pathlib.Path(path)
    if not p.exists(): return []
    out = []
    with p.open("rb") as fh:
        try: fh.seek(-min(p.stat().st_size, 6_000_000), 2)
        except OSError: pass
        for l in fh.read().decode("utf-8", "ignore").splitlines()[1:]:
            try: s = json.loads(l)
            except Exception: continue
            if s.get("t", 0) >= since and not s.get("stale_fill") and not s.get("post_fill_check"): out.append(s)
    return out

# ---------------------------------------------------------------- scanner
class Scanner:
    def __init__(self, root: pathlib.Path = ROOT, period: float = 60.0, assets: tuple = ("BTC", "ETH"), verbose: bool = False):
        from .kalshi_public import KalshiPublic
        import os
        os.environ.pop("PREDHFT_PRIORITY", None)                       # never the priority lane
        self.k = KalshiPublic(min_interval_s=1.0); self.root = pathlib.Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.period = period; self.assets = assets; self.verbose = verbose; self.active: dict[str, dict] = {}; self.m1s: dict[str, tuple[float, float]] = {}
        self.hits = self.root / "hits.jsonl"; self.log = self.root / "scan.log"; self.lag_seen: set = set(); self.lag_since = time.time() - 900
    def logline(self, m):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {m}"; print(line, flush=True); self.log.open("a", encoding="utf-8").write(line + "\n")
    def _env(self, asset: str) -> dict[int, float]:
        v = self.m1s.get(asset)
        if not v or time.time() - v[0] > 1800: self.m1s[asset] = (time.time(), envelope_bp(asset) or {s: 25.0 for s in ENV_S}); v = self.m1s[asset]
        return v[1]
    def fetch_markets(self) -> tuple[dict, int]:
        by_close: dict[tuple, list] = collections.defaultdict(list); req = 0
        for asset in self.assets:
            for s in SERIES[asset]:
                try: ms = list(self.k.markets(series_ticker=s, status="open", limit=1000, max_pages=1)); req += 1
                except Exception as e: self.logline(f"markets error {s}: {type(e).__name__} {str(e)[:80]}"); continue
                for m in ms:
                    if interval(m): by_close[(asset, m["close_time"])].append(m)
        return by_close, req
    def books(self, tickers: list[str]) -> dict[str, dict]:
        out = {}; tickers = sorted(set(tickers))
        for i in range(0, len(tickers), 10):
            try:
                d = self.k.get("/markets/orderbooks", tickers=tickers[i:i + 10])
                for ob in d.get("orderbooks", []): out[ob["ticker"]] = ob
            except Exception as e: self.logline(f"orderbooks error {type(e).__name__} {str(e)[:80]}")
        return out
    def cycle(self) -> dict:
        t0 = time.time(); by_close, req = self.fetch_markets(); n_cand = 0; n_port = 0; hits = []
        for (asset, close), ms in by_close.items():
            ports = candidates(ms); n_port += len(ports)
            cands = sorted([p for p in ports if p["gross_pc"] > (0 if p["kind"] == "yes_no" else -1e-9)], key=lambda p: -p["net_pc"])
            n_cand += len(cands)
            if not cands: continue
            tks = []
            for p in cands:                                               # depth budget: <= 30 tickers (3 batched requests) per close
                if len(set(tks) | {tk for tk, _ in p["legs"]}) > 30: break
                tks += [tk for tk, _ in p["legs"]]
            cands = [p for p in cands if all(tk in tks for tk, _ in p["legs"])]
            obs = self.books(tks); req += (len(set(tks)) + 9) // 10
            for p in cands:
                lv = [asks_from_book(obs.get(tk, {}), s) for tk, s in p["legs"]]
                ex = executable(lv, p["payoff"])
                row = {"t": t0, "asset": asset, "close_time": close, **p, "legs": [list(l) for l in p["legs"]], "exec_size": ex["size"], "exec_gross": ex["gross"], "exec_net": ex["net"], "vwap": ex["vwap"], "depth_top3": ex["depth"]}
                if ex["size"] > 0 and ex["net"] > 0: hits.append(row)
                elif self.verbose: self.logline(f"cand {p['kind']} {p['legs']} gross_pc={p['gross_pc']} net_pc={p['net_pc']} exec_size={ex['size']} (not executable after fees/depth)")
        # (d) from the P13 tape, zero requests
        for asset in ("BTC", "ETH", "SOL"):
            for h in lag_hits_from_snaps(tail_snaps(P13_BOOKS[asset], self.lag_since), self._env(asset)):
                key = f"reslag|{h['ticker']}|{int(h['first_seen'])}"
                if key in self.lag_seen: continue
                if time.time() - h["last_seen"] < 5: continue                # episode may still be running; pick it up next cycle
                self.lag_seen.add(key); h.update({"t": t0, "asset": asset, "hit_id": key, "legs": [[h["ticker"], h["side"]]], "exec_size": h["size_first"], "exec_net": round(h["net_pc"] * min(h["size_first"], 100), 4)})
                self.hits.open("a", encoding="utf-8").write(json.dumps(h) + "\n"); self.logline(f"HIT reslag {h['ticker']} {h['side']} ask={h['ask_first']} size={h['size_first']} secs_left={h['secs_left_first']} req={h['req_bp']}bp bound={h['bound_bp']}bp persist={h['persist_s']}s")
        self.lag_since = t0 - 2 * self.period
        # persistence bookkeeping for (a)-(c)
        now_ids = set()
        for row in hits:
            hid = row["kind"] + "|" + "|".join(f"{tk}:{s}" for tk, s in row["legs"]); now_ids.add(hid)
            a = self.active.get(hid)
            row["hit_id"] = hid; row["first_seen"] = a["first_seen"] if a else t0; row["n_obs"] = (a["n_obs"] + 1) if a else 1; row["persist_s"] = round(t0 - row["first_seen"], 1)
            self.active[hid] = {"first_seen": row["first_seen"], "n_obs": row["n_obs"], "last": t0}
            self.hits.open("a", encoding="utf-8").write(json.dumps(row) + "\n")
            self.logline(f"HIT {row['kind']} {row['legs']} cost={row['cost']} payoff={row['payoff']} net_pc={row['net_pc']} exec_size={row['exec_size']} exec_net=${row['exec_net']} persist={row['persist_s']}s")
        for hid in [h for h in self.active if h not in now_ids]:
            a = self.active.pop(hid); self.logline(f"END {hid} persisted {a['last'] - a['first_seen'] + self.period:.0f}s over {a['n_obs']} obs")
        summ = {"t": t0, "closes": len(by_close), "markets": sum(len(v) for v in by_close.values()), "portfolios": n_port, "cands_top": n_cand, "hits": len(hits), "requests": req, "secs": round(time.time() - t0, 1)}
        (self.root / "cycles.jsonl").open("a", encoding="utf-8").write(json.dumps(summ) + "\n")
        if self.verbose or hits: self.logline(f"cycle {summ}")
        return summ

def report(root: pathlib.Path = ROOT, since_hours: float | None = None) -> dict:
    p = root / "hits.jsonl"; rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
    if since_hours: rows = [r for r in rows if r["t"] >= time.time() - since_hours * 3600]
    cyc = root / "cycles.jsonl"; cycles = [json.loads(l) for l in cyc.read_text(encoding="utf-8").splitlines() if l.strip()] if cyc.exists() else []
    if since_hours: cycles = [c for c in cycles if c["t"] >= time.time() - since_hours * 3600]
    last: dict[str, dict] = {}
    for r in rows: last[r.get("hit_id") or r["kind"] + str(r["legs"])] = r
    out = {"cycles": len(cycles), "requests": sum(c["requests"] for c in cycles), "portfolios_per_cycle": (st.median(c["portfolios"] for c in cycles) if cycles else 0), "by_kind": {}}
    for kind in sorted({r["kind"] for r in last.values()}):
        hs = [r for r in last.values() if r["kind"] == kind]
        out["by_kind"][kind] = {"n": len(hs), "median_persist_s": st.median(r.get("persist_s", 0) for r in hs), "median_net_pc": st.median(r["net_pc"] for r in hs), "median_exec_size": st.median(r.get("exec_size", 0) for r in hs), "sum_exec_net_usd": round(sum(r.get("exec_net", 0) for r in hs), 2)}
    return out

def offline_lag(lot: int = 10) -> dict:
    out = {}
    for asset, path in P13_BOOKS.items():
        p = pathlib.Path(path)
        if not p.exists(): continue
        snaps = []
        for l in p.open(encoding="utf-8", errors="ignore"):
            try: s = json.loads(l)
            except Exception: continue
            if s.get("mins_left") is not None and s["mins_left"] < 1.0 and s.get("spot_src") == "cfb": snaps.append(s)
        env = envelope_bp(asset, hours=1e6) or {s: 25.0 for s in ENV_S}
        hits = lag_hits_from_snaps(snaps, env, lot)
        windows = {s["ticker"] for s in snaps}; locked_any = set(); lock_secs = []
        for s in snaps:
            L = lock_state(s, env)
            if L and L["locked"]: locked_any.add(s["ticker"]); lock_secs.append(L["secs_left"])
        out[asset] = {"windows_cfb": len(windows), "windows_locked_any": len(locked_any), "envelope_bp": env, "hits": len(hits),
                      "median_persist_s": (st.median(h["persist_s"] for h in hits) if hits else None), "median_ask": (st.median(h["ask_first"] for h in hits) if hits else None),
                      "median_net_pc": (st.median(h["net_pc"] for h in hits) if hits else None), "median_size": (st.median(h["size_first"] for h in hits) if hits else None),
                      "median_secs_left_at_first_lock": (st.median(lock_secs) if lock_secs else None), "examples": hits[:3]}
    return out

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--loop", action="store_true"); ap.add_argument("--once", action="store_true"); ap.add_argument("--report", action="store_true")
    ap.add_argument("--offline-lag", action="store_true"); ap.add_argument("--period", type=float, default=60.0); ap.add_argument("--since-hours", type=float, default=None)
    ap.add_argument("--assets", default="BTC,ETH"); ap.add_argument("--verbose", action="store_true"); ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args()
    if a.report: print(json.dumps(report(pathlib.Path(a.root), a.since_hours), indent=1))
    elif a.offline_lag: print(json.dumps(offline_lag(), indent=1))
    else:
        from .s7_live import single_instance
        _lock = single_instance(pathlib.Path(a.root))
        sc = Scanner(pathlib.Path(a.root), a.period, tuple(a.assets.split(",")), a.verbose); sc.logline(f"start period={a.period}s assets={a.assets} loop={a.loop}")
        while True:
            try: s = sc.cycle()
            except KeyboardInterrupt: raise
            except Exception as e: sc.logline(f"error {type(e).__name__}: {str(e)[:160]}"); s = {"secs": 0}
            if not a.loop: break
            time.sleep(max(5.0, a.period - s.get("secs", 0)))
