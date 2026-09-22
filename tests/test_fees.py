from __future__ import annotations

from decimal import Decimal

from kalshi_trader.fees import (
    effective_buy_price,
    effective_sell_price,
    fee_per_contract,
    order_fee_cents,
)


def test_fee_peaks_at_fifty_cents_and_rounds_up() -> None:
    rate = Decimal("0.07")
    # 0.07 * 10 * 0.5 * 0.5 = 0.175 dollars -> 18 cents after ceiling.
    assert order_fee_cents(10, Decimal("0.50"), rate) == 18
    # 0.07 * 10 * 0.05 * 0.95 = 0.03325 -> 4 cents.
    assert order_fee_cents(10, Decimal("0.05"), rate) == 4
    assert order_fee_cents(10, Decimal("0.50"), Decimal("0")) == 0
    assert order_fee_cents(0, Decimal("0.50"), rate) == 0


def test_flat_bps_model_would_have_understated_mid_price_fees() -> None:
    # The old 70bps-of-notional model charged 10 * 0.50 * 0.007 = 3.5 cents for
    # ten contracts at 50 cents. Kalshi actually charges 18.
    assert order_fee_cents(10, Decimal("0.50"), Decimal("0.07")) > 5 * 3


def test_effective_prices_include_per_contract_fee() -> None:
    rate = Decimal("0.07")
    assert fee_per_contract(Decimal("0.50"), rate) == Decimal("0.0175")
    assert effective_buy_price(Decimal("0.50"), rate) == Decimal("0.5175")
    assert effective_sell_price(Decimal("0.50"), rate) == Decimal("0.4825")
