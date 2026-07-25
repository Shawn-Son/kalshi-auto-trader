# Operations runbook

## Staged rollout

1. Run unit tests and the backtester on a frozen dataset.
2. Run paper mode against live public data for at least two weeks.
3. Run Kalshi demo with the final configuration and restart the process during active orders.
4. Shadow production market data with order submission disabled.
5. Start production at one contract per order and a daily loss limit you can comfortably lose.
6. Increase limits only after reviewing fills, rejects, slippage, calibration, and reconciliation.

Changing code and increasing limits in the same deployment is prohibited operationally.

## Startup

```bash
source .venv/bin/activate
kalshi-trader doctor --config config/local.toml
kalshi-trader status --config config/local.toml
kalshi-trader run --config config/local.toml --confirm-live
```

Run under a supervisor that restarts on failure with a bounded backoff. Ensure the host clock is
NTP-synchronized because request signatures contain millisecond timestamps.

## Normal monitoring

Alert on:

- any `UNKNOWN` order state;
- repeated HTTP 401, 409, 429, or 5xx responses;
- failed cancellation;
- stale or missing signals;
- no successful market-data cycle;
- database I/O failure;
- exposure, daily loss, and balance approaching their limits;
- process restart or clock offset.

Logs are JSON on standard error. Ship them to append-only storage without credentials.

## Emergency stop

1. Cancel all orders in Kalshi's UI.
2. Stop the trader process.
3. Enable the local switch:

   ```bash
   kalshi-trader kill-switch on --config config/local.toml
   ```

4. Verify positions and resting orders directly in Kalshi.
5. Preserve logs and the SQLite database for reconciliation.

The kill switch prevents new local submissions. It cannot guarantee cancellation if the network
or exchange is unavailable.

## Recovery after an ambiguous submission

An order can be accepted even if the HTTP response is lost. Never generate a second client order
ID to "try again." This repository reuses the original ID and reconciles duplicates against the
order list. If state remains `unknown`, keep the kill switch on and reconcile in Kalshi before
resuming.

## Database

SQLite runs in WAL mode with full synchronous writes. Keep the database on a local durable disk,
not an eventually consistent network mount. Back up the database only with a SQLite-aware backup
tool while the process is running.
