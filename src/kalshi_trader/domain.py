from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from enum import StrEnum

ONE = Decimal("1")
CENT = Decimal("0.01")


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"


class OrderAction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStyle(StrEnum):
    TAKER = "taker"
    MAKER = "maker"


class OrderStatus(StrEnum):
    PENDING = "pending"
    RESTING = "resting"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Signal:
    ticker: str
    fair_probability: Decimal
    generated_at: datetime
    model_version: str

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        return (current - self.generated_at).total_seconds()


@dataclass(frozen=True)
class Position:
    """Net contracts held in one market. At most one side is non-zero."""

    ticker: str
    outcome: Outcome | None
    count: int
    cost_cents: int = 0

    @property
    def is_flat(self) -> bool:
        return self.outcome is None or self.count <= 0

    def held(self, outcome: Outcome) -> int:
        return self.count if self.outcome is outcome and self.count > 0 else 0

    @staticmethod
    def flat(ticker: str) -> Position:
        return Position(ticker=ticker, outcome=None, count=0)


@dataclass(frozen=True)
class MarketQuote:
    ticker: str
    yes_bid: Decimal
    yes_ask: Decimal
    no_bid: Decimal
    no_ask: Decimal
    yes_bid_size: Decimal
    yes_ask_size: Decimal
    no_bid_size: Decimal
    no_ask_size: Decimal
    observed_at: datetime
    status: str = "open"

    def ask(self, outcome: Outcome) -> Decimal:
        return self.yes_ask if outcome is Outcome.YES else self.no_ask

    def bid(self, outcome: Outcome) -> Decimal:
        return self.yes_bid if outcome is Outcome.YES else self.no_bid

    def ask_size(self, outcome: Outcome) -> Decimal:
        return self.yes_ask_size if outcome is Outcome.YES else self.no_ask_size

    def spread_cents(self, outcome: Outcome) -> int:
        return dollars_to_cents_up(self.ask(outcome) - self.bid(outcome))


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    ticker: str
    outcome: Outcome
    count: int
    limit_price: Decimal
    fair_probability: Decimal
    signal_generated_at: datetime
    model_version: str
    action: OrderAction = OrderAction.BUY
    style: OrderStyle = OrderStyle.TAKER

    @property
    def is_buy(self) -> bool:
        return self.action is OrderAction.BUY

    @property
    def max_loss_cents(self) -> int:
        """Worst-case cash at risk. A sell releases exposure, so it risks nothing new."""
        if not self.is_buy:
            return 0
        return dollars_to_cents_up(self.limit_price * self.count)

    @property
    def outcome_probability(self) -> Decimal:
        return self.fair_probability if self.outcome is Outcome.YES else ONE - self.fair_probability

    @property
    def edge_bps(self) -> int:
        """Gross edge before fees: fair value minus price for buys, price minus fair for sells."""
        if self.is_buy:
            return int((self.outcome_probability - self.limit_price) * 10_000)
        return int((self.limit_price - self.outcome_probability) * 10_000)

    def api_book_side_and_price(self) -> tuple[str, Decimal]:
        """Map to Kalshi's single YES book: every order is a YES bid or a YES ask.

        Buying YES / selling NO adds a YES bid. Selling YES / buying NO adds a YES ask.
        NO prices are quoted as 1 - YES price.
        """
        yes_terms = self.limit_price if self.outcome is Outcome.YES else ONE - self.limit_price
        buys_yes = (self.outcome is Outcome.YES) == self.is_buy
        return ("bid" if buys_yes else "ask"), yes_terms


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    client_order_id: str
    status: OrderStatus
    filled_count: Decimal
    remaining_count: Decimal
    average_fill_price: Decimal | None = None
    fee_dollars: Decimal = Decimal("0")
    raw: dict[str, object] | None = None


@dataclass(frozen=True)
class PortfolioSnapshot:
    balance_cents: int
    total_exposure_cents: int
    market_exposure_cents: int
    open_orders: int
    daily_pnl_cents: int
    observed_at: datetime


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def dollars_to_cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def dollars_to_cents_up(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_CEILING))


def format_dollars(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}"
