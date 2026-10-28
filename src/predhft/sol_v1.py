"""SOL-v1 — ONE settlement-aware SOL 15M paper policy on Kalshi (KXSOL15M). docs/SOL_V1_2026-10-28.md.

PAPER ONLY: no order is ever sent; the Kalshi key is used only by the read-only websocket feeds (cfb_feed, kalshi_ws_book).
BTC/ETH are context inputs only (their trading is disabled here by construction: the policy knows one series).

Pipeline (Reddit baseline u/masatumas adopted where it earns its place, see the doc):
  inputs (official CF Benchmarks SOLUSD_RTI 1 Hz tape + Kalshi book, each stamped with its own receipt time and age)
  -> features (settlement decomposition: observed partial average, remaining samples, realised 1-s vol)
  -> probability (P(settlement avg >= strike), Gaussian on the REMAINING-sample mean with calibrated variance inflation KAPPA)
  -> EV per contract at the DEPTH-WEIGHTED executable price for the obtainable quantity, fee (Kalshi taker, ceil-to-cent per
     order), one latency/adverse-selection haircut in probability space (no second slippage term)
  -> independent gates (staleness, window, price bounds, min edge, min lot) -> sizing via risk.size (shared ledger, caps, halts,
     ONE position per close) -> paper fill against the re-checked book (partial fill allowed, never above displayed depth)
  -> position management: hold vs executable exit (depth-weighted bids after fee) every poll; NO stop-loss (a stop is a
     guaranteed exit at the worst price); exits are taker only (we never assume a resting order fills because price touched it)
  -> settlement from the exchange result (cross-checked against the feed's own 60-s average)
  -> fixed-interval portfolio marks (conservative: open positions at executable liquidation value, 0 if no bid).

Every function above the live loop is pure (no I/O) so that scripts/sol_v1_replay.py runs the SAME code path on the tape.
"""
from __future__ import annotations
import argparse, bisect, datetime as dt, json, math, pathlib, time
from .fees import kalshi_taker_fee

VERSION = "SOL-v1"; FAMILY = "sol-v1"; SERIES = "KXSOL15M"; ASSET = "SOL"; PERIOD_S = 900
TICK = 0.001                      # 15M books quote in 0.1 c
STRIKE_DP = 4                     # SOLUSD_RTI strike / expiration value precision (AUDIT_2026-10-28 §3: 66/66 exact at 4 dp)
WINDOW_S = 60                     # settlement = simple average of the 60 one-second index values before close

CFG = {
    # probability model
    # variance-inflation (sd multiplier) on the remaining-sample mean, CALIBRATED on the development split (scripts/sol_v1_replay.py):
    # realised 1-s vol over 900 s scaled by the exact remaining-sample variance gives standardised settlement residuals with
    # rms 0.71-0.84 inside the final two minutes (we do NOT go below 1: never more confident than the tape) and 1.03-1.38 at
    # 3-14 min (variance grows faster than linearly: VR(60 s) 1.23 on the feed). kappa_final applies when secs_left <= kappa_split_s.
    "kappa_final": 1.0, "kappa_early": 1.15, "kappa_split_s": 120.0,
    # Kalshi price as the strong baseline: p_used = w * p_model + (1 - w) * market mid (two-sided, spread <= blend_max_spread),
    # w fitted on the development split by log loss per horizon group (final <= 120 s, mid <= 600 s, early > 600 s). Dev chose
    # 1.0 / 0.9 / 0.0; validation agreed on mid and early and disagreed on final (0.0) -> final set to the equal-weight prior.
    # w_early = 0 means: before T-10 min the policy never disagrees with the market, i.e. no entry there.
    "blend_w": {"final": 0.5, "mid": 0.9, "early": 0.0}, "blend_split_s": [120.0, 600.0], "blend_max_spread": 0.10,
    "vol_window_s": 900,          # realised 1-s vol window (official feed)
    "vol_min_n": 120,             # min 1-s returns for the realised estimate; else fall back to sigma_fallback
    "sigma_fallback_1s": 0.9e-4,  # 0.9 bp/s (tape median) when the feed tail is short
    "vol_floor_1s": 0.3e-4,       # never below 0.3 bp/s
    # gates (independent; each reports its own reason)
    "max_cfb_age_s": 2.0, "max_book_age_s": 2.5, "max_latency_s": 1.0,
    "entry_min_secs_left": 10.0, "entry_max_secs_left": 840.0,
    "min_px": 0.03, "max_px": 0.97,
    "edge_base_c": 3.0,           # minimum expected net per contract (c) after fee and haircut
    "sigma_relse": 0.17,          # relative sd error of the vol estimate -> edge requirement |z| phi(z) relse (P12 dyn_thr term, kept)
    "lot": 10, "min_lot": 5,
    "order_transit_s": 0.3,       # assumed submit->match time added to the measured decision latency for the haircut
    "fill_survival": 0.9,         # expected fraction of intended qty that fills (LAYERX §3 ~6 % misses); scales $EV only, never price
    # position management
    "exit_margin_c": 2.0,         # exit only if executable proceeds exceed hold value by this many c/ct
    "mark_interval_s": 60.0,
}

def Phi(x: float) -> float: return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def phi(x: float) -> float: return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

# ----------------------------------------------------------------------------------------------------------- feed features
def cfb_series(rows) -> list[tuple[int, float]]:
    """(second, value) sorted, one value per second (last wins), from cfb_feed rows ({value, value_ts_ms}) or (sec, value) tuples."""
    by: dict[int, float] = {}
    for r in rows:
        if isinstance(r, dict):
            v = r.get("value"); ts = r.get("value_ts_ms")
            if v and ts: by[int(ts) // 1000] = float(v)
        else:
            by[int(r[0])] = float(r[1])
    return sorted(by.items())

def realized_sigma_1s(series: list[tuple[int, float]], now_sec: int, window_s: int = CFG["vol_window_s"], min_n: int = CFG["vol_min_n"],
                      floor: float = CFG["vol_floor_1s"], fallback: float = CFG["sigma_fallback_1s"]) -> tuple[float, int, str]:
    """Realised 1-s log-return sd over [now-window, now] from consecutive-second pairs. Returns (sigma, n_returns, source)."""
    secs = [s for s, _ in series]
    lo = bisect.bisect_left(secs, now_sec - window_s); hi = bisect.bisect_right(secs, now_sec)
    seg = series[lo:hi]; ss = 0.0; n = 0
    for (a, va), (b, vb) in zip(seg, seg[1:]):
        if b - a == 1 and va > 0 and vb > 0:
            r = math.log(vb / va); ss += r * r; n += 1
    if n < min_n: return max(fallback, floor), n, "fallback"
    return max(math.sqrt(ss / n), floor), n, "realized"

def settlement_terms(series: list[tuple[int, float]], close_ts: int, now_sec: int) -> dict:
    """Decompose the settlement average at time now_sec (seconds, UTC): observed window samples (official ticks with
    value_ts in [T-60, T) received so far), their mean, the number of remaining samples m = 60 - n, the latest index value
    x_now (martingale forecast of every remaining sample) and the unit-variance factor V of the remaining-sample mean:
    Var(mean of future samples) = sigma_1s^2 * x^2 * V, V = (1/m^2) * sum_j sum_k min(l_j, l_k), l = lead time (s) of each
    remaining sample from the latest observed second (0 for window seconds that were missed in the past)."""
    T = int(close_ts); w0 = T - WINDOW_S
    secs = [s for s, _ in series]
    hi = bisect.bisect_right(secs, min(now_sec, T - 1))
    if hi == 0: return {"n": 0, "partial": None, "m": WINDOW_S, "x_now": None, "last_sec": None, "V": None, "leads": None}
    last_sec, x_now = series[hi - 1]
    lo = bisect.bisect_left(secs, w0)
    obs = [v for s, v in series[lo:hi] if w0 <= s < T]
    seen = {s for s, _ in series[lo:hi] if w0 <= s < T}
    n = len(obs); partial = (sum(obs) / n) if n else None
    leads = sorted(max(s - last_sec, 0) for s in range(w0, T) if s not in seen)
    m = len(leads)
    V = None
    if m:
        # sum_j sum_k min(l_j, l_k) with leads ascending = sum_j l_j * (2*(m-j)+1), j 1-based
        tot = sum(l * (2 * (m - j) + 1) for j, l in enumerate(leads, start=1)); V = tot / (m * m)
    return {"n": n, "partial": partial, "m": m, "x_now": x_now, "last_sec": last_sec, "V": V, "leads": (leads[0], leads[-1]) if m else None}

def kappa_for(secs_left: float, cfg: dict = CFG) -> float:
    return cfg["kappa_final"] if secs_left <= cfg["kappa_split_s"] else cfg["kappa_early"]

def blend_w_for(secs_left: float, cfg: dict = CFG) -> float:
    a, b = cfg["blend_split_s"]
    return cfg["blend_w"]["final"] if secs_left <= a else (cfg["blend_w"]["mid"] if secs_left <= b else cfg["blend_w"]["early"])

def p_used_from(p_model: float, mid: float | None, spread: float | None, secs_left: float, cfg: dict = CFG) -> tuple[float, float]:
    """(p_used, w): market-anchored probability. Falls back to the model alone when the book is not two-sided or too wide."""
    w = blend_w_for(secs_left, cfg)
    if mid is None or spread is None or spread > cfg["blend_max_spread"]: return p_model, 1.0
    return w * p_model + (1 - w) * mid, w

def p_settle_ge(terms: dict, K: float, sigma_1s: float, kappa: float = 1.0, strike_dp: int = STRIKE_DP) -> dict:
    """P(settlement average >= K) from the decomposition. Tie rule '>=' on a strike published to strike_dp decimals: the average
    is compared after rounding, so the YES region is avg >= K - 0.5 ulp. Limits: m -> 0 gives the indicator of the observed
    partial; n = 0 (before the final minute) gives the Brownian forecast of the whole window mean."""
    n, m, P, x, V = terms["n"], terms["m"], terms["partial"], terms["x_now"], terms["V"]
    K_eff = K - 0.5 * 10 ** (-strike_dp)
    if x is None: return {"p": None, "z": None, "sd_mean": None, "mu": None, "why": "no_index"}
    mu = ((n * P) if n else 0.0) + m * x                           # 60 * E[settlement]
    target = WINDOW_S * K_eff
    if m == 0 or V is None or V <= 0:
        return {"p": 1.0 if mu >= target else 0.0, "z": (math.inf if mu >= target else -math.inf), "sd_mean": 0.0, "mu": mu / WINDOW_S, "why": "determined"}
    sd_future_mean = kappa * sigma_1s * x * math.sqrt(V)              # sd of the mean of the m remaining samples ($)
    sd_mu = m * sd_future_mean                                      # sd of the 60*settlement sum
    z = (mu - target) / sd_mu
    return {"p": Phi(z), "z": z, "sd_mean": sd_mu / WINDOW_S, "mu": mu / WINDOW_S, "why": "model"}

# ------------------------------------------------------------------------------------------------------------- book / exec
def side_levels(book: dict, side: str, action: str) -> list[tuple[float, float]]:
    """Executable levels in OUR side's price space, best first. book = {bids:[(yes_px,qty)] best first, asks:[(yes_px,qty)] best first}.
    buy yes -> hit YES asks; buy no -> hit NO asks = 1 - YES bids; sell yes -> hit YES bids; sell no -> hit NO bids = 1 - YES asks."""
    bids = [(float(p), float(q)) for p, q in (book.get("bids") or []) if q and 0 < float(p) < 1]
    asks = [(float(p), float(q)) for p, q in (book.get("asks") or []) if q and 0 < float(p) < 1]
    if action == "buy":
        return sorted(asks) if side == "yes" else sorted(((round(1 - p, 6), q) for p, q in bids))
    return sorted(bids, reverse=True) if side == "yes" else sorted(((round(1 - p, 6), q) for p, q in asks), reverse=True)

def walk(levels: list[tuple[float, float]], qty: float, limit: float | None = None, action: str = "buy") -> dict:
    """Depth-weighted execution of up to `qty` against `levels` (best first), never beyond `limit` (max price when buying,
    min price when selling). Returns {qty (filled, may be partial/0), avg_px, notional, levels_used, best}."""
    filled = 0.0; notional = 0.0; used = 0
    for p, q in levels:
        if limit is not None and ((action == "buy" and p > limit + 1e-12) or (action == "sell" and p < limit - 1e-12)): break
        take = min(q, qty - filled)
        if take <= 0: break
        filled += take; notional += take * p; used += 1
        if filled >= qty - 1e-9: break
    return {"qty": filled, "avg_px": (notional / filled if filled > 0 else None), "notional": notional, "levels_used": used, "best": (levels[0][0] if levels else None)}

def fee_pc(qty: int, px: float) -> float:
    """Kalshi taker fee per contract for an order of qty at px (ceil-to-cent per ORDER, divided by qty)."""
    if qty <= 0 or not (0 < px < 1): return 0.0
    return kalshi_taker_fee(int(qty), px) / int(qty)

def adverse_haircut_pc(z: float | None, sd_mean: float | None, x_now: float | None, sigma_1s: float, latency_s: float, kappa: float = 1.0) -> float:
    """Probability-space cost of a one-sigma index move during our latency: dp/dx * (x sigma kappa sqrt(L)). dp/dx = phi(z) / sd_mean
    (sd_mean = sd of the settlement average in $). Inf when the outcome is hypersensitive (sd_mean -> 0 with z finite)."""
    if z is None or sd_mean is None or x_now is None: return 0.0
    if not math.isfinite(z): return 0.0                              # determined outcome: no sensitivity
    if sd_mean <= 0: return 1.0
    return min(1.0, phi(z) * (x_now * sigma_1s * kappa * math.sqrt(max(latency_s, 0.0))) / sd_mean)

def edge_requirement_pc(z: float | None, base_c: float = CFG["edge_base_c"], relse: float = CFG["sigma_relse"]) -> float:
    """Minimum expected net per contract: base + sigma-estimation uncertainty |z| phi(z) relse (P12 dyn_thr term, no spread term:
    the depth-weighted price already paid the spread)."""
    if z is None or not math.isfinite(z): return base_c / 100
    return base_c / 100 + abs(z) * phi(z) * relse

def entry_ev(p_yes: float, side: str, book: dict, qty_target: int, z: float | None, sd_mean: float | None, x_now: float | None, sigma_1s: float,
             latency_s: float, cfg: dict = CFG, kappa: float = 1.0) -> dict:
    """Expected net per contract and in $ for buying `side` up to qty_target: depth-weighted executable price for the obtainable qty
    at or below a bounded limit (p_side - fee - edge requirement), one fee, one adverse haircut. No double counting: the price walk
    pays the book, the haircut pays for information arriving during latency, fill_survival scales expected quantity only."""
    p_side = p_yes if side == "yes" else 1 - p_yes
    req = edge_requirement_pc(z, cfg["edge_base_c"], cfg["sigma_relse"])
    adverse = adverse_haircut_pc(z, sd_mean, x_now, sigma_1s, latency_s + cfg["order_transit_s"], kappa)
    levels = side_levels(book, side, "buy")
    best = levels[0][0] if levels else None
    # bounded limit: the most we may pay and still clear the requirement (fee evaluated at the limit itself, conservative)
    limit = None
    if best is not None:
        lim = p_side - adverse - req
        for _ in range(4): lim = p_side - adverse - req - fee_pc(qty_target, min(max(lim, 0.001), 0.999))
        limit = math.floor(min(max(lim, 0.0), cfg["max_px"]) / TICK + 1e-9) * TICK
    w = walk(levels, qty_target, limit, "buy") if (limit is not None and limit >= cfg["min_px"]) else {"qty": 0.0, "avg_px": None, "notional": 0.0, "levels_used": 0, "best": best}
    depth_within_limit = sum(q_ for p_, q_ in levels if limit is not None and p_ <= limit + 1e-12)      # displayed size we could take (uncapped)
    q = int(math.floor(w["qty"] + 1e-9)); px = w["avg_px"]
    out = {"side": side, "p_side": p_side, "best": best, "limit": limit, "qty_obtainable": q, "depth_within_limit": depth_within_limit, "px_dw": px, "req_pc": req, "adverse_pc": adverse,
           "fee_pc": None, "ev_pc": None, "ev_usd": None, "levels_used": w["levels_used"]}
    if q <= 0 or px is None: out["why"] = "no_depth_within_limit" if best is not None else "no_offer"; return out
    fee = fee_pc(q, px); ev = p_side - px - fee - adverse
    out.update({"fee_pc": fee, "ev_pc": ev, "ev_usd": ev * q * cfg["fill_survival"], "why": ("ok" if ev >= req else "edge_lt_req")})
    return out

def exit_value(side: str, book: dict, qty: int) -> dict:
    """Executable liquidation of qty contracts of `side`: depth-weighted bids on our side, taker fee once. proceeds = 0 if no bid."""
    levels = side_levels(book, side, "sell")
    w = walk(levels, qty, None, "sell")
    if w["qty"] <= 0 or w["avg_px"] is None: return {"qty": 0, "avg_px": None, "proceeds": 0.0, "fee": 0.0, "partial": True, "best_bid": None}
    q = int(math.floor(w["qty"] + 1e-9)); px = w["avg_px"]
    fee = kalshi_taker_fee(q, px) if q > 0 else 0.0
    return {"qty": q, "avg_px": px, "proceeds": q * px - fee, "fee": fee, "partial": q < qty, "best_bid": w["best"]}

def hold_or_exit(p_yes: float, side: str, book: dict, qty: int, margin_c: float = CFG["exit_margin_c"]) -> dict:
    """Hold value = p_side * qty (expected settlement, unmarked). Exit iff the FULL position can be liquidated and the executable
    proceeds beat the hold value by margin_c per contract. Never a stop-loss. Returns the comparison every time (logged)."""
    p_side = p_yes if side == "yes" else 1 - p_yes
    ex = exit_value(side, book, qty); hold = p_side * qty
    decision = "exit" if (ex["qty"] >= qty and ex["proceeds"] - hold >= margin_c / 100 * qty) else "hold"
    return {"decision": decision, "hold_value": hold, "exit_proceeds": ex["proceeds"], "exit_px": ex["avg_px"], "exit_qty": ex["qty"], "exit_fee": ex["fee"], "p_side": p_side}

def conservative_mark(side: str, book: dict | None, qty: int) -> float:
    """Open-position mark for portfolio accounting: executable liquidation value after fee; 0 when no bid / no book."""
    if not book or qty <= 0: return 0.0
    return max(0.0, exit_value(side, book, qty)["proceeds"])

# ----------------------------------------------------------------------------------------------------------- gates/decide
def staleness(now: float, cfb_recv_t: float | None, book_recv_t: float | None, cfg: dict = CFG) -> dict:
    ages = {"cfb_age_s": (None if cfb_recv_t is None else round(now - cfb_recv_t, 3)), "book_age_s": (None if book_recv_t is None else round(now - book_recv_t, 3))}
    reasons = []
    if ages["cfb_age_s"] is None or ages["cfb_age_s"] > cfg["max_cfb_age_s"]: reasons.append("stale_cfb")
    if ages["book_age_s"] is None or ages["book_age_s"] > cfg["max_book_age_s"]: reasons.append("stale_book")
    return {**ages, "ok": not reasons, "reasons": reasons}

def decide(now: float, close_ts: int, K: float, series: list[tuple[int, float]], cfb_recv_t: float | None, book: dict, book_recv_t: float | None,
           size_fn, cfg: dict = CFG, latency_s: float = 0.0, now_sec: int | None = None) -> dict:
    """One entry evaluation (pure). size_fn(side, p_side, px, avail_qty, fee_pc, p_mkt_side) -> (qty, reason) wraps risk.size
    (live) or a ledger-free Kelly (replay). now_sec = latest index second RECEIVED by `now` (replay passes it explicitly so the
    tape is read causally). Returns the full feature/probability/EV record with a `decision` and ordered `reasons`."""
    secs_left = close_ts - now
    st = staleness(now, cfb_recv_t, book_recv_t, cfg)
    if now_sec is None: now_sec = int(math.floor(now))
    sig, nret, sig_src = realized_sigma_1s(series, now_sec, cfg["vol_window_s"], cfg["vol_min_n"], cfg["vol_floor_1s"], cfg["sigma_fallback_1s"])
    terms = settlement_terms(series, close_ts, now_sec)
    kap = kappa_for(secs_left, cfg); pr = p_settle_ge(terms, K, sig, kap)
    yb = book.get("yes_bid"); ya = book.get("yes_ask")
    mid = ((yb + ya) / 2) if (yb is not None and ya is not None) else None
    spread = (ya - yb) if mid is not None else None
    p_used, w_used = (p_used_from(pr["p"], mid, spread, secs_left, cfg) if pr["p"] is not None else (None, None))
    rec = {"t": now, "close_ts": close_ts, "secs_left": round(secs_left, 2), "K": K, "x_now": terms["x_now"], "partial": terms["partial"], "n_obs": terms["n"], "m_rem": terms["m"],
           "V": terms["V"], "sigma_1s": sig, "sigma_n": nret, "sigma_src": sig_src, "kappa": kap, "p_model": pr["p"], "p_yes": p_used, "blend_w": w_used, "z": (None if pr["z"] is None or not math.isfinite(pr["z"]) else pr["z"]),
           "sd_mean": pr["sd_mean"], "mu": pr["mu"], "p_why": pr["why"], "yes_bid": yb, "yes_ask": ya, "mid": mid, "bids": book.get("bids"), "asks": book.get("asks"),
           "book_src": book.get("src"), "book_recv_t": book_recv_t, **st, "latency_s": latency_s, "decision": "abstain", "reasons": list(st["reasons"]), "ev": None, "qty": 0}
    if pr["p"] is None: rec["reasons"].append("no_index"); return rec
    if not (cfg["entry_min_secs_left"] <= secs_left <= cfg["entry_max_secs_left"]): rec["reasons"].append("outside_entry_window")
    if rec["reasons"]: return rec
    # evaluate both sides, keep the better $EV; abstain if neither clears
    evs = {s: entry_ev(p_used, s, book, cfg["lot"], pr["z"], pr["sd_mean"], terms["x_now"], sig, latency_s, cfg, kap) for s in ("yes", "no")}
    rec["ev"] = evs
    ok = [e for e in evs.values() if e["why"] == "ok" and e["px_dw"] is not None and cfg["min_px"] <= e["px_dw"] <= cfg["max_px"]]
    if not ok:
        rec["reasons"].append("no_positive_ev" if all(e["why"] in ("no_offer", "no_depth_within_limit") for e in evs.values()) else "edge_lt_req"); return rec
    e = max(ok, key=lambda e: e["ev_usd"])
    # p_used already anchors on the market (blend_w); risk.size therefore receives p_market=None (no second shrink)
    qty, why = size_fn(e["side"], e["p_side"], e["px_dw"], e["depth_within_limit"], e["fee_pc"], None)
    qty = int(min(qty, e["qty_obtainable"]))
    rec["size_reason"] = why
    if qty < cfg["min_lot"]: rec["reasons"].append("size_lt_min_lot"); return rec
    rec["decision"] = "enter"; rec["side"] = e["side"]; rec["qty"] = qty; rec["limit"] = e["limit"]; rec["px_dw"] = e["px_dw"]; rec["ev_pc"] = e["ev_pc"]; rec["p_side"] = e["p_side"]
    return rec

def paper_fill(side: str, qty: int, limit: float, book2: dict, tick: float = TICK) -> dict:
    """Marketable limit order simulated against the RE-CHECKED book: depth-weighted walk up to `limit`, + one real tick per contract
    (conservative), partial fill allowed, never more than displayed. Returns {qty, px, cost, fee, partial, status}."""
    w = walk(side_levels(book2, side, "buy"), qty, limit, "buy")
    q = int(math.floor(w["qty"] + 1e-9))
    if q <= 0: return {"qty": 0, "px": None, "cost": 0.0, "fee": 0.0, "partial": True, "status": "unfilled"}
    px = min(w["avg_px"] + tick, 0.999); cost = q * px; fee = kalshi_taker_fee(q, px)
    return {"qty": q, "px": px, "cost": cost, "fee": fee, "partial": q < qty, "status": ("partial" if q < qty else "filled")}

def settle_from_feed(series: list[tuple[int, float]], close_ts: int, K: float, strike_dp: int = STRIKE_DP) -> dict | None:
    """Our own settlement reconstruction: mean of the 60 window ticks if all 60 are present; None otherwise."""
    T = int(close_ts); secs = [s for s, _ in series]
    lo = bisect.bisect_left(secs, T - WINDOW_S); hi = bisect.bisect_left(secs, T)
    vals = [v for s, v in series[lo:hi]]
    if len(vals) != WINDOW_S: return None
    avg = round(sum(vals) / WINDOW_S, strike_dp)
    return {"avg": avg, "result": ("yes" if avg >= K else "no")}

def kelly_size_fn(bankroll: float = 250.0, frac: float = 0.25, lot_cap: int = CFG["lot"], min_lot: int = CFG["min_lot"], thin_book: int = 50, thin_fraction: float = 0.5):
    """Ledger-free sizing with the same arithmetic as risk.size (Kelly on 50/50 blend with market when available, lot cap,
    thin-book halving, min lot). Used by the replay and tests; the live loop uses risk.size itself."""
    from .risk import kelly_contracts
    def fn(side, p_side, px, avail, fee, p_mkt_side):
        pb = p_side if p_mkt_side is None else 0.5 * p_side + 0.5 * p_mkt_side
        k = kelly_contracts(pb, px, fee, bankroll=bankroll, frac=frac)
        if k <= 0: return 0, f"no edge after shrink (p={p_side:.2f} p_blend={pb:.2f} c={px + fee:.3f})"
        av = avail * (thin_fraction if avail < thin_book else 1.0)
        q = int(min(k, lot_cap, av))
        if q < min_lot: return 0, f"below min lot (kelly={k} cap={lot_cap} avail={avail})"
        return q, f"kelly={k} p_blend={pb:.2f} cap={lot_cap}"
    return fn

# ---------------------------------------------------------------------------------------------------------------- live loop
ROOT = pathlib.Path("data/sol-v1")

def _append(path: pathlib.Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh: fh.write(json.dumps(obj) + "\n")

def _log(root: pathlib.Path, m: str):
    line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {m}"; print(line, flush=True)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "runner.log").open("a", encoding="utf-8") as fh: fh.write(line + "\n")

def _rest_book(k, ticker: str) -> dict:
    from .s7_live import book_sides
    t0 = time.time(); ob = k.orderbook(ticker); t1 = time.time(); bs = book_sides(ob, depth=5)
    return {"yes_bid": bs["yes_bid"], "yes_ask": bs["yes_ask"], "bids": bs["bids"], "asks": bs["asks"], "recv_t": t1, "fetch_s": round(t1 - t0, 3), "src": "rest"}

def _ws_book(ticker: str, root: pathlib.Path, max_age: float) -> dict | None:
    p = root / "book" / "latest.json"
    try: d = json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None
    if d.get("ticker") != ticker or time.time() - d.get("recv_t", 0) > max_age: return None
    return {"yes_bid": d.get("yes_bid"), "yes_ask": d.get("yes_ask"), "bids": d.get("bids"), "asks": d.get("asks"), "recv_t": d["recv_t"], "fetch_s": 0.0, "src": "ws", "seq": d.get("seq")}

class CfbTail:
    """Incremental reader of data/cfb/<ASSET>.jsonl: first call tails the last `keep_s` seconds, later calls read only the bytes
    appended since (no re-parsing of the whole tail every poll). Keeps (sec, value) sorted/deduped and the latest receipt time."""
    def __init__(self, asset: str = ASSET, keep_s: float = CFG["vol_window_s"] + 120, root: pathlib.Path = pathlib.Path("data/cfb")):
        self.path = root / f"{asset}.jsonl"; self.keep = keep_s; self.off: int | None = None; self.buf = b""; self.series: list[tuple[int, float]] = []; self.recv_t: float | None = None
    def _ingest(self, r: dict):
        v = r.get("value"); ts = r.get("value_ts_ms")
        if not v or not ts: return
        sec = int(ts) // 1000
        if self.series and sec <= self.series[-1][0]:
            if sec == self.series[-1][0]: self.series[-1] = (sec, float(v))
            return
        self.series.append((sec, float(v))); self.recv_t = r.get("t", self.recv_t)
    def refresh(self, now: float) -> tuple[list[tuple[int, float]], float | None]:
        if not self.path.exists(): return self.series, self.recv_t
        if self.off is None:
            from .feed_tail import cfb_recent
            for r in cfb_recent(ASSET, self.keep, now, self.path.parent): self._ingest(r)
            self.off = self.path.stat().st_size
        else:
            with self.path.open("rb") as fh:
                fh.seek(self.off); chunk = fh.read(); self.off = fh.tell()
            data = self.buf + chunk; lines = data.split(b"\n"); self.buf = lines.pop()      # keep a trailing partial line
            for l in lines:
                if not l.strip(): continue
                try: self._ingest(json.loads(l))
                except Exception: continue
        cut = int(now - self.keep)
        if self.series and self.series[0][0] < cut:
            i = bisect.bisect_left([s for s, _ in self.series], cut); self.series = self.series[i:]
        return self.series, self.recv_t

_CFB = None
def _cfb_tail(now: float, seconds: float) -> tuple[list[tuple[int, float]], float | None]:
    global _CFB
    if _CFB is None: _CFB = CfbTail(keep_s=max(seconds, CFG["vol_window_s"] + 120))
    return _CFB.refresh(now)

class Positions:
    """Paper positions for this family: fills.jsonl is the source of truth; intent ids are stable (family:ticker:side)."""
    def __init__(self, root: pathlib.Path):
        self.root = root; self.fills = root / "fills.jsonl"; self.open: dict[str, dict] = {}; self.intents: set[str] = set()
        if self.fills.exists():
            for l in self.fills.read_text(encoding="utf-8").splitlines():
                if not l.strip(): continue
                r = json.loads(l); self.intents.add(r.get("intent_id", ""))
                if r.get("kind") == "fill" and r.get("status") == "open": self.open[r["ticker"]] = r
                elif r.get("kind") in ("settle", "exit") and r["ticker"] in self.open: self.open.pop(r["ticker"], None)
    def has_close(self, close_ts: int) -> bool: return any(p["close_ts"] == close_ts for p in self.open.values())

def run(root: pathlib.Path = ROOT, poll_s: float = 2.0, poll_final_s: float = 0.8, scored_only: bool = False, use_ws: bool = True):
    from .kalshi_public import KalshiPublic
    from .risk import Ledger, size as risk_size, MODE
    from .s7_live import single_instance
    if MODE != "paper": raise RuntimeError("SOL-v1 is paper-only; refuse to start with PREDHFT_MODE != paper")
    _lock = single_instance(root)
    k = KalshiPublic(min_interval_s=0.25); pos = Positions(root); led = Ledger(root, family=FAMILY)
    cfg = dict(CFG); _log(root, f"start {VERSION} series={SERIES} cfg={json.dumps(cfg)} scored_only={scored_only} use_ws={use_ws} mode={MODE}")
    market: dict | None = None; mkt_close = None; last_mark = 0.0; last_disc = 0.0; cycles: set[int] = set()
    while True:
        try:
            now = time.time(); close_ts = (int(now) // PERIOD_S + 1) * PERIOD_S; secs_left = close_ts - now
            # ---- settlement of closed positions (exchange result; feed cross-check)
            for tk, p in list(pos.open.items()):
                if now < p["close_ts"] + 90: continue
                try: m = k.get(f"/markets/{tk}")["market"]
                except Exception: continue
                res = m.get("result")
                if res in ("yes", "no"):
                    series, _ = _cfb_tail(now, 1200); own = settle_from_feed(series, p["close_ts"], p["K"])
                    win = res == p["side"]; pnl = (1.0 if win else 0.0) * p["qty"] - p["cost"] - p["fee"]
                    row = {"kind": "settle", "t": now, "intent_id": p["intent_id"], "ticker": tk, "close_ts": p["close_ts"], "side": p["side"], "qty": p["qty"], "result": res,
                           "expiration_value": m.get("expiration_value"), "feed_avg": (own or {}).get("avg"), "feed_result": (own or {}).get("result"), "pnl": pnl}
                    _append(pos.fills, row); pos.open.pop(tk, None); led.record_settle(tk, pnl)
                    _log(root, f"SETTLED {tk} side={p['side']} result={res} feed={row['feed_result']} qty={p['qty']} pnl={pnl:+.2f}")
            # ---- market discovery (cached per close; floor_strike appears shortly after open)
            if mkt_close != close_ts or market is None:
                if now - last_disc >= 5:
                    last_disc = now
                    try:
                        iso = dt.datetime.fromtimestamp(close_ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        ms = [m for m in k.markets(series_ticker=SERIES, status="open", limit=200, max_pages=2) if m.get("close_time") == iso and m.get("floor_strike")]
                        market = ms[0] if ms else None; mkt_close = close_ts if market else None
                        if market: _log(root, f"MARKET {market['ticker']} K={float(market['floor_strike'])} close={iso}"); cycles.add(close_ts)
                    except Exception as e: _log(root, f"discovery error {type(e).__name__}: {str(e)[:120]}"); market = None
            # ---- fixed-interval portfolio mark (always, incl. idle)
            if now - last_mark >= cfg["mark_interval_s"]:
                last_mark = now; realized = sum(e.get("pnl", 0.0) for e in led.events(0) if e["kind"] == "settle" and e.get("family") == FAMILY)
                mark = 0.0
                for tk, p in pos.open.items():
                    b = p.get("_last_book"); mark += conservative_mark(p["side"], b, p["qty"]) if b else 0.0
                _append(root / "marks.jsonl", {"t": now, "realized": round(realized, 4), "open_cost": round(sum(p["cost"] + p["fee"] for p in pos.open.values()), 4), "open_mark": round(mark, 4),
                                               "equity": round(250.0 + realized - sum(p["cost"] + p["fee"] for p in pos.open.values()) + mark, 4), "n_open": len(pos.open)})
            if market is None: time.sleep(3); continue
            tk = market["ticker"]; K = float(market["floor_strike"])
            # ---- inputs with their own receipt stamps
            t_loop = time.time()
            series, cfb_recv = _cfb_tail(t_loop, cfg["vol_window_s"] + 120)
            book = (_ws_book(tk, root, cfg["max_book_age_s"]) if use_ws else None) or _rest_book(k, tk)
            t_dec = time.time()
            held = pos.open.get(tk)
            if held is not None:
                held["_last_book"] = book
                now_sec = int(t_dec); sl = close_ts - t_dec; sig, _, _ = realized_sigma_1s(series, now_sec); pr = p_settle_ge(settlement_terms(series, close_ts, now_sec), K, sig, kappa_for(sl, cfg))
                if pr["p"] is not None:
                    yb_, ya_ = book.get("yes_bid"), book.get("yes_ask"); mid_ = ((yb_ + ya_) / 2) if (yb_ is not None and ya_ is not None) else None
                    p_u, _w = p_used_from(pr["p"], mid_, (ya_ - yb_) if mid_ is not None else None, sl, cfg)
                    h = hold_or_exit(p_u, held["side"], book, held["qty"], cfg["exit_margin_c"]); h["p_model"] = pr["p"]
                    _append(root / "decisions.jsonl", {"kind": "hold_exit", "t": t_dec, "ticker": tk, "close_ts": close_ts, "secs_left": round(close_ts - t_dec, 2), "p_yes": p_u, **h, "book_age_s": round(t_dec - book["recv_t"], 3), "book_src": book["src"]})
                    if h["decision"] == "exit" and not scored_only:
                        ex = exit_value(held["side"], book, held["qty"]); pnl = ex["proceeds"] - held["cost"] - held["fee"]
                        _append(pos.fills, {"kind": "exit", "t": t_dec, "intent_id": held["intent_id"], "ticker": tk, "close_ts": close_ts, "side": held["side"], "qty": held["qty"], "exit_px": ex["avg_px"], "exit_fee": ex["fee"], "proceeds": ex["proceeds"], "pnl": pnl, "p_side_at_exit": h["p_side"]})
                        pos.open.pop(tk, None); led.record_settle(tk, pnl); _log(root, f"PAPER EXIT {held['side'].upper()} {held['qty']}@{ex['avg_px']:.3f} {tk} proceeds={ex['proceeds']:.2f} pnl={pnl:+.2f} p_side={h['p_side']:.3f}")
            else:
                def size_fn(side, p_side, px, avail, fee, p_mkt_side):
                    return risk_size(SERIES, p_side, px, avail, led, lot_cap=cfg["lot"], p_market=p_mkt_side, fee_per_contract=fee, close_ts=close_ts)
                rec = decide(t_dec, close_ts, K, series, cfb_recv, book, book["recv_t"], size_fn, cfg, latency_s=t_dec - t_loop)
                rec.update({"kind": "entry_eval", "ticker": tk, "version": VERSION, "book_fetch_s": book.get("fetch_s")})
                _append(root / "snapshots.jsonl", rec)
                if rec["decision"] == "enter" and not pos.has_close(close_ts):
                    intent_id = f"{FAMILY}:{tk}:{rec['side']}"
                    if intent_id in pos.intents: rec["decision"] = "abstain"; rec["reasons"].append("intent_exists")
                    elif scored_only: rec["decision"] = "abstain"; rec["reasons"].append("scored_only")
                    else:
                        pos.intents.add(intent_id)
                        time.sleep(cfg["order_transit_s"])                       # simulated submit->match transit, then re-check the book an order would meet
                        book2 = (_ws_book(tk, root, 0.5) if use_ws else None) or _rest_book(k, tk); t_fill = time.time(); latency = t_fill - t_dec
                        if latency > cfg["max_latency_s"]:
                            _append(root / "decisions.jsonl", {"kind": "entry_abort", "t": t_fill, "intent_id": intent_id, "ticker": tk, "reason": "latency", "latency_s": round(latency, 3)}); _log(root, f"ABORT {tk} latency {latency:.2f}s > {cfg['max_latency_s']}")
                        else:
                            f = paper_fill(rec["side"], rec["qty"], rec["limit"], book2)
                            row = {"kind": "fill", "t": t_fill, "intent_id": intent_id, "ticker": tk, "close_ts": close_ts, "K": K, "side": rec["side"], "qty": f["qty"], "intended_qty": rec["qty"], "limit": rec["limit"], "px": f["px"], "px_dw_at_decision": rec["px_dw"],
                                   "cost": f["cost"], "fee": f["fee"], "partial": f["partial"], "status": ("open" if f["qty"] > 0 else "unfilled"), "p_yes": rec["p_yes"], "p_model": rec["p_model"], "blend_w": rec["blend_w"], "p_side": rec["p_side"], "ev_pc": rec["ev_pc"], "mid": rec["mid"], "secs_left": rec["secs_left"],
                                   "x_now": rec["x_now"], "partial_avg": rec["partial"], "n_obs": rec["n_obs"], "sigma_1s": rec["sigma_1s"], "cfb_age_s": rec["cfb_age_s"], "book_age_s": rec["book_age_s"], "book_src": book["src"], "recheck_src": book2["src"],
                                   "decision_latency_s": round(latency, 3), "recheck_recv_t": book2["recv_t"], "size_reason": rec.get("size_reason"), "version": VERSION, "mode": MODE}
                            _append(pos.fills, row)
                            if f["qty"] > 0:
                                pos.open[tk] = row; led.record_open(tk, SERIES, f["cost"], close_ts=close_ts, side=rec["side"])
                                _log(root, f"PAPER BUY {rec['side'].upper()} {f['qty']}/{rec['qty']}@{f['px']:.3f} {tk} p_side={rec['p_side']:.3f} ev={100 * rec['ev_pc']:+.1f}c T-{rec['secs_left']:.0f}s lat={latency:.2f}s src={book['src']}")
                            else: _log(root, f"UNFILLED {tk} {rec['side']} limit={rec['limit']:.3f} re-check best={rec['ev'][rec['side']]['best']}")
            time.sleep(poll_final_s if secs_left < 120 else poll_s)
        except KeyboardInterrupt: raise
        except Exception as e:
            _log(root, f"error {type(e).__name__}: {str(e)[:160]}"); time.sleep(5)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT)); ap.add_argument("--poll", type=float, default=2.0); ap.add_argument("--poll-final", type=float, default=0.8)
    ap.add_argument("--scored-only", action="store_true"); ap.add_argument("--no-ws", action="store_true")
    a = ap.parse_args(); run(pathlib.Path(a.root), a.poll, a.poll_final, a.scored_only, not a.no_ws)
