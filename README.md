# prediction-hft

Low-latency **maker** bot for US-legal prediction markets (Kalshi primary, Polymarket US secondary).
Phase 1 = paper only, CPU only, zero spend, no credentials. See [docs/PHASE1_PLAN.md](docs/PHASE1_PLAN.md) for the gates (G1–G7), strategy families (S1–S6) and evidence base.

Rules of this repo
- Maker-only. Taker orders exist only to flatten risk and are logged as such.
- Every result in `reports/` must be reproducible from a command in this repo and data under `data/`.
- No secrets in Git, logs, or chat. API keys live in Windows Credential Manager (helper in `scripts/`).
- Timestamps are UTC everywhere; DST is a test case, not an assumption.

Layout
```
src/predhft/kalshi_public.py   Kalshi public REST client (markets, events, candlesticks, settled history)
src/predhft/fees.py            Kalshi / Polymarket US fee formulas incl. per-order round-up
src/predhft/calibration.py     Reliability bins, Brier, log-loss, FLB curve helpers
src/predhft/scan.py            Venue scan: liquidity, spreads, turnover, mutually-exclusive sums
tests/                         unit tests (run: uv run pytest)
docs/                          plan, reports
```

Quick start
```
uv sync
uv run pytest
uv run python -m predhft.scan            # live venue scan -> reports/scan-<date>.md
uv run python -m predhft.flb KXHIGHNY    # favorite-longshot / calibration probe for a series
```
