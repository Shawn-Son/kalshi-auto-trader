from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from kalshi_trader.domain import MarketQuote
from kalshi_trader.models.base import MarketContext, blend_logit, write_signals
from kalshi_trader.models.ensemble import EnsembleModel, PlattCalibrator
from kalshi_trader.models.external import (
    ExternalOddsModel,
    ExternalRow,
    devig_power,
    load_external_rows,
)
from kalshi_trader.models.structural import (
    StructuralModel,
    implied_overround,
    isotonic_decreasing,
    normalize_exclusive,
)
from kalshi_trader.signals import load_signals


def _quote(ticker: str, bid: str, ask: str) -> MarketQuote:
    return MarketQuote(
        ticker=ticker,
        yes_bid=Decimal(bid),
        yes_ask=Decimal(ask),
        no_bid=Decimal("1") - Decimal(ask),
        no_ask=Decimal("1") - Decimal(bid),
        yes_bid_size=Decimal("10"),
        yes_ask_size=Decimal("10"),
        no_bid_size=Decimal("10"),
        no_ask_size=Decimal("10"),
        observed_at=datetime.now(UTC),
        status="active",
    )


def _ctx(ticker: str, bid: str, ask: str, **kw) -> MarketContext:
    return MarketContext(
        ticker=ticker,
        event_ticker=kw.pop("event", "EV"),
        series_ticker="S",
        title=ticker,
        quote=_quote(ticker, bid, ask),
        **kw,
    )


def test_normalize_exclusive_removes_overround() -> None:
    fair = normalize_exclusive({"A": Decimal("0.60"), "B": Decimal("0.30"), "C": Decimal("0.18")})
    assert abs(sum(fair.values()) - 1) < Decimal("0.0001")
    assert fair["A"] < Decimal("0.60")


def test_isotonic_pools_violators() -> None:
    fitted = isotonic_decreasing([Decimal("0.9"), Decimal("0.5"), Decimal("0.7"), Decimal("0.1")])
    assert fitted == [Decimal("0.9"), Decimal("0.6"), Decimal("0.6"), Decimal("0.1")]
    monotone = [Decimal("0.8"), Decimal("0.5"), Decimal("0.2")]
    assert isotonic_decreasing(monotone) == monotone


def test_structural_model_fixes_ladder_and_exclusive_event() -> None:
    contexts = [
        _ctx("T70", "0.30", "0.32", strike_type="greater", floor_strike=Decimal("70")),
        _ctx("T72", "0.40", "0.42", strike_type="greater", floor_strike=Decimal("72")),  # violates
        _ctx("T74", "0.10", "0.12", strike_type="greater", floor_strike=Decimal("74")),
        _ctx("X1", "0.60", "0.62", event="EX", mutually_exclusive=True, strike_type="between"),
        _ctx("X2", "0.50", "0.52", event="EX", mutually_exclusive=True, strike_type="between"),
    ]
    predictions = StructuralModel().predict(contexts)
    # T70 and T72 pooled to their mean 0.36; T74 unchanged.
    assert predictions["T70"] == predictions["T72"] == Decimal("0.36")
    assert predictions["T74"] == Decimal("0.11")
    # 0.61 + 0.51 = 1.12 -> normalized.
    assert abs(predictions["X1"] + predictions["X2"] - 1) < Decimal("0.0001")
    assert implied_overround(contexts)["EX"] == Decimal("0.12")


def test_devig_power_sums_to_one_and_keeps_order() -> None:
    fair = devig_power([Decimal("0.55"), Decimal("0.35"), Decimal("0.20")])  # book sums to 1.10
    assert abs(sum(fair) - 1) < Decimal("0.0001")
    assert fair[0] > fair[1] > fair[2]


def test_external_model_shrinks_toward_market(tmp_path: Path) -> None:
    rows = [ExternalRow("A", Decimal("0.80"), None, 1.0)]
    contexts = [_ctx("A", "0.49", "0.51")]
    full_trust = ExternalOddsModel(rows, shrink_to_source=1.0).predict(contexts)["A"]
    half_trust = ExternalOddsModel(rows, shrink_to_source=0.5).predict(contexts)["A"]
    assert full_trust == Decimal("0.8")
    assert Decimal("0.50") < half_trust < Decimal("0.80")


def test_external_rows_accept_decimal_odds_and_groups(tmp_path: Path) -> None:
    path = tmp_path / "odds.csv"
    path.write_text("ticker,decimal_odds,group\nA,1.80,g\nB,2.30,g\n")
    model = ExternalOddsModel(load_external_rows(path))
    fair = model.source_probabilities()
    assert abs(fair["A"][0] + fair["B"][0] - 1) < Decimal("0.0001")


def test_ensemble_blends_and_calibrates() -> None:
    class Fixed:
        version = "fixed"

        def __init__(self, value: str) -> None:
            self.value = Decimal(value)

        def predict(self, contexts):
            return {c.ticker: self.value for c in contexts}

    contexts = [_ctx("A", "0.49", "0.51")]
    blended = EnsembleModel([(Fixed("0.60"), 1.0), (Fixed("0.60"), 1.0)]).predict(contexts)
    assert blended["A"] == Decimal("0.6")
    with_market = EnsembleModel([(Fixed("0.60"), 1.0)], include_market=1.0).predict(contexts)
    assert Decimal("0.50") < with_market["A"] < Decimal("0.60")
    shrunk = EnsembleModel(
        [(Fixed("0.90"), 1.0)], calibrator=PlattCalibrator(a=0.5, b=0.0)
    ).predict(contexts)
    assert Decimal("0.5") < shrunk["A"] < Decimal("0.9")
    assert blend_logit(((Decimal("0.5"), 1.0),)) == Decimal("0.5")


def test_write_signals_is_loadable_by_engine(tmp_path: Path) -> None:
    path = tmp_path / "signals.csv"
    write_signals({"A": Decimal("0.6234"), "B": Decimal("0.001")}, path, model_version="m")
    loaded = load_signals(path)
    assert loaded["A"].fair_probability == Decimal("0.6234")
    assert loaded["B"].fair_probability == Decimal("0.0050")  # clamped floor
    assert loaded["A"].model_version == "m"
