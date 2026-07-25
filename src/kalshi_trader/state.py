from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from kalshi_trader.domain import OrderIntent, OrderResult, OrderStatus, Outcome


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY,
                order_id TEXT,
                ticker TEXT NOT NULL,
                outcome TEXT NOT NULL,
                count INTEGER NOT NULL,
                limit_price TEXT NOT NULL,
                max_loss_cents INTEGER NOT NULL,
                fair_probability TEXT NOT NULL,
                model_version TEXT NOT NULL,
                signal_generated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                filled_count TEXT NOT NULL DEFAULT '0',
                average_fill_price TEXT,
                fee_dollars TEXT NOT NULL DEFAULT '0',
                error TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orders_ticker_status
                ON orders(ticker, status);
            CREATE TABLE IF NOT EXISTS controls (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS equity_baselines (
                trading_day TEXT PRIMARY KEY,
                equity_cents INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_account (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                balance_cents INTEGER NOT NULL,
                realized_pnl_cents INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                ticker TEXT NOT NULL,
                outcome TEXT NOT NULL,
                count INTEGER NOT NULL,
                cost_cents INTEGER NOT NULL,
                PRIMARY KEY(ticker, outcome)
            );
            """
        )
        if self.get_control("kill_switch") is None:
            self.set_control("kill_switch", "false")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def close(self) -> None:
        self._connection.close()

    def record_intent(self, intent: OrderIntent) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            self._connection.execute(
                """
                INSERT INTO orders (
                    client_order_id, ticker, outcome, count, limit_price,
                    max_loss_cents, fair_probability, model_version,
                    signal_generated_at, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.client_order_id,
                    intent.ticker,
                    intent.outcome.value,
                    intent.count,
                    str(intent.limit_price),
                    intent.max_loss_cents,
                    str(intent.fair_probability),
                    intent.model_version,
                    intent.signal_generated_at.isoformat(),
                    OrderStatus.PENDING.value,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise StateError(f"duplicate client_order_id {intent.client_order_id}") from exc

    def record_result(self, result: OrderResult, *, error: str | None = None) -> None:
        raw_json = json.dumps(result.raw, sort_keys=True) if result.raw is not None else None
        cursor = self._connection.execute(
            """
            UPDATE orders
            SET order_id = ?, status = ?, filled_count = ?, average_fill_price = ?,
                fee_dollars = ?, error = ?, raw_json = ?, updated_at = ?
            WHERE client_order_id = ?
            """,
            (
                result.order_id,
                result.status.value,
                str(result.filled_count),
                str(result.average_fill_price) if result.average_fill_price is not None else None,
                str(result.fee_dollars),
                error,
                raw_json,
                datetime.now(UTC).isoformat(),
                result.client_order_id,
            ),
        )
        if cursor.rowcount != 1:
            raise StateError(f"unknown client_order_id {result.client_order_id}")

    def reject_intent(self, client_order_id: str, reason: str) -> None:
        cursor = self._connection.execute(
            "UPDATE orders SET status = ?, error = ?, updated_at = ? WHERE client_order_id = ?",
            (
                OrderStatus.REJECTED.value,
                reason,
                datetime.now(UTC).isoformat(),
                client_order_id,
            ),
        )
        if cursor.rowcount != 1:
            raise StateError(f"unknown client_order_id {client_order_id}")

    def last_order_at(self, ticker: str) -> datetime | None:
        row = self._connection.execute(
            """
            SELECT created_at FROM orders
            WHERE ticker = ? AND status NOT IN (?, ?)
            ORDER BY created_at DESC LIMIT 1
            """,
            (ticker, OrderStatus.REJECTED.value, OrderStatus.CANCELED.value),
        ).fetchone()
        return datetime.fromisoformat(row["created_at"]) if row else None

    def get_control(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM controls WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_control(self, key: str, value: str) -> None:
        self._connection.execute(
            """
            INSERT INTO controls(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, datetime.now(UTC).isoformat()),
        )

    def kill_switch_active(self) -> bool:
        return self.get_control("kill_switch") == "true"

    def set_kill_switch(self, active: bool) -> None:
        self.set_control("kill_switch", "true" if active else "false")

    def daily_pnl(self, equity_cents: int, *, trading_day: date | None = None) -> int:
        day = (trading_day or datetime.now(UTC).date()).isoformat()
        row = self._connection.execute(
            "SELECT equity_cents FROM equity_baselines WHERE trading_day = ?", (day,)
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO equity_baselines VALUES (?, ?, ?)",
                (day, equity_cents, datetime.now(UTC).isoformat()),
            )
            return 0
        return equity_cents - int(row["equity_cents"])

    def initialize_paper(self, starting_balance_cents: int) -> None:
        self._connection.execute(
            """
            INSERT INTO paper_account(singleton, balance_cents)
            VALUES (1, ?)
            ON CONFLICT(singleton) DO NOTHING
            """,
            (starting_balance_cents,),
        )

    def paper_account(self) -> tuple[int, int]:
        row = self._connection.execute(
            "SELECT balance_cents, realized_pnl_cents FROM paper_account WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise StateError("paper account is not initialized")
        return int(row["balance_cents"]), int(row["realized_pnl_cents"])

    def apply_paper_fill(self, intent: OrderIntent, *, fill_price: Decimal, fee_cents: int) -> None:
        cost_cents = (
            int((fill_price * intent.count * 100).quantize(Decimal("1"), rounding=ROUND_CEILING))
            + fee_cents
        )
        with self.transaction():
            cursor = self._connection.execute(
                """
                UPDATE paper_account SET balance_cents = balance_cents - ?
                WHERE singleton = 1 AND balance_cents >= ?
                """,
                (cost_cents, cost_cents),
            )
            if cursor.rowcount != 1:
                raise StateError("paper account has insufficient cash")
            self._connection.execute(
                """
                INSERT INTO paper_positions(ticker, outcome, count, cost_cents)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker, outcome) DO UPDATE SET
                    count = count + excluded.count,
                    cost_cents = cost_cents + excluded.cost_cents
                """,
                (intent.ticker, intent.outcome.value, intent.count, cost_cents),
            )

    def paper_exposure(self, ticker: str | None = None) -> int:
        if ticker is None:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(cost_cents), 0) AS exposure FROM paper_positions"
            ).fetchone()
        else:
            row = self._connection.execute(
                """
                SELECT COALESCE(SUM(cost_cents), 0) AS exposure
                FROM paper_positions WHERE ticker = ?
                """,
                (ticker,),
            ).fetchone()
        return int(row["exposure"])

    def open_order_count(self) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count FROM orders
            WHERE status IN (?, ?)
            """,
            (OrderStatus.PENDING.value, OrderStatus.RESTING.value),
        ).fetchone()
        return int(row["count"])

    def resting_order_ids(self) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT order_id FROM orders
            WHERE status = ? AND order_id IS NOT NULL
            """,
            (OrderStatus.RESTING.value,),
        ).fetchall()
        return [str(row["order_id"]) for row in rows]

    def mark_canceled(self, order_id: str) -> None:
        self._connection.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
            (OrderStatus.CANCELED.value, datetime.now(UTC).isoformat(), order_id),
        )

    def status_summary(self) -> dict[str, object]:
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS count FROM orders GROUP BY status"
        ).fetchall()
        return {
            "kill_switch": self.kill_switch_active(),
            "orders": {str(row["status"]): int(row["count"]) for row in rows},
        }

    def order_intents(self) -> list[OrderIntent]:
        rows = self._connection.execute("SELECT * FROM orders ORDER BY created_at").fetchall()
        return [
            OrderIntent(
                client_order_id=row["client_order_id"],
                ticker=row["ticker"],
                outcome=Outcome(row["outcome"]),
                count=int(row["count"]),
                limit_price=Decimal(row["limit_price"]),
                fair_probability=Decimal(row["fair_probability"]),
                signal_generated_at=datetime.fromisoformat(row["signal_generated_at"]),
                model_version=row["model_version"],
            )
            for row in rows
        ]
