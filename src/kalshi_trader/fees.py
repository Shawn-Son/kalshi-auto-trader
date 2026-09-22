from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from kalshi_trader.domain import ONE

# Kalshi's published fee schedule charges, per order,
#     fee = ceil_to_cent(rate * count * price * (1 - price))
# where `rate` is 0.07 for takers on most series and a lower (often zero) rate
# for makers. The charge peaks at a 50-cent price and vanishes near 0 and 1.
# A flat basis-point charge on notional is wrong by roughly 5x at mid prices,
# which is exactly where most mispricing lives, so every fee in this codebase
# goes through this module.

DEFAULT_TAKER_RATE = Decimal("0.07")
DEFAULT_MAKER_RATE = Decimal("0")


def order_fee_cents(count: int, price: Decimal, rate: Decimal) -> int:
    """Whole-order fee in cents, rounded up to the next cent like the exchange does."""
    if count <= 0 or rate <= 0:
        return 0
    raw_dollars = rate * count * price * (ONE - price)
    return int((raw_dollars * 100).quantize(Decimal("1"), rounding=ROUND_CEILING))


def fee_per_contract(price: Decimal, rate: Decimal) -> Decimal:
    """Un-rounded fee per contract in dollars, used for edge and sizing math."""
    if rate <= 0:
        return Decimal("0")
    return rate * price * (ONE - price)


def effective_buy_price(price: Decimal, rate: Decimal) -> Decimal:
    """What a buyer really pays per contract once the fee is included."""
    return price + fee_per_contract(price, rate)


def effective_sell_price(price: Decimal, rate: Decimal) -> Decimal:
    """What a seller really receives per contract once the fee is included."""
    return price - fee_per_contract(price, rate)
