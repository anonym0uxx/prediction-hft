"""P18 — Thompson-sampling lever selection for the P13/P16 final-minute rule (docs/TS_LEVERS_2026-10-27.md, PREREG P18).

Idea. The levers of the final-minute favourite rule (edge threshold, decision timing, favourite cap, order style) are not learned
from bandit feedback: every settled 15M window is replayed from the recorded book snapshots (data/<family>/books.jsonl) and the
official result, and the realised per-contract P&L EVERY arm would have made is computed (`replay_window`). So all arms get a
full-information update every window; Thompson sampling only decides which arm places the single live paper bet and reports the
posterior probability that each arm is best. Paper only, CPU only, no network inside the replay.

Arms (36, full factorial over the four levers that interact mechanically; see docs for the pruning argument):
    edge  E in {3,5,7} c      : p_cal - fill price - taker fee per contract >= E
    time  W in {early T-55..T-30, mid T-40..T-10, late T-25..T-10} seconds before close
    cap   C in {0.95, 0.97}   : favourite side offered (take) / bid (join) at <= C
    order O in {take, join}   : take = ask + 1 tick (and the next snapshot must still satisfy the cap: the live re-check);
                                join = rest at the best bid of our side, filled only if a LATER snapshot shows the ask of our side
                                strictly below our bid (someone sold through us; adverse selection is in the data), maker fee 0
Size is NOT an arm: it leaves per-contract P&L unchanged, so it cannot be learned from per-contract posteriors. It is derived:
    mult = 1.0 if P(mu_arm > 0) >= SIZE_FULL_PBEST and n_cl >= N_MIN else 0.5 (risk.size remains the ceiling).
Hour filter is NOT an arm either: it is a mask over the same replays (all / US-day 06-16 PT / high-vol PT hours frozen from
the data at first bootstrap), reported as a stratified posterior; promotion to the grid only per PREREG P18.

Posterior: Normal-Normal with known-variance approximation on per-contract P&L in cents, one observation per settlement close
(the three 15M assets share close_ts and have .7-.9 correlated errors, so a close is the cluster: its observation is the mean over
the assets the arm traded). mu0 = 0 (market calibrated), tau0 = 10 c; sigma^2 = pooled cluster variance shrunk to 35 c with 5
pseudo-clusters, floored at 15 c.

State: data/ts/levers_state.json (obs per arm per close per series; idempotent re-replay), log data/ts/arm_pnl.jsonl.
CLI:  uv run python -m predhft.ts_levers bootstrap [--families s7p13-KX*15M,s7p15-KX*15M] [--fetch]   (replays all history)
      uv run python -m predhft.ts_levers report                                                   (prints the posterior table)
"""
from __future__ import annotations
import datetime as dt, itertools, json, math, pathlib, random, time
from dataclasses import dataclass, asdict
from .fees import kalshi_taker_fee

ROOT = pathlib.Path("data/ts")
STATE = ROOT / "levers_state.json"
ARM_LOG = ROOT / "arm_pnl.jsonl"
LOT = 10                         # paper lot used for fill eligibility (resting size >= LOT) and fee rounding
TICK = 0.01
D_MIN_BP = {"KXBTC15M": 1.0, "KXETH15M": 1.0, "KXSOL15M": 2.0, "KXBTCD": 1.0, "KXETHD": 1.0, "KXSOLD": 2.0}
BASIS_BP = {"BTC": 0.44, "ETH": 0.7, "SOL": 1.2}
BASIS_NOISE_BP = 0.6
# posterior hyper-parameters (cents per contract)
MU0, TAU0, SIGMA_PRIOR, SIGMA_PSEUDO_N, SIGMA_FLOOR = 0.0, 10.0, 35.0, 5.0, 15.0
N_MIN = 30                       # clusters before an arm may be selected on its own posterior (PREREG P18)
N_FLOOR = 40                     # clusters after which a negative-mean arm is floored (not zeroed) in the sampling distribution
P_FLOOR = 0.02
SIZE_FULL_PBEST = 0.75           # P(mu > 0) needed for the full risk.size; else half
TIMING = {"early": (30.0, 55.2), "mid": (10.0, 40.0), "late": (10.0, 25.0), "full": (10.0, 55.2)}   # 55.2 s = the runners' t_open 0.92 min (T-55 nominal)
PT = dt.timezone(dt.timedelta(hours=-7))   # Pacific Daylight Time (report convention); DST ends 2026-11-01 -> -8

def Phi(x: float) -> float: return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def p_cal(d_bp: float, sig1: float, mins_left: float, basis_noise_bp: float = BASIS_NOISE_BP) -> float:
    """Same formula as s7_live.p_cal (REBASE §5); duplicated so the replay never imports the runner (no httpx, no side effects)."""
    r = max(min(mins_left, 1.0), 0.02); sd_bp = math.sqrt((1.5 * sig1 * 1e4 * r * math.sqrt(r / 3.0)) ** 2 + basis_noise_bp ** 2)
    return Phi(d_bp / sd_bp) if sd_bp > 0 else (1.0 if d_bp > 0 else 0.0)

@dataclass(frozen=True)
class Arm:
    edge_c: int          # 3 | 5 | 7
    timing: str          # early | mid | late | full
    cap: float           # 0.95 | 0.97
    order: str           # take | join
    floor: float = 0.0   # market-agreement floor: our side must be priced >= floor (0.5 = only buy the market favourite); candidates only
    @property
    def id(self) -> str: return f"E{self.edge_c}-{self.timing}-C{int(round(self.cap * 100))}-{self.order}" + (f"-F{int(round(self.floor * 100))}" if self.floor else "")
    @property
    def window_s(self) -> tuple[float, float]: return TIMING[self.timing]
    @property
    def live_ok(self) -> bool: return self.order == "take"     # paper runner can only take; join arms are replay-scored only

ARMS: list[Arm] = [Arm(e, w, c, o) for e, w, c, o in itertools.product((3, 5, 7), ("early", "mid", "late"), (0.95, 0.97), ("take", "join"))]
REF_ARM = Arm(5, "full", 0.95, "take")       # the incumbent P13/P16 rule, replayed and reported, not sampled
# candidates (replayed + reported, NOT sampled until PREREG P18 promotes them): market-agreement floor .50 = buy only when our side
# is also the market favourite. Added after the bootstrap showed every take loss was a market longshot -> in-sample for closes before P18_T0.
CAND_ARMS: list[Arm] = [Arm(e, w, 0.95, "take", 0.5) for e, w in itertools.product((3, 5, 7), ("early", "mid"))]
ALL_ARMS: list[Arm] = ARMS + [REF_ARM] + CAND_ARMS
ARM_BY_ID: dict[str, Arm] = {a.id: a for a in ALL_ARMS}
DEFAULT_ARM_ID = REF_ARM.id

def asset_of(series: str) -> str:
    return series[2:5] if series.startswith("KX") else series[:3]

def normalise(snaps: list[dict], series: str, basis_applied: bool) -> list[dict]:
    """Keep final-minute rows with a book, sorted by t; apply the replica basis correction once when the recording runner did not
    (s7p15 rows). Adds secs_left, d_bp, p_cal, fav side/price/size for both order styles."""
    asset = asset_of(series); dmin = D_MIN_BP.get(series, 1.0); out = []
    for r in sorted((s for s in snaps if s.get("ticker") and s.get("mins_left") is not None and s.get("yes_ask") is not None), key=lambda s: s["t"]):
        ml = r["mins_left"]
        if ml >= 1.0: continue
        spot, partial, n = r.get("spot"), r.get("partial_twap"), r.get("partial_n") or 0
        if not spot or r.get("K") in (None, 0): continue
        src = r.get("spot_src") or "replica"
        if not basis_applied and src != "cfb":
            b = 1 + BASIS_BP.get(asset, 0.0) * 1e-4; spot = spot * b; partial = partial * b if partial else partial
        d_bp = None; pc = None
        if partial and n >= 3:
            rr = max(min(ml, 1.0), 0.02); proj = (1 - rr) * partial + rr * spot; d_bp = 1e4 * (proj / r["K"] - 1)
            pc = p_cal(d_bp, r.get("sigma1") or 0.0006, ml, basis_noise_bp=(0.0 if src == "cfb" else BASIS_NOISE_BP))
        yb, ya, ybs, yas = float(r["yes_bid"] or 0), float(r["yes_ask"] or 0), float(r.get("bid_size") or 0), float(r.get("ask_size") or 0)
        out.append({"t": r["t"], "secs_left": ml * 60.0, "mins_left": ml, "d_bp": d_bp, "p_cal": pc, "ok": d_bp is not None and abs(d_bp) >= dmin,
                    "yb": yb, "ya": ya, "ybs": ybs, "yas": yas})
    return out

def _side_quotes(s: dict, side: str) -> tuple[float, float, float, float]:
    """(ask, ask_size, bid, bid_size) for our side in that side's price space."""
    if side == "yes": return s["ya"], s["yas"], s["yb"], s["ybs"]
    return (1 - s["yb"]) if s["yb"] > 0 else 1.0, s["ybs"], (1 - s["ya"]) if 0 < s["ya"] < 1 else 0.0, s["yas"]

def replay_window(snapshots: list[dict], result: str, arm: Arm, series: str = "KXBTC15M", basis_applied: bool = True, lot: int = LOT) -> dict | None:
    """Counterfactual single bet of `arm` on one market window. Returns None if the arm would not have traded, else
    {pnl_c (net cents per contract), side, price, fee_c, secs_left, p_cal, d_bp, fill: take|join}. `result` in {yes,no}."""
    if result not in ("yes", "no"): return None
    S = snapshots if (snapshots and "secs_left" in snapshots[0]) else normalise(snapshots, series, basis_applied)
    lo, hi = arm.window_s; edge_min = arm.edge_c / 100.0
    for i, s in enumerate(S):
        if not (lo <= s["secs_left"] <= hi) or not s["ok"]: continue
        side = "yes" if s["d_bp"] > 0 else "no"; p_side = s["p_cal"] if side == "yes" else 1 - s["p_cal"]
        ask, ask_sz, bid, bid_sz = _side_quotes(s, side)
        if arm.floor and (ask if arm.order == "take" else bid) < arm.floor: continue
        if arm.order == "take":
            if not (0 < ask <= arm.cap) or ask_sz < lot: continue
            # live re-check: the next snapshot (<= 4 s later) must still offer our side <= cap; pay the worse of the two asks + 1 tick
            nxt = S[i + 1] if i + 1 < len(S) and S[i + 1]["t"] - s["t"] <= 4.0 else None
            if nxt is not None:
                ask2, sz2, _, _ = _side_quotes(nxt, side)
                if not (0 < ask2 <= arm.cap) or sz2 < lot: continue
                ask = max(ask, ask2)
            price = min(ask + TICK, 0.99); fee = kalshi_taker_fee(lot, price) / lot
            if p_side - price - fee < edge_min: continue
            win = 1.0 if result == side else 0.0
            return {"pnl_c": round(100 * (win - price - fee), 3), "side": side, "price": round(price, 3), "fee_c": round(100 * fee, 3), "secs_left": round(s["secs_left"], 1), "p_cal": round(p_side, 4), "d_bp": round(s["d_bp"], 2), "fill": "take"}
        else:  # join: rest at our side's best bid (needs a real bid), filled iff a later snapshot's ask of our side < our bid
            if not (0 < bid <= arm.cap) or bid >= ask: continue
            price = bid
            if p_side - price < edge_min: continue
            for s2 in S[i + 1:]:
                a2, _, _, _ = _side_quotes(s2, side)
                if 0 < a2 < price - 1e-9:
                    win = 1.0 if result == side else 0.0
                    return {"pnl_c": round(100 * (win - price), 3), "side": side, "price": round(price, 3), "fee_c": 0.0, "secs_left": round(s["secs_left"], 1), "p_cal": round(p_side, 4), "d_bp": round(s["d_bp"], 2), "fill": "join", "filled_secs_left": round(s2["secs_left"], 1)}
            return None     # posted, never crossed: no trade
    return None

def replay_all(snapshots: list[dict], result: str, series: str, basis_applied: bool = True, arms: list[Arm] | None = None) -> dict[str, dict | None]:
    S = normalise(snapshots, series, basis_applied)
    return {a.id: replay_window(S, result, a, series) for a in (arms or ALL_ARMS)}

# ----------------------------------------------------------------------------------------------------------------------- posterior
def _lock(path: pathlib.Path):
    """Cross-process file lock (three runners share the state). Returns an open handle; release with _unlock."""
    path.parent.mkdir(parents=True, exist_ok=True); fh = open(path.with_suffix(".lock"), "a+")
    try:
        import msvcrt
        for _ in range(200):
            try: msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1); return fh
            except OSError: time.sleep(0.05)
    except ImportError: return fh
    return fh

def _unlock(fh):
    try:
        import msvcrt; fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception: pass
    fh.close()

def load_state(path: pathlib.Path = STATE) -> dict:
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except Exception: pass
    return {"version": 1, "obs": {}, "hivol_hours_pt": None, "updated": None}

def save_state(state: dict, path: pathlib.Path = STATE):
    path.parent.mkdir(parents=True, exist_ok=True); state["updated"] = time.time()
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(state), encoding="utf-8"); tmp.replace(path)

def record_window(state: dict, close_ts: int, series: str, ticker: str, outcomes: dict[str, dict | None], source: str = "live", log_path: pathlib.Path | None = ARM_LOG) -> dict:
    """Store every arm's counterfactual P&L for one (close, series). Idempotent per (arm, close, series). Appends one log row per arm that traded."""
    cl = str(int(close_ts)); rows = []
    for arm_id, o in outcomes.items():
        d = state["obs"].setdefault(arm_id, {}).setdefault(cl, {})
        if o is None: d.pop(series, None); continue
        d[series] = o["pnl_c"]; rows.append({"t": time.time(), "close_ts": int(close_ts), "series": series, "ticker": ticker, "arm": arm_id, "source": source, **o})
    if log_path is not None and rows:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            for r in rows: f.write(json.dumps(r) + "\n")
    return state

def update_state_file(close_ts: int, series: str, ticker: str, outcomes: dict[str, dict | None], source: str = "live", path: pathlib.Path = STATE) -> dict:
    fh = _lock(path)
    try:
        st = load_state(path); record_window(st, close_ts, series, ticker, outcomes, source, log_path=path.parent / "arm_pnl.jsonl"); save_state(st, path); return st
    finally: _unlock(fh)

def cluster_obs(state: dict, arm_id: str, hour_mask=None) -> list[float]:
    """One observation per close: mean per-contract P&L over the series the arm traded on that close."""
    out = []
    for cl, d in state["obs"].get(arm_id, {}).items():
        if not d: continue
        if hour_mask is not None and not hour_mask(int(cl)): continue
        out.append(sum(d.values()) / len(d))
    return out

def posterior(xs: list[float], mu0: float = MU0, tau0: float = TAU0) -> dict:
    """Normal-Normal, known-variance approx. sigma^2 = (n s^2 + k sigma_p^2)/(n + k), floored. Returns mean, sd (of the mean), n, raw mean, se."""
    n = len(xs); m = sum(xs) / n if n else 0.0
    ss = sum((x - m) ** 2 for x in xs) if n > 1 else 0.0
    s2 = (ss + SIGMA_PSEUDO_N * SIGMA_PRIOR ** 2) / (max(n - 1, 0) + SIGMA_PSEUDO_N); sigma = max(math.sqrt(s2), SIGMA_FLOOR)
    prec = 1 / tau0 ** 2 + n / sigma ** 2; mean = (mu0 / tau0 ** 2 + sum(xs) / sigma ** 2) / prec
    return {"n": n, "raw_mean": m, "se": (sigma / math.sqrt(n) if n else None), "sigma": sigma, "mean": mean, "sd": math.sqrt(1 / prec), "p_pos": Phi(mean / math.sqrt(1 / prec))}

def hour_pt(close_ts: int) -> int: return dt.datetime.fromtimestamp(close_ts, PT).hour
def mask_usday(close_ts: int) -> bool: return 6 <= hour_pt(close_ts) < 16
def mask_hivol(hours: list[int] | None):
    hs = set(hours or []); return (lambda c: hour_pt(c) in hs)
MASKS = {"all": None, "usday": mask_usday, "offday": (lambda c: not mask_usday(c))}

def posterior_summary(state: dict, arms: list[Arm] | None = None, hour_filter: str = "all", draws: int = 4000, seed: int | None = 0) -> list[dict]:
    """Per-arm table: n (closes), n_trades, raw mean c/ct, se, posterior mean/sd, P(mu>0), P(best) (Monte Carlo over the sampled arms)."""
    arms = arms or ALL_ARMS
    mask = MASKS.get(hour_filter) if hour_filter != "hivol" else mask_hivol(state.get("hivol_hours_pt"))
    rows = []
    for a in arms:
        xs = cluster_obs(state, a.id, mask); po = posterior(xs)
        ntr = sum(len(d) for cl, d in state["obs"].get(a.id, {}).items() if mask is None or mask(int(cl)))
        rows.append({"arm": a.id, "sampled": a in ARMS, "live_ok": a.live_ok, "n": po["n"], "n_trades": ntr, "raw_mean": po["raw_mean"], "se": po["se"], "mean": po["mean"], "sd": po["sd"], "p_pos": po["p_pos"], "sigma": po["sigma"]})
    pb = p_best(rows, draws=draws, seed=seed)
    for r in rows: r["p_best"] = pb.get(r["arm"], 0.0)
    return rows

def p_best(rows: list[dict], draws: int = 4000, seed: int | None = 0) -> dict[str, float]:
    """Monte Carlo P(arm has the highest mean) among the sampled (grid) arms; reference/non-sampled arms get P(best)=P(their draw beats all grid draws)."""
    rng = random.Random(seed); grid = [r for r in rows if r.get("sampled", True)]; others = [r for r in rows if not r.get("sampled", True)]
    if not grid: return {}
    wins = {r["arm"]: 0 for r in rows}
    for _ in range(draws):
        best, bv = None, -1e9
        for r in grid:
            v = rng.gauss(r["mean"], r["sd"])
            if v > bv: best, bv = r["arm"], v
        wins[best] += 1
        for r in others:
            if rng.gauss(r["mean"], r["sd"]) > bv: wins[r["arm"]] += 1
    return {k: v / draws for k, v in wins.items()}

def selection_probs(rows: list[dict], live_only: bool = True) -> dict[str, float]:
    """Thompson selection distribution = P(best) over eligible arms, then the P18 guard: an arm with posterior mean < 0 and n >= N_FLOOR
    keeps >= P_FLOOR (never zero: live fills are the only check on the replay's fill model); arms with n < N_MIN cannot exceed the
    reference arm's probability (they are selected on their prior only). Renormalised."""
    el = [r for r in rows if r.get("sampled", True) and (r.get("live_ok", True) or not live_only)]
    if not el: return {}
    pb = p_best(el, draws=4000, seed=None); ref = next((r for r in rows if r["arm"] == DEFAULT_ARM_ID), None)
    p = {}
    for r in el:
        v = pb.get(r["arm"], 0.0)
        if r["n"] < N_MIN and ref is not None: v = min(v, max(pb.get(ref["arm"], 0.0), 1.0 / len(el)))
        if r["mean"] < 0 and r["n"] >= N_FLOOR: v = max(v, P_FLOOR)
        p[r["arm"]] = v
    z = sum(p.values()) or 1.0
    return {k: v / z for k, v in p.items()}

def sample_best(state: dict | None = None, live_only: bool = True, rng: random.Random | None = None, hour_filter: str = "all") -> tuple[Arm, dict]:
    """Pick the arm for the next live paper bet. Returns (arm, info) where info has the selection probability table and the size multiplier."""
    state = state if state is not None else load_state(); rng = rng or random.Random()
    rows = posterior_summary(state, hour_filter=hour_filter, draws=2000, seed=None)
    probs = selection_probs(rows, live_only=live_only)
    if not probs: return REF_ARM, {"probs": {}, "size_mult": 0.5, "reason": "no eligible arm"}
    u = rng.random(); acc = 0.0; pick = next(iter(probs))
    for k, v in probs.items():
        acc += v
        if u <= acc: pick = k; break
    row = next(r for r in rows if r["arm"] == pick)
    mult = 1.0 if (row["p_pos"] >= SIZE_FULL_PBEST and row["n"] >= N_MIN) else 0.5
    return ARM_BY_ID[pick], {"probs": probs, "size_mult": mult, "n": row["n"], "mean": row["mean"], "sd": row["sd"], "p_best": row["p_best"]}

# ----------------------------------------------------------------------------------------------------------------------- bootstrap
def load_books(fam_dir: pathlib.Path) -> dict[tuple[int, str], list[dict]]:
    """books.jsonl -> {(close_ts, ticker): rows} for final-minute rows."""
    out: dict[tuple[int, str], list[dict]] = {}
    p = fam_dir / "books.jsonl"
    if not p.exists(): return out
    with p.open(encoding="utf-8") as f:
        for l in f:
            if not l.strip(): continue
            try: r = json.loads(l)
            except Exception: continue
            if not r.get("ticker") or r.get("mins_left") is None or r["mins_left"] >= 1.0 or "close_ts" not in r: continue
            out.setdefault((int(r["close_ts"]), r["ticker"]), []).append(r)
    return out

def learn_hivol_hours(books_by_fam: dict[str, dict], top_frac: float = 1 / 3) -> list[int]:
    """High-vol PT hours = top tercile of the median per-window final-minute spot range (bp of K), pooled over assets. Frozen in state."""
    by_hour: dict[int, list[float]] = {}
    for fam, bk in books_by_fam.items():
        for (cl, tk), rows in bk.items():
            sp = [r["spot"] for r in rows if r.get("spot") and 10 <= r["mins_left"] * 60 <= 60]
            if len(sp) < 5 or not rows[0].get("K"): continue
            by_hour.setdefault(hour_pt(cl), []).append(1e4 * (max(sp) - min(sp)) / rows[0]["K"])
    med = {h: sorted(v)[len(v) // 2] for h, v in by_hour.items() if len(v) >= 3}
    if not med: return []
    k = max(1, int(round(len(med) * top_frac)))
    return sorted(h for h, _ in sorted(med.items(), key=lambda kv: -kv[1])[:k])

def fetch_results(tickers: list[str], cache_path: pathlib.Path, fetch: bool = True, seed_paths: list[pathlib.Path] = ()) -> dict[str, dict]:
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    for sp in seed_paths:
        if sp.exists():
            for k, v in json.loads(sp.read_text()).items():
                if v.get("result") in ("yes", "no") and k not in cache: cache[k] = v
    need = [t for t in tickers if cache.get(t, {}).get("result") not in ("yes", "no")]
    if fetch and need:
        from .kalshi_public import KalshiPublic
        k = KalshiPublic(min_interval_s=0.5)
        for i, t in enumerate(need):
            try:
                m = k.get(f"/markets/{t}")["market"]; cache[t] = {"result": m.get("result"), "expiration_value": m.get("expiration_value"), "floor_strike": m.get("floor_strike"), "close_time": m.get("close_time")}
            except Exception as e: print("fetch err", t, e)
            if i % 20 == 0: cache_path.parent.mkdir(parents=True, exist_ok=True); cache_path.write_text(json.dumps(cache))
    cache_path.parent.mkdir(parents=True, exist_ok=True); cache_path.write_text(json.dumps(cache))
    return cache

def bootstrap(data_root: pathlib.Path = pathlib.Path("data"), families: list[str] | None = None, fetch: bool = True, state_path: pathlib.Path = STATE, refreeze: bool = False) -> dict:
    """Replay every recorded final-minute window of the given families for all arms; initialise/refresh the posterior state (idempotent)."""
    families = families or [f"s7p13-KX{a}15M" for a in ("BTC", "ETH", "SOL")] + [f"s7p15-KX{a}15M" for a in ("BTC", "ETH", "SOL")]
    books = {fam: load_books(data_root / fam) for fam in families}
    tickers = sorted({tk for bk in books.values() for (cl, tk) in bk if cl < time.time() - 150})
    res = fetch_results(tickers, state_path.parent / "results.json", fetch=fetch, seed_paths=[pathlib.Path("reports/sharpe/results.json")])
    fh = _lock(state_path)
    try:
        st = load_state(state_path)
        if st.get("hivol_hours_pt") is None or refreeze: st["hivol_hours_pt"] = learn_hivol_hours(books)
        # merge snapshots of the same (close, ticker) across families (p13 + p15 record the same market); p15 rows need the basis
        merged: dict[tuple[int, str], list[dict]] = {}
        for fam, bk in books.items():
            basis_applied = fam.startswith("s7p13")
            series = fam.split("-", 1)[1]
            for key, rows in bk.items():
                merged.setdefault(key, []).extend(normalise(rows, series, basis_applied))
        n_win = n_res = 0
        for (cl, tk), S in merged.items():
            r = res.get(tk, {}).get("result")
            if r not in ("yes", "no"): continue
            S.sort(key=lambda s: s["t"]); series = tk.split("-")[0]; n_res += 1
            out = {a.id: replay_window(S, r, a, series) for a in ALL_ARMS}
            record_window(st, cl, series, tk, out, source="bootstrap", log_path=None); n_win += 1
        st["bootstrap"] = {"t": time.time(), "windows": n_win, "with_result": n_res, "families": families}
        save_state(st, state_path)
    finally: _unlock(fh)
    return st

def fmt_table(rows: list[dict], top: int | None = None) -> str:
    rows = sorted(rows, key=lambda r: -r["p_best"])
    if top: rows = rows[:top]
    out = ["| arm | n closes | n trades | mean c/ct | se | post mean | post sd | P(mu>0) | P(best) |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        se = f"{r['se']:.1f}" if r["se"] is not None else "—"
        out.append(f"| {r['arm']}{'' if r['sampled'] else (' (ref)' if r['arm'] == DEFAULT_ARM_ID else ' (cand)')} | {r['n']} | {r['n_trades']} | {r['raw_mean']:+.1f} | {se} | {r['mean']:+.1f} | {r['sd']:.1f} | {r['p_pos']:.2f} | {r['p_best']:.3f} |")
    return "\n".join(out)

if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["bootstrap", "report"]); ap.add_argument("--families", default=None); ap.add_argument("--no-fetch", action="store_true"); ap.add_argument("--refreeze", action="store_true"); ap.add_argument("--hours", default="all")
    a = ap.parse_args()
    if a.cmd == "bootstrap":
        st = bootstrap(families=a.families.split(",") if a.families else None, fetch=not a.no_fetch, refreeze=a.refreeze); print(json.dumps(st["bootstrap"]), "hivol_hours_pt", st["hivol_hours_pt"])
    st = load_state(); print(fmt_table(posterior_summary(st, hour_filter=a.hours)))
