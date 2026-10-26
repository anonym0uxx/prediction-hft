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

class KalshiPublic:
    def __init__(self, timeout: float = 20.0, min_interval_s: float = 0.05):
        self._c = httpx.Client(base_url=BASE, headers=UA, timeout=timeout)
        self._last = 0.0; self._min = min_interval_s

    def get(self, path: str, **params) -> dict:
        dt = time.monotonic() - self._last
        if dt < self._min: time.sleep(self._min - dt)
        for attempt in range(4):
            r = self._c.get(path, params={k: v for k, v in params.items() if v is not None})
            self._last = time.monotonic()
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
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
