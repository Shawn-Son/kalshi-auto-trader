from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from kalshi_trader.domain import MarketQuote, Outcome, Signal, parse_utc


@dataclass(frozen=True)
class MarketRecord:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    status: str
    close_time: datetime | None
    result: Outcome | None
    settled_at: datetime | None


@dataclass(frozen=True)
class HistoryRow:
    """One point-in-time observation joined with the market's eventual result."""

    ticker: str
    observed_at: datetime
    yes_bid: Decimal
    yes_ask: Decimal
    no_bid: Decimal
    no_ask: Decimal
    yes_ask_size: Decimal
    no_ask_size: Decimal
    close_time: datetime | None
    result: Outcome | None
    fair_probability: Decimal | None
    model_version: str | None


class HistoryStore:
    """Append-only research database, separate from the trading journal.

    Tables:
    - markets: one row per market with its latest status and settlement result;
    - market_snapshots: top-of-book plus a JSON ladder at each observation;
    - signal_snapshots: whatever the signal file said at each observation, so that a
      model's live predictions can later be scored against outcomes.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS markets (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT NOT NULL,
                series_ticker TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                close_time TEXT,
                result TEXT,
                settled_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);
            CREATE TABLE IF NOT EXISTS market_snapshots (
                ticker TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                yes_bid TEXT NOT NULL,
                yes_ask TEXT NOT NULL,
                no_bid TEXT NOT NULL,
                no_ask TEXT NOT NULL,
                yes_bid_size TEXT NOT NULL,
                yes_ask_size TEXT NOT NULL,
                no_bid_size TEXT NOT NULL,
                no_ask_size TEXT NOT NULL,
                close_time TEXT,
                book_json TEXT,
                PRIMARY KEY (ticker, observed_at)
            );
            CREATE TABLE IF NOT EXISTS signal_snapshots (
                ticker TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                fair_probability TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                model_version TEXT NOT NULL,
                PRIMARY KEY (ticker, observed_at)
            );
            """
        )

    def close(self) -> None:
        self._connection.close()

    # -- markets -------------------------------------------------------------

    def upsert_market(self, record: MarketRecord, *, seen_at: datetime) -> None:
        self._connection.execute(
            """
            INSERT INTO markets (
                ticker, event_ticker, series_ticker, title, status, close_time,
                result, settled_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                status = excluded.status,
                close_time = COALESCE(excluded.close_time, markets.close_time),
                result = COALESCE(excluded.result, markets.result),
                settled_at = COALESCE(excluded.settled_at, markets.settled_at),
                last_seen_at = excluded.last_seen_at
            """,
            (
                record.ticker,
                record.event_ticker,
                record.series_ticker,
                record.title,
                record.status,
                record.close_time.isoformat() if record.close_time else None,
                record.result.value if record.result else None,
                record.settled_at.isoformat() if record.settled_at else None,
                seen_at.isoformat(),
                seen_at.isoformat(),
            ),
        )

    def unresolved_tickers(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT ticker FROM markets WHERE result IS NULL ORDER BY ticker"
        ).fetchall()
        return [str(row["ticker"]) for row in rows]

    def market(self, ticker: str) -> MarketRecord | None:
        row = self._connection.execute(
            "SELECT * FROM markets WHERE ticker = ?", (ticker,)
        ).fetchone()
        return _row_to_market(row) if row else None

    # -- snapshots -----------------------------------------------------------

    def record_snapshot(self, quote: MarketQuote, *, book: object | None = None) -> None:
        self._connection.execute(
            """
            INSERT OR REPLACE INTO market_snapshots (
                ticker, observed_at, status, yes_bid, yes_ask, no_bid, no_ask,
                yes_bid_size, yes_ask_size, no_bid_size, no_ask_size, close_time, book_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quote.ticker,
                quote.observed_at.isoformat(),
                quote.status,
                str(quote.yes_bid),
                str(quote.yes_ask),
                str(quote.no_bid),
                str(quote.no_ask),
                str(quote.yes_bid_size),
                str(quote.yes_ask_size),
                str(quote.no_bid_size),
                str(quote.no_ask_size),
                quote.close_time.isoformat() if quote.close_time else None,
                json.dumps(book, default=str) if book is not None else None,
            ),
        )

    def record_signal(self, signal: Signal, *, observed_at: datetime) -> None:
        self._connection.execute(
            """
            INSERT OR REPLACE INTO signal_snapshots (
                ticker, observed_at, fair_probability, generated_at, model_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                signal.ticker,
                observed_at.isoformat(),
                str(signal.fair_probability),
                signal.generated_at.isoformat(),
                signal.model_version,
            ),
        )

    def snapshot_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM market_snapshots").fetchone()
        return int(row["n"])

    def summary(self) -> dict[str, object]:
        markets = self._connection.execute(
            "SELECT status, COUNT(*) AS n FROM markets GROUP BY status"
        ).fetchall()
        resolved = self._connection.execute(
            "SELECT COUNT(*) AS n FROM markets WHERE result IS NOT NULL"
        ).fetchone()
        span = self._connection.execute(
            "SELECT MIN(observed_at) AS lo, MAX(observed_at) AS hi FROM market_snapshots"
        ).fetchone()
        signals = self._connection.execute("SELECT COUNT(*) AS n FROM signal_snapshots").fetchone()
        return {
            "markets_by_status": {str(row["status"]): int(row["n"]) for row in markets},
            "resolved_markets": int(resolved["n"]),
            "snapshots": self.snapshot_count(),
            "signal_snapshots": int(signals["n"]),
            "first_snapshot": span["lo"],
            "last_snapshot": span["hi"],
        }

    # -- export --------------------------------------------------------------

    def history_rows(self, *, resolved_only: bool = True) -> Iterator[HistoryRow]:
        """Snapshots joined with results and the latest signal at or before each observation."""
        query = """
            SELECT s.ticker, s.observed_at, s.yes_bid, s.yes_ask, s.no_bid, s.no_ask,
                   s.yes_ask_size, s.no_ask_size, s.close_time, m.result,
                   (
                       SELECT g.fair_probability FROM signal_snapshots g
                       WHERE g.ticker = s.ticker AND g.observed_at <= s.observed_at
                       ORDER BY g.observed_at DESC LIMIT 1
                   ) AS fair_probability,
                   (
                       SELECT g.model_version FROM signal_snapshots g
                       WHERE g.ticker = s.ticker AND g.observed_at <= s.observed_at
                       ORDER BY g.observed_at DESC LIMIT 1
                   ) AS model_version
            FROM market_snapshots s
            JOIN markets m ON m.ticker = s.ticker
        """
        if resolved_only:
            query += " WHERE m.result IS NOT NULL"
        query += " ORDER BY s.observed_at, s.ticker"
        for row in self._connection.execute(query):
            yield HistoryRow(
                ticker=str(row["ticker"]),
                observed_at=parse_utc(str(row["observed_at"])),
                yes_bid=Decimal(row["yes_bid"]),
                yes_ask=Decimal(row["yes_ask"]),
                no_bid=Decimal(row["no_bid"]),
                no_ask=Decimal(row["no_ask"]),
                yes_ask_size=Decimal(row["yes_ask_size"]),
                no_ask_size=Decimal(row["no_ask_size"]),
                close_time=parse_utc(str(row["close_time"])) if row["close_time"] else None,
                result=Outcome(row["result"]) if row["result"] else None,
                fair_probability=(
                    Decimal(row["fair_probability"]) if row["fair_probability"] else None
                ),
                model_version=str(row["model_version"]) if row["model_version"] else None,
            )


def _row_to_market(row: sqlite3.Row) -> MarketRecord:
    return MarketRecord(
        ticker=str(row["ticker"]),
        event_ticker=str(row["event_ticker"]),
        series_ticker=str(row["series_ticker"]),
        title=str(row["title"]),
        status=str(row["status"]),
        close_time=parse_utc(str(row["close_time"])) if row["close_time"] else None,
        result=Outcome(row["result"]) if row["result"] else None,
        settled_at=parse_utc(str(row["settled_at"])) if row["settled_at"] else None,
    )
