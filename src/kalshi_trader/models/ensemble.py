from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from kalshi_trader.models.base import MarketContext, ProbabilityModel, blend_logit, logit, sigmoid


@dataclass(frozen=True)
class PlattCalibrator:
    """p' = sigmoid(a * logit(p) + b). Identity is a=1, b=0.

    Fit it with `kalshi-trader evaluate --fit-calibration` on rows a model actually
    predicted at the time. a < 1 means the model was overconfident; b shifts bias.
    """

    a: float = 1.0
    b: float = 0.0

    def apply(self, probability: Decimal) -> Decimal:
        return sigmoid(self.a * logit(probability) + self.b)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"a": self.a, "b": self.b}, indent=2) + "\n")

    @staticmethod
    def load(path: Path) -> PlattCalibrator:
        raw = json.loads(path.read_text())
        return PlattCalibrator(float(raw["a"]), float(raw["b"]))


class EnsembleModel:
    """Log-odds blend of several models, then an optional calibration map.

    A model that has no opinion on a ticker simply drops out of that ticker's blend;
    a ticker no model covers gets no signal. If `include_market` is set, the market
    mid joins the blend with that weight, which is the standard shrinkage prior.
    """

    def __init__(
        self,
        members: Sequence[tuple[ProbabilityModel, float]],
        *,
        include_market: float = 0.0,
        calibrator: PlattCalibrator | None = None,
    ) -> None:
        if not members:
            raise ValueError("ensemble needs at least one member")
        self.members = list(members)
        self.include_market = include_market
        self.calibrator = calibrator or PlattCalibrator()

    @property
    def version(self) -> str:
        parts = "+".join(f"{m.version}x{w:g}" for m, w in self.members)
        cal = f"|cal({self.calibrator.a:.3f},{self.calibrator.b:.3f})"
        return f"ensemble[{parts}]{cal}"

    def predict(self, contexts: Sequence[MarketContext]) -> dict[str, Decimal]:
        votes: dict[str, list[tuple[Decimal, float]]] = {}
        for model, weight in self.members:
            for ticker, probability in model.predict(contexts).items():
                votes.setdefault(ticker, []).append((probability, weight))
        if self.include_market > 0:
            for context in contexts:
                if context.ticker in votes and context.mid is not None:
                    votes[context.ticker].append((context.mid, self.include_market))
        return {
            ticker: self.calibrator.apply(blend_logit(parts)) for ticker, parts in votes.items()
        }
