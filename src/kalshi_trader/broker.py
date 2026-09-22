from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from kalshi_trader.api import KalshiClient
from kalshi_trader.config import FeeConfig, PaperConfig
from kalshi_trader.domain import (
    CENT,
    MarketQuote,
    OrderIntent,
    OrderResult,
    OrderStatus,
    OrderStyle,
    PortfolioSnapshot,
    Position,
)
from kalshi_trader.fees import order_fee_cents
from kalshi_trader.state import StateError, StateStore

logger = logging.getLogger("kalshi_trader.broker")


class Broker(Protocol):
    async def quote(self, ticker: str) -> MarketQuote: ...

    async def snapshot(self, ticker: str) -> PortfolioSnapshot: ...

    async def position(self, ticker: str) -> Position: ...

    async def place(self, intent: OrderIntent, quote: MarketQuote) -> OrderResult: ...

    async def reconcile(self, ticker: str, quote: MarketQuote) -> list[OrderResult]: ...

    async def cancel(self, order_id: str) -> None: ...

    async def close(self) -> None: ...


class PaperBroker:
    """Local simulation against real public quotes.

    Fill model, deliberately conservative:
    - taker buys fill immediately at ask + slippage, taker sells at bid - slippage;
    - maker orders rest, and fill on a later cycle only once the far side of the book
      trades through the limit (ask <= limit for buys, bid >= limit for sells);
    - queue position is not modeled, so maker fill rates are optimistic.
    """

    def __init__(
        self,
        market_data: KalshiClient,
        state: StateStore,
        config: PaperConfig,
        fees: FeeConfig,
    ) -> None:
        self.market_data = market_data
        self.state = state
        self.config = config
        self.fees = fees
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

    async def position(self, ticker: str) -> Position:
        return self.state.paper_position(ticker)

    async def place(self, intent: OrderIntent, quote: MarketQuote) -> OrderResult:
        if intent.style is OrderStyle.MAKER:
            crosses = (
                quote.ask(intent.outcome) <= intent.limit_price
                if intent.is_buy
                else quote.bid(intent.outcome) >= intent.limit_price
            )
            if crosses:
                return self._result(intent, OrderStatus.REJECTED)
            return self._result(intent, OrderStatus.RESTING)
        slip = CENT * self.config.slippage_cents
        if intent.is_buy:
            fill_price = min(Decimal("0.99"), quote.ask(intent.outcome) + slip)
            if intent.limit_price < fill_price:
                return self._result(intent, OrderStatus.RESTING)
        else:
            fill_price = max(CENT, quote.bid(intent.outcome) - slip)
            if intent.limit_price > fill_price:
                return self._result(intent, OrderStatus.RESTING)
        return self._fill(intent, fill_price)

    async def reconcile(self, ticker: str, quote: MarketQuote) -> list[OrderResult]:
        """Fill resting paper orders that the market has since traded through."""
        results: list[OrderResult] = []
        for intent in self.state.resting_intents(ticker):
            touched = (
                quote.ask(intent.outcome) <= intent.limit_price
                if intent.is_buy
                else quote.bid(intent.outcome) >= intent.limit_price
            )
            if not touched:
                continue
            result = self._fill(intent, intent.limit_price)
            self.state.record_result(result)
            logger.info(
                "paper resting order filled",
                extra={
                    "ticker": ticker,
                    "client_order_id": intent.client_order_id,
                    "decision": result.status.value,
                },
            )
            results.append(result)
        return results

    def _fill(self, intent: OrderIntent, fill_price: Decimal) -> OrderResult:
        fee_cents = order_fee_cents(intent.count, fill_price, self.fees.rate_for(intent.style))
        try:
            if intent.is_buy:
                self.state.apply_paper_fill(intent, fill_price=fill_price, fee_cents=fee_cents)
            else:
                self.state.apply_paper_sell(intent, fill_price=fill_price, fee_cents=fee_cents)
        except StateError:
            return self._result(intent, OrderStatus.REJECTED)
        return OrderResult(
            order_id=f"paper-{intent.client_order_id}",
            client_order_id=intent.client_order_id,
            status=OrderStatus.FILLED,
            filled_count=Decimal(intent.count),
            remaining_count=Decimal("0"),
            average_fill_price=fill_price,
            fee_dollars=Decimal(fee_cents) / 100,
        )

    @staticmethod
    def _result(intent: OrderIntent, status: OrderStatus) -> OrderResult:
        return OrderResult(
            order_id=f"paper-{intent.client_order_id}",
            client_order_id=intent.client_order_id,
            status=status,
            filled_count=Decimal("0"),
            remaining_count=Decimal(intent.count),
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

    async def position(self, ticker: str) -> Position:
        return await self.client.position(ticker)

    async def place(self, intent: OrderIntent, quote: MarketQuote) -> OrderResult:
        return await self.client.place_order(intent)

    async def reconcile(self, ticker: str, quote: MarketQuote) -> list[OrderResult]:
        """Refresh the local status of resting orders from the exchange."""
        results: list[OrderResult] = []
        for intent in self.state.resting_intents(ticker):
            found = await self.client.find_order(ticker, intent.client_order_id)
            if found is None or found.status is OrderStatus.RESTING:
                continue
            self.state.record_result(found)
            results.append(found)
        return results

    async def cancel(self, order_id: str) -> None:
        await self.client.cancel_order(order_id)

    async def close(self) -> None:
        await self.client.close()
