"""`python -m predhft.ext_feeds [polymarket,hyperliquid,binance_flow,events]` - runs the selected feeds, each in its own thread,
single process, log to stderr. Read-only public endpoints; no credentials are read anywhere in this package."""
from __future__ import annotations
import importlib, sys, threading, time

ALL = ("polymarket", "hyperliquid", "binance_flow", "events")


def main(names: list[str]):
    th = []
    for n in names:
        mod = importlib.import_module(f"predhft.ext_feeds.{n}")
        t = threading.Thread(target=mod.loop, name=n, daemon=True); t.start(); th.append(t)
        print(f"ext_feeds: started {n}", file=sys.stderr)
    while any(t.is_alive() for t in th): time.sleep(5)


if __name__ == "__main__":
    main((sys.argv[1] if len(sys.argv) > 1 else ",".join(ALL)).split(","))
