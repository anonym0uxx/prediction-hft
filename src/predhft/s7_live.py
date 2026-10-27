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
    def logline(self, m):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {m}"
        print(line, flush=True); self.log.open("a").write(line + "\n")
    def append(self, path, obj): path.open("a").write(json.dumps(obj) + "\n")
    def rewrite_trades(self, trades): self.trades.write_text("".join(json.dumps(t) + "\n" for t in trades))

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
            t["status"] = "settled"; t["result"] = res; t["pnl"] = (1.0 if win else 0.0) * t["qty"] - t["cost"] - t["fee"]
            state.open_positions.pop(t["ticker"], None); changed = True; Ledger(state.root, family=state.root.name).record_settle(t["ticker"], t["pnl"])
            state.logline(f"SETTLED {t['ticker']} side={t['side']} result={res} qty={t['qty']} pnl={t['pnl']:+.2f}")
    if changed: state.rewrite_trades(trades)

def single_instance(root: pathlib.Path):
    """Windows-safe exclusive lock: a file opened with O_EXCL-style semantics via msvcrt locking; held for process lifetime."""
    import os, msvcrt
    root.mkdir(parents=True, exist_ok=True)
    fh = open(root / "runner.lock", "a+")
    try: msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("another runner holds the lock; exiting", flush=True); raise SystemExit(0)
    return fh

def run(thr: float, t_open: float, t_close: float, lot: int, poll_s: float, root: pathlib.Path, series: str = "KXBTCD", product: str = "BTC-USD", sigma_gate: bool = False):
    _lock = single_instance(root)
    k = KalshiPublic(min_interval_s=0.12); state = State(root); sig = None; sig_ts = 0
    state.logline(f"start {series}/{product} thr={thr} window=T-{t_open}..T-{t_close}min lot={lot} sigma_gate={sigma_gate} sigma_max={SIGMA_MAX.get(series)}")
    while True:
        try:
            now = utcnow(); next_hour = (int(now) // 3600 + 1) * 3600; mins_left = (next_hour - now) / 60
            if mins_left > t_open + 1 or mins_left < t_close:
                settle(k, state); time.sleep(min(20, max(2, (mins_left - t_open) * 60) if mins_left > t_open else 5)); continue
            if now - sig_ts > 900:
                s = sigma_1m(product); sig = s or sig; sig_ts = now
            close_iso = dt.datetime.fromtimestamp(next_hour, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rep = replica_latest(product.split("-")[0])          # BRTI replica (median of constituent mids) if fresh, else Coinbase last trade
            spot = rep["replica"] if rep else float(jget(f"{CB_BASE}/{product}/ticker")["price"])
            partial, partial_n = (rep.get("partial_twap"), rep.get("partial_n", 0)) if rep else (None, 0)
            cands = [m for m in k.markets(series_ticker=series, status="open", limit=1000, max_pages=2) if m["close_time"] == close_iso and m.get("floor_strike")]
            # near the money: within 4 sigma*sqrt(t) in log space
            band = 4 * (sig or 0.0006) * math.sqrt(max(mins_left, 0.5))
            near = sorted([m for m in cands if abs(math.log(spot / float(m["floor_strike"]))) <= band], key=lambda m: abs(math.log(spot / float(m["floor_strike"]))))
            if mins_left < 1.5: near = near[:3]                 # final minute: only the 3 nearest strikes (shared rate limiter)
            # one directional position per asset-hour (review C6): collect candidates, take the best edge/sd, re-check the book before filling (F2)
            have_hour = any(t.get("close_ts") == next_hour for t in state.open_positions.values())
            best = None
            for m in near:
                K = float(m["floor_strike"]); tk = m["ticker"]
                try: ob = k.orderbook(tk)
                except Exception: continue
                yb, ya, ybs, yas = book_touch(ob)
                p = p_yes(spot, K, sig or 0.0006, mins_left, partial, partial_n)
                mid = (yb + ya) / 2 if 0 < ya < 1 else None
                regime = ("lo" if (sig or 0.0006) < SIGMA_MAX.get(series, 9) else "hi")
                imb = (ybs - yas) / (ybs + yas) if (ybs + yas) > 0 else None
                sig_mkt = None
                if mid and 0.05 < mid < 0.95 and abs(math.log(spot / K)) > 1e-6:
                    try: sig_mkt = abs(math.log(spot / K)) / (abs(PhiInv(mid)) * math.sqrt(twap_horizon(mins_left)))
                    except (ZeroDivisionError, ValueError): sig_mkt = None
                snap = {"t": now, "mins_left": round(mins_left, 2), "ticker": tk, "K": K, "spot": spot, "spot_src": "replica" if rep else "coinbase", "partial_twap": partial, "partial_n": partial_n, "sigma1": sig, "sigma_regime": regime,
                        "sigma_mkt": sig_mkt, "spread": round(ya - yb, 2), "imb": imb, "p_model": p, "yes_bid": yb, "yes_ask": ya, "bid_size": ybs, "ask_size": yas, "close_ts": next_hour}
                state.append(state.books, snap)
                # scored rows (P6 regime split / P9 final-minute books skill): one row per strike per poll inside the trading window
                if mid and (t_close <= mins_left <= t_open):
                    state.append(state.score, {"t": now, "family": f"{root.name}-{'books' if mins_left < 1.0 else 'sigma-' + regime}", "ticker": tk, "p_model": p, "p_market": mid, "yes_bid": yb, "yes_ask": ya,
                                               "bid_size": ybs, "ask_size": yas, "close_ts": next_hour, "mins_left": round(mins_left, 2), "sigma1": sig, "sigma_mkt": sig_mkt})
                if have_hour or tk in state.open_positions or not (t_close <= mins_left <= t_open): continue
                if sigma_gate and regime == "hi": continue        # P6: sigma-hi is scored only, never traded
                side = None
                if p - ya >= thr and 0 < ya < 1 and yas > 0: side, edge = "yes", p - ya
                elif (yb - p) >= thr and 0 < yb < 1 and ybs > 0: side, edge = "no", yb - p
                if side:
                    score = edge / math.sqrt(max(p * (1 - p), 1e-4))
                    if best is None or score > best["score"]: best = {"score": score, "tk": tk, "K": K, "side": side, "p": p, "mid": (yb + ya) / 2, "t_dec": time.time(), "regime": regime, "spread": round(ya - yb, 2), "imb": imb, "sigma_mkt": sig_mkt}
            if best:
                tk, side, p = best["tk"], best["side"], best["p"]
                try: yb2, ya2, ybs2, yas2 = book_touch(k.orderbook(tk))
                except Exception: yb2 = None
                latency = time.time() - best["t_dec"]
                ok = yb2 is not None and ((side == "yes" and p - ya2 >= thr and 0 < ya2 < 1 and yas2 > 0) or (side == "no" and yb2 - p >= thr and 0 < yb2 < 1 and ybs2 > 0))
                if not ok:
                    state.logline(f"STALE {tk} {side} touch moved before fill (latency {latency:.2f}s)"); state.append(state.books, {"t": now, "ticker": tk, "stale_fill": True})
                else:
                    price = min((ya2 if side == "yes" else 1 - yb2) + 0.01, 0.99); avail = yas2 if side == "yes" else ybs2   # +1 tick slippage
                    p_side = p if side == "yes" else 1 - p; pm_side = best["mid"] if side == "yes" else 1 - best["mid"]
                    led = Ledger(root, family=root.name)
                    qty, why = risk_size(series, p_side, price, avail, led, lot_cap=lot, p_market=pm_side, fee_per_contract=kalshi_taker_fee(10, price) / 10, close_ts=next_hour)
                    if qty <= 0: state.logline(f"SKIP {tk} {side} {why}")
                    else:
                        cost = qty * price; fee = kalshi_taker_fee(qty, price)
                        t = {"t": now, "ticker": tk, "close_ts": next_hour, "side": side, "price": price, "qty": qty, "avail_at_touch": avail, "slippage": 0.01, "decision_latency_s": round(latency, 2),
                             "cost": cost, "fee": fee, "p_model": p, "p_market": best["mid"], "spot": spot, "K": best["K"], "mins_left": round(mins_left, 2), "status": "open", "series": series, "risk": why,
                             "sigma1": sig, "sigma_regime": best["regime"], "spread": best["spread"], "imb": best["imb"], "sigma_mkt": best["sigma_mkt"]}
                        state.append(state.trades, t); state.open_positions[tk] = t; led.record_open(tk, series, cost, close_ts=next_hour)
                        state.logline(f"PAPER BUY {side.upper()} {qty}@{price:.2f} {tk} p_model={p:.2f} avail={avail:.0f} T-{mins_left:.1f}m lat={latency:.2f}s")
            time.sleep(1.0 if mins_left < 1.5 else poll_s)
        except KeyboardInterrupt: raise
        except Exception as e:
            state.logline(f"error {type(e).__name__}: {str(e)[:160]}"); Ledger(root).record_error(f"{type(e).__name__}: {e}"); time.sleep(5)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--thr", type=float, default=0.05); ap.add_argument("--t-open", type=float, default=8)
    ap.add_argument("--t-close", type=float, default=2); ap.add_argument("--root", default=None); ap.add_argument("--sigma-gate", action="store_true"); ap.add_argument("--lot", type=int, default=20); ap.add_argument("--poll", type=float, default=4.0); ap.add_argument("--series", default="KXBTCD"); ap.add_argument("--product", default="BTC-USD")
    a = ap.parse_args(); root = pathlib.Path(a.root) if a.root else (pathlib.Path("data/s7live") if a.series == "KXBTCD" else pathlib.Path(f"data/s7live-{a.series}"))
    run(a.thr, a.t_open, a.t_close, a.lot, a.poll, root, a.series, a.product, a.sigma_gate)
