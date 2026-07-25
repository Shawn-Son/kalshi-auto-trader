from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from kalshi_trader.config import RiskConfig, StrategyConfig
from kalshi_trader.domain import MarketQuote, OrderIntent, PortfolioSnapshot
from kalshi_trader.fastcore import FastRiskCore, NumericRisk


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    code: str
    reason: str


class RiskEngine:
    def __init__(
        self,
        risk: RiskConfig,
        strategy: StrategyConfig,
        fastcore: FastRiskCore | None = None,
    ) -> None:
        self.risk = risk
        self.strategy = strategy
        self.fastcore = fastcore or FastRiskCore()

    def evaluate(
        self,
        intent: OrderIntent,
        quote: MarketQuote,
        portfolio: PortfolioSnapshot,
        *,
        last_order_at: datetime | None,
        kill_switch: bool,
        now: datetime | None = None,
    ) -> RiskDecision:
        current = now or datetime.now(UTC)
        numeric_code = self.fastcore.validate(
            NumericRisk(
                count=intent.count,
                order_notional_cents=intent.max_loss_cents,
                market_exposure_cents=portfolio.market_exposure_cents,
                total_exposure_cents=portfolio.total_exposure_cents,
                open_orders=portfolio.open_orders,
                balance_cents=portfolio.balance_cents,
                max_count=self.risk.max_contracts_per_order,
                max_order_notional_cents=self.risk.max_order_notional_cents,
                max_market_exposure_cents=self.risk.max_market_exposure_cents,
                max_total_exposure_cents=self.risk.max_total_exposure_cents,
                max_open_orders=self.risk.max_open_orders,
                min_balance_cents=self.risk.min_balance_cents,
            )
        )
        numeric_failures = {
            1: ("ORDER_COUNT", "contract count exceeds per-order limit"),
            2: ("ORDER_NOTIONAL", "max order loss exceeds per-order limit"),
            3: ("MARKET_EXPOSURE", "market exposure limit would be exceeded"),
            4: ("TOTAL_EXPOSURE", "total exposure limit would be exceeded"),
            5: ("OPEN_ORDERS", "open-order limit reached"),
            6: ("BALANCE", "available balance below floor"),
        }
        if numeric_code != 0:
            code, reason = numeric_failures.get(
                numeric_code, ("FASTCORE_ERROR", "native risk core returned an unknown result")
            )
            return RiskDecision(False, code, reason)
        checks: tuple[tuple[bool, str, str], ...] = (
            (not kill_switch, "KILL_SWITCH", "kill switch is active"),
            (quote.status == "open", "MARKET_CLOSED", f"market status is {quote.status}"),
            (
                portfolio.daily_pnl_cents > -self.risk.max_daily_loss_cents,
                "DAILY_LOSS",
                "daily loss limit reached",
            ),
            (
                quote.spread_cents(intent.outcome) <= self.risk.max_spread_cents,
                "SPREAD",
                "spread exceeds configured limit",
            ),
            (
                quote.ask_size(intent.outcome) >= self.risk.min_top_level_contracts,
                "LIQUIDITY",
                "insufficient top-level liquidity",
            ),
            (
                intent.edge_bps >= self.strategy.min_edge_bps,
                "EDGE",
                "edge fell below threshold",
            ),
            (
                intent.limit_price > 0 and intent.limit_price < 1,
                "PRICE",
                "limit price must be strictly between zero and one",
            ),
        )
        for passed, code, reason in checks:
            if not passed:
                return RiskDecision(False, code, reason)
        if last_order_at is not None:
            elapsed = (current - last_order_at).total_seconds()
            if elapsed < self.risk.cooldown_seconds:
                return RiskDecision(False, "COOLDOWN", "market cooldown is active")
        return RiskDecision(True, "APPROVED", "all pre-trade checks passed")
