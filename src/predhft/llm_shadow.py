"""P23 — LLM early-entry shadow (SCORED-ONLY sidecar; docs/PREREG.md §P23).

Owner's hypothesis (Alon): entering EARLIER in the 15-minute window, with the SAME deterministic snapshot plus gathered context
handed to the LLM Hermes uses, may beat the deterministic SOL-v1 gates that only find hair-splitting opportunities near the close.

What this is:   at fixed decision times (default T-10 min and T-5 min) per KXSOL15M market, tail SOL-v1's own snapshots.jsonl
(the deterministic evaluation that is already running), assemble a deterministic context packet, ask the model for a calibrated
P(YES), run that probability through SOL-v1's OWN EV / execution functions at the same snapshot (depth-weighted price, fee,
adverse haircut, edge requirement, Kelly sizing, paper fill) and log the hypothetical decision next to the deterministic policy's
actual decision at that moment. Settlement and scoring: scripts/llm_shadow_score.py.
What this is NOT: a trading change. Nothing here touches risk state, fills.jsonl or any order path, and the model is never inside
the 2-s hot loop (fixed decision times only).
P24 (v2, docs/PREREG.md §P24): the SAME call also returns a `decision` block that src/predhft/llm_direct.py paper-acts on
directly (no edge gates, hard risk limits only, own family data/llm-direct/, shared global risk ledger). This module stays the
single owner of the API call and the budget; the direct arm is a consumer of the parsed answer (`--no-direct` disables it).

Budget: persisted per Pacific day (data/llm-shadow/budget.json): hard cap on calls (default 192 = 2 x 96 SOL markets), max_tokens
<= 400, temperature 0, exponential backoff, stop on HTTP 402 or a 429 storm. BTC/ETH are off by default (--assets SOL).
Security (binding): the API key is read from Hermes' .env at runtime into memory only; never printed, logged, copied or placed in
command arguments; every logged string passes through Redactor.redact.
"""
from __future__ import annotations
import argparse, bisect, datetime as dt, hashlib, json, math, os, pathlib, re, time
from zoneinfo import ZoneInfo
from . import sol_v1 as S

VERSION = "P23-llm-shadow-v2"           # v2: the same call also returns the P24 direct trade decision block (llm_direct.py); new prompt_hash -> cohorts P23-v3 / P24-v1
ROOT = pathlib.Path("data/llm-shadow")
SNAP = pathlib.Path("data/sol-v1/snapshots.jsonl")
HERMES_HOME = pathlib.Path(os.environ.get("HERMES_HOME", str(pathlib.Path.home() / "AppData" / "Local" / "hermes")))
PT = ZoneInfo("America/Los_Angeles")

CFG = {
    "decision_secs_left": [600.0, 300.0],   # T-10 min, T-5 min
    "decision_tolerance_s": 45.0,           # fire when secs_left in (d - tol, d]; one call per (close, d)
    "max_snapshot_age_s": 12.0,             # the SOL-v1 snapshot we hand over must be this fresh
    "daily_call_cap": 192, "max_tokens": 400, "temperature": 0.0, "timeout_s": 60.0,
    "retries": 3, "backoff_base_s": 2.0, "rate_limit_storm": 5,   # 429s in one day before stopping
    "reasoning": {"effort": "low"},         # the Hermes default model is a reasoning model: without this it spends all 400 tokens thinking and returns empty content (verified 2026-10-28)
    "index_path_s": 600, "index_path_step_s": 10, "candle_minutes": 60,
    "perp_max_age_s": 10.0, "context_max_age_s": 300.0,
    "assets": ["SOL"],                      # BTC/ETH off by default (budget); context candles always include BTC/ETH as co-movers
}

SYSTEM_PROMPT = """You are a probability forecaster for a Kalshi 15-minute SOL market (series KXSOL15M). Answer with a calibrated probability, not advice.

EXACT SETTLEMENT RULE: the market asks whether the SOL price at close is at or above the strike K. "Price" means the CF Benchmarks SOLUSD Real-Time Index (SOLUSD_RTI, 1 value per second). The expiration value is the simple average of the 60 one-second index values in the minute BEFORE the close (seconds T-60 .. T-1), rounded to 4 decimals. K is the same 60-second average taken before the market OPEN (15 minutes earlier), also 4 decimals. YES settles if expiration value >= K; an exact tie is YES. Nothing else matters: not the spot on any exchange, not the last print, not the candle close.

You receive a deterministic packet: the live policy's own snapshot (its model probability p_model from a Gaussian on the remaining-sample mean, the Kalshi YES mid and top-3 book both sides, the observed partial average of the settlement window if it has started, realised 1-second volatility, seconds left, K, the current official index and its distance to K in basis points), the recent official index path, 1-minute candles for SOL/BTC/ETH, perp funding/open-interest/basis and order-flow context when fresh. Every field carries its age in seconds. Stale or missing fields are marked; do not invent them.

SECOND TASK (independent of the probability; a PAPER trading account, no real money): you also manage a small paper position in this market. Account: paper bankroll $250. Each contract pays $1.00 if your side wins and $0 otherwise; prices are in dollars 0.01..0.99 (YES price p; a NO contract costs 1 - p). Lot size 5..20 contracts; one position per market. Buying hits the displayed offers: a YES buy walks the asks_top3, a NO buy walks the bids_top3 at NO price 1 - yes_bid, depth-weighted, never above your limit_price; if no displayed size is at or below your limit you get no fill (partial fills happen). Kalshi taker fee per order = ceil to the cent of 0.07 x qty x price x (1 - price) (about 1.2 c/contract near 0.30, 1.75 c at 0.50), charged on entry and again on any exit; settlement is free. Your decision is executed AS GIVEN: no edge threshold, no blending, no second opinion — only hard risk limits (lot 5..20, $15 cost per market, per-close exposure caps, loss halts) can shrink or refuse it. Say NO_TRADE freely; most 15-minute markets offer nothing. The packet field "direct_arm" tells you the allowed actions and, when you hold a position, its side, qty, entry price, current bid/ask and unrealised P&L: then choose HOLD (keep to settlement) or EXIT (sell the whole position now at the executable bid, depth-weighted, fee again).

Respond ONLY with a JSON object, no prose outside it:
{"p_yes": <number 0..1, probability the market settles YES>,
 "rationale": "<at most 2 sentences>",
 "beyond_market": "<one sentence: what information, if any, you used that the current market mid does not already reflect; say 'none' if none>",
 "decision": {"action": "<one of the allowed actions: BUY_YES | BUY_NO | NO_TRADE, or HOLD | EXIT when holding>",
              "qty": <integer 5..20; 0 for NO_TRADE/HOLD/EXIT>,
              "limit_price": <number 0.01..0.99 in dollars, the most you pay per contract on YOUR side (NO price = 1 - yes price); null unless buying>,
              "hold_plan": "<HOLD_TO_SETTLEMENT | REVIEW_AT_T5>",
              "reason": "<at most 2 sentences>"}}
Be calibrated: the market mid is a strong baseline; deviate only for a stated reason. Do not output anything else."""

PROMPT_HASH = hashlib.sha256((VERSION + "\n" + SYSTEM_PROMPT).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------------------------ credentials
class Redactor:
    """Holds the secret in memory; redact() removes it (and any >= 12-char fragment of it) from text destined for logs."""
    def __init__(self, secret: str | None):
        self.secret = secret or ""
        self._frags = {self.secret[i:i + 12] for i in range(0, max(len(self.secret) - 11, 0))} if len(self.secret) >= 12 else set()
    def redact(self, text) -> str:
        s = str(text)
        if self.secret and self.secret in s: s = s.replace(self.secret, "[REDACTED]")
        for f in self._frags:
            if f in s: s = s.replace(f, "[REDACTED]")
        return re.sub(r"\b(sk|nous|key)[-_][A-Za-z0-9_\-]{16,}", "[REDACTED]", s)

def load_credentials(home: pathlib.Path = HERMES_HOME) -> tuple[str, str, str]:
    """(api_key, model_name, base_url) from Hermes' .env (NOUS_API_KEY=...) and config.yaml (model: default: ...). Never logged.
    Overrides: PREDHFT_LLM_MODEL, PREDHFT_LLM_BASE_URL environment variables (names only; the key always comes from the file)."""
    key = None
    for line in (home / ".env").read_text(encoding="utf-8").splitlines():
        l = line.strip()
        if l.startswith("export "): l = l[7:].strip()
        if l.startswith("NOUS_API_KEY="):
            key = l.split("=", 1)[1].strip().strip('"').strip("'"); break
    if not key: raise RuntimeError("NOUS_API_KEY not found in Hermes .env")
    model = os.environ.get("PREDHFT_LLM_MODEL"); base = os.environ.get("PREDHFT_LLM_BASE_URL")
    cfg_lines = (home / "config.yaml").read_text(encoding="utf-8").splitlines()
    if not model:
        in_model = False
        for l in cfg_lines:
            if l.strip() == "model:": in_model = True; continue
            if in_model and l.strip().startswith("default:"): model = l.split(":", 1)[1].strip().strip('"'); break
            if in_model and l and not l.startswith(" "): in_model = False
    if not base:
        for l in cfg_lines:
            if l.strip().startswith("base_url:"): base = l.split(":", 1)[1].strip().strip('"'); break
    return key, (model or "unknown"), (base or "https://inference-api.nousresearch.com/v1").rstrip("/")


# ------------------------------------------------------------------------------------------------------------------ budget
def pt_day(t: float) -> str: return dt.datetime.fromtimestamp(t, PT).strftime("%Y-%m-%d")

class Budget:
    """Persisted daily (Pacific) budget: calls, tokens, stop flag. Stops on cap, on HTTP 402, or on >= rate_limit_storm 429s."""
    def __init__(self, path: pathlib.Path, cap: int = CFG["daily_call_cap"], storm: int = CFG["rate_limit_storm"]):
        self.path = path; self.cap = cap; self.storm = storm; self.state = self._load()
    def _fresh(self, day: str) -> dict: return {"day_pt": day, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "n429": 0, "n402": 0, "errors": 0, "stopped": False, "stop_reason": None}
    def _load(self) -> dict:
        try: return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception: return self._fresh(pt_day(time.time()))
    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True); self.path.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
    def roll(self, now: float):
        d = pt_day(now)
        if self.state.get("day_pt") != d: self.state = self._fresh(d); self._save()
    def allow(self, now: float) -> tuple[bool, str]:
        self.roll(now)
        if self.state["stopped"]: return False, f"stopped:{self.state['stop_reason']}"
        if self.state["calls"] >= self.cap: self.state.update({"stopped": True, "stop_reason": "daily_cap"}); self._save(); return False, "daily_cap"
        return True, "ok"
    def record(self, usage: dict | None):
        self.state["calls"] += 1
        if usage: self.state["prompt_tokens"] += int(usage.get("prompt_tokens") or 0); self.state["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        self._save()
    def http_error(self, status: int):
        self.state["errors"] += 1
        if status == 402: self.state["n402"] += 1; self.state.update({"stopped": True, "stop_reason": "http_402"})
        elif status == 429:
            self.state["n429"] += 1
            if self.state["n429"] >= self.storm: self.state.update({"stopped": True, "stop_reason": "http_429_storm"})
        self._save()
    def snapshot(self) -> dict: return {k: self.state[k] for k in ("day_pt", "calls", "prompt_tokens", "completion_tokens", "n429", "stopped", "stop_reason")}


# ------------------------------------------------------------------------------------------------------------------ packet
def _r(x, d=4):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), d)

def index_path_summary(series: list[tuple[int, float]], now_sec: int, span_s: int = CFG["index_path_s"], step_s: int = CFG["index_path_step_s"]) -> dict | None:
    """Official index sampled every step_s over the last span_s (last value at or before each grid second) + summary stats."""
    if not series: return None
    secs = [s for s, _ in series]; pts = []
    for g in range(now_sec - span_s, now_sec + 1, step_s):
        i = bisect.bisect_right(secs, g)
        pts.append(_r(series[i - 1][1], 4) if i > 0 and secs[i - 1] > g - step_s else None)
    vals = [v for v in pts if v is not None]
    if len(vals) < 2: return None
    rets = [math.log(b / a) for a, b in zip(vals, vals[1:]) if a > 0 and b > 0]
    return {"step_s": step_s, "span_s": span_s, "values": pts, "first": vals[0], "last": vals[-1], "min": min(vals), "max": max(vals),
            "ret_bp": _r(1e4 * (vals[-1] / vals[0] - 1), 2), "rv_bp_per_step": _r(1e4 * math.sqrt(sum(r * r for r in rets) / len(rets)), 2) if rets else None, "n_missing": sum(v is None for v in pts)}

def candle_summary(df, now: float, minutes: int = CFG["candle_minutes"]) -> dict | None:
    """Last `minutes` CLOSED 1-min candles (open time < now - 60): returns 5/15/30/60 min, realised vol, volume. Pure (pandas in, dict out)."""
    if df is None or not len(df): return None
    cutoff = int(now) // 60 * 60 - 60
    d = df[(df.ts <= cutoff) & (df.ts > cutoff - minutes * 60)].sort_values("ts")
    if len(d) < 5: return None
    c = d.close.values.astype(float); ts = d.ts.values.astype(int)
    def ret(k): return _r(1e4 * (c[-1] / c[-1 - k] - 1), 2) if len(c) > k else None
    lr = [math.log(b / a) for a, b in zip(c, c[1:]) if a > 0 and b > 0]
    return {"n": int(len(d)), "last_close_ts": int(ts[-1]), "age_s": _r(now - (ts[-1] + 60), 1), "last_close": _r(c[-1], 4),
            "ret_5m_bp": ret(5), "ret_15m_bp": ret(15), "ret_30m_bp": ret(30), "ret_60m_bp": ret(min(60, len(c) - 1)),
            "rv_1m_bp": _r(1e4 * math.sqrt(sum(r * r for r in lr) / len(lr)), 2) if lr else None,
            "range_60m_bp": _r(1e4 * (float(d.high.max()) / float(d.low.min()) - 1), 2), "vol_sum": _r(float(d.volume.sum()), 2), "returns_1m_bp": [_r(1e4 * r, 1) for r in lr[-15:]]}

def comove(a: dict | None, b: dict | None) -> dict | None:
    """Correlation of the last 1-min returns of two candle summaries (co-move context, descriptive only)."""
    if not a or not b or not a.get("returns_1m_bp") or not b.get("returns_1m_bp"): return None
    x = a["returns_1m_bp"]; y = b["returns_1m_bp"]; n = min(len(x), len(y)); x = x[-n:]; y = y[-n:]
    if n < 5: return None
    mx = sum(x) / n; my = sum(y) / n; sx = math.sqrt(sum((v - mx) ** 2 for v in x)); sy = math.sqrt(sum((v - my) ** 2 for v in y))
    return {"n": n, "corr": _r(sum((p - mx) * (q - my) for p, q in zip(x, y)) / (sx * sy), 3) if sx > 0 and sy > 0 else None}

def assemble_packet(snap: dict, now: float, series: list[tuple[int, float]] | None = None, candles: dict | None = None,
                    perp: dict | None = None, context: dict | None = None, decision_secs_left: float | None = None, cfg: dict = CFG, direct: dict | None = None) -> dict:
    """Deterministic context packet from a SOL-v1 entry_eval snapshot plus optional context, every field with its age (s). Pure.
    `direct` (P24) = {allowed_actions, position|None} from llm_direct.direct_block; absent -> the probability-only packet of P23-v1."""
    K = float(snap["K"]); x = snap.get("x_now"); close_ts = int(snap["close_ts"])
    pk = {"version": VERSION, "ticker": snap.get("ticker"), "close_ts_utc": dt.datetime.fromtimestamp(close_ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "decision_secs_left": decision_secs_left, "secs_left_at_snapshot": _r(close_ts - snap["t"], 1), "snapshot_age_s": _r(now - snap["t"], 2),
          "strike_K": _r(K, 4), "official_index_now": _r(x, 4), "spot_minus_K_bp": _r(1e4 * (x / K - 1), 2) if x else None, "index_age_s": snap.get("cfb_age_s"),
          "settlement_window": {"started": bool(snap.get("n_obs")), "partial_avg": _r(snap.get("partial"), 4), "n_observed": snap.get("n_obs"), "n_remaining": snap.get("m_rem")},
          "policy_model": {"p_model": _r(snap.get("p_model"), 4), "p_used_blend": _r(snap.get("p_yes"), 4), "blend_w_model": snap.get("blend_w"), "z": _r(snap.get("z"), 3),
                           "sd_settle_mean_usd": _r(snap.get("sd_mean"), 4), "sigma_1s_bp": _r(1e4 * snap["sigma_1s"], 3) if snap.get("sigma_1s") else None, "sigma_src": snap.get("sigma_src"),
                           "decision": snap.get("decision"), "reasons": snap.get("reasons")},
          "kalshi_book": {"yes_bid": snap.get("yes_bid"), "yes_ask": snap.get("yes_ask"), "mid": _r(snap.get("mid"), 4), "bids_top3": [[_r(p, 3), _r(q, 1)] for p, q in (snap.get("bids") or [])[:3]],
                          "asks_top3": [[_r(p, 3), _r(q, 1)] for p, q in (snap.get("asks") or [])[:3]], "book_age_s": snap.get("book_age_s"), "src": snap.get("book_src")}}
    now_sec = int(now)
    pk["official_index_path"] = index_path_summary(series, now_sec, cfg["index_path_s"], cfg["index_path_step_s"]) if series else None
    cs = {a: candle_summary(df, now, cfg["candle_minutes"]) for a, df in (candles or {}).items()}
    pk["candles_1m"] = cs or None
    pk["comove"] = {"SOL_BTC": comove(cs.get("SOL"), cs.get("BTC")), "SOL_ETH": comove(cs.get("SOL"), cs.get("ETH"))} if cs else None
    if perp and perp.get("t") is not None and now - perp["t"] <= cfg["perp_max_age_s"]:
        pk["perp_okx"] = {"px": _r(perp.get("px"), 4), "basis_vs_index_bp": perp.get("basis_med_bp"), "age_s": _r(now - perp["t"], 1)}
    else: pk["perp_okx"] = {"stale_or_missing": True, "age_s": (_r(now - perp["t"], 1) if perp and perp.get("t") else None)}
    if context and context.get("t") is not None and now - context["t"] <= cfg["context_max_age_s"]:
        keep = ("funding", "premium", "oi_usd", "oi_chg_15m_bp", "ls_ratio", "cb_imb_60s", "cb_imb_300s", "cb_notional_300s", "okx_taker_imb_5m", "liq_long_15m", "liq_short_15m", "fng")
        pk["context"] = {**{k: context.get(k) for k in keep if k in context}, "age_s": _r(now - context["t"], 1)}
    else: pk["context"] = {"stale_or_missing": True, "age_s": (_r(now - context["t"], 1) if context and context.get("t") else None)}
    if direct is not None: pk["direct_arm"] = direct
    return pk

def packet_hash(pk: dict) -> str: return hashlib.sha256(json.dumps(pk, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

def messages_for(pk: dict) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "PACKET:\n" + json.dumps(pk, sort_keys=True, separators=(",", ":"), default=str)}]


# ------------------------------------------------------------------------------------------------------------------ parse
_NUM = r"([01](?:\.\d+)?|\.\d+|\d{1,3}(?:\.\d+)?\s*%)"

def _to_p(s: str) -> float | None:
    s = s.strip()
    try:
        if s.endswith("%"): v = float(s[:-1].strip()) / 100
        else:
            v = float(s)
            if 5.0 <= v <= 100.0: v = v / 100            # bare percent (82 -> .82); 1 < v < 5 is ambiguous -> rejected
        return v if 0.0 <= v <= 1.0 else None
    except Exception: return None

def parse_response(text: str) -> dict:
    """Robust parse: JSON object (possibly fenced / with prose around) -> 'p_yes'; else regex fallbacks (p_yes / probability ... 0.73 / 73%).
    Returns {p_llm, rationale, beyond_market, parse, decision_raw}; p_llm None when nothing usable. decision_raw = the model's
    'decision' value exactly as given (any type) for llm_direct.parse_decision; None when absent (an absent block is NO_TRADE)."""
    out = {"p_llm": None, "rationale": None, "beyond_market": None, "parse": "none", "decision_raw": None}
    if not text: return out
    t = text.strip()
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        blob = m.group(0)
        for cand in (blob, re.sub(r",\s*}", "}", blob), re.sub(r",\s*([}\]])", r"\1", blob)):
            try: d = json.loads(cand)
            except Exception: continue
            if isinstance(d, dict):
                if "decision" in d: out["decision_raw"] = d["decision"]
                for k in ("p_yes", "probability", "p", "prob_yes", "p_llm"):
                    if k in d:
                        v = d[k]; p = _to_p(str(v))
                        if p is not None:
                            out.update({"p_llm": p, "rationale": (str(d.get("rationale"))[:400] if d.get("rationale") is not None else None),
                                        "beyond_market": (str(d.get("beyond_market"))[:300] if d.get("beyond_market") is not None else None), "parse": "json"}); return out
                break
    for pat, tag in ((r'"?p_yes"?\s*[:=]\s*' + _NUM, "regex_key"), (r"probability[^0-9]{0,40}" + _NUM, "regex_prob"), (r"\b" + _NUM + r"\s*(?:probability|chance|likely)", "regex_trail")):
        mm = re.search(pat, t, re.I)
        if mm:
            p = _to_p(mm.group(1))
            if p is not None: out.update({"p_llm": p, "rationale": t[:400], "parse": tag}); return out
    return out


# ------------------------------------------------------------------------------------------------------------------ LLM client
class LLMClient:
    """OpenAI-compatible chat completions with exponential backoff. The key lives only in this object; errors are redacted."""
    def __init__(self, key: str, model: str, base_url: str, cfg: dict = CFG, transport=None):
        import httpx
        self.model = model; self.base = base_url; self.cfg = cfg; self.red = Redactor(key)
        self._c = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "predhft-p23/0.1"}, timeout=cfg["timeout_s"], transport=transport)
    def complete(self, messages: list[dict], budget: Budget | None = None) -> dict:
        """Returns {ok, text, usage, latency_s, status, error, attempts}. Backoff on 429/5xx/network; 402 and 4xx (other) do not retry."""
        body = {"model": self.model, "messages": messages, "max_tokens": int(min(self.cfg["max_tokens"], 400)), "temperature": float(self.cfg["temperature"])}
        if self.cfg.get("reasoning"): body["reasoning"] = self.cfg["reasoning"]
        last = {"ok": False, "text": None, "usage": None, "latency_s": None, "status": None, "error": None, "attempts": 0}
        for attempt in range(1, int(self.cfg["retries"]) + 1):
            last["attempts"] = attempt; t0 = time.time()
            try:
                r = self._c.post("/chat/completions", json=body); lat = time.time() - t0; last["latency_s"] = round(lat, 3); last["status"] = r.status_code
                if r.status_code == 200:
                    d = r.json(); txt = ((d.get("choices") or [{}])[0].get("message") or {}).get("content")
                    if isinstance(txt, list): txt = "".join(x.get("text", "") for x in txt if isinstance(x, dict))
                    last.update({"ok": True, "text": txt, "usage": d.get("usage"), "model_reported": d.get("model"), "finish_reason": (d.get("choices") or [{}])[0].get("finish_reason")}); return last
                last["error"] = self.red.redact(f"http_{r.status_code}: {r.text[:200]}")
                if budget is not None: budget.http_error(r.status_code)
                if r.status_code == 402 or (400 <= r.status_code < 500 and r.status_code != 429): return last
                if budget is not None and budget.state.get("stopped"): return last
            except Exception as e:
                last["latency_s"] = round(time.time() - t0, 3); last["error"] = self.red.redact(f"{type(e).__name__}: {str(e)[:200]}")
            if attempt < int(self.cfg["retries"]): time.sleep(self.cfg["backoff_base_s"] * (2 ** (attempt - 1)))
        return last


# ------------------------------------------------------------------------------------------------------------------ decision simulation
def simulate_entry(p_yes: float, snap: dict, cfg: dict = S.CFG, size_fn=None) -> dict:
    """Pass a probability through SOL-v1's own entry pipeline at the SAME snapshot: entry_ev both sides (depth-weighted price,
    fee, adverse haircut, edge requirement), price bounds, ledger-free Kelly sizing, paper fill against the same book (+1 tick).
    Pure. Returns {decision, side, qty, px, cost, fee, ev_pc, reasons, ev}."""
    book = {"bids": snap.get("bids") or [], "asks": snap.get("asks") or []}
    z = snap.get("z"); sd = snap.get("sd_mean"); x = snap.get("x_now"); sig = snap.get("sigma_1s") or cfg["sigma_fallback_1s"]
    kap = snap.get("kappa") or S.kappa_for(float(snap.get("secs_left", 0)), cfg); lat = float(snap.get("latency_s") or 0.0)
    out = {"p_yes": p_yes, "decision": "abstain", "side": None, "qty": 0, "px": None, "cost": 0.0, "fee": 0.0, "ev_pc": None, "limit": None, "reasons": [], "ev": None}
    if p_yes is None or not (0.0 <= p_yes <= 1.0): out["reasons"].append("no_probability"); return out
    if not (book["bids"] or book["asks"]): out["reasons"].append("no_book"); return out
    evs = {s: S.entry_ev(p_yes, s, book, cfg["lot"], z, sd, x, sig, lat, cfg, kap) for s in ("yes", "no")}
    out["ev"] = {s: {k: e.get(k) for k in ("p_side", "best", "limit", "qty_obtainable", "px_dw", "req_pc", "adverse_pc", "fee_pc", "ev_pc", "why")} for s, e in evs.items()}
    ok = [e for e in evs.values() if e["why"] == "ok" and e["px_dw"] is not None and cfg["min_px"] <= e["px_dw"] <= cfg["max_px"]]
    if not ok:
        out["reasons"].append("no_positive_ev" if all(e["why"] in ("no_offer", "no_depth_within_limit") for e in evs.values()) else "edge_lt_req"); return out
    e = max(ok, key=lambda e: e["ev_usd"])
    size_fn = size_fn or S.kelly_size_fn()
    qty, why = size_fn(e["side"], e["p_side"], e["px_dw"], e["depth_within_limit"], e["fee_pc"], None); qty = int(min(qty, e["qty_obtainable"])); out["size_reason"] = why
    if qty < cfg["min_lot"]: out["reasons"].append("size_lt_min_lot"); return out
    f = S.paper_fill(e["side"], qty, e["limit"], book)
    out.update({"decision": ("enter" if f["qty"] > 0 else "unfilled"), "side": e["side"], "qty": f["qty"], "intended_qty": qty, "px": f["px"], "cost": f["cost"], "fee": f["fee"], "ev_pc": e["ev_pc"], "limit": e["limit"], "p_side": e["p_side"]})
    return out

def policy_decision_at(snap: dict) -> dict:
    """The deterministic policy's actual decision at that snapshot (for the paired comparison)."""
    d = {"decision": snap.get("decision"), "reasons": snap.get("reasons"), "p_model": snap.get("p_model"), "p_used": snap.get("p_yes"), "blend_w": snap.get("blend_w"), "snapshot_src": snap.get("snapshot_src", "sol_v1")}
    if snap.get("policy_holding"): d["decision"] = "holding"; d["note"] = "SOL-v1 already holds a position in this market (entered earlier); the rebuilt evaluation's decision is hypothetical"
    elif snap.get("decision") == "enter": d.update({"side": snap.get("side"), "qty": snap.get("qty"), "px_dw": snap.get("px_dw"), "limit": snap.get("limit"), "ev_pc": snap.get("ev_pc")})
    return d


# ------------------------------------------------------------------------------------------------------------------ scheduling
def due_decisions(secs_left: float, done: set, close_ts: int, decision_secs: list[float] = CFG["decision_secs_left"], tol: float = CFG["decision_tolerance_s"]) -> list[float]:
    """Decision times d (secs_left) whose window (d - tol, d] contains the current secs_left and that have not fired for this close."""
    return [d for d in decision_secs if (d - tol) < secs_left <= d and (close_ts, d) not in done]

def done_from_log(path: pathlib.Path) -> set:
    done = set()
    if not path.exists(): return done
    for l in path.read_text(encoding="utf-8").splitlines():
        try: r = json.loads(l)
        except Exception: continue
        if r.get("close_ts") is not None and r.get("decision_secs_left") is not None: done.add((int(r["close_ts"]), float(r["decision_secs_left"])))
    return done

def rebuild_snapshot(ref: dict, now: float, sol_root: pathlib.Path = pathlib.Path("data/sol-v1")) -> dict | None:
    """SOL-v1 writes entry_eval snapshots only while it has NO position in the market; once it holds one (it may enter from T-10 min)
    the stream stops. Rebuild an equivalent evaluation with SOL-v1's own pure `decide` on the live official feed + websocket book
    (ledger-free Kelly sizing) so the LLM still gets the same deterministic fields at T-5. Marked snapshot_src = 'rebuilt'."""
    book = S._ws_book(ref["ticker"], sol_root, 10.0)
    if book is None: return None
    series, cfb_recv = S._cfb_tail(now, S.CFG["vol_window_s"] + 120)
    if not series: return None
    rec = S.decide(now, int(ref["close_ts"]), float(ref["K"]), series, cfb_recv, book, book["recv_t"], S.kelly_size_fn(), S.CFG, latency_s=0.0)
    rec.update({"kind": "entry_eval", "ticker": ref["ticker"], "version": S.VERSION, "snapshot_src": "rebuilt"})
    try:
        if S.Positions(sol_root).has_close(int(ref["close_ts"])): rec["policy_holding"] = True
    except Exception: pass
    return rec

def latest_snapshot(path: pathlib.Path = SNAP, tail_bytes: int = 400_000) -> dict | None:
    """Last entry_eval row of SOL-v1's snapshots.jsonl (reads only the tail)."""
    if not path.exists(): return None
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - tail_bytes)); data = fh.read()
    last = None
    for l in data.split(b"\n")[1 if size > tail_bytes else 0:]:
        if not l.strip(): continue
        try: r = json.loads(l)
        except Exception: continue
        if r.get("kind") == "entry_eval": last = r
    return last


# ------------------------------------------------------------------------------------------------------------------ live sidecar
def _append(path: pathlib.Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh: fh.write(json.dumps(obj) + "\n")

def _log(root: pathlib.Path, red: Redactor, m: str):
    line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {red.redact(m)}"; print(line, flush=True)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "runner.log").open("a", encoding="utf-8") as fh: fh.write(line + "\n")

def gather_context(now: float, red: Redactor, root: pathlib.Path, assets=("SOL", "BTC", "ETH")) -> tuple[list, dict, dict | None, dict | None, list[str]]:
    """Official index series (CfbTail), 1-min candles (Coinbase via candles.fetch_1m, cached), perp + context latest.json. Errors are listed, never fatal."""
    errs = []; series = []; candles = {}; perp = None; ctx = None
    try: series, _ = S._cfb_tail(now, CFG["index_path_s"] + 120)
    except Exception as e: errs.append(red.redact(f"cfb:{type(e).__name__}:{str(e)[:60]}"))
    try:
        from .candles import fetch_1m
        for a in assets:
            try: candles[a] = fetch_1m(a, now - (CFG["candle_minutes"] + 5) * 60, now)
            except Exception as e: errs.append(red.redact(f"candles_{a}:{type(e).__name__}:{str(e)[:60]}"))
    except Exception as e: errs.append(red.redact(f"candles:{type(e).__name__}:{str(e)[:60]}"))
    try:
        from .perp_feed import latest as perp_latest; perp = perp_latest("SOL", max_age=CFG["perp_max_age_s"] * 100)      # age is reported in the packet; staleness decided there
    except Exception as e: errs.append(red.redact(f"perp:{type(e).__name__}"))
    try:
        from .context_feed import latest as ctx_latest; ctx = ctx_latest("SOL", max_age=CFG["context_max_age_s"] * 100)
    except Exception as e: errs.append(red.redact(f"context:{type(e).__name__}"))
    return series, candles, perp, ctx, errs

def forecast_once(client: LLMClient, budget: Budget, snap: dict, decision_secs_left: float, now: float, root: pathlib.Path, dry_run: bool = False, direct=None) -> dict:
    """One decision: packet -> (LLM) -> parse -> simulate -> log. Returns the logged row.
    `direct` (P24, llm_direct.DirectArm or None): the SAME call's decision block is parsed and paper-acted on by the direct arm;
    its summary is logged in the row (direct_decision / direct_result). None -> P23 probability arm only."""
    red = client.red
    series, candles, perp, ctx, errs = gather_context(now, red, root)
    dblock = direct.block(snap, decision_secs_left, now) if direct is not None else None
    pk = assemble_packet(snap, now, series, candles, perp, ctx, decision_secs_left, direct=dblock)
    msgs = messages_for(pk); ph = packet_hash(pk)
    row = {"kind": "forecast", "version": VERSION, "t_sent": None, "t_recv": None, "latency_s": None, "ticker": snap.get("ticker"), "close_ts": int(snap["close_ts"]), "decision_secs_left": decision_secs_left,
           "secs_left": pk["secs_left_at_snapshot"], "snapshot_t": snap["t"], "snapshot_age_s": pk["snapshot_age_s"], "snapshot_src": snap.get("snapshot_src", "sol_v1"), "K": snap["K"], "x_now": snap.get("x_now"), "p_llm": None, "p_model": snap.get("p_model"), "p_used": snap.get("p_yes"),
           "mid": snap.get("mid"), "yes_bid": snap.get("yes_bid"), "yes_ask": snap.get("yes_ask"), "rationale": None, "beyond_market": None, "parse": None, "prompt_hash": PROMPT_HASH, "packet_hash": ph,
           "model_name": client.model, "tokens": None, "budget_state": None, "context_errors": errs or None, "packet_fields_stale": {k: True for k in ("perp_okx", "context") if pk.get(k, {}).get("stale_or_missing")} or None,
           "policy_decision": policy_decision_at(snap), "llm_decision": None, "direct_arm": dblock, "direct_decision": None, "direct_result": None, "error": None}
    ok, why = budget.allow(now)
    if not ok: row.update({"error": f"budget:{why}", "budget_state": budget.snapshot()}); _append(root / "forecasts.jsonl", row); return row
    if dry_run:
        row.update({"error": "dry_run", "budget_state": budget.snapshot(), "prompt_chars": sum(len(m["content"]) for m in msgs)}); _append(root / "forecasts.jsonl", row); return row
    row["t_sent"] = time.time(); res = client.complete(msgs, budget); row["t_recv"] = time.time(); row["latency_s"] = round(row["t_recv"] - row["t_sent"], 3)
    if res["ok"]:
        budget.record(res.get("usage")); pr = parse_response(res["text"] or "")
        row.update({"p_llm": pr["p_llm"], "rationale": (red.redact(pr["rationale"]) if pr["rationale"] else None), "beyond_market": (red.redact(pr["beyond_market"]) if pr["beyond_market"] else None), "parse": pr["parse"],
                    "tokens": res.get("usage"), "model_reported": res.get("model_reported"), "attempts": res["attempts"], "finish_reason": res.get("finish_reason")})
        if pr["p_llm"] is None: row["error"] = ("truncated_" + str(res.get("finish_reason")) if not res["text"] else "unparsable"); row["raw_head"] = red.redact((res["text"] or "")[:200])
        else: row["llm_decision"] = simulate_entry(pr["p_llm"], snap)
        if direct is not None:
            # P24: act on the decision block of the same answer (malformed / absent -> NO_TRADE or HOLD, error logged by the arm)
            try:
                dres = direct.act(snap, decision_secs_left, pr.get("decision_raw"), dblock, row["t_recv"], prompt_hash=PROMPT_HASH, model_name=client.model, raw_present=bool(res["text"]))
                row["direct_decision"] = dres.get("decision"); row["direct_result"] = dres.get("result")
            except Exception as e:
                row["direct_result"] = {"status": "arm_error", "error": red.redact(f"{type(e).__name__}: {str(e)[:160]}")}
    else: row.update({"error": res["error"], "attempts": res["attempts"], "status": res["status"]})
    row["budget_state"] = budget.snapshot()
    _append(root / "forecasts.jsonl", row)
    return row

def current_snapshot(now: float, max_age: float = CFG["max_snapshot_age_s"]) -> dict | None:
    """Latest SOL-v1 entry_eval if fresh; otherwise (policy holds a position -> stream stopped) a rebuilt evaluation for the same close."""
    snap = latest_snapshot()
    if snap is None: return None
    if now - snap["t"] <= max_age: snap.setdefault("snapshot_src", "sol_v1"); return snap
    if int(snap["close_ts"]) - now <= 0: return None                     # that market is over; discovery of the next one not yet logged
    try: return rebuild_snapshot(snap, now)
    except Exception: return None

def run(root: pathlib.Path = ROOT, poll_s: float = 5.0, decision_secs: list[float] | None = None, cap: int | None = None, dry_run: bool = False, once: bool = False, now_fn=time.time, direct_enabled: bool = True):
    decision_secs = decision_secs or CFG["decision_secs_left"]; CFG["decision_secs_left"] = decision_secs
    from .s7_live import single_instance; _lock = single_instance(root)      # one sidecar per root: two loops would double every call and the budget count
    key, model, base = load_credentials(); client = LLMClient(key, model, base); red = client.red; del key
    budget = Budget(root / "budget.json", cap or CFG["daily_call_cap"]); done = done_from_log(root / "forecasts.jsonl")
    direct = None
    if direct_enabled and not dry_run:
        from .llm_direct import DirectArm; direct = DirectArm(red=red)       # P24: paper-acts on the decision block of the same call (own family dir, shared risk ledger)
    _log(root, red, f"start {VERSION} model={model} base={base} decision_secs={decision_secs} cap={budget.cap} prompt_hash={PROMPT_HASH} dry_run={dry_run} direct={'on:' + direct.VERSION if direct else 'off'} budget={budget.snapshot()}")
    while True:
        try:
            now = now_fn()
            if direct is not None: direct.tick(now)                           # resting-order fill monitor (exec model v2) + settlements of this arm's positions + 60-s equity marks (incl. idle)
            sleep_s = direct.sleep_s(poll_s) if direct is not None else poll_s  # ~1 s while one of the arm's orders rests, the sidecar's own poll otherwise
            snap = current_snapshot(now)
            if snap is None: time.sleep(sleep_s); continue
            close_ts = int(snap["close_ts"]); secs_left = close_ts - now
            for d in due_decisions(secs_left, done, close_ts, decision_secs, CFG["decision_tolerance_s"]):
                done.add((close_ts, d))
                row = forecast_once(client, budget, snap, d, now, root, dry_run, direct=direct)
                _log(root, red, f"FORECAST {row['ticker']} T-{d:.0f}s p_llm={row['p_llm']} mid={row['mid']} p_model={None if row['p_model'] is None else round(row['p_model'], 4)} lat={row['latency_s']} tokens={(row['tokens'] or {}).get('total_tokens') if row['tokens'] else None} "
                               f"llm_dec={(row['llm_decision'] or {}).get('decision')} policy_dec={row['policy_decision'].get('decision')} direct={(row['direct_decision'] or {}).get('action')}->{(row['direct_result'] or {}).get('status')} err={row['error']} budget={row['budget_state']['calls'] if row['budget_state'] else None}")
                if once: return row
            time.sleep(sleep_s)
        except KeyboardInterrupt: raise
        except Exception as e:
            _log(root, red, f"error {type(e).__name__}: {str(e)[:160]}"); time.sleep(poll_s)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--loop", action="store_true", help="run the sidecar (tails data/sol-v1/snapshots.jsonl)")
    ap.add_argument("--once", action="store_true", help="exit after the first forecast (verification)")
    ap.add_argument("--now", action="store_true", help="fire one forecast immediately on the latest snapshot regardless of decision time (verification only; logged with decision_secs_left = actual)")
    ap.add_argument("--root", default=str(ROOT)); ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--decision-secs", default=",".join(str(int(x)) for x in CFG["decision_secs_left"]), help="comma list of secs_left decision times (default 600,300)")
    ap.add_argument("--model", default=None, help="model id override (e.g. anthropic/claude-opus-5.5)")
    ap.add_argument("--cap", type=int, default=CFG["daily_call_cap"]); ap.add_argument("--dry-run", action="store_true", help="assemble + log packets, no LLM call")
    ap.add_argument("--no-direct", action="store_true", help="disable the P24 direct-decision arm (probability arm only; the prompt still asks for the block)")
    a = ap.parse_args()
    if a.model: os.environ["PREDHFT_LLM_MODEL"] = a.model
    root = pathlib.Path(a.root); ds = [float(x) for x in a.decision_secs.split(",") if x.strip()]
    if a.now:
        key, model, base = load_credentials(); client = LLMClient(key, model, base); del key
        snap = current_snapshot(time.time()); assert snap is not None, "no SOL-v1 snapshot (fresh or rebuildable)"
        row = forecast_once(client, Budget(root / "budget.json", a.cap), snap, round(int(snap["close_ts"]) - time.time(), 1), time.time(), root, a.dry_run)
        print(json.dumps({k: row[k] for k in ("ticker", "secs_left", "p_llm", "mid", "p_model", "latency_s", "tokens", "rationale", "beyond_market", "llm_decision", "policy_decision", "error", "budget_state")}, indent=1, default=str))
    elif a.loop or a.once: run(root, a.poll, ds, a.cap, a.dry_run, a.once, direct_enabled=not a.no_direct)
    else: ap.print_help()
