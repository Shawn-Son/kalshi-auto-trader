from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from kalshi_trader.domain import Signal, parse_utc


class SignalError(ValueError):
    pass


def load_signals(path: Path, *, now: datetime | None = None) -> dict[str, Signal]:
    current = now or datetime.now(UTC)
    signals: dict[str, Signal] = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            required = {"ticker", "fair_probability", "generated_at", "model_version"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise SignalError(f"{path} must contain columns: {', '.join(sorted(required))}")
            for line, row in enumerate(reader, start=2):
                try:
                    ticker = row["ticker"].strip()
                    probability = Decimal(row["fair_probability"])
                    generated_at = parse_utc(row["generated_at"])
                    model_version = row["model_version"].strip()
                except (InvalidOperation, ValueError, KeyError) as exc:
                    raise SignalError(f"{path}:{line}: invalid signal: {exc}") from exc
                if not ticker or not model_version:
                    raise SignalError(f"{path}:{line}: ticker and model_version are required")
                if not Decimal("0") < probability < Decimal("1"):
                    raise SignalError(f"{path}:{line}: fair_probability must be between 0 and 1")
                if generated_at > current and (generated_at - current).total_seconds() > 5:
                    raise SignalError(f"{path}:{line}: generated_at is in the future")
                if ticker in signals:
                    raise SignalError(f"{path}:{line}: duplicate ticker {ticker}")
                signals[ticker] = Signal(ticker, probability, generated_at, model_version)
    except OSError as exc:
        raise SignalError(f"cannot read signal file {path}: {exc}") from exc
    return signals
