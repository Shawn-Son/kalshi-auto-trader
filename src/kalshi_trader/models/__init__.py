from __future__ import annotations

from pathlib import Path

from kalshi_trader.config import ModelConfig
from kalshi_trader.models.base import (
    MarketContext,
    ProbabilityModel,
    load_contexts,
    write_signals,
)
from kalshi_trader.models.ensemble import EnsembleModel, PlattCalibrator
from kalshi_trader.models.external import ExternalOddsModel, load_external_rows
from kalshi_trader.models.structural import StructuralModel

__all__ = [
    "EnsembleModel",
    "ExternalOddsModel",
    "MarketContext",
    "PlattCalibrator",
    "ProbabilityModel",
    "StructuralModel",
    "build_model",
    "external_tickers",
    "load_contexts",
    "write_signals",
]


def external_tickers(config: ModelConfig, external_input: Path | None = None) -> tuple[str, ...]:
    """Tickers named in the external file, so the signal command fetches their quotes."""
    source = external_input or config.external_input
    if source is None or not source.exists():
        return ()
    return tuple(dict.fromkeys(row.ticker for row in load_external_rows(source)))


def build_model(config: ModelConfig, *, external_input: Path | None = None) -> ProbabilityModel:
    """Assemble the configured ensemble. Members with zero weight are skipped."""
    members: list[tuple[ProbabilityModel, float]] = []
    if config.structural_weight > 0:
        members.append((StructuralModel(), config.structural_weight))
    source = external_input or config.external_input
    # A file passed on the command line is an explicit request: give it weight 1
    # when the config leaves external_weight at 0.
    external_weight = config.external_weight
    if external_input is not None and external_weight <= 0:
        external_weight = 1.0
    if external_weight > 0 and source is not None:
        rows = load_external_rows(source)
        members.append(
            (ExternalOddsModel(rows, shrink_to_source=config.shrink_to_source), external_weight)
        )
    if not members:
        raise ValueError(
            "no model members: set model.structural_weight > 0 or provide an external input"
        )
    calibrator = (
        PlattCalibrator.load(config.calibration_path)
        if config.calibration_path and config.calibration_path.exists()
        else None
    )
    return EnsembleModel(members, include_market=config.market_weight, calibrator=calibrator)
