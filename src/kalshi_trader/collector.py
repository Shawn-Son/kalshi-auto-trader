from __future__ import annotations

import asyncio
import csv
import logging
import os
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_trader.api import KalshiAPIError, KalshiClient, quote_from_book
from kalshi_trader.config import CollectorConfig
from kalshi_trader.domain import TRADABLE_STATUSES, Outcome, parse_utc
from kalshi_trader.history import HistoryRow, HistoryStore, MarketRecord
from kalshi_trader.signals import SignalError, load_signals

logger = logging.getLogger("kalshi_trader.collector")

RESOLVED_STATUSES = frozenset({"settled", "finalized"})

EXPORT_COLUMNS = (
    "ticker",
    "observed_at",
    "fair_probability",
    "model_version",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "yes_ask_size",
    "no_ask_size",
    "close_time",
    "result",
)


class Collector:
    """Polls public market data into the history store.

    Every cycle it (1) discovers open markets in the configured series and universe,
    (2) snapshots each one's top of book and ladder, (3) records the current signal
    file so live predictions can be scored later, and (4) re-checks previously seen
    markets that have not resolved yet and records their settlement result.
    """

    def __init__(
        self,
        client: KalshiClient,
        store: HistoryStore,
        config: CollectorConfig,
        *,
        tickers: tuple[str, ...] = (),
        signal_path: Path | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.config = config
        self.tickers = tickers
        self.signal_path = signal_path
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(config.concurrency)

    def stop(self) -> None:
        self._stop.set()

    async def run(self, *, once: bool = False) -> None:
        try:
            while not self._stop.is_set():
                started = datetime.now(UTC)
                try:
                    await self.run_cycle()
                except KalshiAPIError:
                    logger.exception("collector cycle failed; will retry next interval")
                if once:
                    break
                elapsed = (datetime.now(UTC) - started).total_seconds()
                delay = max(1.0, self.config.interval_seconds - elapsed)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
        finally:
            await self.client.close()

    async def run_cycle(self) -> dict[str, int]:
        now = datetime.now(UTC)
        discovered = await self._discover()
        snapshots = 0
        for market in discovered:
            if await self._snapshot(market, now):
                snapshots += 1
        signals = self._record_signals(now, {str(m.get("ticker")) for m in discovered})
        resolved = await self._resolve_pending(now, {str(m.get("ticker")) for m in discovered})
        stats = {
            "discovered": len(discovered),
            "snapshots": snapshots,
            "signals": signals,
            "resolved": resolved,
        }
        logger.info("collector cycle complete", extra=stats)
        return stats

    async def _discover(self) -> list[dict[str, Any]]:
        markets: dict[str, dict[str, Any]] = {}
        for series in self.config.series:
            try:
                page = await self.client.list_markets(
                    series_ticker=series, status="open", max_markets=self.config.max_markets
                )
            except KalshiAPIError:
                logger.exception("series discovery failed", extra={"series": series})
                continue
            for market in page:
                markets[str(market.get("ticker"))] = market
        for ticker in self.tickers:
            if ticker in markets:
                continue
            try:
                markets[ticker] = await self.client.get_market(ticker)
            except KalshiAPIError:
                logger.exception("ticker lookup failed", extra={"ticker": ticker})
        return list(markets.values())[: self.config.max_markets]

    async def _snapshot(self, market: dict[str, Any], now: datetime) -> bool:
        ticker = str(market.get("ticker", ""))
        if not ticker:
            return False
        self.store.upsert_market(_market_record(market), seen_at=now)
        if str(market.get("status")) not in TRADABLE_STATUSES:
            return False
        async with self._semaphore:
            try:
                book = await self.client.get_orderbook(ticker, depth=self.config.orderbook_depth)
            except KalshiAPIError:
                logger.exception("orderbook fetch failed", extra={"ticker": ticker})
                return False
        if not book.two_sided:
            return False
        quote = quote_from_book(ticker, market, book)
        self.store.record_snapshot(quote, book={"yes_bids": book.yes_bids, "no_bids": book.no_bids})
        return True

    def _record_signals(self, now: datetime, tickers: set[str]) -> int:
        if self.signal_path is None or not self.signal_path.exists():
            return 0
        try:
            signals = load_signals(self.signal_path, now=now)
        except SignalError:
            logger.exception("signal file rejected; not recorded")
            return 0
        recorded = 0
        for ticker, signal in signals.items():
            if tickers and ticker not in tickers:
                continue
            self.store.record_signal(signal, observed_at=now)
            recorded += 1
        return recorded

    async def _resolve_pending(self, now: datetime, still_open: set[str]) -> int:
        resolved = 0
        for ticker in self.store.unresolved_tickers():
            if ticker in still_open:
                continue
            async with self._semaphore:
                try:
                    market = await self.client.get_market(ticker)
                except KalshiAPIError:
                    logger.exception("settlement lookup failed", extra={"ticker": ticker})
                    continue
            record = _market_record(market)
            self.store.upsert_market(record, seen_at=now)
            if record.result is not None:
                resolved += 1
        return resolved


def _market_record(market: dict[str, Any]) -> MarketRecord:
    ticker = str(market.get("ticker", ""))
    event_ticker = str(market.get("event_ticker", ""))
    series_ticker = str(market.get("series_ticker") or event_ticker.rsplit("-", 1)[0])
    status = str(market.get("status", "unknown"))
    result_raw = str(market.get("result") or "").strip().lower()
    result = Outcome(result_raw) if result_raw in {"yes", "no"} else None
    close_raw = market.get("close_time")
    settled_raw = market.get("settlement_ts")
    return MarketRecord(
        ticker=ticker,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=str(market.get("title", "")),
        status=status,
        close_time=parse_utc(str(close_raw)) if isinstance(close_raw, str) and close_raw else None,
        result=result if result is not None or status in RESOLVED_STATUSES else None,
        settled_at=(
            parse_utc(str(settled_raw))
            if isinstance(settled_raw, str) and settled_raw and result is not None
            else None
        ),
    )


def export_history(
    store: HistoryStore,
    output: Path,
    *,
    require_signal: bool = False,
    resolved_only: bool = True,
) -> int:
    """Write the backtest CSV atomically. Returns the number of rows written."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    written = 0
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in store.history_rows(resolved_only=resolved_only):
            if require_signal and row.fair_probability is None:
                continue
            writer.writerow(_export_row(row))
            written += 1
    os.replace(tmp, output)
    return written


def _export_row(row: HistoryRow) -> dict[str, str]:
    return {
        "ticker": row.ticker,
        "observed_at": row.observed_at.isoformat().replace("+00:00", "Z"),
        "fair_probability": str(row.fair_probability) if row.fair_probability is not None else "",
        "model_version": row.model_version or "",
        "yes_bid": str(row.yes_bid),
        "yes_ask": str(row.yes_ask),
        "no_bid": str(row.no_bid),
        "no_ask": str(row.no_ask),
        "yes_ask_size": str(row.yes_ask_size),
        "no_ask_size": str(row.no_ask_size),
        "close_time": row.close_time.isoformat().replace("+00:00", "Z") if row.close_time else "",
        "result": row.result.value if row.result else "",
    }
