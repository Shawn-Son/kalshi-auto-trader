from __future__ import annotations

from decimal import Decimal

from kalshi_trader.sizing import SizingConfig, cap_by_risk, size_position

CFG = SizingConfig(
    kelly_fraction=0.25,
    max_bankroll_fraction=0.05,
    min_annualized_return=0.0,
    default_days_to_resolution=7.0,
)
FEE = Decimal("0.07")


def test_full_kelly_formula_after_fees() -> None:
    # price 0.50 + fee 0.0175 = 0.5175; edge = 0.80 - 0.5175 = 0.2825
    decision = size_position(
        fair=Decimal("0.80"),
        price=Decimal("0.50"),
        fee_rate=FEE,
        bankroll_cents=100_000,
        config=CFG,
    )
    assert decision.kelly_full == Decimal("0.2825") / Decimal("0.4825")
    # quarter Kelly = 0.146, capped at 5% of bankroll -> 5000 cents / 51.75 = 96 contracts
    assert decision.fraction == Decimal("0.05")
    assert decision.contracts == 96
    assert decision.expected_return > Decimal("0.5")


def test_no_edge_after_fees_sizes_to_zero() -> None:
    # gross edge 1 cent is eaten by the 1.75 cent fee at mid prices
    decision = size_position(
        fair=Decimal("0.51"),
        price=Decimal("0.50"),
        fee_rate=FEE,
        bankroll_cents=100_000,
        config=CFG,
    )
    assert decision.contracts == 0
    assert not decision.edge_exists
    assert decision.reason == "no edge after fees"


def test_small_bankroll_rounds_to_zero() -> None:
    decision = size_position(
        fair=Decimal("0.60"), price=Decimal("0.50"), fee_rate=FEE, bankroll_cents=500, config=CFG
    )
    assert decision.contracts == 0
    assert decision.reason == "stake rounds to zero"


def test_annualized_hurdle_rejects_slow_edges() -> None:
    hurdle = SizingConfig(0.25, 0.05, min_annualized_return=1.0, default_days_to_resolution=7.0)
    # 3.5% return over 90 days annualizes to ~14%: below a 100% hurdle.
    slow = size_position(
        fair=Decimal("0.55"),
        price=Decimal("0.50"),
        fee_rate=FEE,
        bankroll_cents=100_000,
        config=hurdle,
        seconds_to_close=90 * 86_400,
    )
    assert slow.contracts == 0
    assert "below hurdle" in slow.reason
    # The same edge resolving in one day annualizes to over 1000%.
    fast = size_position(
        fair=Decimal("0.55"),
        price=Decimal("0.50"),
        fee_rate=FEE,
        bankroll_cents=100_000,
        config=hurdle,
        seconds_to_close=86_400,
    )
    assert fast.contracts > 0
    assert fast.annualized_return > 10


def test_maker_rate_zero_means_no_fee_drag() -> None:
    taker = size_position(
        fair=Decimal("0.60"),
        price=Decimal("0.50"),
        fee_rate=FEE,
        bankroll_cents=100_000,
        config=CFG,
    )
    maker = size_position(
        fair=Decimal("0.60"),
        price=Decimal("0.50"),
        fee_rate=Decimal("0"),
        bankroll_cents=100_000,
        config=CFG,
    )
    assert maker.kelly_full > taker.kelly_full


def test_cap_by_risk_applies_every_limit() -> None:
    assert (
        cap_by_risk(
            100,
            price=Decimal("0.50"),
            max_contracts_per_order=5,
            max_order_notional_cents=1000,
            exposure_room_cents=1000,
        )
        == 5
    )
    assert (
        cap_by_risk(
            100,
            price=Decimal("0.50"),
            max_contracts_per_order=50,
            max_order_notional_cents=250,
            exposure_room_cents=1000,
        )
        == 5
    )
    assert (
        cap_by_risk(
            100,
            price=Decimal("0.50"),
            max_contracts_per_order=50,
            max_order_notional_cents=1000,
            exposure_room_cents=120,
        )
        == 2
    )
    assert (
        cap_by_risk(
            3,
            price=Decimal("0.50"),
            max_contracts_per_order=5,
            max_order_notional_cents=1000,
            exposure_room_cents=0,
        )
        == 0
    )
