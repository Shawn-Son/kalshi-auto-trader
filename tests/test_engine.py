from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from kalshi_trader.api import KalshiClient
from kalshi_trader.broker import PaperBroker
from kalshi_trader.domain import OrderAction, Outcome
from kalshi_trader.engine import TradingEngine
from kalshi_trader.state import StateStore


class FakeMarket:
    def __init__(self, yes_bid: str, no_bid: str) -> None:
        self.yes_bid = yes_bid
        self.no_bid = no_bid

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(
                200,
                json={
                    "orderbook_fp": {
                        "yes_dollars": [[self.yes_bid, "50.00"]],
                        "no_dollars": [[self.no_bid, "50.00"]],
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "market": {
                    "ticker": "TEST",
                    "status": "active",
                    "close_time": "2026-12-31T00:00:00Z",
                }
            },
        )


def _write_signal(path, probability: str) -> None:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    path.write_text(
        f"ticker,fair_probability,generated_at,model_version\nTEST,{probability},{now},t\n"
    )


def _engine(app_config, market: FakeMarket) -> tuple[TradingEngine, StateStore]:
    # One cent of improvement so taker limits clear the paper broker's slippage.
    app_config = replace(
        app_config, strategy=replace(app_config.strategy, price_improvement_cents=1)
    )
    state = StateStore(app_config.runtime.database_path)
    client = KalshiClient(
        "https://example.test/trade-api/v2", transport=httpx.MockTransport(market)
    )
    broker = PaperBroker(client, state, app_config.paper, app_config.fees)
    return TradingEngine(app_config, broker, state), state


@pytest.mark.asyncio
async def test_paper_cycle_places_kelly_sized_buy_then_holds(app_config) -> None:
    # Market 0.49 / 0.51, signal 0.65. Kelly: cost 0.51+0.0175=0.5275,
    # f* = 0.1225/0.4725 = 0.259; quarter Kelly 0.065 capped at 5% of 100.00 =
    # 5.00 -> 9 contracts, then risk caps: 5 per order, 250 notional / 51 = 4.
    _write_signal(app_config.runtime.signal_path, "0.65")
    engine, state = _engine(app_config, FakeMarket("0.4900", "0.4900"))
    await engine.run_cycle()
    intents = state.order_intents()
    assert len(intents) == 1
    assert intents[0].action is OrderAction.BUY
    assert intents[0].outcome is Outcome.YES
    assert intents[0].count == 4
    position = state.paper_position("TEST")
    assert position.outcome is Outcome.YES and position.count == 4
    balance, realized = state.paper_account()
    # 4 filled at ask + 1c slippage = 0.52 -> 208c; fee ceil(0.07*4*0.52*0.48*100) = 7c
    assert balance == 10_000 - 208 - 7
    assert realized == 0

    # Second cycle: already at target (top-up only if target grows) -> no new order.
    await engine.run_cycle()
    assert len(state.order_intents()) == 1
    await engine.broker.close()
    state.close()


@pytest.mark.asyncio
async def test_signal_flip_sells_position_before_reversing(app_config) -> None:
    _write_signal(app_config.runtime.signal_path, "0.65")
    engine, state = _engine(app_config, FakeMarket("0.4900", "0.4900"))
    await engine.run_cycle()
    assert state.paper_position("TEST").count == 4

    # Signal collapses to 0.30 while the YES bid is still 0.49: exit.
    _write_signal(app_config.runtime.signal_path, "0.30")
    engine.state = state  # cooldown does not apply to sells
    await engine.run_cycle()
    intents = state.order_intents()
    assert intents[-1].action is OrderAction.SELL
    assert intents[-1].count == 4
    assert state.paper_position("TEST").is_flat
    _, realized = state.paper_account()
    assert realized < 0  # sold at 0.48 after buying at 0.52

    # Third cycle: flat now, and NO has edge (0.70 fair vs 0.51 ask) -> buy NO.
    await engine.run_cycle()
    last = state.order_intents()[-1]
    assert last.action is OrderAction.BUY and last.outcome is Outcome.NO
    await engine.broker.close()
    state.close()


@pytest.mark.asyncio
async def test_zero_size_skips_without_journal_entry(app_config) -> None:
    # Fair 0.53 vs ask 0.51: gross edge 200bps < min 500bps -> strategy skip; nothing journaled.
    _write_signal(app_config.runtime.signal_path, "0.53")
    engine, state = _engine(app_config, FakeMarket("0.4900", "0.4900"))
    await engine.run_cycle()
    assert state.order_intents() == []
    assert engine.strategy.evaluate  # smoke: engine constructed with fees
    await engine.broker.close()
    state.close()
