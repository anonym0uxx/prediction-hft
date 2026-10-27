# Data layout: paper vs live are separate sources of truth

| | Paper (today) | Live (future) |
|---|---|---|
| Process switch | default (`PREDHFT_MODE` unset or `paper`) | `PREDHFT_MODE=live` set on the scheduled task |
| Family roots | `data/<family>/` (trades.jsonl, books.jsonl, supply.jsonl, scoreboard.jsonl, runner.log) | `data/live/<family>/` |
| Global risk ledger | `data/risk/events.jsonl` | `data/live/risk/events.jsonl` |
| Row tag | every trade and ledger row carries `"mode": "paper"` | `"mode": "live"` |
| Loss halts | shadow-logged (`kind=halt_shadow`), not enforced | enforced |
| Enforcement | `s7_live.State` refuses to write under `data/live/` in paper mode | `State` refuses any root not under `data/live/` in live mode |
| Daily summary | reads `data/` roots | will read `data/live/` roots separately, never summed with paper |

Market data feeds (replica, cfb official feed, perp, context/sentiment, universe DB) are shared inputs, not sources of truth
for P&L, and live under `data/replica`, `data/cfb`, `data/perp`, `data/context`.

Order placement code does not exist yet. When it is built it will live in a module that imports `risk.MODE` and refuses to run
unless `MODE == "live"`; the Kalshi key is used read-only (websocket) until then.
