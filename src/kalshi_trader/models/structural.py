from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from kalshi_trader.domain import ONE
from kalshi_trader.models.base import MarketContext, clamp

"""Structural models: no external data, only internal consistency of the book.

Two constraints hold by construction and are cheap to check:

1. Mutually exclusive events (Kalshi flags them) must have YES probabilities that
   sum to one across the event's markets. When quoted mids sum to 1.08, every market
   is overpriced by a total of 8 cents and at least one of them is overpriced enough
   to short. Normalizing mids to sum to one gives a fair vector.

2. Threshold ladders must be monotone: P(high >= 72) <= P(high >= 70), and
   P(high <= 63) <= P(high <= 65). When the quotes violate this the ladder contains
   a free lunch, and isotonic regression (pool adjacent violators) is the
   least-squares monotone fit.

These are not alpha by themselves. Kalshi market makers close obvious gaps fast, so
the edge here is usually small and short-lived, and it needs maker-style execution to
survive fees. The value is (a) a sanity check on any external model, and (b) a
baseline fair vector that other models can shrink toward.
"""

VERSION = "structural-2026.09"


def normalize_exclusive(mids: dict[str, Decimal]) -> dict[str, Decimal]:
    total = sum(mids.values(), Decimal("0"))
    if total <= 0:
        return {}
    return {ticker: clamp(mid / total) for ticker, mid in mids.items()}


def isotonic_decreasing(values: Sequence[Decimal]) -> list[Decimal]:
    """Pool-adjacent-violators for a non-increasing sequence."""
    blocks: list[tuple[Decimal, int]] = []  # (block mean, block size)
    for value in values:
        blocks.append((value, 1))
        while len(blocks) >= 2 and blocks[-2][0] < blocks[-1][0]:
            (mean_a, size_a), (mean_b, size_b) = blocks[-2], blocks[-1]
            merged = (mean_a * size_a + mean_b * size_b) / (size_a + size_b)
            blocks[-2:] = [(merged, size_a + size_b)]
    fitted: list[Decimal] = []
    for mean, size in blocks:
        fitted.extend([mean] * size)
    return fitted


class StructuralModel:
    """Fair probabilities from exclusivity and monotonicity constraints alone."""

    version = VERSION

    def __init__(self, *, min_group_size: int = 2) -> None:
        self.min_group_size = min_group_size

    def predict(self, contexts: Sequence[MarketContext]) -> dict[str, Decimal]:
        predictions: dict[str, Decimal] = {}
        predictions.update(self._exclusive_events(contexts))
        predictions.update(self._ladders(contexts))
        return predictions

    def _exclusive_events(self, contexts: Sequence[MarketContext]) -> dict[str, Decimal]:
        groups: dict[str, dict[str, Decimal]] = defaultdict(dict)
        for context in contexts:
            mid = context.mid
            if context.mutually_exclusive and mid is not None:
                groups[context.event_ticker][context.ticker] = mid
        out: dict[str, Decimal] = {}
        for members in groups.values():
            if len(members) >= self.min_group_size:
                out.update(normalize_exclusive(members))
        return out

    def _ladders(self, contexts: Sequence[MarketContext]) -> dict[str, Decimal]:
        """Within one event, 'greater' strikes and 'less' strikes each form a ladder."""
        greater: dict[str, list[tuple[Decimal, str, Decimal]]] = defaultdict(list)
        less: dict[str, list[tuple[Decimal, str, Decimal]]] = defaultdict(list)
        for context in contexts:
            mid = context.mid
            if mid is None:
                continue
            if context.strike_type in {"greater", "greater_or_equal"} and context.floor_strike:
                greater[context.event_ticker].append((context.floor_strike, context.ticker, mid))
            elif context.strike_type in {"less", "less_or_equal"} and context.cap_strike:
                less[context.event_ticker].append((context.cap_strike, context.ticker, mid))
        out: dict[str, Decimal] = {}
        for ladder in greater.values():
            if len(ladder) < self.min_group_size:
                continue
            ladder.sort()  # ascending strike -> probability must not increase
            fitted = isotonic_decreasing([mid for _, _, mid in ladder])
            out.update({ticker: clamp(p) for (_, ticker, _), p in zip(ladder, fitted, strict=True)})
        for ladder in less.values():
            if len(ladder) < self.min_group_size:
                continue
            ladder.sort(reverse=True)  # descending cap -> probability must not increase
            fitted = isotonic_decreasing([mid for _, _, mid in ladder])
            out.update({ticker: clamp(p) for (_, ticker, _), p in zip(ladder, fitted, strict=True)})
        return out


def implied_overround(contexts: Sequence[MarketContext]) -> dict[str, Decimal]:
    """Sum of YES mids per mutually exclusive event, minus one. Diagnostic only."""
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for context in contexts:
        if context.mutually_exclusive and context.mid is not None:
            totals[context.event_ticker] += context.mid
    return {event: total - ONE for event, total in totals.items()}
