from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_trader.domain import OrderAction, OrderStyle, Outcome, Position, Signal
from kalshi_trader.risk import RiskEngine
from kalshi_trader.strategy import ProbabilityMispricingStrategy


def _strategy(app_config, **overrides) -> ProbabilityMispricingStrategy:
    return ProbabilityMispricingStrategy(replace(app_config.strategy, **overrides), app_config.fees)


def test_strategy_buys_yes_when_underpriced(app_config, quote) -> None:
    signal = Signal("TEST", Decimal("0.65"), quote.observed_at, "v1")
    decision = _strategy(app_config).evaluate(signal, quote, count=2)
    assert decision.intent is not None
    assert decision.intent.outcome is Outcome.YES
    assert decision.intent.action is OrderAction.BUY
    assert decision.intent.edge_bps == 1400


def test_strategy_edge_threshold_is_net_of_fees(app_config, quote) -> None:
    # Gross edge 0.56 - 0.51 = 500bps exactly meets the threshold, but the taker fee
    # at 51 cents is 0.07 * 0.51 * 0.49 = 1.75 cents, so the net edge is ~325bps.
    signal = Signal("TEST", Decimal("0.56"), quote.observed_at, "v1")
    decision = _strategy(app_config).evaluate(signal, quote, count=2)
    assert decision.intent is None
    assert "after fees" in decision.reason


def test_strategy_rejects_stale_signal(app_config, quote) -> None:
    stale = quote.observed_at - timedelta(seconds=61)
    signal = Signal("TEST", Decimal("0.65"), stale, "v1")
    decision = _strategy(app_config).evaluate(signal, quote, count=2)
    assert decision.intent is None
    assert decision.reason == "signal is stale"


def test_strategy_only_tops_up_to_target(app_config, quote) -> None:
    signal = Signal("TEST", Decimal("0.65"), quote.observed_at, "v1")
    held = Position("TEST", Outcome.YES, count=1, cost_cents=51)
    decision = _strategy(app_config).evaluate(signal, quote, count=3, position=held)
    assert decision.intent is not None
    assert decision.intent.count == 2
    at_target = Position("TEST", Outcome.YES, count=3, cost_cents=153)
    decision = _strategy(app_config).evaluate(signal, quote, count=3, position=at_target)
    assert decision.intent is None
    assert "already holding" in decision.reason


def test_strategy_exits_when_bid_exceeds_fair_value(app_config, quote) -> None:
    # Hold YES bought earlier; the signal has since fallen to 0.40 while the bid is 0.49.
    signal = Signal("TEST", Decimal("0.40"), quote.observed_at, "v1")
    held = Position("TEST", Outcome.YES, count=4, cost_cents=200)
    decision = _strategy(app_config).evaluate(
        signal, quote, count=2, position=held, max_order_count=3
    )
    assert decision.intent is not None
    assert decision.intent.action is OrderAction.SELL
    assert decision.intent.outcome is Outcome.YES
    assert decision.intent.count == 3  # capped per order; the rest goes next cycle
    assert decision.intent.limit_price == Decimal("0.49")
    assert decision.intent.max_loss_cents == 0


def test_strategy_does_not_open_opposite_side_while_positioned(app_config, quote) -> None:
    # Signal favors NO strongly, but we hold YES and the bid is not yet above fair
    # value by the exit threshold, so nothing happens this cycle.
    signal = Signal("TEST", Decimal("0.475"), quote.observed_at, "v1")
    held = Position("TEST", Outcome.YES, count=2, cost_cents=100)
    decision = _strategy(app_config).evaluate(signal, quote, count=2, position=held)
    assert decision.intent is None


def test_maker_style_rests_inside_the_spread(app_config, quote) -> None:
    signal = Signal("TEST", Decimal("0.65"), quote.observed_at, "v1")
    decision = _strategy(
        app_config, order_style=OrderStyle.MAKER, price_improvement_cents=1
    ).evaluate(signal, quote, count=2)
    assert decision.intent is not None
    assert decision.intent.style is OrderStyle.MAKER
    # bid 0.49 + 1 cent improvement = 0.50, which is still below the 0.51 ask.
    assert decision.intent.limit_price == Decimal("0.50")


def test_risk_approves_sell_without_exposure_checks(app_config, quote, intent, portfolio) -> None:
    sell = replace(intent, action=OrderAction.SELL, limit_price=Decimal("0.49"))
    crowded = replace(portfolio, market_exposure_cents=10_000, total_exposure_cents=10_000)
    decision = RiskEngine(app_config.risk, app_config.strategy).evaluate(
        sell, quote, crowded, last_order_at=None, kill_switch=False
    )
    assert decision.approved


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
