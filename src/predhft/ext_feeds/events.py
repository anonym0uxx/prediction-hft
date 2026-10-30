"""Scheduled-event + exchange-status feed. ForexFactory's weekly calendar JSON (public, refreshed hourly here; personal, non-
redistributed use) gives the next high-impact USD release (FOMC, CPI, NFP ...); a release inside or just before a 15-minute window
raises realised vol (the literature finds crypto vol jumps at US macro announcements), it does not pick a direction. Coinbase and
Kraken status pages (statuspage.io JSON) are constituent exchanges of SOLUSD_RTI: an incident there changes how the official index
behaves. Writes data/ext/events.jsonl / .latest.json every POLL_S (status) with the calendar re-fetched every CAL_REFRESH_S."""
from __future__ import annotations
import datetime as dt, sys, time
from . import get_json, write_row

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATUS = {"coinbase": "https://status.coinbase.com/api/v2/status.json", "kraken": "https://status.kraken.com/api/v2/status.json"}
POLL_S = 300.0
CAL_REFRESH_S = 3600.0


def parse_calendar(items: list[dict], currencies=("USD",), impacts=("High",)) -> list[tuple[float, str, str]]:
    """[(unix_ts, title, impact)] of the selected releases, sorted. FF dates carry an explicit offset (e.g. 2026-10-25T15:00:00-04:00)."""
    out = []
    for it in items:
        if it.get("country") not in currencies or it.get("impact") not in impacts: continue
        try: ts = dt.datetime.fromisoformat(it["date"]).timestamp()
        except Exception: continue
        out.append((ts, str(it.get("title"))[:60], str(it.get("impact"))))
    return sorted(out)


def window_flags(cal: list[tuple[float, str, str]], now: float, close_ts: int | None = None, window_s: int = 900, before_s: int = 1800) -> dict:
    """Next high-impact release after now (title, minutes); whether one falls inside the window ending at close_ts, or in the
    before_s before that window opens."""
    close_ts = int(close_ts if close_ts is not None else (now // window_s) * window_s + window_s)
    w0 = close_ts - window_s
    nxt = next(((ts, title) for ts, title, _ in cal if ts >= now), None)
    return {"next_high_usd_title": (nxt[1] if nxt else None), "next_high_usd_in_min": (round((nxt[0] - now) / 60, 1) if nxt else None),
            "high_usd_in_window": any(w0 <= ts < close_ts for ts, _, _ in cal), "high_usd_within_30m_before": any(w0 - before_s <= ts < w0 for ts, _, _ in cal), "window_close_ts": close_ts}


def status_indicator(payload: dict) -> str | None:
    try: return f"{payload['status']['indicator']}:{payload['status']['description']}"[:60]
    except Exception: return None


def one_poll(state: dict, now: float) -> dict:
    row = {"t": now}
    if now - state.get("cal_t", 0) > CAL_REFRESH_S or "cal" not in state:
        try: state["cal"] = parse_calendar(get_json(FF_URL, timeout=10)); state["cal_t"] = now
        except Exception as e: row["cal_err"] = f"{type(e).__name__}:{str(e)[:60]}"
    if state.get("cal") is not None: row.update(window_flags(state["cal"], now)); row["calendar_age_s"] = round(now - state["cal_t"], 1)
    for name, url in STATUS.items():
        try: row[f"{name}_status"] = status_indicator(get_json(url))
        except Exception as e: row[f"{name}_status"] = None; row[f"{name}_err"] = f"{type(e).__name__}"
    return row


def loop(every: float = POLL_S):
    state: dict = {}
    while True:
        t0 = time.time()
        try: write_row("events", one_poll(state, t0))
        except Exception as e: print("events feed error", type(e).__name__, str(e)[:80], file=sys.stderr)
        time.sleep(max(1.0, every - (time.time() - t0)))


if __name__ == "__main__":
    loop()
