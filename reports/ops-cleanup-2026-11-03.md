# Ops cleanup — 2026-11-03 (P32-era)

## Opus shadow restart (P23-llm-shadow v2c)
- Was dark since 10-29: 100 failed forecasts, nearly all `http_402` (Nous inference credits), its daily budget guard latching `stopped`.
- Fix: credits restored; killed the stale runner (old process survived `schtasks /end` — child `python` keeps running, so `/run` respawned and the old in-memory `stopped` state kept winning; found two PIDs via `Win32_Process` and killed 3588), cleared `data/llm-shadow/budget.json` (`stopped=false`, `n402=0`), removed `runner.lock`, fresh start 09:44 PT.
- Verified: 09:55:02 PT forecast `p_llm=0.81`, latency 5.4 s, 8,477 tokens, $0.040/call, `err=none`. Scorecard resumed (cap 192/day).

## Legacy task decommission (24 disabled today)
Disabled (data kept on disk; reversible with `schtasks /change /tn <name> /enable`):
- **Research recorders/watchers:** AAA-Gas, OpenQuotes, Rain-J1, Recorder (fusion), Replica, FeedWatch (inference_queue watch-feeds), Reddit feed, Stocktwits, Sports-S6, StructScan, Tournament-Fwd, Weather-J1.
- **Old S7-era paper traders (superseded by P32 FavT5):** S7-15M-BTC/ETH/SOL, S7-Index-INX/NDX, S7-P13-* (BTC15M/BTCD/ETH15M/SOL15M), S7-Paper/-ETH/-SOL.

## Still running (the whole active stack)
| Task | Role |
|---|---|
| PredHFT-FavT5 | P32 paper trader (the only trader) |
| PredHFT-LLM-Shadow | Opus v2c shadow forecasts (scorecard) |
| PredHFT-SOL-v1 | `--scored-only`: writes the snapshots the shadow packet consumes |
| PredHFT-SOL-WSBook | Kalshi WS book → `data/sol-v1/book/latest`, feeds SOL-v1 snapshots (REST fallback exists) |
| PredHFT-CFB / Context / Perp / ExtFeeds | feeds the v2c packet reads: index path, candles, perp/OI/basis, Polymarket/Hyperliquid/Binance ext |

## Verification of dependency graph (grep-based)
- FavT5 reads only the Kalshi REST API (`markets`, `orderbook`) — no local feed deps.
- v2c packet reads: `data/cfb`, `data/candles`, `data/perp`, `data/context/<asset>` candles, `data/sol-v1/snapshots`, ext fields. Nothing active reads replica/aaa/rain/ts-journals/reddit/social — confirmed before disabling.