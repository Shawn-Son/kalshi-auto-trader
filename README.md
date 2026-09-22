# Kalshi Auto Trader

A risk-first Python/C++ framework for researching probability mispricing and executing
event-contract orders through Kalshi's current V2 Trade API.

This is execution infrastructure, not a profitable strategy in a box. It will only trade when
you supply a recent, independently generated fair-probability signal. No ROI is promised, and
the included example signal is deliberately non-tradable.

## What is included

- Three isolated modes: local paper simulation, Kalshi demo, and locked production.
- Current V2 event-order API (`/portfolio/events/orders`) with fixed-point prices.
- RSA-PSS request signing with secrets loaded only from environment variables.
- Idempotent client order IDs, retry/backoff, duplicate-order reconciliation, and SQLite WAL
  journaling.
- Pre-trade limits for order loss, market and portfolio exposure, daily loss, balance,
  liquidity, spread, stale signals, cooldown, and concurrent open orders.
- Persistent kill switch and managed-order cancellation on shutdown.
- Kalshi's real fee schedule, `ceil(rate * count * price * (1 - price))`, applied everywhere:
  edge thresholds, paper fills, and the backtester. A flat basis-point fee understates the true
  cost by roughly 5x at mid prices.
- Position-aware entries (top up to target, never stack the same side) and rule-based exits
  (sell when the bid exceeds fair value by `exit_edge_bps` after fees). A flip always closes the
  held side before it opens the other.
- Taker or maker order style. Maker orders rest inside the spread with `post_only` and pay the
  maker rate; taker orders cross and pay the taker rate.
- A chronological, fee/slippage-aware backtester with Brier score and drawdown reporting.
- An optional C++ risk kernel with a stable C ABI and Python fallback.
- Structured JSON logs and tests around the dangerous boundaries.

## Architecture

```text
timestamped fair probabilities
            |
            v
 probability mispricing strategy
            |
            v
 Python + optional C++ risk checks <--- Kalshi portfolio/order state
            |
            v
 durable intent journal (SQLite)
            |
            v
 paper broker | demo API | locked production API
```

Python owns orchestration, signing, state, and reconciliation. The C++ library is deliberately
small: it accelerates deterministic integer-only pre-trade checks without owning credentials or
network access.

## Quick start

Requirements: Python 3.11+, and CMake plus a C++20 compiler only if you want the native kernel.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
kalshi-trader doctor --config config/paper.toml
pytest
```

Replace `signals/example.csv` with a current market ticker and current UTC timestamp, then:

```bash
kalshi-trader run --config config/paper.toml --once
kalshi-trader status --config config/paper.toml
```

Paper mode reads public production order books but never authenticates and never sends an order.
Taker orders fill immediately at the touch plus slippage. Maker orders rest and fill on a later
cycle only once the far side of the book trades through the limit; queue position is not modeled,
so paper maker fill rates are optimistic.

## Order lifecycle

Each polling cycle, per market:

1. Reconcile resting orders (paper: fill if traded through; live: refresh from the exchange).
2. Read the current net position.
3. Exit check: if we hold a side and its best bid minus fee exceeds fair value by
   `strategy.exit_edge_bps`, sell up to `risk.max_contracts_per_order` of it. Nothing else
   happens this cycle.
4. Entry check: pick the side whose entry price (taker: ask, maker: bid + improvement) sits below
   fair value by `strategy.min_edge_bps` after fees. Buy only the difference between the target
   count and what is already held. Never open a side while the opposite side is held.
5. Risk checks, journal, submit.

Sells bypass exposure, balance, and cooldown limits because they release exposure; they are still
blocked by the kill switch, closed markets, oversized counts, and are sent `reduce_only`.

## Signal contract

The engine reloads a CSV atomically on every cycle:

```csv
ticker,fair_probability,generated_at,model_version
KXEXAMPLE-26,0.6200,2026-07-25T18:30:00Z,my-model-2026-07-25
```

Write a new file and rename it over the old path; do not update rows in place while the engine is
reading. A signal is rejected if it is stale, too far in the future, malformed, outside configured
probability bounds, or missing a model version.

The probability model must be developed and validated separately. At minimum, evaluate
calibration, Brier score, time leakage, regime stability, data-source latency, and whether the
signal remains positive after actual fees and fill quality.

## Backtesting

The dataset format is:

```csv
ticker,observed_at,fair_probability,yes_ask,no_ask,result
MARKET-A,2025-01-01T12:00:00Z,0.70,0.55,0.47,yes
```

Only the first qualifying observation for each ticker is traded, preventing accidental repeated
entry from dense snapshots.

```bash
kalshi-trader backtest data/history.csv \
  --config config/paper.toml \
  --output outputs/backtest.json
```

The backtester models taker entries with the configured `fees.taker_rate`, slippage, and hold to
settlement. Confirm the rate for each series you trade against Kalshi's published schedule, and use
point-in-time order-book data—not final or revised data.

## Demo trading

Create a demo API key and keep the private key outside the repository:

```bash
export KALSHI_API_KEY_ID="..."
export KALSHI_PRIVATE_KEY_PATH="/secure/path/demo-private-key.key"
kalshi-trader doctor --config config/demo.toml
kalshi-trader run --config config/demo.toml
```

Demo and production credentials are not interchangeable.

## Production interlock

Production is intentionally disabled. All three controls must be present:

1. Change `runtime.allow_live` to `true` in a private `config/local.toml`.
2. Set `KALSHI_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK` in the process environment.
3. Pass `--confirm-live` on the command line.

```bash
kalshi-trader run --config config/local.toml --confirm-live
```

Use `kalshi-trader kill-switch on --config config/local.toml` to prevent new orders. The CLI kill
switch is persistent, but it does not signal a different already-running process immediately; that
process observes it at the next polling cycle. For an emergency, also cancel orders in Kalshi's UI.

## Optional C++ risk core

```bash
make cpp
./build/cpp/kalshi_fastcore_bench
export KALSHI_FASTCORE_LIB="$PWD/build/cpp/libkalshi_fastcore.dylib"  # macOS
```

Use `.so` on Linux. If the variable is absent, behavior falls back to the tested Python
implementation.

## Go-live gate

Do not fund production until all of these are true:

- The exact strategy has passed point-in-time backtests, walk-forward validation, and a long demo
  run with realistic fees and slippage.
- Reconciliation has been tested under timeouts, 429s, disconnects, partial fills, process crashes,
  and restarts.
- Exposure limits have been reviewed against your actual bankroll and worst-case correlated event
  outcomes.
- You have alerts outside this process, host time synchronization, encrypted secret management,
  database backups, and a human emergency-cancel procedure.
- You have reviewed current Kalshi rules, API changelog, market settlement terms, and all legal and
  tax obligations that apply to you.

See [OPERATIONS.md](docs/OPERATIONS.md) for the staged rollout.

## API sources

The integration follows Kalshi's official documentation:

- <https://docs.kalshi.com/getting_started/api_environments>
- <https://docs.kalshi.com/getting_started/quick_start_authenticated_requests>
- <https://docs.kalshi.com/api-reference/orders/create-order-v2>
- <https://docs.kalshi.com/getting_started/rate_limits>
- <https://docs.kalshi.com/changelog>

Kalshi can change its API. Pin a release, monitor the changelog, and rerun contract tests before
deploying.
