from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from kalshi_trader.config import StrategyConfig
from kalshi_trader.domain import CENT, ONE, MarketQuote, OrderIntent, Outcome, Signal


@dataclass(frozen=True)
class StrategyDecision:
    intent: OrderIntent | None
    reason: str


class ProbabilityMispricingStrategy:
    """Turns an independently produced fair probability into a limit-order intent."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def evaluate(
        self,
        signal: Signal,
        quote: MarketQuote,
        *,
        count: int,
    ) -> StrategyDecision:
        if signal.ticker != quote.ticker:
            return StrategyDecision(None, "signal/quote ticker mismatch")
        if quote.status != "open":
            return StrategyDecision(None, f"market status is {quote.status}")
        if signal.age_seconds(quote.observed_at) > self.config.max_signal_age_seconds:
            return StrategyDecision(None, "signal is stale")
        if signal.age_seconds(quote.observed_at) < -5:
            return StrategyDecision(None, "signal timestamp is in the future")
        probability = signal.fair_probability
        if (
            not Decimal(str(self.config.min_probability))
            <= probability
            <= Decimal(str(self.config.max_probability))
        ):
            return StrategyDecision(None, "probability outside configured confidence bounds")

        candidates = (
            (Outcome.YES, probability - quote.yes_ask),
            (Outcome.NO, (ONE - probability) - quote.no_ask),
        )
        outcome, edge = max(candidates, key=lambda item: item[1])
        edge_bps = int(edge * 10_000)
        if edge_bps < self.config.min_edge_bps:
            return StrategyDecision(None, f"edge {edge_bps}bps below threshold")

        improvement = CENT * self.config.price_improvement_cents
        limit_price = min(Decimal("0.99"), quote.ask(outcome) + improvement)
        intent = OrderIntent(
            client_order_id=str(uuid.uuid4()),
            ticker=signal.ticker,
            outcome=outcome,
            count=count,
            limit_price=limit_price,
            fair_probability=probability,
            signal_generated_at=signal.generated_at,
            model_version=signal.model_version,
        )
        return StrategyDecision(intent, f"{outcome.value} edge {intent.edge_bps}bps")
