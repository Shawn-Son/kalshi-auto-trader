from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from kalshi_trader.config import FeeConfig, StrategyConfig
from kalshi_trader.domain import (
    CENT,
    ONE,
    MarketQuote,
    OrderAction,
    OrderIntent,
    OrderStyle,
    Outcome,
    Position,
    Signal,
)
from kalshi_trader.fees import fee_per_contract

MAX_PRICE = Decimal("0.99")
MIN_PRICE = CENT


@dataclass(frozen=True)
class StrategyDecision:
    intent: OrderIntent | None
    reason: str


class ProbabilityMispricingStrategy:
    """Turns an independently produced fair probability into an order intent.

    Decision order for one market:
    1. Exit: if we hold a side whose best bid now exceeds fair value by more than
       `exit_edge_bps` after fees, sell the whole position. Nothing else happens this
       cycle, so a flip always closes before it opens the other way.
    2. Entry: pick the side whose price (taker: ask, maker: bid + improvement) sits
       below fair value by at least `min_edge_bps` after fees. Only top up to the
       target count; never add to a side we already hold at target.
    """

    def __init__(self, config: StrategyConfig, fees: FeeConfig) -> None:
        self.config = config
        self.fees = fees

    def evaluate(
        self,
        signal: Signal,
        quote: MarketQuote,
        *,
        count: int,
        position: Position | None = None,
        max_order_count: int | None = None,
    ) -> StrategyDecision:
        held = position or Position.flat(quote.ticker)
        if signal.ticker != quote.ticker:
            return StrategyDecision(None, "signal/quote ticker mismatch")
        if not quote.is_open:
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

        exit_decision = self._exit(signal, quote, held, max_order_count)
        if exit_decision is not None:
            return exit_decision
        return self._entry(signal, quote, count=count, held=held)

    # -- exits ---------------------------------------------------------------

    def _exit(
        self,
        signal: Signal,
        quote: MarketQuote,
        held: Position,
        max_order_count: int | None,
    ) -> StrategyDecision | None:
        if held.is_flat or held.outcome is None:
            return None
        outcome = held.outcome
        fair = self._fair(signal.fair_probability, outcome)
        sell_price = self._sell_price(quote, outcome)
        if sell_price is None:
            return None
        rate = self.fees.rate_for(self.config.order_style)
        net_edge = sell_price - fee_per_contract(sell_price, rate) - fair
        net_edge_bps = int(net_edge * 10_000)
        if net_edge_bps < self.config.exit_edge_bps:
            return None
        sell_count = held.count if max_order_count is None else min(held.count, max_order_count)
        intent = self._intent(signal, outcome, sell_count, sell_price, action=OrderAction.SELL)
        return StrategyDecision(
            intent, f"exit {outcome.value} x{sell_count}: bid exceeds fair by {net_edge_bps}bps"
        )

    # -- entries -------------------------------------------------------------

    def _entry(
        self, signal: Signal, quote: MarketQuote, *, count: int, held: Position
    ) -> StrategyDecision:
        rate = self.fees.rate_for(self.config.order_style)
        best: tuple[Outcome, Decimal, int] | None = None
        for outcome in (Outcome.YES, Outcome.NO):
            price = self._buy_price(quote, outcome)
            if price is None:
                continue
            net_edge = self._fair(signal.fair_probability, outcome) - price
            net_edge -= fee_per_contract(price, rate)
            net_edge_bps = int(net_edge * 10_000)
            if best is None or net_edge_bps > best[2]:
                best = (outcome, price, net_edge_bps)
        if best is None:
            return StrategyDecision(None, "no valid entry price on either side")
        outcome, price, net_edge_bps = best
        if net_edge_bps < self.config.min_edge_bps:
            return StrategyDecision(None, f"edge {net_edge_bps}bps below threshold after fees")
        opposite = Outcome.NO if outcome is Outcome.YES else Outcome.YES
        if held.held(opposite) > 0:
            return StrategyDecision(
                None, f"holding {opposite.value}; will not open {outcome.value} until exited"
            )
        remaining = count - held.held(outcome)
        if remaining <= 0:
            return StrategyDecision(
                None, f"already holding {held.held(outcome)} {outcome.value} (target {count})"
            )
        intent = self._intent(signal, outcome, remaining, price, action=OrderAction.BUY)
        return StrategyDecision(
            intent, f"{outcome.value} edge {net_edge_bps}bps after fees ({intent.style.value})"
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _fair(probability: Decimal, outcome: Outcome) -> Decimal:
        return probability if outcome is Outcome.YES else ONE - probability

    def _buy_price(self, quote: MarketQuote, outcome: Outcome) -> Decimal | None:
        improvement = CENT * self.config.price_improvement_cents
        if self.config.order_style is OrderStyle.MAKER:
            # Join or improve the bid, but stay strictly inside the spread.
            price = min(quote.bid(outcome) + improvement, quote.ask(outcome) - CENT)
        else:
            price = quote.ask(outcome) + improvement
        price = min(MAX_PRICE, price)
        return price if price >= MIN_PRICE else None

    def _sell_price(self, quote: MarketQuote, outcome: Outcome) -> Decimal | None:
        improvement = CENT * self.config.price_improvement_cents
        if self.config.order_style is OrderStyle.MAKER:
            price = max(quote.ask(outcome) - improvement, quote.bid(outcome) + CENT)
        else:
            price = quote.bid(outcome) - improvement
        price = max(MIN_PRICE, price)
        return price if price <= MAX_PRICE else None

    def _intent(
        self,
        signal: Signal,
        outcome: Outcome,
        count: int,
        price: Decimal,
        *,
        action: OrderAction,
    ) -> OrderIntent:
        return OrderIntent(
            client_order_id=str(uuid.uuid4()),
            ticker=signal.ticker,
            outcome=outcome,
            count=count,
            limit_price=price,
            fair_probability=signal.fair_probability,
            signal_generated_at=signal.generated_at,
            model_version=signal.model_version,
            action=action,
            style=self.config.order_style,
        )
