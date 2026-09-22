from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from kalshi_trader.domain import ONE
from kalshi_trader.models.base import MarketContext, blend_logit, clamp

"""External-probability model.

The most reliable retail edge on Kalshi comes from probabilities someone else already
computed well: a weather service's forecast distribution, a bookmaker's line, an
economist consensus. This model ingests such a file and turns it into a fair
probability per ticker.

Input CSV columns:
    ticker         Kalshi market ticker
    probability    external YES probability in (0, 1)    -- OR --
    decimal_odds   bookmaker decimal odds for YES (probability = 1 / odds)
    group          optional: markets in the same group are mutually exclusive
                   and their raw probabilities carry the source's vig; the group
                   is de-vigged before use
    weight         optional per-row confidence in (0, 1], default 1

De-vig uses the power method: find k such that sum(p_i ** k) == 1. It handles
favorite-longshot bias better than plain proportional scaling.

Shrinkage: the final estimate is a log-odds blend of the external probability
(weight = row weight * shrink) and the market mid (weight = 1 - shrink). shrink = 1
trusts the source fully; 0.5 splits the difference. Markets are usually right, so
start well below 1 and let calibration data move it.
"""

VERSION = "external-2026.09"


class ExternalInputError(ValueError):
    pass


@dataclass(frozen=True)
class ExternalRow:
    ticker: str
    probability: Decimal
    group: str | None
    weight: float


def load_external_rows(path: Path) -> list[ExternalRow]:
    rows: list[ExternalRow] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(line for line in handle if not line.lstrip().startswith("#"))
            names = set(reader.fieldnames or ())
            if "ticker" not in names or not ({"probability", "decimal_odds"} & names):
                raise ExternalInputError(
                    f"{path} needs columns ticker and probability (or decimal_odds)"
                )
            for line, raw in enumerate(reader, start=2):
                ticker = (raw.get("ticker") or "").strip()
                if not ticker:
                    raise ExternalInputError(f"{path}:{line}: ticker is required")
                try:
                    if raw.get("probability"):
                        probability = Decimal(raw["probability"])
                    else:
                        odds = Decimal(raw["decimal_odds"])
                        if odds <= 1:
                            raise ExternalInputError(f"{path}:{line}: decimal odds must exceed 1")
                        probability = ONE / odds
                    weight = float(raw.get("weight") or 1.0)
                except (InvalidOperation, ValueError, KeyError) as exc:
                    raise ExternalInputError(f"{path}:{line}: invalid row: {exc}") from exc
                if not 0 < probability < 1 or not 0 < weight <= 1:
                    raise ExternalInputError(f"{path}:{line}: probability/weight out of range")
                group = (raw.get("group") or "").strip() or None
                rows.append(ExternalRow(ticker, probability, group, weight))
    except OSError as exc:
        raise ExternalInputError(f"cannot read {path}: {exc}") from exc
    return rows


def devig_power(probabilities: Sequence[Decimal]) -> list[Decimal]:
    """Scale by exponent k so that sum(p ** k) == 1; bisection on k in [0.1, 10]."""
    values = [float(p) for p in probabilities]
    total = sum(values)
    if len(values) < 2 or abs(total - 1.0) < 1e-9:
        return [clamp(p) for p in probabilities]
    lo, hi = 0.1, 10.0
    for _ in range(80):
        k = (lo + hi) / 2
        s = sum(v**k for v in values)
        if s > 1.0:
            lo = k  # too much mass: raise the exponent
        else:
            hi = k
    k = (lo + hi) / 2
    return [clamp(Decimal(str(v**k))) for v in values]


class ExternalOddsModel:
    def __init__(self, rows: Sequence[ExternalRow], *, shrink_to_source: float = 0.7) -> None:
        if not 0 < shrink_to_source <= 1:
            raise ExternalInputError("shrink_to_source must be in (0, 1]")
        self.rows = list(rows)
        self.shrink = shrink_to_source

    version = VERSION

    def source_probabilities(self) -> dict[str, tuple[Decimal, float]]:
        """De-vigged external probability and weight per ticker."""
        grouped: dict[str, list[ExternalRow]] = defaultdict(list)
        singles: dict[str, tuple[Decimal, float]] = {}
        for row in self.rows:
            if row.group:
                grouped[row.group].append(row)
            else:
                singles[row.ticker] = (clamp(row.probability), row.weight)
        for members in grouped.values():
            fair = devig_power([m.probability for m in members])
            for member, p in zip(members, fair, strict=True):
                singles[member.ticker] = (p, member.weight)
        return singles

    def predict(self, contexts: Sequence[MarketContext]) -> dict[str, Decimal]:
        source = self.source_probabilities()
        mids = {c.ticker: c.mid for c in contexts}
        out: dict[str, Decimal] = {}
        for ticker, (probability, weight) in source.items():
            mid = mids.get(ticker)
            source_weight = self.shrink * weight
            if mid is None:
                out[ticker] = probability
                continue
            out[ticker] = blend_logit(((probability, source_weight), (mid, 1 - self.shrink)))
        return out
