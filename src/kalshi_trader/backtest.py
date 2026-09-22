from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from kalshi_trader.config import AppConfig
from kalshi_trader.domain import ONE, Outcome, parse_utc
from kalshi_trader.fees import fee_per_contract, order_fee_cents
from kalshi_trader.sizing import cap_by_risk, size_position


class BacktestError(ValueError):
    pass


@dataclass(frozen=True)
class BacktestRow:
    ticker: str
    observed_at: datetime
    fair_probability: Decimal
    yes_ask: Decimal
    no_ask: Decimal
    result: Outcome
    close_time: datetime | None = None


@dataclass(frozen=True)
class BacktestReport:
    starting_balance_cents: int
    ending_balance_cents: int
    pnl_cents: int
    roi: float
    max_drawdown: float
    trades: int
    wins: int
    win_rate: float
    brier_score: float
    skipped_by_sizing: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def load_backtest_rows(path: Path) -> list[BacktestRow]:
    rows: list[BacktestRow] = []
    required = {
        "ticker",
        "observed_at",
        "fair_probability",
        "yes_ask",
        "no_ask",
        "result",
    }
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise BacktestError(f"dataset must contain: {', '.join(sorted(required))}")
            for line, raw in enumerate(reader, start=2):
                if not raw.get("fair_probability", "").strip() or not raw.get("result", "").strip():
                    continue  # collector rows without a signal or an outcome are not tradable
                try:
                    outcome = Outcome(raw["result"].strip().lower())
                    close_raw = (raw.get("close_time") or "").strip()
                    row = BacktestRow(
                        ticker=raw["ticker"].strip(),
                        observed_at=parse_utc(raw["observed_at"]),
                        fair_probability=Decimal(raw["fair_probability"]),
                        yes_ask=Decimal(raw["yes_ask"]),
                        no_ask=Decimal(raw["no_ask"]),
                        result=outcome,
                        close_time=parse_utc(close_raw) if close_raw else None,
                    )
                except (KeyError, ValueError) as exc:
                    raise BacktestError(f"{path}:{line}: invalid row: {exc}") from exc
                if (
                    not row.ticker
                    or not 0 < row.fair_probability < 1
                    or not 0 < row.yes_ask < 1
                    or not 0 < row.no_ask < 1
                ):
                    raise BacktestError(f"{path}:{line}: probabilities/prices must be in (0,1)")
                rows.append(row)
    except OSError as exc:
        raise BacktestError(f"cannot read {path}: {exc}") from exc
    return sorted(rows, key=lambda row: row.observed_at)


def run_backtest(config: AppConfig, rows: list[BacktestRow]) -> BacktestReport:
    starting = config.paper.starting_balance_cents
    balance = starting
    peak = starting
    max_drawdown = Decimal("0")
    trades = 0
    wins = 0
    brier_total = Decimal("0")
    evaluated = 0
    skipped_by_sizing = 0
    traded_tickers: set[str] = set()
    # The backtester models taker entries: it lifts the ask, pays slippage and the
    # taker fee, and holds to settlement. Maker fills need queue data it does not have.
    fee_rate = config.fees.taker_rate
    for row in rows:
        actual = Decimal("1") if row.result is Outcome.YES else Decimal("0")
        brier_total += (row.fair_probability - actual) ** 2
        evaluated += 1
        if row.ticker in traded_tickers:
            continue
        slip = Decimal(config.paper.slippage_cents) / 100
        yes_price = row.yes_ask + slip
        no_price = row.no_ask + slip
        yes_edge = row.fair_probability - yes_price - fee_per_contract(yes_price, fee_rate)
        no_edge = (ONE - row.fair_probability) - no_price - fee_per_contract(no_price, fee_rate)
        outcome, price, edge = (
            (Outcome.YES, yes_price, yes_edge)
            if yes_edge >= no_edge
            else (Outcome.NO, no_price, no_edge)
        )
        if int(edge * 10_000) < config.strategy.min_edge_bps:
            continue
        if not 0 < price < 1:
            continue
        fair = row.fair_probability if outcome is Outcome.YES else ONE - row.fair_probability
        seconds_to_close = (
            (row.close_time - row.observed_at).total_seconds() if row.close_time else None
        )
        sized = size_position(
            fair=fair,
            price=price,
            fee_rate=fee_rate,
            bankroll_cents=balance,
            config=config.sizing,
            seconds_to_close=seconds_to_close,
        )
        per_contract_cents = max(1, int((price * 100).to_integral_value()))
        count = cap_by_risk(
            min(sized.contracts, balance // per_contract_cents),
            price=price,
            max_contracts_per_order=config.risk.max_contracts_per_order,
            max_order_notional_cents=config.risk.max_order_notional_cents,
            exposure_room_cents=config.risk.max_market_exposure_cents,
        )
        if count <= 0:
            skipped_by_sizing += 1
            continue
        fee_cents = order_fee_cents(count, price, fee_rate)
        cost_cents = (
            int((price * count * 100).quantize(Decimal("1"), rounding=ROUND_CEILING)) + fee_cents
        )
        payout_cents = count * 100 if outcome is row.result else 0
        balance += payout_cents - cost_cents
        trades += 1
        wins += int(payout_cents > 0)
        traded_tickers.add(row.ticker)
        peak = max(peak, balance)
        drawdown = Decimal(peak - balance) / peak if peak else Decimal("0")
        max_drawdown = max(max_drawdown, drawdown)
    pnl = balance - starting
    return BacktestReport(
        starting_balance_cents=starting,
        ending_balance_cents=balance,
        pnl_cents=pnl,
        roi=pnl / starting,
        max_drawdown=float(max_drawdown),
        trades=trades,
        wins=wins,
        win_rate=wins / trades if trades else 0.0,
        brier_score=float(brier_total / evaluated) if evaluated else 0.0,
        skipped_by_sizing=skipped_by_sizing,
    )
