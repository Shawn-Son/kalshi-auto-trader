from __future__ import annotations

import csv
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from kalshi_trader.domain import Outcome
from kalshi_trader.models.base import logit
from kalshi_trader.models.ensemble import PlattCalibrator

"""Scoring a probability model against settled outcomes.

Metrics reported per dataset:
- brier:        mean (p - y)^2, lower is better; 0.25 is a coin flip.
- log_loss:     mean -log p(y), the proper score Kelly sizing actually cares about.
- market_brier / market_log_loss: same, using the market mid, i.e. the bar to beat.
- kl_to_market: mean KL(model || market) in nats. It measures how strongly the
                model disagrees with the market, not how right it is. A model that
                beats the market on Brier with tiny KL is finding real, small edges;
                a large KL with worse Brier is noise.
- calibration:  ten bins of predicted probability with observed frequency and count,
                plus expected calibration error (ECE).
- skill:        1 - brier / market_brier; positive means the model beats the market.
"""


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class ScoredRow:
    ticker: str
    predicted: float
    market: float | None
    outcome: int


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float


@dataclass(frozen=True)
class CalibrationReport:
    rows: int
    markets: int
    brier: float
    log_loss: float
    market_brier: float | None
    market_log_loss: float | None
    skill_vs_market: float | None
    kl_to_market: float | None
    ece: float
    bins: list[CalibrationBin]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def load_scored_rows(path: Path, *, first_per_ticker: bool = True) -> list[ScoredRow]:
    """Read the export/backtest CSV; needs ticker, fair_probability, result, and a mid."""
    rows: list[ScoredRow] = []
    seen: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            names = set(reader.fieldnames or ())
            if not {"ticker", "fair_probability", "result"} <= names:
                raise CalibrationError(f"{path} needs ticker, fair_probability, result columns")
            for line, raw in enumerate(reader, start=2):
                ticker = raw["ticker"].strip()
                result = raw["result"].strip().lower()
                probability = raw["fair_probability"].strip()
                if not probability or result not in {"yes", "no"}:
                    continue
                if first_per_ticker and ticker in seen:
                    continue
                seen.add(ticker)
                try:
                    predicted = float(Decimal(probability))
                except ArithmeticError as exc:
                    raise CalibrationError(f"{path}:{line}: bad probability") from exc
                market = _market_mid(raw)
                rows.append(
                    ScoredRow(ticker, predicted, market, int(Outcome(result) is Outcome.YES))
                )
    except OSError as exc:
        raise CalibrationError(f"cannot read {path}: {exc}") from exc
    if not rows:
        raise CalibrationError(f"{path} contains no scoreable rows (need result + probability)")
    return rows


def _market_mid(raw: dict[str, str]) -> float | None:
    bid, ask = raw.get("yes_bid", "").strip(), raw.get("yes_ask", "").strip()
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    if ask:
        return float(ask)
    return None


def _clip(p: float) -> float:
    return min(1 - 1e-6, max(1e-6, p))


def brier(rows: Sequence[ScoredRow], *, use_market: bool = False) -> float | None:
    values = [(r.market if use_market else r.predicted, r.outcome) for r in rows]
    pairs = [(p, y) for p, y in values if p is not None]
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(rows: Sequence[ScoredRow], *, use_market: bool = False) -> float | None:
    values = [(r.market if use_market else r.predicted, r.outcome) for r in rows]
    pairs = [(_clip(p), y) for p, y in values if p is not None]
    if not pairs:
        return None
    return -sum(math.log(p if y else 1 - p) for p, y in pairs) / len(pairs)


def kl_to_market(rows: Sequence[ScoredRow]) -> float | None:
    """Mean KL(model || market) over rows with a market mid."""
    total = 0.0
    n = 0
    for row in rows:
        if row.market is None:
            continue
        p, q = _clip(row.predicted), _clip(row.market)
        total += p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))
        n += 1
    return total / n if n else None


def calibration_bins(rows: Sequence[ScoredRow], *, bins: int = 10) -> list[CalibrationBin]:
    buckets: list[list[ScoredRow]] = [[] for _ in range(bins)]
    for row in rows:
        index = min(bins - 1, int(row.predicted * bins))
        buckets[index].append(row)
    out: list[CalibrationBin] = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        out.append(
            CalibrationBin(
                lower=i / bins,
                upper=(i + 1) / bins,
                count=len(bucket),
                mean_predicted=sum(r.predicted for r in bucket) / len(bucket),
                observed_rate=sum(r.outcome for r in bucket) / len(bucket),
            )
        )
    return out


def expected_calibration_error(bins: Sequence[CalibrationBin], total: int) -> float:
    if total == 0:
        return 0.0
    return sum(b.count / total * abs(b.mean_predicted - b.observed_rate) for b in bins)


def evaluate(rows: Sequence[ScoredRow]) -> CalibrationReport:
    model_brier = brier(rows)
    model_ll = log_loss(rows)
    assert model_brier is not None and model_ll is not None
    mkt_brier = brier(rows, use_market=True)
    bins = calibration_bins(rows)
    return CalibrationReport(
        rows=len(rows),
        markets=len({r.ticker for r in rows}),
        brier=model_brier,
        log_loss=model_ll,
        market_brier=mkt_brier,
        market_log_loss=log_loss(rows, use_market=True),
        skill_vs_market=(1 - model_brier / mkt_brier) if mkt_brier else None,
        kl_to_market=kl_to_market(rows),
        ece=expected_calibration_error(bins, len(rows)),
        bins=bins,
    )


def fit_platt(
    rows: Sequence[ScoredRow], *, ridge: float = 1e-3, iterations: int = 50
) -> PlattCalibrator:
    """Maximum-likelihood a, b for sigmoid(a * logit(p) + b) via Newton's method.

    A small ridge toward the identity keeps the fit sane on tiny datasets. With fewer
    than roughly 100 resolved rows the fit will mostly reflect noise; report it but do
    not trust it.
    """
    xs = [logit(r.predicted) for r in rows]
    ys = [float(r.outcome) for r in rows]
    a, b = 1.0, 0.0
    for _ in range(iterations):
        g_a = ridge * (a - 1.0)
        g_b = ridge * b
        h_aa = ridge
        h_ab = 0.0
        h_bb = ridge
        for x, y in zip(xs, ys, strict=True):
            z = a * x + b
            p = 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))
            r = p - y
            w = p * (1 - p)
            g_a += r * x
            g_b += r
            h_aa += w * x * x
            h_ab += w * x
            h_bb += w
        det = h_aa * h_bb - h_ab * h_ab
        if det <= 1e-12:
            break
        step_a = (h_bb * g_a - h_ab * g_b) / det
        step_b = (h_aa * g_b - h_ab * g_a) / det
        a -= step_a
        b -= step_b
        if abs(step_a) < 1e-9 and abs(step_b) < 1e-9:
            break
    return PlattCalibrator(a=a, b=b)
