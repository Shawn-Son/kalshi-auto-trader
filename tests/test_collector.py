from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from kalshi_trader.api import KalshiClient
from kalshi_trader.collector import Collector, export_history
from kalshi_trader.domain import Outcome
from kalshi_trader.history import HistoryStore


def _market(ticker: str, status: str, result: str = "") -> dict[str, object]:
    return {
        "ticker": ticker,
        "event_ticker": "KXTEST-26SEP23",
        "title": ticker,
        "status": status,
        "result": result,
        "close_time": "2026-09-24T05:00:00Z",
        "settlement_ts": "2026-09-24T06:00:00Z" if result else None,
    }


class FakeExchange:
    """Two markets: A stays open, B settles YES on the second cycle."""

    def __init__(self) -> None:
        self.cycle = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/markets"):
            markets = [_market("A", "active")]
            if self.cycle == 0:
                markets.append(_market("B", "active"))
            return httpx.Response(200, json={"markets": markets, "cursor": ""})
        if path.endswith("/orderbook"):
            return httpx.Response(
                200,
                json={
                    "orderbook_fp": {
                        "yes_dollars": [["0.4000", "5.00"], ["0.4400", "12.00"]],
                        "no_dollars": [["0.5000", "3.00"], ["0.5300", "9.00"]],
                    }
                },
            )
        if path.endswith("/markets/B"):
            return httpx.Response(200, json={"market": _market("B", "finalized", "yes")})
        if path.endswith("/markets/A"):
            return httpx.Response(200, json={"market": _market("A", "active")})
        return httpx.Response(404, json={"message": "not found"})


@pytest.mark.asyncio
async def test_collector_snapshots_records_signals_and_resolves(app_config, tmp_path) -> None:
    exchange = FakeExchange()
    client = KalshiClient(
        "https://example.test/trade-api/v2", transport=httpx.MockTransport(exchange)
    )
    store = HistoryStore(tmp_path / "history.db")
    signal_path = tmp_path / "signals.csv"
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    signal_path.write_text(
        "ticker,fair_probability,generated_at,model_version\n"
        f"B,0.7000,{now},test-v1\n"
        f"ZZZ,0.5000,{now},test-v1\n"
    )
    from dataclasses import replace

    collector = Collector(
        client,
        store,
        replace(app_config.collector, series=("KXTEST",)),
        signal_path=signal_path,
    )
    first = await collector.run_cycle()
    assert first == {"discovered": 2, "snapshots": 2, "signals": 1, "resolved": 0}
    assert store.market("B") is not None and store.market("B").result is None

    exchange.cycle = 1
    second = await collector.run_cycle()
    assert second["discovered"] == 1
    assert second["resolved"] == 1
    resolved = store.market("B")
    assert resolved is not None
    assert resolved.result is Outcome.YES
    assert resolved.status == "finalized"
    assert resolved.settled_at is not None
    assert store.unresolved_tickers() == ["A"]
    await client.close()

    output = tmp_path / "history.csv"
    rows = export_history(store, output, require_signal=True)
    assert rows == 1
    with output.open() as handle:
        exported = list(csv.DictReader(handle))
    assert exported[0]["ticker"] == "B"
    assert exported[0]["fair_probability"] == "0.7000"
    assert exported[0]["yes_ask"] == "0.4700"  # 1 - best NO bid 0.53
    store.close()


def test_history_store_upsert_keeps_first_result(tmp_path: Path) -> None:
    from kalshi_trader.history import MarketRecord

    store = HistoryStore(tmp_path / "h.db")
    seen = datetime(2026, 9, 22, tzinfo=UTC)
    store.upsert_market(
        MarketRecord("T", "E", "S", "title", "active", None, None, None), seen_at=seen
    )
    store.upsert_market(
        MarketRecord("T", "E", "S", "title", "finalized", None, Outcome.NO, seen), seen_at=seen
    )
    store.upsert_market(
        MarketRecord("T", "E", "S", "title", "finalized", None, None, None), seen_at=seen
    )
    record = store.market("T")
    assert record is not None and record.result is Outcome.NO
    assert store.summary()["resolved_markets"] == 1
    store.close()


def test_export_without_signal_leaves_probability_blank(tmp_path: Path) -> None:
    from kalshi_trader.domain import MarketQuote
    from kalshi_trader.history import MarketRecord

    store = HistoryStore(tmp_path / "h.db")
    seen = datetime(2026, 9, 22, tzinfo=UTC)
    store.upsert_market(
        MarketRecord("T", "E", "S", "title", "finalized", None, Outcome.NO, seen), seen_at=seen
    )
    store.record_snapshot(
        MarketQuote(
            ticker="T",
            yes_bid=Decimal("0.40"),
            yes_ask=Decimal("0.45"),
            no_bid=Decimal("0.55"),
            no_ask=Decimal("0.60"),
            yes_bid_size=Decimal("1"),
            yes_ask_size=Decimal("2"),
            no_bid_size=Decimal("3"),
            no_ask_size=Decimal("4"),
            observed_at=seen,
            status="active",
        )
    )
    out = tmp_path / "out.csv"
    assert export_history(store, out) == 1
    assert export_history(store, out, require_signal=True) == 0
    with out.open() as handle:
        assert list(csv.DictReader(handle)) == []
    store.close()
