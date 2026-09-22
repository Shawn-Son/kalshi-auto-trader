from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from decimal import Decimal

from kalshi_trader.api import KalshiAPIError
from kalshi_trader.broker import Broker
from kalshi_trader.config import AppConfig
from kalshi_trader.domain import (
    MarketQuote,
    OrderResult,
    OrderStatus,
    Outcome,
    PortfolioSnapshot,
    Signal,
)
from kalshi_trader.risk import RiskEngine
from kalshi_trader.signals import SignalError, load_signals
from kalshi_trader.sizing import cap_by_risk, size_position
from kalshi_trader.state import StateStore
from kalshi_trader.strategy import ProbabilityMispricingStrategy

logger = logging.getLogger("kalshi_trader.engine")


class TradingEngine:
    def __init__(
        self,
        config: AppConfig,
        broker: Broker,
        state: StateStore,
    ) -> None:
        self.config = config
        self.broker = broker
        self.state = state
        self.strategy = ProbabilityMispricingStrategy(config.strategy, config.fees)
        self.risk = RiskEngine(config.risk, config.strategy)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, *, once: bool = False) -> None:
        try:
            while not self._stop.is_set():
                await self.run_cycle()
                if once:
                    break
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.config.runtime.poll_interval_seconds
                    )
        finally:
            if self.config.runtime.cancel_open_orders_on_shutdown:
                await self.cancel_managed_orders()
            await self.broker.close()

    async def run_cycle(self) -> None:
        try:
            signals = load_signals(self.config.runtime.signal_path)
        except SignalError:
            logger.exception("signal file rejected; cycle failed closed")
            return
        configured = self.config.universe.tickers
        tickers = configured or tuple(signals)
        if not tickers:
            logger.warning("no configured tickers or signals")
            return
        if self.state.kill_switch_active():
            logger.warning("kill switch active; skipping cycle")
            return
        for ticker in tickers:
            signal = signals.get(ticker)
            if signal is None:
                logger.warning("ticker has no signal", extra={"ticker": ticker})
                continue
            await self._process_ticker(ticker, signal)

    async def _process_ticker(self, ticker: str, signal: Signal) -> None:
        started = time.perf_counter()
        try:
            quote = await self.broker.quote(ticker)
            await self.broker.reconcile(ticker, quote)
            position = await self.broker.position(ticker)
            portfolio = await self.broker.snapshot(ticker)
            decision = self.strategy.evaluate(
                signal,
                quote,
                count=self.config.risk.max_contracts_per_order,
                position=position,
                max_order_count=self.config.risk.max_contracts_per_order,
                sizer=self._sizer(signal, quote, portfolio),
            )
            if decision.intent is None:
                logger.info(
                    "no trade",
                    extra={"ticker": ticker, "decision": "skip", "reason": decision.reason},
                )
                return
            intent = decision.intent
            last_order_at = self.state.last_order_at(ticker)
            self.state.record_intent(intent)
            risk = self.risk.evaluate(
                intent,
                quote,
                portfolio,
                last_order_at=last_order_at,
                kill_switch=self.state.kill_switch_active(),
            )
            if not risk.approved:
                self.state.reject_intent(intent.client_order_id, f"{risk.code}: {risk.reason}")
                logger.warning(
                    "risk rejected order",
                    extra={
                        "ticker": ticker,
                        "client_order_id": intent.client_order_id,
                        "decision": risk.code,
                        "reason": risk.reason,
                    },
                )
                return
            try:
                result = await self.broker.place(intent, quote)
            except Exception as exc:
                unknown = OrderResult(
                    order_id="",
                    client_order_id=intent.client_order_id,
                    status=OrderStatus.UNKNOWN,
                    filled_count=Decimal("0"),
                    remaining_count=Decimal(intent.count),
                )
                self.state.record_result(unknown, error=str(exc))
                raise
            self.state.record_result(result)
            logger.info(
                "order submitted",
                extra={
                    "ticker": ticker,
                    "client_order_id": intent.client_order_id,
                    "order_id": result.order_id,
                    "action": intent.action.value,
                    "decision": result.status.value,
                    "reason": decision.reason,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        except (KalshiAPIError, ValueError):
            logger.exception(
                "ticker processing failed closed",
                extra={
                    "ticker": ticker,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )

    def _sizer(
        self, signal: Signal, quote: MarketQuote, portfolio: PortfolioSnapshot
    ) -> Callable[[Outcome, Decimal], int]:
        """Target contracts for a side at a price: fractional Kelly, then risk caps."""
        risk = self.config.risk
        fee_rate = self.config.fees.rate_for(self.config.strategy.order_style)

        def target(outcome: Outcome, price: Decimal) -> int:
            fair = (
                signal.fair_probability if outcome is Outcome.YES else 1 - signal.fair_probability
            )
            decision = size_position(
                fair=fair,
                price=price,
                fee_rate=fee_rate,
                bankroll_cents=portfolio.balance_cents,
                config=self.config.sizing,
                seconds_to_close=quote.seconds_to_close(),
            )
            capped = cap_by_risk(
                decision.contracts,
                price=price,
                max_contracts_per_order=risk.max_contracts_per_order,
                max_order_notional_cents=risk.max_order_notional_cents,
                exposure_room_cents=risk.max_market_exposure_cents
                - portfolio.market_exposure_cents,
            )
            logger.debug(
                "sizing",
                extra={
                    "ticker": quote.ticker,
                    "decision": outcome.value,
                    "reason": decision.reason,
                    "kelly_contracts": decision.contracts,
                    "capped_contracts": capped,
                    "annualized_return": f"{decision.annualized_return:.3f}",
                },
            )
            return capped

        return target

    async def cancel_managed_orders(self) -> None:
        for order_id in self.state.resting_order_ids():
            try:
                await self.broker.cancel(order_id)
            except Exception:
                logger.exception("failed to cancel managed order", extra={"order_id": order_id})
            else:
                self.state.mark_canceled(order_id)
                logger.info("canceled managed order", extra={"order_id": order_id})
