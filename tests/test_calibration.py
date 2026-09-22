from __future__ import annotations

import random
from pathlib import Path

from kalshi_trader.calibration import (
    ScoredRow,
    evaluate,
    fit_platt,
    kl_to_market,
    load_scored_rows,
)


def _synthetic(n: int, overconfidence: float, seed: int = 7) -> list[ScoredRow]:
    """True p ~ U(0.05, 0.95); model reports p pushed away from 0.5 by `overconfidence`."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        truth = rng.uniform(0.05, 0.95)
        outcome = int(rng.random() < truth)
        logit = __import__("math").log(truth / (1 - truth)) * overconfidence
        reported = 1 / (1 + __import__("math").exp(-logit))
        rows.append(ScoredRow(f"M{i}", reported, truth, outcome))
    return rows


def test_perfect_model_beats_noisy_market() -> None:
    rows = _synthetic(3000, overconfidence=1.0)
    report = evaluate(rows)
    assert report.brier < 0.25
    assert report.ece < 0.05
    assert report.kl_to_market is not None and report.kl_to_market < 1e-9


def test_platt_fit_recovers_overconfidence() -> None:
    rows = _synthetic(4000, overconfidence=2.0)
    calibrator = fit_platt(rows)
    # Reported logits are 2x too large; the fitted slope should be about 0.5.
    assert 0.4 < calibrator.a < 0.6
    assert abs(calibrator.b) < 0.15
    fixed = [
        ScoredRow(
            r.ticker,
            float(calibrator.apply(__import__("decimal").Decimal(str(r.predicted)))),
            r.market,
            r.outcome,
        )
        for r in rows
    ]
    assert evaluate(fixed).ece < evaluate(rows).ece


def test_kl_measures_disagreement_not_accuracy() -> None:
    agree = [ScoredRow("A", 0.6, 0.6, 1)]
    disagree = [ScoredRow("A", 0.9, 0.6, 1)]
    assert kl_to_market(agree) == 0
    assert kl_to_market(disagree) > 0.1


def test_load_scored_rows_uses_first_observation_per_ticker(tmp_path: Path) -> None:
    path = tmp_path / "h.csv"
    path.write_text(
        "ticker,observed_at,fair_probability,yes_bid,yes_ask,result\n"
        "A,2026-01-01T00:00:00Z,0.70,0.50,0.52,yes\n"
        "A,2026-01-01T01:00:00Z,0.90,0.80,0.82,yes\n"
        "B,2026-01-01T00:00:00Z,,0.50,0.52,no\n"
    )
    rows = load_scored_rows(path)
    assert [r.ticker for r in rows] == ["A"]
    assert rows[0].predicted == 0.7 and rows[0].market == 0.51
    assert len(load_scored_rows(path, first_per_ticker=False)) == 2
