from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from kalshi_trader.api import KalshiClient
from kalshi_trader.config import PaperConfig
from kalshi_trader.domain import (
    CENT,
    MarketQuote,
    OrderIntent,
    OrderResult,
    OrderStatus,
    PortfolioSnapshot,
)
from kalshi_trader.state import StateError, StateStore


class Broker(Protocol):
    async def quote(self, ticker: str) -> MarketQuote: ...

    async def snapshot(self, ticker: str) -> PortfolioSnapshot: ...

    async def place(self, intent: OrderIntent, quote: MarketQuote) -> OrderResult: ...

    async def cancel(self, order_id: str) -> None: ...

    async def close(self) -> None: ...


class PaperBroker:
    def __init__(self, market_data: KalshiClient, state: StateStore, config: PaperConfig) -> None:
        self.market_data = market_data
        self.state = state
        self.config = config
        self.state.initialize_paper(config.starting_balance_cents)

    async def quote(self, ticker: str) -> MarketQuote:
        return await self.market_data.get_quote(ticker)

    async def snapshot(self, ticker: str) -> PortfolioSnapshot:
        balance, realized_pnl = self.state.paper_account()
        return PortfolioSnapshot(
            balance_cents=balance,
            total_exposure_cents=self.state.paper_exposure(),
            market_exposure_cents=self.state.paper_exposure(ticker),
            open_orders=self.state.open_order_count(),
            daily_pnl_cents=realized_pnl,
            observed_at=datetime.now(UTC),
        )

    async def place(self, intent: OrderIntent, quote: MarketQuote) -> OrderResult:
        simulated_price = min(
            Decimal("0.99"),
            quote.ask(intent.outcome) + CENT * self.config.slippage_cents,
        )
        if intent.limit_price < simulated_price:
            return OrderResult(
                order_id=f"paper-{intent.client_order_id}",
                client_order_id=intent.client_order_id,
                status=OrderStatus.RESTING,
                filled_count=Decimal("0"),
                remaining_count=Decimal(intent.count),
            )
        raw_fee = simulated_price * intent.count * Decimal(self.config.fee_bps) / 10_000
        fee_cents = int((raw_fee * 100).quantize(Decimal("1"), rounding=ROUND_CEILING))
        try:
            self.state.apply_paper_fill(intent, fill_price=simulated_price, fee_cents=fee_cents)
        except StateError:
            return OrderResult(
                order_id=f"paper-{intent.client_order_id}",
                client_order_id=intent.client_order_id,
                status=OrderStatus.REJECTED,
                filled_count=Decimal("0"),
                remaining_count=Decimal(intent.count),
            )
        return OrderResult(
            order_id=f"paper-{intent.client_order_id}",
            client_order_id=intent.client_order_id,
            status=OrderStatus.FILLED,
            filled_count=Decimal(intent.count),
            remaining_count=Decimal("0"),
            average_fill_price=simulated_price,
            fee_dollars=Decimal(fee_cents) / 100,
        )

    async def cancel(self, order_id: str) -> None:
        return None

    async def close(self) -> None:
        await self.market_data.close()


class KalshiBroker:
    def __init__(self, client: KalshiClient, state: StateStore) -> None:
        self.client = client
        self.state = state

    async def quote(self, ticker: str) -> MarketQuote:
        return await self.client.get_quote(ticker)

    async def snapshot(self, ticker: str) -> PortfolioSnapshot:
        return await self.client.portfolio_snapshot(ticker, self.state)

    async def place(self, intent: OrderIntent, quote: MarketQuote) -> OrderResult:
        return await self.client.place_order(intent)

    async def cancel(self, order_id: str) -> None:
        await self.client.cancel_order(order_id)

    async def close(self) -> None:
        await self.client.close()
