"""Kalshi public REST (no credentials). Field names are *_dollars / *_fp strings on current responses."""
from __future__ import annotations
import time, json
from dataclasses import dataclass
from typing import Any, Iterator
import httpx

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "predhft/0.1 (paper research)"}

def f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None

import json as _json, os as _os, pathlib as _pl
_RL = _pl.Path("data/risk"); _RL_INTERVAL = 0.5  # one shared slot per 0.4 s across ALL predhft processes (~2.5 req/s < Kalshi public ~3 req/s)

def _shared_slot():
    """Cross-process token bucket: lock file + last-call timestamp (Windows msvcrt)."""
    try:
        import msvcrt
        _RL.mkdir(parents=True, exist_ok=True); lock = _RL / "kalshi.lock"; stamp = _RL / "kalshi.last"
        with open(lock, "a+") as fh:
            for _ in range(300):
                try: msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1); break
                except OSError: time.sleep(0.02)
            else: return
            try:
                try: last = float(stamp.read_text() or 0)
                except Exception: last = 0.0
                try: bo = float((_RL / "backoff.until").read_text()) if (_RL / "backoff.until").exists() else 0.0
                except Exception: bo = 0.0
                interval = 0.15 if _os.environ.get("PREDHFT_PRIORITY") else _RL_INTERVAL   # S7 decision loops get a priority lane (6.5 s latency seen)
                wait = max(last + interval, bo) - time.time()
                if wait > 0: time.sleep(wait)
                stamp.write_text(repr(time.time()))
            finally:
                try: fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError: pass
    except ImportError: pass

def _log429(path: str):
    try:
        _RL.mkdir(parents=True, exist_ok=True); (_RL / "backoff.until").write_text(repr(time.time() + 5.0))   # all processes pause 5 s after any 429
        (_RL / "429.jsonl").open("a").write(_json.dumps({"t": time.time(), "path": path, "pid": _os.getpid()}) + "\n")
    except Exception: pass

class KalshiPublic:
    def __init__(self, timeout: float = 20.0, min_interval_s: float = 0.05):
        self._c = httpx.Client(base_url=BASE, headers=UA, timeout=timeout)
        self._last = 0.0; self._min = min_interval_s

    def get(self, path: str, **params) -> dict:
        dt = time.monotonic() - self._last
        if dt < self._min: time.sleep(self._min - dt)
        for attempt in range(4):
            _shared_slot()
            r = self._c.get(path, params={k: v for k, v in params.items() if v is not None})
            self._last = time.monotonic()
            if r.status_code == 429:
                _log429(path); time.sleep(2 ** attempt); continue
            r.raise_for_status(); return r.json()
        r.raise_for_status(); return r.json()

    def status(self) -> dict: return self.get("/exchange/status")

    def events(self, status: str = "open", nested: bool = True, limit: int = 200, max_pages: int | None = None) -> Iterator[dict]:
        cur = None; pages = 0
        while True:
            d = self.get("/events", status=status, with_nested_markets=str(nested).lower(), limit=limit, cursor=cur)
            yield from d.get("events", [])
            cur = d.get("cursor"); pages += 1
            if not cur or (max_pages and pages >= max_pages): return

    def markets(self, series_ticker: str | None = None, status: str = "open", limit: int = 1000, max_pages: int | None = None) -> Iterator[dict]:
        cur = None; pages = 0
        while True:
            d = self.get("/markets", series_ticker=series_ticker, status=status, limit=limit, cursor=cur)
            ms = d.get("markets", []); yield from ms
            cur = d.get("cursor"); pages += 1
            if not cur or not ms or (max_pages and pages >= max_pages): return

    def orderbook(self, ticker: str) -> dict: return self.get(f"/markets/{ticker}/orderbook")

    def candlesticks(self, series: str, ticker: str, start_ts: int, end_ts: int, period_minutes: int = 60) -> list[dict]:
        d = self.get(f"/series/{series}/markets/{ticker}/candlesticks", start_ts=start_ts, end_ts=end_ts, period_interval=period_minutes)
        return d.get("candlesticks", [])

def mid_from_candle(c: dict) -> float | None:
    """Mid in dollars from a candlestick; prefers bid/ask close, falls back to price close. Handles cents vs dollars."""
    def val(obj, k):
        if not isinstance(obj, dict): return None
        v = obj.get(f"{k}_dollars", obj.get(k))
        try: v = float(v)
        except (TypeError, ValueError): return None
        return v / 100 if v > 1 else v
    yb, ya = val(c.get("yes_bid"), "close"), val(c.get("yes_ask"), "close")
    if yb is not None and ya is not None and ya >= yb: return (yb + ya) / 2
    return val(c.get("price"), "close")
