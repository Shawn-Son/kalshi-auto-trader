from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from kalshi_trader.backtest import BacktestRow, run_backtest
from kalshi_trader.domain import Outcome


def test_backtest_is_chronological_and_one_trade_per_ticker(app_config) -> None:
    rows = [
        BacktestRow(
            ticker="A",
            observed_at=datetime(2025, 1, 1, tzinfo=UTC),
            fair_probability=Decimal("0.80"),
            yes_ask=Decimal("0.50"),
            no_ask=Decimal("0.52"),
            result=Outcome.YES,
        ),
        BacktestRow(
            ticker="A",
            observed_at=datetime(2025, 1, 2, tzinfo=UTC),
            fair_probability=Decimal("0.90"),
            yes_ask=Decimal("0.50"),
            no_ask=Decimal("0.52"),
            result=Outcome.YES,
        ),
    ]
    report = run_backtest(app_config, rows)
    assert report.trades == 1
    assert report.wins == 1
    assert report.ending_balance_cents > report.starting_balance_cents


def test_backtest_charges_kalshi_fee_not_flat_bps(app_config) -> None:
    rows = [
        BacktestRow(
            ticker="A",
            observed_at=datetime(2025, 1, 1, tzinfo=UTC),
            fair_probability=Decimal("0.80"),
            yes_ask=Decimal("0.49"),
            no_ask=Decimal("0.53"),
            result=Outcome.YES,
        )
    ]
    report = run_backtest(app_config, rows)
    # price 0.49 + 1 cent slippage = 0.50; 5 contracts cost 250 cents.
    # fee = ceil(0.07 * 5 * 0.5 * 0.5 * 100) = ceil(8.75) = 9 cents; payout 500.
    assert report.trades == 1
    assert report.pnl_cents == 500 - 250 - 9
