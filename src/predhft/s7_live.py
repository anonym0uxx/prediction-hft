"""S7 live PAPER runner (no account, no orders). Polls Kalshi public order books for near-the-money KXBTCD strikes in the final
minutes of each hour, computes the spot-distance model, and paper-fills a taker buy against the size actually resting at the touch.
Logs every book snapshot and paper trade; settles from the official market result. CPU only.

Usage: uv run python -m predhft.s7_live --thr 0.05 --t-open 8 --t-close 2 --lot 20
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, statistics as st, time, urllib.request
from .kalshi_public import KalshiPublic, f
from .fees import kalshi_taker_fee
from .risk import Ledger, size as risk_size

def replica_latest(asset: str):
    try:
        from .index_replica import latest
        return latest(asset)
    except Exception: return None

def context_latest(asset: str):
    try:
        from .context_feed import latest
        return latest(asset)
    except Exception: return None

def cfb_latest(asset: str):
    try:
        from .cfb_feed import latest
        return latest(asset)
    except Exception: return None

def perp_latest(asset: str):
    try:
        from .perp_feed import latest
        return latest(asset)
    except Exception: return None

CB_BASE = "https://api.exchange.coinbase.com/products"
def utcnow() -> float: return time.time()
def ts(s: str) -> int: return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
def Phi(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def PhiInv(p: float) -> float:
    """Acklam rational approximation of the normal quantile (|err| < 1.2e-9)."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p)); return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - 0.02425:
        q = math.sqrt(-2 * math.log(1 - p)); return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5; r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)

# P6 sigma-regime gate (docs/EDGE_PLAN_2026-10-26.md A1): fixed thresholds, bp/min of 1-min log returns, never re-estimated
SIGMA_MAX = {"KXBTCD": 0.000439, "KXETHD": 0.000571, "KXSOLD": 0.000575}
# P13 projection-side rule (docs/FOCUS_2026-10-27.md §7): replica basis (official expiration_value - replica), bp, measured on settled markets
BASIS_BP = {"BTC": 0.44, "ETH": 0.7, "SOL": 1.2}
LATENCY_GUARD_S = 1.5   # SHARPE_PLAN §5.1: price re-check is the real guard
BELIEF_FLOOR = None     # falsified (REBASE_2026-10-27 §2: 15/18 at |d| 1-2 bp); P13 sizes on p_cal instead
D_MIN_BP = {"KXBTC15M": 1.0, "KXETH15M": 1.0, "KXSOL15M": 2.0, "KXBTCD": 1.0, "KXETHD": 1.0, "KXSOLD": 2.0}
def twap_horizon(mins_left: float) -> float:
    """Settlement is the mean of the index over the final minute (rules card): Var(mean_[T-1,T] - S_t) = sigma^2 (tau - 1 + 1/3) for tau >= 1."""
    return max(mins_left - 2.0 / 3.0, 0.05)

def p_yes(spot: float, K: float, sig1: float, mins_left: float, partial_twap: float | None = None, partial_n: int = 0) -> float:
    """P(settlement average > K). Outside the final minute: Brownian to the window mean (twap_horizon). Inside the final minute (review C3):
    settlement = (1-r)*partial_avg + r*future_avg, r = remaining fraction; future_avg ~ spot * exp(N(0, sig1^2 * r/3)) in minutes units."""
    if mins_left >= 1.0 or partial_twap is None or partial_n < 3:
        return Phi(math.log(spot / K) / (sig1 * math.sqrt(twap_horizon(mins_left))))
    r = max(min(mins_left, 1.0), 0.02)
    k_eff = (K - (1 - r) * partial_twap) / r              # level the remaining average must exceed
    if k_eff <= 0: return 0.999
    return Phi(math.log(spot / k_eff) / (sig1 * math.sqrt(r / 3.0)))

def p_yes_t(spot: float, K: float, sig1: float, mins_left: float, nu: float = 8.0, scale: float = 0.8, partial_twap: float | None = None, partial_n: int = 0) -> tuple[float, float]:
    """Student-t version of p_yes (S7_REDESIGN 3.1): returns (p, z). Leptokurtic settlement moves: tighter core, fatter tails."""
    if mins_left >= 1.0 or partial_twap is None or partial_n < 3:
        z = math.log(spot / K) / (sig1 * math.sqrt(twap_horizon(mins_left))); return tcdf(z / scale, nu), z
    r = max(min(mins_left, 1.0), 0.02); k_eff = (K - (1 - r) * partial_twap) / r
    if k_eff <= 0: return 0.999, 9.0
    z = math.log(spot / k_eff) / (sig1 * math.sqrt(r / 3.0)); return tcdf(z / scale, nu), z
BASIS_NOISE_BP = 0.6   # replica-vs-official residual sd (REBASE_2026-10-27 §2); 0 once the official cfbenchmarks feed is used
def p_cal(d_bp: float, sig1: float, mins_left: float, basis_noise_bp: float = BASIS_NOISE_BP) -> float:
    """Calibrated final-minute probability that the settlement average lands above K given projection distance d_bp (bp):
    remaining-variance sd = 1.5 * sig1 * r * sqrt(r/3) (realised tails ~1.5x Gaussian), plus replica basis noise. REBASE §5."""
    r = max(min(mins_left, 1.0), 0.02); sd_bp = math.sqrt((1.5 * sig1 * 1e4 * r * math.sqrt(r / 3.0)) ** 2 + basis_noise_bp ** 2)
    return Phi(d_bp / sd_bp) if sd_bp > 0 else (1.0 if d_bp > 0 else 0.0)
def jget(u):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "predhft"}), timeout=10).read())

class State:
    def __init__(self, root: pathlib.Path):
        self.root = root; root.mkdir(parents=True, exist_ok=True); self.score = root / "scoreboard.jsonl"
        self.trades = root / "trades.jsonl"; self.books = root / "books.jsonl"; self.log = root / "runner.log"
        self.open_positions: dict[str, dict] = {}
        if self.trades.exists():
            for l in self.trades.read_text().splitlines():
                t = json.loads(l)
                if t.get("status") == "open": self.open_positions[t["ticker"]] = t
    def trades_tail(self, n: int = 20):
        try: return [json.loads(l) for l in self.trades.read_text(encoding="utf-8").splitlines()[-n:] if l.strip()]
        except FileNotFoundError: return []
    def logline(self, m):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {m}"
        print(line, flush=True); self.log.open("a").write(line + "\n")
    def append(self, path, obj): path.open("a").write(json.dumps(obj) + "\n")
    def rewrite_trades(self, trades): self.trades.write_text("".join(json.dumps(t) + "\n" for t in trades))

def tcdf(x: float, nu: float) -> float:
    """Student-t CDF via the regularized incomplete beta (continued fraction; no scipy)."""
    def betacf(a, b, x):
        MAXIT, EPS, FPMIN = 200, 3e-14, 1e-300
        qab, qap, qam = a + b, a + 1, a - 1; c, d = 1.0, 1 - qab * x / qap
        d = 1 / (d if abs(d) > FPMIN else FPMIN); h = d
        for m in range(1, MAXIT + 1):
            m2 = 2 * m; aa = m * (b - m) * x / ((qam + m2) * (a + m2))
            d = 1 + aa * d; d = 1 / (d if abs(d) > FPMIN else FPMIN); c = 1 + aa / c; c = c if abs(c) > FPMIN else FPMIN; h *= d * c
            aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
            d = 1 + aa * d; d = 1 / (d if abs(d) > FPMIN else FPMIN); c = 1 + aa / c; c = c if abs(c) > FPMIN else FPMIN; de = d * c; h *= de
            if abs(de - 1) < EPS: break
        return h
    def ibeta(a, b, x):
        if x <= 0: return 0.0
        if x >= 1: return 1.0
        lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
        return math.exp(lb) * betacf(a, b, x) / a if x < (a + 1) / (a + b + 2) else 1 - math.exp(lb) * betacf(b, a, 1 - x) / b
    xx = nu / (nu + x * x); p = 0.5 * ibeta(nu / 2, 0.5, xx)
    return 1 - p if x > 0 else p

def phi(x): return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
def dyn_thr(z: float, spread: float, relse: float = 0.17, base: float = 0.02) -> float:
    """Edge required = base + sigma-uncertainty term (|z| phi(z) relse) + half the spread (S7_REDESIGN 4)."""
    return base + abs(z) * phi(z) * relse + spread / 2

def sigma_ewma(product: str = "BTC-USD", lam: float = 0.94) -> float | None:
    end = dt.datetime.now(dt.timezone.utc); start = end - dt.timedelta(minutes=180)
    d = jget(f"{CB_BASE}/{product}/candles?granularity=60&start={start.isoformat()}&end={end.isoformat()}")
    px = [float(c[4]) for c in sorted(d)]
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    if len(rets) < 90: return None
    v = st.fmean(r * r for r in rets[:60])
    for r in rets[60:]: v = lam * v + (1 - lam) * r * r
    return math.sqrt(v)

def sigma_1m(product: str = "BTC-USD") -> float | None:
    end = dt.datetime.now(dt.timezone.utc); start = end - dt.timedelta(minutes=180)
    d = jget(f"{CB_BASE}/{product}/candles?granularity=60&start={start.isoformat()}&end={end.isoformat()}")
    px = [float(c[4]) for c in sorted(d)]
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    return st.pstdev(rets[-120:]) if len(rets) >= 60 else None

def book_touch(ob: dict):
    """Return (yes_bid, yes_ask, yes_bid_size, yes_ask_size) in dollars/contracts from orderbook_fp."""
    o = ob.get("orderbook_fp") or {}
    yes = [(float(p), float(q)) for p, q in (o.get("yes_dollars") or [])]
    no = [(float(p), float(q)) for p, q in (o.get("no_dollars") or [])]
    yb, ybs = (max(yes)[0], max(yes)[1]) if yes else (0.0, 0.0)
    if no:
        nb = max(no); ya, yas = 1 - nb[0], nb[1]
    else: ya, yas = 1.0, 0.0
    return yb, ya, ybs, yas

def settle(k: KalshiPublic, state: State):
    if not state.open_positions: return
    trades = [json.loads(l) for l in state.trades.read_text().splitlines()]
    changed = False
    for t in trades:
        if t.get("status") != "open" or utcnow() < t["close_ts"] + 120: continue
        try: m = k.get(f"/markets/{t['ticker']}")["market"]
        except Exception: continue
        res = m.get("result")
        if res in ("yes", "no"):
            win = (res == t["side"])
            t["status"] = "settled"; t["result"] = res; t["expiration_value"] = m.get("expiration_value"); t["pnl"] = (1.0 if win else 0.0) * t["qty"] - t["cost"] - t["fee"]
            state.open_positions.pop(t["ticker"], None); changed = True; Ledger(state.root, family=state.root.name).record_settle(t["ticker"], t["pnl"])
            state.logline(f"SETTLED {t['ticker']} side={t['side']} result={res} qty={t['qty']} pnl={t['pnl']:+.2f}")
    if changed: state.rewrite_trades(trades)

def maker_settle(k: KalshiPublic, state: State):
    """Scored-only maker family: fill iff a print on our side went strictly through q between post and T-3; P&L = y_side - q (maker fee 0)."""
    f = state.root / "maker_posts.jsonl"
    if not f.exists(): return
    rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]; changed = False; now = utcnow()
    for r in rows:
        if r["status"] != "posted" or now < r["close_ts"] + 300: continue
        try:
            m = k.get(f"/markets/{r['ticker']}")["market"]; res = m.get("result")
            if res not in ("yes", "no"): continue
            tr = k.get("/markets/trades", ticker=r["ticker"], limit=200).get("trades", [])
        except Exception: continue
        filled = 0.0
        for t in tr:
            ts_ = dt.datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
            if not (r["t_post"] < ts_ <= r["close_ts"] - 180): continue
            yp = float(t["yes_price_dollars"]); cnt = float(t["count_fp"])
            # we rest a YES bid at q (side yes) -> filled when a NO-taker print occurs strictly below q; NO bid at q -> YES-taker print strictly above 1-q
            if r["side"] == "yes" and t["taker_side"] == "no" and yp < r["q"] - 1e-9: filled += cnt
            if r["side"] == "no" and t["taker_side"] == "yes" and yp > 1 - r["q"] + 1e-9: filled += cnt
        r["status"] = "filled" if filled > 0 else "unfilled"; r["fill_size"] = filled; r["result"] = res
        r["pnl_pc"] = ((1.0 if res == r["side"] else 0.0) - r["q"]); changed = True
        state.append(state.score, {"t": now, "family": r["family"], "ticker": r["ticker"], "p_model": r["p_model"], "p_market": r["mid"], "close_ts": r["close_ts"], "maker_status": r["status"], "pnl_pc": r["pnl_pc"]})
    if changed: f.write_text("".join(json.dumps(r) + "\n" for r in rows))

def single_instance(root: pathlib.Path):
    """Windows-safe exclusive lock: a file opened with O_EXCL-style semantics via msvcrt locking; held for process lifetime."""
    import os, msvcrt
    root.mkdir(parents=True, exist_ok=True)
    fh = open(root / "runner.lock", "a+")
    try: msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("another runner holds the lock; exiting", flush=True); raise SystemExit(0)
    return fh

def run(thr: float, t_open: float, t_close: float, lot: int, poll_s: float, root: pathlib.Path, series: str = "KXBTCD", product: str = "BTC-USD", sigma_gate: bool = False,
        sigma_mode: str = "trail120", dist: str = "normal", nu: float = 8.0, scale: float = 0.8, thr_mode: str = "fixed", scored_only: bool = False, maker_score: bool = False, period_s: int = 3600, proj_gate: bool = False):
    _lock = single_instance(root)
    import os; os.environ["PREDHFT_PRIORITY"] = "1"
    k = KalshiPublic(min_interval_s=0.12); state = State(root); sig = None; sig_ts = 0; maker_posted: set = set(); cands_cache = (None, []); nofill = {}; _sent_cache: dict = {}
    state.logline(f"start {series}/{product} period={period_s}s thr={thr} window=T-{t_open}..T-{t_close}min lot={lot} sigma_gate={sigma_gate} sigma={sigma_mode} dist={dist} nu={nu} scale={scale} thr_mode={thr_mode} scored_only={scored_only} maker_score={maker_score} proj_gate={proj_gate}")
    while True:
        try:
            now = utcnow(); next_hour = (int(now) // period_s + 1) * period_s; mins_left = (next_hour - now) / 60
            if mins_left > t_open + 1 or mins_left < t_close:
                for ct in [c for c in list(nofill) if isinstance(c, int) and c <= now]:
                    g = nofill.pop(ct)
                    traded = any(t.get("close_ts") == ct for t in state.open_positions.values()) or any(t.get("close_ts") == ct for t in state.trades_tail())
                    if not traded: state.logline(f"NOFILL {g['tk']} reasons={g['reasons']} min_fav={g['min_fav']:.3f}")
                    sup = g.get("supply", {}); state.append(state.root / "supply.jsonl", {"close_ts": ct, "ticker": g["tk"], "min_fav": g["min_fav"], "traded": traded, "utc_hour": dt.datetime.fromtimestamp(ct, dt.timezone.utc).hour, "reasons": g["reasons"], **sup})
                settle(k, state); maker_settle(k, state); time.sleep(min(20, max(2, (mins_left - t_open) * 60) if mins_left > t_open else 5)); continue
            if sig is None or now - sig_ts > 900:                   # P13 diag: never fetch Coinbase candles inside the final minute (38 s hang class)
                s = (sigma_ewma(product) if sigma_mode == "ewma94" else sigma_1m(product)); sig = s or sig; sig_ts = now
            close_iso = dt.datetime.fromtimestamp(next_hour, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rep = replica_latest(product.split("-")[0])          # BRTI replica (median of constituent mids) if fresh, else Coinbase last trade
            cfb = cfb_latest(product.split("-")[0]); spot_src = "replica"
            if cfb and cfb.get("value"):                          # OFFICIAL settlement feed (Kalshi cfbenchmarks_value): exact index + settlement-aligned partial
                rep = {"replica": cfb["value"], "partial_twap": cfb.get("partial_twap"), "partial_n": cfb.get("partial_n", 0), "official": True}; spot_src = "cfb"
            spot = rep["replica"] if rep else float(jget(f"{CB_BASE}/{product}/ticker")["price"])
            partial, partial_n = (rep.get("partial_twap"), rep.get("partial_n", 0)) if rep else (None, 0)
            basis_used = 0.0
            ctx = context_latest(product.split("-")[0]) or {}; ctx_f = {k: ctx.get(k) for k in ("cb_imb_60s", "cb_imb_300s", "okx_taker_imb_5m", "funding", "premium", "oi_chg_15m_bp", "ls_ratio", "liq_long_15m", "liq_short_15m", "fng")}
            # P15 sentiment leg (docs/SENTIMENT_DESIGN_2026-10-27.md §3.4): social_features.latest(); old reddit_* names kept as null
            ctx_f.update({"reddit_mentions": None, "reddit_sent": None})
            SENT_KEYS = ("rd_z_posts_60m", "rd_dir_ewma_120", "rd_n_dir_180m", "st_bull_ratio_60m", "st_z_msgs_60m", "st_dir_ewma_30", "st_disag_60m", "st_users_60m", "st_msgs_15m",
                         "rd_newest_age_s", "st_newest_age_s", "rd_stale_s", "st_stale_s", "sent_as_of", "sent_age_s", "lex_version", "news_flag")
            # computed at decision time from the store (newest fetched posts, causal on fetched_ts); recomputed at most once per 20 s per runner
            try:
                from .social_features import features_now, latest as social_latest
                if now - _sent_cache.get("t", 0) > 20:
                    _sent_cache["v"] = features_now(product.split("-")[0]) or social_latest(product.split("-")[0]) or {}; _sent_cache["t"] = now
                sl = dict(_sent_cache.get("v") or {}); sl["sent_age_s"] = round(now - sl["sent_as_of"], 1) if sl.get("sent_as_of") else None
                ctx_f.update({k: sl.get(k) for k in SENT_KEYS})
            except Exception: ctx_f.update({k: None for k in SENT_KEYS})
            perp = perp_latest(product.split("-")[0]); perp_spot = perp_age = perp_basis = None
            if perp and perp.get("basis_med_bp") is not None:
                perp_age = round(now - perp["t"], 2); perp_basis = perp["basis_med_bp"]
                if perp_age <= 2.0: perp_spot = perp["px"] / (1 + perp_basis * 1e-4) * (1 + BASIS_BP.get(product.split("-")[0], 0.0) * 1e-4)
            if proj_gate and rep and not rep.get("official"):       # basis correction only for the replica (official feed needs none)
                basis_used = BASIS_BP.get(product.split("-")[0], 0.0); spot *= (1 + basis_used * 1e-4)
                if partial: partial *= (1 + basis_used * 1e-4)
            if cands_cache[0] != close_iso or (not cands_cache[1] and now % 10 < 2):
                cands_cache = (close_iso, [m for m in k.markets(series_ticker=series, status="open", limit=1000, max_pages=2) if m["close_time"] == close_iso and m.get("floor_strike")])
            cands = cands_cache[1]
            # near the money: within 4 sigma*sqrt(t) in log space
            band = 4 * (sig or 0.0006) * math.sqrt(max(mins_left, 0.5))
            near = sorted([m for m in cands if period_s < 3600 or abs(math.log(spot / float(m["floor_strike"]))) <= band], key=lambda m: abs(math.log(spot / float(m["floor_strike"]))))   # 15M: single market, no band
            if mins_left < 1.5: near = near[:3]
            # pinned markets (favourite >= .99 or empty offer twice) are polled every 5 s only
            pinned = {tk for tk, (cnt, ts) in nofill.get("pinned", {}).items() if cnt >= 2 and now - ts < 5}
            near = [m for m in near if m["ticker"] not in pinned]                 # final minute: only the 3 nearest strikes (shared rate limiter)
            # one directional position per asset-hour (review C6): collect candidates, take the best edge/sd, re-check the book before filling (F2)
            have_hour = any(t.get("close_ts") == next_hour for t in state.open_positions.values())
            best = None
            for m in near:
                K = float(m["floor_strike"]); tk = m["ticker"]
                try: ob = k.orderbook(tk)
                except Exception: continue
                yb, ya, ybs, yas = book_touch(ob)
                p_n = p_yes(spot, K, sig or 0.0006, mins_left, partial, partial_n)
                p_t, z = p_yes_t(spot, K, sig or 0.0006, mins_left, nu, scale, partial, partial_n)
                p = p_t if dist == "t" else p_n
                mid = (yb + ya) / 2 if 0 < ya < 1 else None
                thr_used = dyn_thr(z, ya - yb) if thr_mode == "dyn" else thr
                regime = ("lo" if (sig or 0.0006) < SIGMA_MAX.get(series, 9) else "hi")
                imb = (ybs - yas) / (ybs + yas) if (ybs + yas) > 0 else None
                sig_mkt = None
                if mid and 0.05 < mid < 0.95 and abs(math.log(spot / K)) > 1e-6:
                    try: sig_mkt = abs(math.log(spot / K)) / (abs(PhiInv(mid)) * math.sqrt(twap_horizon(mins_left)))
                    except (ZeroDivisionError, ValueError): sig_mkt = None
                snap = {"t": now, "mins_left": round(mins_left, 2), "ticker": tk, "K": K, "spot": spot, "spot_src": "replica" if rep else "coinbase", "partial_twap": partial, "partial_n": partial_n, "sigma1": sig, "sigma_regime": regime,
                        "sigma_mkt": sig_mkt, "spread": round(ya - yb, 2), "imb": imb, "p_model": p, "p_t": p_t, "p_normal": p_n, "z": z, "thr_used": round(thr_used, 4), "sigma_mode": sigma_mode, "yes_bid": yb, "yes_ask": ya, "bid_size": ybs, "ask_size": yas, "close_ts": next_hour, "spot_perp": perp_spot, "perp_age_s": perp_age, "perp_basis_bp": perp_basis, "ctx": ctx_f,
                        "p_cal": (p_cal(1e4 * ((((1 - max(min(mins_left, 1.0), 0.02)) * partial + max(min(mins_left, 1.0), 0.02) * spot) / K) - 1), sig, mins_left, basis_noise_bp=(0.0 if spot_src == "cfb" else BASIS_NOISE_BP)) if (partial and partial_n >= 3 and mins_left < 1.0) else None), "spot_src": spot_src}
                state.append(state.books, snap)
                # scored rows (P6 regime split / P9 final-minute books skill): one row per strike per poll inside the trading window
                if mid and (t_close <= mins_left <= t_open):
                    state.append(state.score, {"t": now, "family": f"{root.name}-{'books' if mins_left < 1.0 else 'sigma-' + regime}", "ticker": tk, "p_model": p, "p_t": p_t, "p_normal": p_n, "p_market": mid, "yes_bid": yb, "yes_ask": ya,
                                               "bid_size": ybs, "ask_size": yas, "close_ts": next_hour, "mins_left": round(mins_left, 2), "sigma1": sig, "sigma_mkt": sig_mkt, "z": z})
                # scored-only maker family (S7_REDESIGN 5.1.7): post p_side - 0.02 at T-5..T-3.5, filled iff traded strictly through before T-3
                if maker_score and 3.5 <= mins_left <= 5.0 and mid and tk not in maker_posted:
                    side_m = "yes" if p > mid else "no"; p_side_m = p if side_m == "yes" else 1 - p; q = math.floor((p_side_m - 0.02) * 100) / 100
                    if 0.02 <= q <= 0.97:
                        state.append(state.root / "maker_posts.jsonl", {"t_post": now, "family": f"{root.name}-maker5", "ticker": tk, "side": side_m, "q": q, "close_ts": next_hour, "p_model": p, "mid": mid, "status": "posted"})
                        maker_posted.add(tk)
                if scored_only or have_hour or tk in state.open_positions or not (t_close <= mins_left <= t_open): continue
                if sigma_gate and regime == "hi": continue        # P6: sigma-hi is scored only, never traded
                side = None
                if proj_gate:
                    # P13: side = sign(projected settlement average - K); no model threshold; buy the favoured side if offered <= .95 with size >= lot
                    g = nofill.setdefault(next_hour, {"reasons": {}, "min_fav": 1.0, "tk": tk}); pin = nofill.setdefault("pinned", {})
                    fav_px = ya if yb >= 0.5 else (1 - yb if 0 < yb else 1.0); g["min_fav"] = min(g["min_fav"], fav_px)
                    if ya >= 0.99 or yas <= 0 or yb <= 0.01: pin[tk] = (pin.get(tk, (0, 0))[0] + 1, now)
                    else: pin.pop(tk, None)
                    if partial is None or partial_n < 3 or mins_left >= 1.0: g["reasons"]["partial"] = g["reasons"].get("partial", 0) + 1; continue
                    r_ = max(min(mins_left, 1.0), 0.02); proj = (1 - r_) * partial + r_ * spot; d_bp = 1e4 * (proj / K - 1)
                    d_perp_bp = (1e4 * (((1 - r_) * partial + r_ * perp_spot) / K - 1)) if perp_spot else None   # P14 scored-only
                    sup = g.setdefault("supply", {"first_t_d_ge_1bp": None, "fav_at_first_d_ge_1bp": None, "t_min_fav": None, "d_at_min_fav": None, "pinned_at_T55": None})
                    if sup["pinned_at_T55"] is None and mins_left <= 0.92: sup["pinned_at_T55"] = bool(fav_px >= 0.99)
                    if fav_px <= g["min_fav"]: sup["t_min_fav"] = now; sup["d_at_min_fav"] = round(d_bp, 2)
                    if sup["first_t_d_ge_1bp"] is None and abs(d_bp) >= D_MIN_BP.get(series, 1.0): sup["first_t_d_ge_1bp"] = now; sup["fav_at_first_d_ge_1bp"] = fav_px
                    if abs(d_bp) < D_MIN_BP.get(series, 1.0): g["reasons"]["d_small"] = g["reasons"].get("d_small", 0) + 1; continue
                    if d_bp > 0 and 0 < ya <= 0.95 and yas >= lot: side, edge = "yes", p - ya
                    elif d_bp < 0 and 0 < yb < 1 and (1 - yb) <= 0.95 and ybs >= lot: side, edge = "no", yb - p
                    else: g["reasons"]["fav_gt_95_or_size"] = g["reasons"].get("fav_gt_95_or_size", 0) + 1
                    if side: edge = max(edge, 0.01); proj_d_bp = d_bp
                else:
                    if mins_left < 1.0 and (partial is None or partial_n < 3): continue    # REBASE item 4: no spot-only final-minute decisions
                    if ya - yb > 0.06 or not (mid and 0.03 <= mid <= 0.97): continue
                    if p - ya >= thr_used and 0 < ya < 1 and yas >= lot: side, edge = "yes", p - ya
                    elif (yb - p) >= thr_used and 0 < yb < 1 and ybs >= lot: side, edge = "no", yb - p
                if side:
                    score = edge / math.sqrt(max(p * (1 - p), 1e-4))
                    if best is None or score > best["score"]: best = {"score": score, "tk": tk, "K": K, "side": side, "p": p, "mid": (yb + ya) / 2, "t_dec": time.time(), "regime": regime, "spread": round(ya - yb, 2), "imb": imb, "sigma_mkt": sig_mkt, "thr_used": thr_used, "z": z, "d_bp": (proj_d_bp if proj_gate else None), "partial_n": partial_n, "d_perp_bp": (d_perp_bp if proj_gate else None)}
                    if proj_gate: break                            # re-check immediately (BTCD 1.65 s latency came from fetching further strikes first)
            if best:
                tk, side, p = best["tk"], best["side"], best["p"]
                try: yb2, ya2, ybs2, yas2 = book_touch(k.orderbook(tk))
                except Exception: yb2 = None
                latency = time.time() - best["t_dec"]
                if proj_gate:
                    ok = yb2 is not None and latency <= LATENCY_GUARD_S and ((side == "yes" and 0 < ya2 <= 0.95 and yas2 >= lot) or (side == "no" and 0 < yb2 < 1 and (1 - yb2) <= 0.95 and ybs2 >= lot))
                else:
                    ok = yb2 is not None and ((side == "yes" and p - ya2 >= best["thr_used"] and 0 < ya2 < 1 and yas2 > 0) or (side == "no" and yb2 - p >= best["thr_used"] and 0 < yb2 < 1 and ybs2 > 0))
                if not ok:
                    state.logline(f"STALE {tk} {side} touch moved before fill (latency {latency:.2f}s)"); state.append(state.books, {"t": now, "ticker": tk, "stale_fill": True})
                else:
                    price = min((ya2 if side == "yes" else 1 - yb2) + 0.01, 0.99); avail = yas2 if side == "yes" else ybs2   # +1 tick slippage
                    p_side = p if side == "yes" else 1 - p; pm_side = best["mid"] if side == "yes" else 1 - best["mid"]
                    led = Ledger(root, family=root.name)
                    if proj_gate:                                      # REBASE: belief = calibrated p_cal (no floor, no market shrink)
                        pc = p_cal(best["d_bp"], sig, mins_left, basis_noise_bp=(0.0 if spot_src == "cfb" else BASIS_NOISE_BP)); p_side = pc if side == "yes" else 1 - pc; best["p_cal"] = pc
                        if p_side - price - kalshi_taker_fee(10, price) / 10 < 0.05:      # P16: calibrated edge >= 5c after fee
                            g3 = nofill.setdefault(next_hour, {"reasons": {}, "min_fav": 1.0, "tk": tk}); g3["reasons"]["p16_edge_lt_5c"] = g3["reasons"].get("p16_edge_lt_5c", 0) + 1
                            state.logline(f"SKIP {tk} {side} p16 edge {p_side - price:.3f} < .05 (p_cal={pc:.3f} price={price:.3f} d={best['d_bp']:.2f}bp)"); continue
                    qty, why = risk_size(series, p_side, price, avail, led, lot_cap=lot, p_market=(None if proj_gate else pm_side), fee_per_contract=kalshi_taker_fee(10, price) / 10, close_ts=next_hour)
                    if qty <= 0:
                        state.logline(f"SKIP {tk} {side} {why}")
                        if proj_gate: g2 = nofill.setdefault(next_hour, {"reasons": {}, "min_fav": 1.0, "tk": tk}); g2["reasons"]["kelly_zero"] = g2["reasons"].get("kelly_zero", 0) + 1
                    else:
                        cost = qty * price; fee = kalshi_taker_fee(qty, price)
                        t = {"t": now, "ticker": tk, "close_ts": next_hour, "side": side, "price": price, "qty": qty, "avail_at_touch": avail, "slippage": 0.01, "decision_latency_s": round(latency, 2),
                             "cost": cost, "fee": fee, "p_model": p, "p_market": best["mid"], "spot": spot, "K": best["K"], "mins_left": round(mins_left, 2), "status": "open", "series": series, "risk": why,
                             "sigma1": sig, "sigma_regime": best["regime"], "spread": best["spread"], "imb": best["imb"], "sigma_mkt": best["sigma_mkt"], "thr_used": round(best["thr_used"], 4), "z": best["z"], "dist": dist, "sigma_mode": sigma_mode, "d_bp": best.get("d_bp"), "partial_n": best.get("partial_n"), "basis_used_bp": basis_used, "spot_src": spot_src, "proj_gate": proj_gate, "floor_used": None, "p_cal": best.get("p_cal"), "latency_guard_s": (LATENCY_GUARD_S if proj_gate else None), "fav_at_decision": (best["mid"] if side == "yes" else 1 - best["mid"]), "spot_perp": perp_spot, "d_perp_bp": best.get("d_perp_bp"), "perp_age_s": perp_age, "perp_basis_bp": perp_basis, "ctx": ctx_f}
                        state.append(state.trades, t); state.open_positions[tk] = t; led.record_open(tk, series, cost, close_ts=next_hour, side=side)
                        state.logline(f"PAPER BUY {side.upper()} {qty}@{price:.2f} {tk} p_model={p:.2f} avail={avail:.0f} T-{mins_left:.1f}m lat={latency:.2f}s")
            time.sleep(0.7 if mins_left < 1.5 else poll_s)
        except KeyboardInterrupt: raise
        except Exception as e:
            state.logline(f"error {type(e).__name__}: {str(e)[:160]}"); Ledger(root).record_error(f"{type(e).__name__}: {e}"); time.sleep(5)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--thr", type=float, default=0.05); ap.add_argument("--t-open", type=float, default=8)
    ap.add_argument("--t-close", type=float, default=2); ap.add_argument("--root", default=None); ap.add_argument("--sigma-gate", action="store_true")
    ap.add_argument("--sigma", default="trail120", choices=["trail120", "ewma94"]); ap.add_argument("--dist", default="normal", choices=["normal", "t"]); ap.add_argument("--nu", type=float, default=8.0); ap.add_argument("--scale", type=float, default=0.8)
    ap.add_argument("--thr-mode", default="fixed", choices=["fixed", "dyn"]); ap.add_argument("--scored-only", action="store_true"); ap.add_argument("--maker-score", action="store_true"); ap.add_argument("--period", type=int, default=3600); ap.add_argument("--proj-gate", action="store_true"); ap.add_argument("--lot", type=int, default=20); ap.add_argument("--poll", type=float, default=4.0); ap.add_argument("--series", default="KXBTCD"); ap.add_argument("--product", default="BTC-USD")
    a = ap.parse_args(); root = pathlib.Path(a.root) if a.root else (pathlib.Path("data/s7live") if a.series == "KXBTCD" else pathlib.Path(f"data/s7live-{a.series}"))
    run(a.thr, a.t_open, a.t_close, a.lot, a.poll, root, a.series, a.product, a.sigma_gate, a.sigma, a.dist, a.nu, a.scale, a.thr_mode, a.scored_only, a.maker_score, a.period, a.proj_gate)
