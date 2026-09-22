from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kalshi_trader.config import (
    AppConfig,
    FeeConfig,
    PaperConfig,
    RiskConfig,
    RuntimeConfig,
    StrategyConfig,
    UniverseConfig,
)
from kalshi_trader.domain import MarketQuote, OrderIntent, Outcome, PortfolioSnapshot


@pytest.fixture
def app_config(tmp_path) -> AppConfig:
    return AppConfig(
        runtime=RuntimeConfig(
            environment="paper",
            poll_interval_seconds=1,
            database_path=tmp_path / "state.db",
            signal_path=tmp_path / "signals.csv",
            log_level="INFO",
            allow_live=False,
            cancel_open_orders_on_shutdown=True,
        ),
        universe=UniverseConfig(("TEST",)),
        strategy=StrategyConfig(
            min_edge_bps=500,
            min_probability=0.02,
            max_probability=0.98,
            max_signal_age_seconds=60,
            price_improvement_cents=0,
        ),
        risk=RiskConfig(
            max_contracts_per_order=5,
            max_order_notional_cents=250,
            max_market_exposure_cents=1000,
            max_total_exposure_cents=2500,
            max_daily_loss_cents=500,
            max_open_orders=10,
            max_spread_cents=10,
            min_top_level_contracts=2,
            min_balance_cents=1000,
            cooldown_seconds=30,
        ),
        paper=PaperConfig(starting_balance_cents=10_000, slippage_cents=1),
        fees=FeeConfig(taker_rate=Decimal("0.07"), maker_rate=Decimal("0")),
    )


@pytest.fixture
def quote() -> MarketQuote:
    return MarketQuote(
        ticker="TEST",
        yes_bid=Decimal("0.49"),
        yes_ask=Decimal("0.51"),
        no_bid=Decimal("0.49"),
        no_ask=Decimal("0.51"),
        yes_bid_size=Decimal("10"),
        yes_ask_size=Decimal("10"),
        no_bid_size=Decimal("10"),
        no_ask_size=Decimal("10"),
        observed_at=datetime.now(UTC),
    )


@pytest.fixture
def intent() -> OrderIntent:
    now = datetime.now(UTC)
    return OrderIntent(
        client_order_id="client-1",
        ticker="TEST",
        outcome=Outcome.YES,
        count=2,
        limit_price=Decimal("0.51"),
        fair_probability=Decimal("0.65"),
        signal_generated_at=now,
        model_version="test-v1",
    )


@pytest.fixture
def portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balance_cents=10_000,
        total_exposure_cents=0,
        market_exposure_cents=0,
        open_orders=0,
        daily_pnl_cents=0,
        observed_at=datetime.now(UTC),
    )
