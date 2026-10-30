"""P25 — pre-digested decision packet for the LLM trader (docs/PREREG.md §P25). Pure functions, no I/O except the two loaders
at the bottom (tail table cache, Quant A's conditions brief), which the sidecar calls once per decision.

Why (audit of 166 Opus answers, docs/P25_PROMPT_AUDIT_2026-10-29.md): the v1 packet handed the model the policy's Gaussian
inputs (p_model, z, sd) and the model re-derived p_model (p_own within 0.6 c of p_model in median; p_llm between mid and p_model in
160/166 answers), inherited the Gaussian's variance inflation (less extreme than the mid in 141/166), and was on the wrong side of
every large disagreement class (favourites >= .80 it doubted won 13/14; tails <= .20 it lifted won 1/15). It also double-counted
the spread ("spread plus fees" against an ask that already is the spread, 44/166), declared 3-9 c edges "too thin" while trading
3-6 c edges, and used the words "fat tails" instead of a number.

What a trader needs pre-digested instead of raw inputs:
  * fee-adjusted BREAKEVEN probability for YES and NO at the EXECUTABLE (depth-weighted) price for lots 5/10/20;
  * the MOVE REQUIRED for each side to win given the partial average already observed (bp, and in sigma units at the remaining
    variance-equivalent horizon V), using the exact settlement decomposition (sol_v1.settlement_terms);
  * the EMPIRICAL tail frequency of that move from the 36-day 1-min history (vol-normalised, signed), next to the Gaussian;
  * realised vs market-IMPLIED vol (the sigma at which the mid prices the same move);
  * book depth / imbalance and the last 60 s of quote changes (how far the price you see moves while your order travels);
  * BTC/ETH co-move in the last 1 / 5 min with the historical beta (is the SOL move idiosyncratic or market-wide);
  * Quant A's realised hit / market-implied / net c per contract for the current state bucket when reports/forensics/conditions_brief.json exists.
The policy's p_model is either shown LAST under `other_forecasters` (variant v2a) or not at all (variant v2b).
"""
from __future__ import annotations
import bisect, datetime as dt, json, math, pathlib, time, zoneinfo
from statistics import NormalDist
from . import sol_v1 as S
from .fees import kalshi_taker_fee

VERSION = "P25-packet-v2"
PT = zoneinfo.ZoneInfo("America/Los_Angeles")      # Quant A's hour buckets are Pacific (same zone as his script)
LOTS = (5, 10, 20)
TAIL_HORIZONS_MIN = (3, 4, 5, 8, 9, 10)
TAIL_K_GRID = [round(-4.0 + 0.1 * i, 1) for i in range(81)]      # signed sigma grid for the empirical CDF
TAIL_ABS_ROWS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
_N = NormalDist()


def _r(x, d=4):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), d)

def Phi(x: float) -> float: return _N.cdf(x)
def inv_Phi(p: float) -> float: return _N.inv_cdf(min(max(p, 1e-6), 1 - 1e-6))


# ------------------------------------------------------------------------------------------------------- fees / breakeven
def fee_pc(px: float, qty: int) -> float:
    """Kalshi taker fee per contract for an order of `qty` at `px` (ceil-to-cent per ORDER / qty)."""
    if qty <= 0 or not (0 < px < 1): return 0.0
    return kalshi_taker_fee(int(qty), px) / int(qty)

def breakeven_p(px: float, qty: int) -> float:
    """Probability your side must have for buying `qty` at `px` to be zero-EV after the taker fee: px + fee_pc."""
    return px + fee_pc(px, qty)

def executable(book: dict, side: str, qty: int) -> dict:
    """Depth-weighted executable price for buying `qty` of `side` now (walk the displayed offers in our price space, no limit),
    the fillable quantity, the fee per contract and the breakeven probability. NO price = 1 - yes_bid (sol_v1.side_levels)."""
    lv = S.side_levels(book, side, "buy")
    w = S.walk(lv, qty, None, "buy")
    q = int(math.floor(w["qty"] + 1e-9))
    if q <= 0 or w["avg_px"] is None:
        return {"lot": qty, "fillable": 0, "px_dw": None, "best": (lv[0][0] if lv else None), "fee_pc": None, "breakeven_p": None, "partial": True}
    px = w["avg_px"]
    return {"lot": qty, "fillable": q, "px_dw": _r(px, 4), "best": _r(lv[0][0], 4), "fee_pc": _r(fee_pc(px, q), 4), "breakeven_p": _r(breakeven_p(px, q), 4), "partial": q < qty}

def breakevens(book: dict, lots=LOTS) -> dict:
    """{yes: {lot: executable}, no: {...}} for the standard lots, plus the displayed top-3 depth and the depth imbalance."""
    out = {"yes": {str(q): executable(book, "yes", q) for q in lots}, "no": {str(q): executable(book, "no", q) for q in lots}}
    out["depth"] = depth_imbalance(book)
    return out

def depth_imbalance(book: dict, levels: int = 3) -> dict:
    bids = [(float(p), float(q)) for p, q in (book.get("bids") or [])[:levels] if q]
    asks = [(float(p), float(q)) for p, q in (book.get("asks") or [])[:levels] if q]
    qb = sum(q for _, q in bids); qa = sum(q for _, q in asks)
    yb, ya = book.get("yes_bid"), book.get("yes_ask")
    return {"yes_bid": yb, "yes_ask": ya, "spread_c": (_r(100 * (ya - yb), 2) if yb is not None and ya is not None else None), "bid_qty_top3": _r(qb, 1), "ask_qty_top3": _r(qa, 1),
            "imbalance": (_r((qb - qa) / (qb + qa), 3) if qb + qa > 0 else None), "bids_top3": [[_r(p, 3), _r(q, 1)] for p, q in bids], "asks_top3": [[_r(p, 3), _r(q, 1)] for p, q in asks]}


# ----------------------------------------------------------------------------------------------------- required move
def required_move(K: float, x_now: float, partial: float | None, n_obs: int, m_rem: int, sigma_1s: float, V: float | None, strike_dp: int = S.STRIKE_DP) -> dict:
    """What the REMAINING samples' mean must be for YES to win, given the observed partial average: avg_needed = (60 K_eff - n P) / m.
    Returns the required move from x_now in bp (YES needs the remaining mean >= that; NO needs it below), the sd of the remaining
    mean in bp at the variance-equivalent horizon V (sigma_1s * sqrt(V), NO kappa), the move in sigma units and the Gaussian
    probability at kappa 1 for reference. m = 0 -> determined by the partial."""
    K_eff = K - 0.5 * 10 ** (-strike_dp)
    if m_rem <= 0:
        return {"determined": True, "yes_wins": bool((partial or 0.0) >= K_eff), "avg_needed": None, "move_bp_for_yes": None, "sd_remaining_mean_bp": 0.0, "sigma_units_for_yes": None, "horizon_eff_s": 0.0, "p_gauss_kappa1": (1.0 if (partial or 0.0) >= K_eff else 0.0)}
    avg_needed = (S.WINDOW_S * K_eff - (n_obs * partial if n_obs and partial is not None else 0.0)) / m_rem
    move_bp = 1e4 * (avg_needed / x_now - 1.0)
    sd_bp = 1e4 * sigma_1s * math.sqrt(V) if V and V > 0 else None
    k = (move_bp / sd_bp) if sd_bp else None
    return {"determined": False, "avg_needed": _r(avg_needed, 4), "move_bp_for_yes": _r(move_bp, 2), "move_bp_for_no": _r(-move_bp, 2), "sd_remaining_mean_bp": _r(sd_bp, 2),
            "sigma_units_for_yes": _r(k, 3), "horizon_eff_s": _r(V, 1), "p_gauss_kappa1": (_r(1 - Phi(k), 4) if k is not None else None), "n_observed": int(n_obs or 0), "n_remaining": int(m_rem)}

def implied_sigma(mid: float | None, mu: float | None, K: float, x_now: float, V: float | None, m_rem: int, sigma_1s: float) -> dict:
    """The 1-s sigma at which a Gaussian prices the mid: mid = Phi((mu - K) / (sigma_impl x sqrt(V) m/60)). Realised / implied
    ratio > 1 means the feed is currently more volatile than the market assumes (favours the side needing the move); < 1 the
    opposite. None when the mid is within 2 c of 0.5 or the distance is tiny (the ratio is not identified there)."""
    out = {"sigma_realised_1s_bp": _r(1e4 * sigma_1s, 3), "sigma_implied_1s_bp": None, "ratio_realised_over_implied": None}
    if mid is None or mu is None or not V or V <= 0 or m_rem <= 0 or abs(mid - 0.5) < 0.02 or abs(mu - K) < 1e-4 * x_now * 0.5: return out
    z = inv_Phi(mid)
    if abs(z) < 1e-9 or (mu - K) * z <= 0: return out
    si = (mu - K) / (z * x_now * math.sqrt(V) * (m_rem / S.WINDOW_S))
    out.update({"sigma_implied_1s_bp": _r(1e4 * si, 3), "ratio_realised_over_implied": _r(sigma_1s / si, 3) if si > 0 else None})
    return out


# ----------------------------------------------------------------------------------------------------- empirical tails
def _trailing_rv(close, ts, window: int = 60):
    """Trailing `window`-minute realised 1-min vol (bp) at each index, None where the minutes are not contiguous."""
    n = len(close); out = [None] * n
    sq = [None] * n
    for i in range(1, n):
        if ts[i] - ts[i - 1] == 60 and close[i - 1] > 0 and close[i] > 0: sq[i] = (1e4 * math.log(close[i] / close[i - 1])) ** 2
    acc = 0.0; cnt = 0; bad = 0
    for i in range(n):
        if sq[i] is None: bad += 1
        else: acc += sq[i]; cnt += 1
        j = i - window
        if j >= 0:
            if sq[j] is None: bad -= 1
            else: acc -= sq[j]; cnt -= 1
        if i >= window and bad == 0 and cnt == window and acc > 0: out[i] = math.sqrt(acc / cnt)
    return out

def build_tail_table(sol_df, btc_df=None, eth_df=None, horizons=TAIL_HORIZONS_MIN, grid=TAIL_K_GRID, rv_window: int = 60) -> dict:
    """Empirical signed distribution of the h-minute log move normalised by the trailing 60-min 1-min realised vol:
    k = move_bp / (rv_1m_bp * sqrt(h)). For each horizon: P(k >= g) on the grid (survival function), P(|k| >= row) next to the
    Gaussian, n, sd(k). Plus SOL~BTC / SOL~ETH 1-min betas and correlations over the same history. Pure (pandas in, dict out)."""
    d = sol_df.sort_values("ts"); ts = [int(v) for v in d.ts.values]; cl = [float(v) for v in d.close.values]
    rv = _trailing_rv(cl, ts, rv_window)
    table = {}
    for h in horizons:
        ks = []
        for i in range(len(cl) - h):
            if rv[i] and ts[i + h] - ts[i] == 60 * h and cl[i] > 0 and cl[i + h] > 0:
                ks.append(1e4 * math.log(cl[i + h] / cl[i]) / (rv[i] * math.sqrt(h)))
        ks.sort(); n = len(ks)
        if n < 500: continue
        surv = [round((n - bisect.bisect_left(ks, g)) / n, 5) for g in grid]
        mean = sum(ks) / n; sd = math.sqrt(sum((k - mean) ** 2 for k in ks) / (n - 1))
        absrows = {str(a): {"empirical": round(sum(1 for k in ks if abs(k) >= a) / n, 4), "gaussian": round(2 * (1 - Phi(a)), 4)} for a in TAIL_ABS_ROWS}
        table[str(h)] = {"n": n, "sd_k": round(sd, 3), "mean_k": round(mean, 4), "survival": surv, "abs_rows": absrows}
    out = {"version": VERSION, "built_at": time.time(), "rv_window_min": rv_window, "grid": list(grid), "horizons_min": list(table.keys()), "table": table,
           "history": {"ts_first": ts[0], "ts_last": ts[-1], "n_candles": len(ts), "days": round((ts[-1] - ts[0]) / 86400, 1)}, "betas": {}}
    for name, other in (("BTC", btc_df), ("ETH", eth_df)):
        if other is None or not len(other): continue
        o = other.sort_values("ts"); ots = {int(t): float(c) for t, c in zip(o.ts.values, o.close.values)}
        xs = []; ys = []
        for i in range(1, len(cl)):
            if ts[i] - ts[i - 1] == 60 and ts[i] in ots and ts[i - 1] in ots and cl[i - 1] > 0 and ots[ts[i - 1]] > 0:
                ys.append(math.log(cl[i] / cl[i - 1])); xs.append(math.log(ots[ts[i]] / ots[ts[i - 1]]))
        if len(xs) > 100:
            mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
            cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)); vx = sum((a - mx) ** 2 for a in xs); vy = sum((b - my) ** 2 for b in ys)
            out["betas"][f"SOL_{name}"] = {"beta": round(cov / vx, 3) if vx > 0 else None, "corr": round(cov / math.sqrt(vx * vy), 3) if vx > 0 and vy > 0 else None, "n": len(xs)}
    return out

def nearest_horizon(table: dict, horizon_min: float) -> str | None:
    hs = table.get("horizons_min") or []
    if not hs: return None
    return min(hs, key=lambda h: abs(float(h) - horizon_min))

def tail_survival(table: dict, horizon_min: float, k: float) -> float | None:
    """P(signed normalised move >= k) at the nearest tabulated horizon, linearly interpolated on the grid; clamped at the ends."""
    h = nearest_horizon(table, horizon_min)
    if h is None: return None
    g = table["grid"]; s = table["table"][h]["survival"]
    if k <= g[0]: return s[0]
    if k >= g[-1]: return s[-1]
    i = bisect.bisect_right(g, k) - 1
    w = (k - g[i]) / (g[i + 1] - g[i])
    return round(s[i] + w * (s[i + 1] - s[i]), 5)

def empirical_block(table: dict | None, req: dict, rv_1m_bp_now: float | None, sigma_1s: float) -> dict | None:
    """Empirical P(YES) / P(NO) for the required move at the effective horizon, under two vol normalisations (the candle 60-min rv
    the table was built with, and the official-feed 1-s sigma x sqrt(60)), plus the |k| rows for this horizon vs the Gaussian."""
    if not table or req.get("determined") or req.get("move_bp_for_yes") is None or not req.get("horizon_eff_s"): return None
    h_min = req["horizon_eff_s"] / 60.0; hk = nearest_horizon(table, h_min)
    if hk is None: return None
    mv = req["move_bp_for_yes"]; out = {"horizon_used_min": float(hk), "horizon_eff_min": _r(h_min, 2), "history_days": table.get("history", {}).get("days"), "n_windows": table["table"][hk]["n"]}
    for tag, vol in (("candle_rv60", rv_1m_bp_now), ("feed_sigma", 1e4 * sigma_1s * math.sqrt(60) if sigma_1s else None)):
        if vol and vol > 0:
            k = mv / (vol * math.sqrt(h_min)); p = tail_survival(table, h_min, k)
            out[tag] = {"vol_1m_bp": _r(vol, 2), "k_for_yes": _r(k, 3), "p_yes_empirical": p, "p_no_empirical": (_r(1 - p, 5) if p is not None else None), "p_yes_gaussian": _r(1 - Phi(k), 4)}
    out["abs_rows_this_horizon"] = {f"P(|move|>={a}sd)": v for a, v in table["table"][hk]["abs_rows"].items()}
    return out


# ------------------------------------------------------------------------------------------------- book tape / co-move
def quote_changes(tape: list[dict], now: float, span_s: float = 60.0) -> dict | None:
    """Last `span_s` of ws book rows (recv_t, yes_bid, yes_ask) before `now`: number of mid changes, mid then vs now, min/max mid,
    largest 1-row jump (c). This is how much the price you see can move while your order travels (~6 s LLM + transit)."""
    rows = [r for r in tape if r.get("recv_t") is not None and now - span_s <= r["recv_t"] <= now and r.get("yes_bid") is not None and r.get("yes_ask") is not None]
    if len(rows) < 2: return None
    rows.sort(key=lambda r: r["recv_t"]); mids = [(r["yes_bid"] + r["yes_ask"]) / 2 for r in rows]
    ch = sum(1 for a, b in zip(mids, mids[1:]) if abs(a - b) > 1e-9)
    jump = max((abs(a - b) for a, b in zip(mids, mids[1:])), default=0.0)
    return {"span_s": span_s, "n_updates": len(rows), "n_mid_changes": ch, "mid_then": _r(mids[0], 4), "mid_now": _r(mids[-1], 4), "mid_change_c": _r(100 * (mids[-1] - mids[0]), 2),
            "mid_min": _r(min(mids), 4), "mid_max": _r(max(mids), 4), "mid_range_c": _r(100 * (max(mids) - min(mids)), 2), "largest_jump_c": _r(100 * jump, 2),
            "spread_now_c": _r(100 * (rows[-1]["yes_ask"] - rows[-1]["yes_bid"]), 2)}

def comove_block(cs: dict | None, betas: dict | None) -> dict | None:
    """SOL vs BTC/ETH returns over the last 1 and 5 min (closed candles), the historical 1-min beta, and the beta-explained vs
    residual (idiosyncratic) part of SOL's 5-min move."""
    if not cs or not cs.get("SOL"): return None
    sol = cs["SOL"]; out = {"SOL": {"ret_1m_bp": (sol.get("returns_1m_bp") or [None])[-1], "ret_5m_bp": sol.get("ret_5m_bp"), "rv_1m_bp_60m": sol.get("rv_1m_bp")}}
    for a in ("BTC", "ETH"):
        c = cs.get(a)
        if not c: continue
        b = (betas or {}).get(f"SOL_{a}") or {}
        r5 = c.get("ret_5m_bp"); r1 = (c.get("returns_1m_bp") or [None])[-1]
        blk = {"ret_1m_bp": r1, "ret_5m_bp": r5, "rv_1m_bp_60m": c.get("rv_1m_bp"), "beta_hist_1m": b.get("beta"), "corr_hist_1m": b.get("corr")}
        if r5 is not None and sol.get("ret_5m_bp") is not None and b.get("beta") is not None:
            blk["sol_5m_explained_by_beta_bp"] = _r(b["beta"] * r5, 2); blk["sol_5m_residual_bp"] = _r(sol["ret_5m_bp"] - b["beta"] * r5, 2)
        out[a] = blk
    return out


# -------------------------------------------------------------------------------------- Quant A conditions brief
# Bucket definitions copied from scripts/forensics_2026_10_29.py (Quant A) so the live state lands in the same cells.
def b_time(s): return "T-10-60s" if s < 60 else "T-60-120s" if s < 120 else "T-2-5m" if s < 300 else "T-5-10m" if s < 600 else "T-10-14m"
def b_px(p): return "px<.10" if p < .10 else "px.10-.25" if p < .25 else "px.25-.50" if p < .50 else "px.50-.75" if p < .75 else "px.75-.90" if p < .90 else "px>=.90"
def b_absz(z):
    if z is None: return "z_na"
    a = abs(z); return "|z|<.5" if a < .5 else "|z|.5-1" if a < 1 else "|z|1-2" if a < 2 else "|z|>=2"
def b_edge(e): return "medge<=0" if e <= 0 else "medge0-5c" if e <= .05 else "medge5-15c" if e <= .15 else "medge>15c"
def b_spread(sp): return "spr<=1c" if sp <= 0.0101 else "spr1-3c" if sp <= 0.0301 else "spr>3c"
def b_depth(q): return "depth<50" if q < 50 else "depth50-200" if q < 200 else "depth>=200"
def b_vol(sg): return "vol<.7bp" if sg < 0.7e-4 else "vol.7-1.2bp" if sg < 1.2e-4 else "vol>=1.2bp"
def b_partial(n): return "partial0" if n == 0 else "partial1-30" if n <= 30 else "partial31-59"
def b_hour(h): return "h00-05PT" if h < 6 else "h06-11PT" if h < 12 else "h12-17PT" if h < 18 else "h18-23PT"
def b_mid(m): return "mid<.10" if m < .1 else "mid.10-.30" if m < .3 else "mid.30-.70" if m < .7 else "mid.70-.90" if m < .9 else "mid>=.90"

def qa_state_buckets(secs_left: float | None, book: dict, sigma_1s: float | None, n_obs: int, hour_pt: int | None, mid: float | None, p_model: float | None, z: float | None) -> dict:
    """The current state in Quant A's bucket labels. Per side: executable price for lot 10 + 1 tick (his execution model), best-level
    depth, model edge and with/against-model pick (the last two only when p_model is given, i.e. variant v2a)."""
    out = {"tb": (b_time(secs_left) if secs_left is not None else None), "sprb": (b_spread(book["yes_ask"] - book["yes_bid"]) if book.get("yes_ask") is not None and book.get("yes_bid") is not None else None),
           "volb": (b_vol(sigma_1s) if sigma_1s else None), "partb": b_partial(int(n_obs or 0)), "hourb": (b_hour(hour_pt) if hour_pt is not None else None), "midb": (b_mid(mid) if mid is not None else None), "zb": (b_absz(z) if p_model is not None else None), "sides": {}}
    for side in ("yes", "no"):
        lv = S.side_levels(book, side, "buy"); w = S.walk(lv, 10, None, "buy") if lv else None
        if not w or w.get("avg_px") is None or w["qty"] <= 0: out["sides"][side] = None; continue
        px = min(0.99, w["avg_px"] + 0.01); d = {"px_exec_lot10_plus_tick": _r(px, 3), "pxb": b_px(px), "depb": b_depth(lv[0][1]), "fav": ("favourite" if px >= 0.5 else "longshot")}
        if p_model is not None and mid is not None:
            p_side = p_model if side == "yes" else 1 - p_model; m_side = mid if side == "yes" else 1 - mid
            d["edgeb"] = b_edge(p_side - px); d["pick"] = "with_model" if p_side > m_side else "against_model"
        out["sides"][side] = d
    return out

def _cell(s: dict) -> dict:
    return {"state": s["state"], "scope": ("OOS" if s["scope"].endswith("OOS") else "IN_SAMPLE"), "n_closes": s.get("n_closes"), "hit": s.get("hit"), "mkt_implied": s.get("mkt_implied"), "net_c": s.get("net_c"), "se_c": s.get("se_c"), "verdict": s.get("verdict")}

def conditions_lookup_qa(brief: dict, st: dict, max_cells: int = 14) -> dict | None:
    """Cells of Quant A's brief (reports/forensics/conditions_brief.json, key 'states') for the current state: per side the
    (time x price x side) cell in both scopes, and - when the model pick is known (v2a) - the (time x model-edge x side) cell plus the
    pick-crossed marginals (time, vol, partial, mid, spread, depth, hour) out of sample. Verdicts are passed through with the
    brief's own caveats (drift warning, uncorrected multiplicity)."""
    states = brief.get("states") or []
    idx = {}
    for s in states:
        idx.setdefault(json.dumps(s["state"], sort_keys=True), []).append(s)
    def get(state: dict, scopes=("second_half_OOS", "full_sample_IN_SAMPLE")):
        return [x for sc in scopes for x in idx.get(json.dumps(state, sort_keys=True), []) if x["scope"] == sc]
    cells = []
    for side in ("yes", "no"):
        d = st["sides"].get(side)
        if not d or not st.get("tb"): continue
        cells += get({"tb": st["tb"], "pxb": d["pxb"], "side": side})
        if d.get("edgeb"): cells += get({"tb": st["tb"], "edgeb": d["edgeb"], "side": side}, ("second_half_OOS",))
        if d.get("pick"):
            for f in ("tb", "volb", "midb", "hourb"):
                if st.get(f): cells += [x for x in get({f: st[f], "pick": d["pick"]}, ("second_half_OOS",)) if x["verdict"] != "insufficient"]
            cells += [x for x in get({"depb": d["depb"], "pick": d["pick"]}, ("second_half_OOS",)) if x["verdict"] != "insufficient"]
    if not cells: return None
    seen = set(); uniq = []
    for c in cells:
        k = (c["scope"], json.dumps(c["state"], sort_keys=True))
        if k not in seen: seen.add(k); uniq.append(_cell(c))
    meta = brief.get("meta") or {}
    return {"source": "reports/forensics/conditions_brief.json", "generated_pt": meta.get("generated_pt"), "state_buckets": {k: v for k, v in st.items() if k != "sides"} | {"sides": st["sides"]},
            "cells": uniq[:max_cells],
            "how_to_read": "hit = realised win rate buying `side` at the depth-weighted lot-10 price + 1 tick and holding to settlement, averaged over ~2-s snapshots in the state; mkt_implied = that price; net_c = realised net c/contract after fee; se_c clustered by close; verdict needs >= 20 closes and |t| >= 2. OOS = second half of the 105-close sample. Not a trading rule: no condition survived multiplicity correction.",
            "drift_warning": meta.get("drift_warning")}

def conditions_lookup(brief: dict | None, state: dict) -> dict | None:
    """Pick the bucket of reports/forensics/conditions_brief.json that matches the current state. Quant A's actual brief (key
    'states', bucket labels) goes through conditions_lookup_qa when `state` carries 'qa' (qa_state_buckets output). Otherwise the
    generic shape: a list under 'buckets' (or the top-level list / dict of named buckets), each with either explicit ranges
    {"match": {field: [lo, hi] | value}} or flat '<field>_lo' / '<field>_hi' keys; fields are looked up in `state`
    (decision_secs_left, mid, p_model, spot_minus_K_bp, sigma_1s_bp, side ...). Returns the matched bucket's reported numbers
    (n, hit, implied, net c/ct, se, verdict) or None. The brief's own 'note'/'summary' is passed through when present."""
    if not brief: return None
    if isinstance(brief, dict) and isinstance(brief.get("states"), list) and state.get("qa"): return conditions_lookup_qa(brief, state["qa"])
    buckets = brief.get("buckets") if isinstance(brief, dict) else brief
    if isinstance(buckets, dict): buckets = [{"name": k, **(v if isinstance(v, dict) else {"value": v})} for k, v in buckets.items()]
    if not isinstance(buckets, list): return None
    def _matches(b: dict) -> bool:
        m = b.get("match") or {}
        conds = []
        for f, rng in m.items(): conds.append((f, rng))
        for k, v in b.items():
            if k.endswith("_lo") and k[:-3] in state: conds.append((k[:-3], [v, b.get(k[:-3] + "_hi", math.inf)]))
        if not conds: return False
        for f, rng in conds:
            sv = state.get(f)
            if sv is None: return False
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                lo = -math.inf if rng[0] is None else rng[0]; hi = math.inf if rng[1] is None else rng[1]
                if not (lo <= sv < hi or (sv == hi and hi == rng[1] and hi >= 1)): return False
            else:
                try: ok = abs(float(sv) - float(rng)) < 1e-9
                except (TypeError, ValueError): ok = str(sv) == str(rng)
                if not ok: return False
        return True
    hits = [b for b in buckets if isinstance(b, dict) and _matches(b)]
    if not hits: return None
    b = hits[0]
    keep = {k: b.get(k) for k in ("name", "n", "hit", "realised_hit", "market_implied", "implied", "net_c_per_ct", "net_c_ct", "se", "se_c_per_ct", "verdict", "match") if k in b}
    if isinstance(brief, dict) and brief.get("note"): keep["brief_note"] = str(brief["note"])[:200]
    keep["source"] = "reports/forensics/conditions_brief.json"
    return keep


# ------------------------------------------------------------------------------------------------------------- packet
def build_packet_v2(snap: dict, now: float, decision_secs_left: float | None, variant: str, base: dict, tape: list[dict] | None = None, table: dict | None = None, brief: dict | None = None,
                    direct: dict | None = None, external: dict | None = None) -> dict:
    """The v2 packet, ordered for a trader: settlement arithmetic -> executable breakevens -> required move + empirical tails ->
    vol realised/implied -> book dynamics -> co-move -> conditions -> context -> direct_arm; `other_forecasters` (p_model) LAST
    and only for variant 'v2a'. `base` is the v1 packet from llm_shadow.assemble_packet (index path, candles, perp, context are
    reused from it; its policy_model block is NOT copied). `external` (P26, variant 'v2c' only) is the ext_feeds.packet_block
    output, appended as `external_signals` after `context`; v2a/v2b packets are byte-identical to before. Pure."""
    K = float(snap["K"]); x = float(snap["x_now"]); sig = float(snap.get("sigma_1s") or S.CFG["sigma_fallback_1s"])
    book = {"yes_bid": snap.get("yes_bid"), "yes_ask": snap.get("yes_ask"), "bids": snap.get("bids") or [], "asks": snap.get("asks") or []}
    n_obs = int(snap.get("n_obs") or 0); m_rem = int(snap.get("m_rem") if snap.get("m_rem") is not None else S.WINDOW_S); V = snap.get("V")
    req = required_move(K, x, snap.get("partial"), n_obs, m_rem, sig, V)
    cs = base.get("candles_1m") or {}
    rv_now = (cs.get("SOL") or {}).get("rv_1m_bp") if cs else None
    emp = empirical_block(table, req, rv_now, sig)
    be = breakevens(book)
    secs_left = base.get("secs_left_at_snapshot")
    pk = {"version": VERSION, "variant": variant, "ticker": base.get("ticker"), "close_ts_utc": base.get("close_ts_utc"), "decision_secs_left": decision_secs_left, "secs_left_at_snapshot": secs_left, "snapshot_age_s": base.get("snapshot_age_s"),
          "settlement": {"strike_K": _r(K, 4), "official_index_now": _r(x, 4), "spot_minus_K_bp": _r(1e4 * (x / K - 1), 2), "index_age_s": snap.get("cfb_age_s"),
                         "window_started": bool(n_obs), "partial_avg": _r(snap.get("partial"), 4), "n_observed": n_obs, "n_remaining": m_rem,
                         "note": "settles on the mean of the 60 one-second official index values before close vs K; tie = YES"},
          "executable_now": {"yes": be["yes"], "no": be["no"], "note": "px_dw = depth-weighted price you pay for that lot now; breakeven_p = px_dw + fee: your side's probability must exceed it. NO price = 1 - yes_bid, computed for you."},
          "required_move": req,
          "empirical_tails": emp,
          "volatility": {**implied_sigma(snap.get("mid"), snap.get("mu"), K, x, V, m_rem, sig), "sigma_src": snap.get("sigma_src"), "rv_1m_bp_last60m_candles": rv_now,
                         "rv_1m_bp_last15m_candles": _rv_last(cs.get("SOL"), 15) if cs else None, "index_path_rv_bp_per_10s": (base.get("official_index_path") or {}).get("rv_bp_per_step")},
          "book": {**be["depth"], "book_age_s": snap.get("book_age_s"), "src": snap.get("book_src"), "last_60s": quote_changes(tape or [], now, 60.0)},
          "comove": comove_block(cs, (table or {}).get("betas")),
          "conditions_brief": conditions_lookup(brief, {"decision_secs_left": decision_secs_left, "mid": snap.get("mid"), "p_model": snap.get("p_model"), "spot_minus_K_bp": 1e4 * (x / K - 1), "sigma_1s_bp": 1e4 * sig, "secs_left": secs_left,
                                                        "qa": qa_state_buckets(secs_left if secs_left is not None else decision_secs_left, book, sig, n_obs, dt.datetime.fromtimestamp(now, PT).hour, snap.get("mid"),
                                                                               (snap.get("p_model") if variant == "v2a" else None), snap.get("z"))}),
          "official_index_path": _thin_path(base.get("official_index_path")),
          "candles_1m": {a: {k: v for k, v in (c or {}).items() if k not in ("returns_1m_bp",) or a == "SOL"} for a, c in cs.items()} if cs else None,
          "perp_okx": base.get("perp_okx"), "context": base.get("context")}
    if variant == "v2c": pk["external_signals"] = external
    if direct is not None: pk["direct_arm"] = direct
    if variant == "v2a":
        pk["other_forecasters"] = {"note": "shown AFTER the decision fields on purpose; these are other opinions, not inputs: compare, do not average",
                                   "policy_gaussian_p_model": _r(snap.get("p_model"), 4), "policy_kappa": snap.get("kappa"), "market_mid": _r(snap.get("mid"), 4)}
    return pk

def _rv_last(c: dict | None, k: int) -> float | None:
    if not c or not c.get("returns_1m_bp"): return None
    r = [v for v in c["returns_1m_bp"][-k:] if v is not None]
    return _r(math.sqrt(sum(v * v for v in r) / len(r)), 2) if r else None

def _thin_path(p: dict | None, step: int = 3) -> dict | None:
    """Index path every 30 s instead of 10 s (token budget), summary stats unchanged."""
    if not p: return None
    vals = p.get("values") or []
    return {**{k: v for k, v in p.items() if k != "values"}, "step_s": p.get("step_s", 10) * step, "values": vals[::step] + ([vals[-1]] if (len(vals) - 1) % step else [])}


# ------------------------------------------------------------------------------------------------------------ loaders (I/O)
TAIL_TABLE_PATH = pathlib.Path("data/llm-shadow/tail_table_SOL.json")
CANDLE_DIR = pathlib.Path("data/candles")
BRIEF_PATH = pathlib.Path("reports/forensics/conditions_brief.json")

def load_or_build_tail_table(path: pathlib.Path = TAIL_TABLE_PATH, candle_dir: pathlib.Path = CANDLE_DIR, now: float | None = None, max_age_s: float = 86400.0, until_ts: int | None = None) -> dict | None:
    """Cached empirical table (rebuilt when older than max_age_s or when `until_ts` asks for a causal cut of the history).
    `until_ts` (offline dry-run) builds from candles with ts < until_ts only and does not touch the cache."""
    now = time.time() if now is None else now
    if until_ts is None:
        try:
            t = json.loads(path.read_text(encoding="utf-8"))
            if now - float(t.get("built_at", 0)) <= max_age_s and t.get("version") == VERSION: return t
        except Exception: pass
    try:
        import pandas as pd
        dfs = {}
        for a in ("SOL", "BTC", "ETH"):
            p = candle_dir / f"{a}_1m.parquet"
            if p.exists():
                d = pd.read_parquet(p)
                dfs[a] = d[d.ts < until_ts] if until_ts is not None else d
        if "SOL" not in dfs: return None
        t = build_tail_table(dfs["SOL"], dfs.get("BTC"), dfs.get("ETH"))
        if until_ts is None:
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(t), encoding="utf-8")
        return t
    except Exception:
        return None

def load_brief(path: pathlib.Path = BRIEF_PATH) -> dict | None:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return None

def book_tape_tail(ticker: str, now: float, span_s: float = 90.0, book_dir: pathlib.Path = pathlib.Path("data/sol-v1/book"), tail_bytes: int = 600_000) -> list[dict]:
    """Last rows of today's (UTC date) ws book tape for `ticker` within span_s before `now` (reads only the file tail)."""
    out = []
    for day in {dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y%m%d"), dt.datetime.fromtimestamp(now - span_s, dt.timezone.utc).strftime("%Y%m%d")}:
        p = book_dir / f"{day}.jsonl"
        if not p.exists(): continue
        size = p.stat().st_size
        with p.open("rb") as fh:
            fh.seek(max(0, size - tail_bytes)); data = fh.read()
        for l in data.split(b"\n")[1 if size > tail_bytes else 0:]:
            if not l.strip(): continue
            try: r = json.loads(l)
            except Exception: continue
            if r.get("ticker") == ticker and r.get("recv_t") is not None and now - span_s <= r["recv_t"] <= now: out.append(r)
    return out
