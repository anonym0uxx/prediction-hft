"""P20 — robust bet acceptance ("the gate"). docs/ROBUST_BETTING_2026-10-27.md, PREREG P20.

A pre-trade checklist every P13/P16 candidate must pass. Pure: `accept(candidate, ctx) -> (ok, reasons, details)` does no I/O,
so every guard is unit-testable and the whole gate replays over the recorded books tape. Wired OPT-IN into s7_live.run
(`--bet-gate`, default off) right before the paper buy; rejections go to runner.log and data/<family>/gate.jsonl.

Guards (all constants pre-registered in CFG; a guard is "off" only by a new PREREG entry):
  a  edge_lcb   : point edge (P16, unchanged) p_cal - take price - fee >= EDGE_MIN_C (5c) AND the LOWER confidence bound of the
                  calibrated p minus price minus fee >= EDGE_LCB_MIN_C (2c). p_lcb = Phi(|d_bp| / sd_hi),
                  sd_hi = max(sd(r_eff) * (1 + Z_LCB * RELSE), SD_EMP_T55_BP * (r_eff / 0.917)^1.5): sigma estimation error (RELSE 0.17,
                  the S7_REDESIGN dyn-threshold term), r_eff = max(r_clock, 1 - partial_n/60) (if the official partial lags the clock
                  MORE of the settlement average is still random than mins_left says), and an empirical floor = the realised
                  projection-error sd at T-55 (2.35 bp, REBASE §2, 163 windows) scaled with the r^1.5 variance shape.
  b  agreement  : our side's market mid >= FAV_MIN (0.60: the market itself calls our side the favourite beyond spread noise) AND
                  disagreement p_cal - mid <= DISAGREE_MAX (0.20: our information is incremental, not contradictory) — OR the
                  "information override": the market provably lacks what we have = official feed (spot_src cfb) AND partial_n >=
                  OVERRIDE_PARTIAL_N (30: half the settlement window is already arithmetic) AND p_lcb >= OVERRIDE_P_LCB (0.90) AND our
                  side's mid >= OVERRIDE_MID_MIN (0.35: not a pure longshot). Prior evidence (IN-SAMPLE, TS bootstrap + the 8 reference
                  replays): favourites priced .64-.96 went 10/10; every take loss had our side's mid <= .64 and a 24-71c disagreement.
                  Thresholds were chosen after seeing those; the P20 test is out-of-sample only.
  c  feed       : spot_src == cfb, official feed age <= FEED_AGE_S (2.0), partial_n within [expected-3, expected+2] where
                  expected = clamp(60 - secs_left, 0, 60). Unknown age => reject unless ctx["age_unknown_ok"] (replay only; flagged).
  d  liquidity  : visible size at our side's ask >= lot; take price = ask + TICK <= PRICE_CAP (0.96 = cap .95 + tick); expected
                  adverse move over LATENCY_S (1.5 s): adverse_c = 100 * phi(z) * r * sigma1_bp * sqrt(LATENCY_S/60) / sd_bp
                  (the probability-space move of a one-sigma index move during the latency); edge_lcb_c - adverse_c >= EDGE_LCB_MIN_C.
  e  regime     : |official index move over the last 30 s| <= K_JUMP (2.5) * sigma1_bp * sqrt(0.5). Unevaluable (no 30-s history)
                  => pass with flag regime_unevaluable (live: wiring reads the cfb tail; replay: snapshot history).
  f  hitrate    : rolling last HR_WINDOW (30) settled gate-accepted outcomes of this asset: Wilson lower bound (z = HR_Z, 1.28 =
                  90 %) >= HR_FLOOR (0.80) once >= HR_MIN_N (20) outcomes exist; below => the asset is scored-only (reject with
                  reason hitrate_floor, ALERT in runner.log); shadow-accepted bets keep being logged and, once settled, re-enter
                  the window (auto-resume when the bound recovers).
"""
from __future__ import annotations
import json, math, pathlib, time
from .fees import kalshi_taker_fee

CFG = {
    "EDGE_MIN_C": 5.0, "EDGE_LCB_MIN_C": 2.0, "Z_LCB": 1.645, "RELSE": 0.17, "VAR_MULT": 1.5, "BASIS_NOISE_BP": 0.6, "SD_EMP_T55_BP": 2.35, "R_T55": 0.917,
    "FAV_MIN": 0.60, "DISAGREE_MAX": 0.20, "OVERRIDE": True, "OVERRIDE_PARTIAL_N": 30, "OVERRIDE_P_LCB": 0.90, "OVERRIDE_MID_MIN": 0.35,
    "FEED_AGE_S": 2.0, "PARTIAL_LAG_MAX": 3, "PARTIAL_LEAD_MAX": 2,
    "TICK": 0.01, "PRICE_CAP": 0.96, "LATENCY_S": 1.5,
    "K_JUMP": 2.5,
    "HR_WINDOW": 30, "HR_MIN_N": 20, "HR_Z": 1.28, "HR_FLOOR": 0.80,
    "LOT": 10,
}
GUARDS = ("edge_lcb", "agreement", "feed", "liquidity", "regime", "hitrate")

def Phi(x: float) -> float: return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def phi(x: float) -> float: return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def sd_bp(sig1: float, r: float, basis_noise_bp: float, var_mult: float = CFG["VAR_MULT"]) -> float:
    """Remaining sd (bp) of the 60-s settlement average vs the projection: same form as s7_live.p_cal (REBASE §5)."""
    r = max(min(r, 1.0), 0.02)
    return math.sqrt((var_mult * sig1 * 1e4 * r * math.sqrt(r / 3.0)) ** 2 + basis_noise_bp ** 2)

def p_lower(d_bp: float, sig1: float, mins_left: float, partial_n: int, official: bool, cfg: dict = CFG) -> dict:
    """Lower confidence bound of p for the side sign(d_bp). Returns {p_point, p_lcb, sd, sd_hi, r_eff, z}."""
    r_clock = max(min(mins_left, 1.0), 0.02); r_eff = max(r_clock, 1.0 - max(partial_n or 0, 0) / 60.0)
    bn = 0.0 if official else cfg["BASIS_NOISE_BP"]
    sd = sd_bp(sig1 or 0.0006, r_clock, bn, cfg["VAR_MULT"])
    sd_hi = max(sd_bp(sig1 or 0.0006, r_eff, bn, cfg["VAR_MULT"]) * (1 + cfg["Z_LCB"] * cfg["RELSE"]), cfg["SD_EMP_T55_BP"] * (r_eff / cfg["R_T55"]) ** 1.5)
    return {"p_point": Phi(abs(d_bp) / sd), "p_lcb": Phi(abs(d_bp) / sd_hi), "sd": sd, "sd_hi": sd_hi, "r_eff": r_eff, "z": abs(d_bp) / sd}

def wilson_lb(k: int, n: int, z: float = CFG["HR_Z"]) -> float:
    if n <= 0: return 0.0
    p = k / n; den = 1 + z * z / n; c = (p + z * z / (2 * n)) / den
    return max(0.0, c - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den)

# ------------------------------------------------------------------------------------------------------------------- guards
def guard_edge(c: dict, cfg: dict = CFG) -> tuple[bool, dict]:
    d = c.get("d_bp"); side = c["side"]
    if d is None or (side == "yes") != (d > 0): return False, {"edge_lcb_c": None, "why": "side_vs_projection"}
    pl = p_lower(d, c.get("sigma1"), c["mins_left"], c.get("partial_n") or 0, c.get("spot_src") == "cfb", cfg)
    lot = c.get("lot") or cfg["LOT"]; price = c["price"]; fee = kalshi_taker_fee(lot, price) / lot if 0 < price < 1 else 1.0
    edge_lcb_c = 100 * (pl["p_lcb"] - price - fee); edge_point_c = 100 * (pl["p_point"] - price - fee)
    return (edge_point_c >= cfg["EDGE_MIN_C"] and edge_lcb_c >= cfg["EDGE_LCB_MIN_C"]), {"p_point": round(pl["p_point"], 4), "p_lcb": round(pl["p_lcb"], 4), "sd_bp": round(pl["sd"], 3), "sd_hi_bp": round(pl["sd_hi"], 3), "r_eff": round(pl["r_eff"], 3),
                                             "fee_c": round(100 * fee, 3), "edge_point_c": round(edge_point_c, 2), "edge_lcb_c": round(edge_lcb_c, 2), "z": round(pl["z"], 3)}

def guard_agreement(c: dict, p_lcb: float | None, cfg: dict = CFG) -> tuple[bool, dict]:
    mid = c.get("mkt_side_mid")
    if mid is None: return False, {"mode": "no_market_mid"}
    dis = ((c.get("p_cal") or 0.0) - mid); det = {"mkt_side_mid": round(mid, 3), "disagreement_c": round(100 * dis, 1)}
    if mid >= cfg["FAV_MIN"] and dis <= cfg["DISAGREE_MAX"] + 1e-9: return True, {"mode": "favourite", **det}
    ov = bool(cfg["OVERRIDE"] and c.get("spot_src") == "cfb" and (c.get("partial_n") or 0) >= cfg["OVERRIDE_PARTIAL_N"] and p_lcb is not None and p_lcb >= cfg["OVERRIDE_P_LCB"] and mid >= cfg["OVERRIDE_MID_MIN"])
    return ov, {"mode": "override" if ov else ("disagreement" if mid >= cfg["FAV_MIN"] else "longshot"), **det}

def guard_feed(c: dict, ctx: dict, cfg: dict = CFG) -> tuple[bool, dict]:
    det = {"spot_src": c.get("spot_src"), "cfb_age_s": c.get("cfb_age_s"), "partial_n": c.get("partial_n")}
    if c.get("spot_src") != "cfb": det["why"] = "not_official"; return False, det
    age = c.get("cfb_age_s")
    if age is None:
        if not ctx.get("age_unknown_ok"): det["why"] = "age_unknown"; return False, det
        det["age_unknown_flag"] = True
    elif age > cfg["FEED_AGE_S"]: det["why"] = "stale"; return False, det
    exp = max(0.0, min(60.0, 60.0 - c["secs_left"])); n = c.get("partial_n") or 0; det["partial_expected"] = round(exp, 1)
    if n < exp - cfg["PARTIAL_LAG_MAX"] or n > exp + cfg["PARTIAL_LEAD_MAX"]: det["why"] = "partial_n_inconsistent"; return False, det
    return True, det

def guard_liquidity(c: dict, edge_lcb_c: float | None, z: float | None, sd: float | None, cfg: dict = CFG) -> tuple[bool, dict]:
    lot = c.get("lot") or cfg["LOT"]; ask, size, price = c.get("ask"), c.get("ask_size") or 0.0, c["price"]
    det = {"ask": ask, "ask_size": size, "price": price}
    if ask is None or not (0 < ask < 1) or size < lot: det["why"] = "size_lt_lot"; return False, det
    if price > cfg["PRICE_CAP"] + 1e-9 or price < ask + cfg["TICK"] - 1e-9: det["why"] = "price_cap"; return False, det
    r = max(min(c["mins_left"], 1.0), 0.02); s_bp = (c.get("sigma1") or 0.0006) * 1e4
    adverse_c = 100 * phi(z or 0.0) * r * s_bp * math.sqrt(cfg["LATENCY_S"] / 60.0) / max(sd or 1e-6, 1e-6)
    det["adverse_c"] = round(adverse_c, 2); det["edge_after_adverse_c"] = (round(edge_lcb_c - adverse_c, 2) if edge_lcb_c is not None else None)
    if edge_lcb_c is None or edge_lcb_c - adverse_c < cfg["EDGE_LCB_MIN_C"]: det["why"] = "adverse_move"; return False, det
    return True, det

def guard_regime(c: dict, ctx: dict, cfg: dict = CFG) -> tuple[bool, dict]:
    mv = ctx.get("move30_bp"); s30 = (c.get("sigma1") or 0.0006) * 1e4 * math.sqrt(0.5); det = {"move30_bp": (round(mv, 2) if mv is not None else None), "sigma30_bp": round(s30, 2), "k_sd": None}
    if mv is None: det["regime_unevaluable"] = True; return True, det
    det["k_sd"] = round(abs(mv) / s30, 2) if s30 > 0 else None
    return abs(mv) <= cfg["K_JUMP"] * s30, det

def guard_hitrate(ctx: dict, cfg: dict = CFG) -> tuple[bool, dict]:
    xs = list(ctx.get("recent_outcomes") or [])[-cfg["HR_WINDOW"]:]; n = len(xs); k = sum(1 for x in xs if x)
    lb = wilson_lb(k, n, cfg["HR_Z"]); det = {"hr_n": n, "hr_k": k, "hr_wilson_lb": round(lb, 3), "hr_floor": cfg["HR_FLOOR"]}
    if n < cfg["HR_MIN_N"]: det["warming"] = True; return True, det
    return lb >= cfg["HR_FLOOR"], det

# ------------------------------------------------------------------------------------------------------------------- accept
def accept(candidate: dict, ctx: dict | None = None, cfg: dict | None = None) -> tuple[bool, list[str], dict]:
    """candidate: series, asset, ticker, close_ts, side (yes|no), price (take price, our side), ask, ask_size, p_cal (our side),
    d_bp (signed, + => YES), sigma1, mins_left, secs_left, partial_n, spot_src, cfb_age_s, mkt_side_mid, lot.
    ctx: move30_bp, recent_outcomes (list of 0/1), age_unknown_ok, disable (set of guard names; replay variants only).
    Returns (ok, reasons, details); reasons lists every failing guard (all guards are evaluated, nothing short-circuits)."""
    ctx = ctx or {}; cfg = {**CFG, **(cfg or {})}; dis = set(ctx.get("disable") or ()); reasons = []; det: dict = {"cfg_edge_min_c": cfg["EDGE_MIN_C"], "cfg_edge_lcb_min_c": cfg["EDGE_LCB_MIN_C"]}
    ok_e, de = guard_edge(candidate, cfg); det.update(de)
    ok_b, db = guard_agreement(candidate, de.get("p_lcb"), cfg); det.update(db)
    ok_c, dc = guard_feed(candidate, ctx, cfg); det.update(dc)
    ok_d, dd = guard_liquidity(candidate, de.get("edge_lcb_c"), de.get("z"), de.get("sd_hi_bp"), cfg); det.update(dd)
    ok_r, dr = guard_regime(candidate, ctx, cfg); det.update(dr)
    ok_h, dh = guard_hitrate(ctx, cfg); det.update(dh)
    for name, ok in zip(GUARDS, (ok_e, ok_b, ok_c, ok_d, ok_r, ok_h)):
        if not ok and name not in dis: reasons.append(name)
    det["guards"] = {name: ok for name, ok in zip(GUARDS, (ok_e, ok_b, ok_c, ok_d, ok_r, ok_h))}; det["disabled"] = sorted(dis)
    return (not reasons), reasons, det

# ------------------------------------------------------------------------------------------------------------ live helpers (I/O)
def recent_outcomes(root: pathlib.Path, n: int = CFG["HR_WINDOW"]) -> list[int]:
    """Last n settled outcomes (1 = win) of gate-accepted bets of this family: real paper trades (trades.jsonl rows carrying `gate`)
    plus settled shadow rows of gate.jsonl (accepted while the asset was scored-only). Ordered by close_ts."""
    xs = []
    for fn, kind in ((root / "trades.jsonl", "trade"), (root / "gate.jsonl", "shadow")):
        if not fn.exists(): continue
        for l in fn.read_text(encoding="utf-8").splitlines():
            if not l.strip(): continue
            try: r = json.loads(l)
            except Exception: continue
            if kind == "trade" and r.get("gate") and r.get("status") == "settled" and r.get("result") in ("yes", "no"): xs.append((r["close_ts"], int(r["result"] == r["side"])))
            if kind == "shadow" and r.get("shadow") and r.get("result") in ("yes", "no"): xs.append((r["close_ts"], int(r["result"] == r["side"])))
    return [w for _, w in sorted(xs)][-n:]

def log_decision(root: pathlib.Path, row: dict):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "gate.jsonl").open("a", encoding="utf-8") as fh: fh.write(json.dumps(row) + "\n")

def settle_shadow(root: pathlib.Path, results: dict[str, dict]) -> int:
    """Attach results to unsettled shadow rows of data/<family>/gate.jsonl (rewrites the file). Returns rows settled."""
    p = root / "gate.jsonl"
    if not p.exists(): return 0
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]; n = 0
    for r in rows:
        if r.get("shadow") and r.get("result") not in ("yes", "no"):
            res = results.get(r["ticker"], {}).get("result")
            if res in ("yes", "no"): r["result"] = res; r["pnl_c"] = round(100 * ((1.0 if res == r["side"] else 0.0) - r["price"]) - r.get("fee_c", 0.0), 2); n += 1
    if n: p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return n

# ------------------------------------------------------------------------------------------------------------------ tape replay
def load_window_rows(fam_dir: pathlib.Path, max_mins_left: float = 2.0) -> dict[tuple[int, str], list[dict]]:
    """books.jsonl -> {(close_ts, ticker): rows with mins_left <= max_mins_left} (keeps the pre-window rows for the 30-s regime history)."""
    out: dict[tuple[int, str], list[dict]] = {}; p = fam_dir / "books.jsonl"
    if not p.exists(): return out
    with p.open(encoding="utf-8") as f:
        for l in f:
            if not l.strip(): continue
            try: r = json.loads(l)
            except Exception: continue
            if not r.get("ticker") or r.get("mins_left") is None or r["mins_left"] > max_mins_left or "close_ts" not in r or ("yes_ask" not in r and "yes_bid" not in r): continue
            out.setdefault((int(r["close_ts"]), r["ticker"]), []).append(r)
    for v in out.values(): v.sort(key=lambda s: s["t"])
    return out

def _quotes(r: dict) -> dict:
    """Book row -> {ya, yb, yas, ybs} for _side_quotes. yes_ask None (no NO bids => no YES offer) -> ya 1.0 size 0; yes_bid None -> yb 0 size 0."""
    ya, yb = r.get("yes_ask"), r.get("yes_bid")
    return {"ya": float(ya) if ya is not None else 1.0, "yb": float(yb) if yb is not None else 0.0,
            "yas": float(r.get("ask_size") or 0) if ya is not None else 0.0, "ybs": float(r.get("bid_size") or 0) if yb is not None else 0.0}

def candidates_from_rows(rows: list[dict], series: str, basis_applied: bool, lot: int = CFG["LOT"], cap: float = 0.95, window_s: tuple[float, float] = (10.0, 55.2), edge_min: float = 0.05) -> list[dict]:
    """Emulate the live P13/P16 candidate generation on recorded snapshots: at each final-minute row with partial_n >= 3 and |d| >= D_MIN,
    side = sign(d), our side offered <= cap with size >= lot, live re-check against the next row (<= 4 s), take price = worse ask + tick,
    P16 calibrated edge >= edge_min. Each candidate carries the gate inputs (+ move30_bp from the recorded spot path)."""
    from .ts_levers import p_cal, D_MIN_BP, BASIS_BP, BASIS_NOISE_BP, _side_quotes, asset_of
    asset = asset_of(series); dmin = D_MIN_BP.get(series, 1.0); out = []
    S = []
    for r in rows:
        spot, partial, n = r.get("spot"), r.get("partial_twap"), r.get("partial_n") or 0; src = r.get("spot_src") or "replica"
        if not spot or not r.get("K"): continue
        if not basis_applied and src != "cfb":
            b = 1 + BASIS_BP.get(asset, 0.0) * 1e-4; spot *= b; partial = partial * b if partial else partial
        S.append({**r, "spot_bc": spot, "partial_bc": partial, "n": n, "src": src})
    for i, r in enumerate(S):
        ml = r["mins_left"]; secs = ml * 60.0
        if ml >= 1.0 or not (window_s[0] <= secs <= window_s[1]) or r["n"] < 3 or not r["partial_bc"]: continue
        rr = max(min(ml, 1.0), 0.02); proj = (1 - rr) * r["partial_bc"] + rr * r["spot_bc"]; d_bp = 1e4 * (proj / r["K"] - 1)
        if abs(d_bp) < dmin: continue
        side = "yes" if d_bp > 0 else "no"
        q = _quotes(r)
        ask, ask_sz, _, _ = _side_quotes(q, side)
        if not (0 < ask <= cap) or ask_sz < lot: continue
        nxt = S[i + 1] if i + 1 < len(S) and S[i + 1]["t"] - r["t"] <= 4.0 else None
        if nxt is not None:
            q2 = _quotes(nxt)
            ask2, sz2, _, _ = _side_quotes(q2, side)
            if not (0 < ask2 <= cap) or sz2 < lot: continue
            ask, ask_sz = max(ask, ask2), min(ask_sz, sz2)
        price = min(ask + CFG["TICK"], 0.99); fee = kalshi_taker_fee(lot, price) / lot
        pc = p_cal(d_bp, r.get("sigma1") or 0.0006, ml, basis_noise_bp=(0.0 if r["src"] == "cfb" else BASIS_NOISE_BP)); p_side = pc if side == "yes" else 1 - pc
        if p_side - price - fee < edge_min: continue
        mid_yes = (q["yb"] + q["ya"]) / 2 if 0 < q["ya"] < 1 else None; mkt_side_mid = (mid_yes if side == "yes" else 1 - mid_yes) if mid_yes is not None else None
        hist = [(s["t"], s["spot_bc"]) for s in S[:i + 1] if r["t"] - 30.0 <= s["t"] <= r["t"]]
        move30 = 1e4 * math.log(hist[-1][1] / hist[0][1]) if len(hist) >= 2 and hist[-1][0] - hist[0][0] >= 15.0 else None
        out.append({"series": series, "asset": asset, "ticker": r["ticker"], "close_ts": int(r["close_ts"]), "t": r["t"], "side": side, "price": round(price, 4), "ask": ask, "ask_size": ask_sz,
                    "p_cal": p_side, "d_bp": d_bp, "sigma1": r.get("sigma1"), "mins_left": ml, "secs_left": secs, "partial_n": r["n"], "spot_src": r["src"], "cfb_age_s": r.get("cfb_age_s"),
                    "mkt_side_mid": mkt_side_mid, "lot": lot, "fee": fee, "move30_bp": move30})
    return out

def replay_window(rows: list[dict], result: str, series: str, basis_applied: bool, ctx_extra: dict | None = None, cfg: dict | None = None) -> dict:
    """First gate-accepted candidate in the window becomes the bet. Returns {traded, pnl_c, price, side, secs_left, n_cand, reasons_hist, first_reject}."""
    cands = candidates_from_rows(rows, series, basis_applied); hist: dict[str, int] = {}; first = None
    for c in cands:
        ctx = {"move30_bp": c["move30_bp"], "age_unknown_ok": True, **(ctx_extra or {})}
        ok, reasons, det = accept(c, ctx, cfg)
        if ok:
            win = 1.0 if result == c["side"] else 0.0
            return {"traded": True, "pnl_c": round(100 * (win - c["price"] - c["fee"]), 2), "price": c["price"], "side": c["side"], "secs_left": round(c["secs_left"], 1), "win": win, "n_cand": len(cands), "reasons_hist": hist, "mode": det.get("mode"), "det": det}
        for rs in reasons: hist[rs] = hist.get(rs, 0) + 1
        if first is None: first = reasons
    return {"traded": False, "n_cand": len(cands), "reasons_hist": hist, "first_reject": first}

def replay_tape(data_root: pathlib.Path = pathlib.Path("data"), families: list[str] | None = None, results: dict | None = None, variants: dict[str, dict] | None = None, fetch: bool = True) -> dict:
    """Replay every recorded 15M window for several gate variants. variants: name -> {"disable": [...], "cfg": {...}}. Reference = no gate (P16 rule as recorded)."""
    from .ts_levers import fetch_results, replay_window as ts_replay, REF_ARM, normalise
    families = families or [f"s7p13-KX{a}15M" for a in ("BTC", "ETH", "SOL")] + [f"s7p15-KX{a}15M" for a in ("BTC", "ETH", "SOL")]
    merged: dict[tuple[int, str], list[tuple[str, dict]]] = {}
    for fam in families:
        for key, rows in load_window_rows(data_root / fam).items(): merged.setdefault(key, []).extend((fam, r) for r in rows)
    tickers = sorted({tk for (cl, tk) in merged if cl < time.time() - 150})
    res = results if results is not None else fetch_results(tickers, data_root / "ts" / "results.json", fetch=fetch, seed_paths=[pathlib.Path("reports/sharpe/results.json")])
    variants = variants or {"gate_full": {}, "gate_no_feed": {"disable": ["feed"]}, "favourite_only": {"disable": ["feed", "edge_lcb", "liquidity", "regime", "hitrate"]}, "no_override": {"cfg": {"OVERRIDE": False}}}
    out: dict = {"n_windows": 0, "variants": {v: {"trades": [], "reasons": {}} for v in variants}, "reference": {"trades": []}}
    for (cl, tk), tagged in sorted(merged.items()):
        r = res.get(tk, {}).get("result")
        if r not in ("yes", "no"): continue
        out["n_windows"] += 1; series = tk.split("-")[0]
        # P13 rows already carry the basis correction (and cfb rows need none); P15 rows need it -> normalise per source family
        rows = []
        for fam, row in tagged:
            if fam.startswith("s7p13") or (row.get("spot_src") == "cfb"): rows.append(row)
            else:
                from .ts_levers import BASIS_BP, asset_of
                b = 1 + BASIS_BP.get(asset_of(series), 0.0) * 1e-4; rr = dict(row); rr["spot"] = row["spot"] * b if row.get("spot") else row.get("spot")
                rr["partial_twap"] = row["partial_twap"] * b if row.get("partial_twap") else row.get("partial_twap"); rows.append(rr)
        rows.sort(key=lambda s: s["t"])
        ref = ts_replay(normalise(rows, series, True), r, REF_ARM, series)
        if ref: out["reference"]["trades"].append({"close_ts": cl, "ticker": tk, "series": series, **ref})
        for v, spec in variants.items():
            o = replay_window(rows, r, series, True, ctx_extra={"disable": spec.get("disable", [])}, cfg=spec.get("cfg"))
            if o["traded"]: out["variants"][v]["trades"].append({"close_ts": cl, "ticker": tk, "series": series, **{k: o[k] for k in ("pnl_c", "price", "side", "secs_left", "win", "mode")}})
            else:
                for rs, n in o["reasons_hist"].items(): out["variants"][v]["reasons"][rs] = out["variants"][v]["reasons"].get(rs, 0) + n
    return out

def summarise(trades: list[dict]) -> dict:
    """n, hit, mean c/ct, close-clustered se, Wilson LB of the hit rate."""
    n = len(trades)
    if not n: return {"n": 0, "hit": None, "mean_c": None, "se_c": None, "hit_lb": None, "closes": 0}
    by: dict[int, list[float]] = {}
    for t in trades: by.setdefault(t["close_ts"], []).append(t["pnl_c"])
    cm = [sum(v) / len(v) for v in by.values()]; G = len(cm); m = sum(cm) / G
    se = math.sqrt(sum((x - m) ** 2 for x in cm) / (G - 1) / G) if G > 1 else None
    k = sum(1 for t in trades if t.get("win") or (t.get("pnl_c", 0) > 0))
    return {"n": n, "hit": k / n, "mean_c": sum(t["pnl_c"] for t in trades) / n, "mean_cl_c": m, "se_c": se, "hit_lb": wilson_lb(k, n), "closes": G}

def fmt_replay(rep: dict, actual: list[dict] | None = None) -> str:
    L = [f"| variant | n bets | closes | hit | Wilson LB90 | mean c/ct | se (close-cl) | top rejection reasons |", "|---|---|---|---|---|---|---|---|"]
    def row(name, s, reasons=None):
        hit = "—" if s["hit"] is None else f"{s['hit']:.2f}"; lb = "—" if s["hit_lb"] is None else f"{s['hit_lb']:.2f}"; mc = "—" if s["mean_c"] is None else f"{s['mean_c']:+.1f}"; se = "—" if not s.get("se_c") else f"{s['se_c']:.1f}"
        rs = ", ".join(f"{k} {v}" for k, v in sorted((reasons or {}).items(), key=lambda kv: -kv[1])[:4])
        return f"| {name} | {s['n']} | {s['closes']} | {hit} | {lb} | {mc} | {se} | {rs} |"
    L.append(row("P16 reference (no gate, replay)", summarise(rep["reference"]["trades"])))
    for v, d in rep["variants"].items(): L.append(row(v, summarise(d["trades"]), d["reasons"]))
    if actual is not None: L.append(row("actual paper trades (P12+P13 15M, settled)", summarise(actual)))
    return "\n".join(L) + f"\n\nwindows with a settled result: {rep['n_windows']}"

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["replay", "settle-shadow"]); ap.add_argument("--families", default=None); ap.add_argument("--no-fetch", action="store_true"); ap.add_argument("--json", default=None)
    a = ap.parse_args()
    if a.cmd == "replay":
        rep = replay_tape(families=a.families.split(",") if a.families else None, fetch=not a.no_fetch)
        actual = []
        for fam in (a.families.split(",") if a.families else [f"s7p13-KX{x}15M" for x in ("BTC", "ETH", "SOL")] + [f"s7p15-KX{x}15M" for x in ("BTC", "ETH", "SOL")]):
            p = pathlib.Path("data") / fam / "trades.jsonl"
            if p.exists():
                for l in p.read_text().splitlines():
                    if not l.strip(): continue
                    t = json.loads(l)
                    if t.get("status") == "settled" and t.get("qty"): actual.append({"close_ts": t["close_ts"], "pnl_c": 100 * t["pnl"] / t["qty"], "win": float(t["result"] == t["side"])})
        print(fmt_replay(rep, actual))
        if a.json: pathlib.Path(a.json).write_text(json.dumps(rep, indent=1))
    else:
        from .ts_levers import fetch_results
        fams = a.families.split(",") if a.families else [f"s7p13-KX{x}15M" for x in ("BTC", "ETH", "SOL")]
        for fam in fams:
            root = pathlib.Path("data") / fam; p = root / "gate.jsonl"
            if not p.exists(): continue
            tks = sorted({json.loads(l)["ticker"] for l in p.read_text().splitlines() if l.strip() and json.loads(l).get("shadow") and json.loads(l).get("result") not in ("yes", "no")})
            res = fetch_results(tks, pathlib.Path("data/ts/results.json"), fetch=not a.no_fetch) if tks else {}
            print(fam, "shadow settled", settle_shadow(root, res))
