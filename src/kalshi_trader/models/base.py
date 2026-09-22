from __future__ import annotations

import csv
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from kalshi_trader.api import KalshiAPIError, KalshiClient, quote_from_book
from kalshi_trader.domain import ONE, MarketQuote, Signal

PROB_FLOOR = Decimal("0.005")
PROB_CEIL = ONE - PROB_FLOOR


@dataclass(frozen=True)
class MarketContext:
    """Everything a model may look at for one market, captured at one instant."""

    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    quote: MarketQuote | None
    strike_type: str | None = None
    floor_strike: Decimal | None = None
    cap_strike: Decimal | None = None
    mutually_exclusive: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> Decimal | None:
        if self.quote is None:
            return None
        return (self.quote.yes_bid + self.quote.yes_ask) / 2


class ProbabilityModel(Protocol):
    """A model returns fair YES probabilities only for markets it has an opinion on."""

    @property
    def version(self) -> str: ...

    def predict(self, contexts: Sequence[MarketContext]) -> dict[str, Decimal]: ...


def clamp(probability: Decimal) -> Decimal:
    return min(PROB_CEIL, max(PROB_FLOOR, probability))


def logit(probability: Decimal | float) -> float:
    p = min(float(PROB_CEIL), max(float(PROB_FLOOR), float(probability)))
    return math.log(p / (1 - p))


def sigmoid(value: float) -> Decimal:
    if value >= 0:
        z = math.exp(-value)
        p = 1 / (1 + z)
    else:
        z = math.exp(value)
        p = z / (1 + z)
    return clamp(Decimal(str(p)))


def blend_logit(parts: Iterable[tuple[Decimal | float, float]]) -> Decimal:
    """Weighted average in log-odds space; weights are normalized."""
    total = 0.0
    weight_sum = 0.0
    for probability, weight in parts:
        if weight <= 0:
            continue
        total += weight * logit(probability)
        weight_sum += weight
    if weight_sum == 0:
        raise ValueError("blend requires at least one positive weight")
    return sigmoid(total / weight_sum)


def write_signals(
    signals: Mapping[str, Decimal],
    path: Path,
    *,
    model_version: str,
    generated_at: datetime | None = None,
) -> int:
    """Atomically write the engine's signal CSV: tmp file then rename."""
    stamp = (generated_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("ticker", "fair_probability", "generated_at", "model_version"))
        for ticker in sorted(signals):
            writer.writerow((ticker, f"{clamp(signals[ticker]):.4f}", stamp, model_version))
    os.replace(tmp, path)
    return len(signals)


def signals_from(predictions: Mapping[str, Decimal], version: str) -> list[Signal]:
    now = datetime.now(UTC)
    return [Signal(ticker, clamp(p), now, version) for ticker, p in predictions.items()]


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def context_from_market(
    market: Mapping[str, Any],
    quote: MarketQuote | None,
    *,
    mutually_exclusive: bool = False,
) -> MarketContext:
    event_ticker = str(market.get("event_ticker", ""))
    return MarketContext(
        ticker=str(market.get("ticker", "")),
        event_ticker=event_ticker,
        series_ticker=str(market.get("series_ticker") or event_ticker.rsplit("-", 1)[0]),
        title=str(market.get("title", "")),
        quote=quote,
        strike_type=str(market["strike_type"]) if market.get("strike_type") else None,
        floor_strike=_decimal_or_none(market.get("floor_strike")),
        cap_strike=_decimal_or_none(market.get("cap_strike")),
        mutually_exclusive=mutually_exclusive,
        raw=dict(market),
    )


async def load_contexts(
    client: KalshiClient,
    *,
    series: Sequence[str],
    tickers: Sequence[str],
    max_markets: int = 200,
    depth: int = 1,
) -> list[MarketContext]:
    """Discover open markets and attach a quote plus the event's exclusivity flag."""
    markets: dict[str, dict[str, Any]] = {}
    for series_ticker in series:
        for market in await client.list_markets(
            series_ticker=series_ticker, status="open", max_markets=max_markets
        ):
            markets[str(market.get("ticker"))] = market
    for ticker in tickers:
        if ticker not in markets:
            markets[ticker] = await client.get_market(ticker)
    exclusive: dict[str, bool] = {}
    contexts: list[MarketContext] = []
    for ticker, market in list(markets.items())[:max_markets]:
        event_ticker = str(market.get("event_ticker", ""))
        if event_ticker and event_ticker not in exclusive:
            exclusive[event_ticker] = await client.event_is_mutually_exclusive(event_ticker)
        quote: MarketQuote | None
        try:
            book = await client.get_orderbook(ticker, depth=depth)
            quote = quote_from_book(ticker, market, book) if book.two_sided else None
        except KalshiAPIError:
            quote = None
        contexts.append(
            context_from_market(
                market, quote, mutually_exclusive=exclusive.get(event_ticker, False)
            )
        )
    return contexts
