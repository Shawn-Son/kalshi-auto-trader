from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_trader.domain import Outcome, Signal
from kalshi_trader.risk import RiskEngine
from kalshi_trader.strategy import ProbabilityMispricingStrategy


def test_strategy_buys_yes_when_underpriced(app_config, quote) -> None:
    signal = Signal("TEST", Decimal("0.65"), quote.observed_at, "v1")
    decision = ProbabilityMispricingStrategy(app_config.strategy).evaluate(signal, quote, count=2)
    assert decision.intent is not None
    assert decision.intent.outcome is Outcome.YES
    assert decision.intent.edge_bps == 1400


def test_strategy_rejects_stale_signal(app_config, quote) -> None:
    stale = quote.observed_at - timedelta(seconds=61)
    signal = Signal("TEST", Decimal("0.65"), stale, "v1")
    decision = ProbabilityMispricingStrategy(app_config.strategy).evaluate(signal, quote, count=2)
    assert decision.intent is None
    assert decision.reason == "signal is stale"


def test_risk_approves_safe_intent(app_config, quote, intent, portfolio) -> None:
    decision = RiskEngine(app_config.risk, app_config.strategy).evaluate(
        intent,
        quote,
        portfolio,
        last_order_at=None,
        kill_switch=False,
    )
    assert decision.approved


def test_risk_rejects_kill_switch(app_config, quote, intent, portfolio) -> None:
    decision = RiskEngine(app_config.risk, app_config.strategy).evaluate(
        intent,
        quote,
        portfolio,
        last_order_at=None,
        kill_switch=True,
    )
    assert not decision.approved
    assert decision.code == "KILL_SWITCH"


def test_risk_rejects_market_exposure(app_config, quote, intent, portfolio) -> None:
    portfolio = replace(portfolio, market_exposure_cents=950)
    decision = RiskEngine(app_config.risk, app_config.strategy).evaluate(
        intent,
        quote,
        portfolio,
        last_order_at=None,
        kill_switch=False,
    )
    assert not decision.approved
    assert decision.code == "MARKET_EXPOSURE"


def test_risk_rejects_cooldown(app_config, quote, intent, portfolio) -> None:
    now = datetime.now(UTC)
    decision = RiskEngine(app_config.risk, app_config.strategy).evaluate(
        intent,
        quote,
        portfolio,
        last_order_at=now - timedelta(seconds=1),
        kill_switch=False,
        now=now,
    )
    assert not decision.approved
    assert decision.code == "COOLDOWN"
