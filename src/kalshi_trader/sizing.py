from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from kalshi_trader.domain import ONE
from kalshi_trader.fees import effective_buy_price

"""Fractional Kelly sizing for binary contracts.

Buying one contract at effective price c (limit price plus the per-contract fee)
pays 1 with probability q and 0 otherwise. Kelly's optimal fraction of bankroll is

    f* = (q - c) / (1 - c)

which is the edge divided by the odds. Full Kelly assumes q is exactly right; it is
not, so `kelly_fraction` (a quarter is common) scales it down, and
`max_bankroll_fraction` caps any single market regardless of how good it looks.

Capital that sits in a contract for three months earns the same edge as capital that
turns over tomorrow, so an edge must be judged per unit time: the expected return
(q - c) / c is annualized by 365 / days_to_resolution and compared with
`min_annualized_return`. Markets without a close time use the configured default.
"""

SECONDS_PER_DAY = Decimal(86_400)
DAYS_PER_YEAR = Decimal(365)
MIN_DAYS = Decimal(1) / 24  # never annualize with less than an hour


@dataclass(frozen=True)
class SizingConfig:
    kelly_fraction: float
    max_bankroll_fraction: float
    min_annualized_return: float
    default_days_to_resolution: float


@dataclass(frozen=True)
class SizingDecision:
    contracts: int
    kelly_full: Decimal
    fraction: Decimal
    expected_return: Decimal
    annualized_return: Decimal
    days_to_resolution: Decimal
    reason: str

    @property
    def edge_exists(self) -> bool:
        return self.kelly_full > 0


def size_position(
    *,
    fair: Decimal,
    price: Decimal,
    fee_rate: Decimal,
    bankroll_cents: int,
    config: SizingConfig,
    seconds_to_close: float | None = None,
) -> SizingDecision:
    """How many contracts to hold in this market at this price, before risk caps."""
    cost = effective_buy_price(price, fee_rate)
    days = (
        max(MIN_DAYS, Decimal(str(seconds_to_close)) / SECONDS_PER_DAY)
        if seconds_to_close is not None
        else Decimal(str(config.default_days_to_resolution))
    )
    if cost >= ONE or cost <= 0:
        return _none(days, "effective price is not inside (0, 1)")
    edge = fair - cost
    if edge <= 0:
        return SizingDecision(
            0,
            edge / (ONE - cost),
            Decimal(0),
            edge / cost,
            edge / cost * DAYS_PER_YEAR / days,
            days,
            "no edge after fees",
        )
    kelly_full = edge / (ONE - cost)
    fraction = min(
        kelly_full * Decimal(str(config.kelly_fraction)),
        Decimal(str(config.max_bankroll_fraction)),
    )
    expected_return = edge / cost
    annualized = expected_return * DAYS_PER_YEAR / days
    hurdle = Decimal(str(config.min_annualized_return))
    if hurdle > 0 and annualized < hurdle:
        return SizingDecision(
            0,
            kelly_full,
            fraction,
            expected_return,
            annualized,
            days,
            f"annualized return {annualized:.2%} below hurdle {hurdle:.0%}",
        )
    stake_cents = Decimal(bankroll_cents) * fraction
    contracts = int((stake_cents / (cost * 100)).quantize(Decimal(1), rounding=ROUND_FLOOR))
    if contracts <= 0:
        return SizingDecision(
            0, kelly_full, fraction, expected_return, annualized, days, "stake rounds to zero"
        )
    return SizingDecision(
        contracts,
        kelly_full,
        fraction,
        expected_return,
        annualized,
        days,
        f"kelly {kelly_full:.3f} x {config.kelly_fraction:g} -> {fraction:.3%} of bankroll",
    )


def _none(days: Decimal, reason: str) -> SizingDecision:
    zero = Decimal(0)
    return SizingDecision(0, zero, zero, zero, zero, days, reason)


def cap_by_risk(
    contracts: int,
    *,
    price: Decimal,
    max_contracts_per_order: int,
    max_order_notional_cents: int,
    exposure_room_cents: int,
) -> int:
    """Shrink a target so the resulting order passes the pre-trade limits."""
    per_contract_cents = max(1, int((price * 100).to_integral_value(rounding=ROUND_FLOOR)))
    return max(
        0,
        min(
            contracts,
            max_contracts_per_order,
            max_order_notional_cents // per_contract_cents,
            exposure_room_cents // per_contract_cents,
        ),
    )
