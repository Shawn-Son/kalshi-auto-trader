from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, TypeVar, cast

from kalshi_trader.domain import OrderStyle

Environment = Literal["paper", "demo", "live"]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    environment: Environment
    poll_interval_seconds: float
    database_path: Path
    signal_path: Path
    log_level: str
    allow_live: bool
    cancel_open_orders_on_shutdown: bool


@dataclass(frozen=True)
class UniverseConfig:
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class StrategyConfig:
    min_edge_bps: int
    min_probability: float
    max_probability: float
    max_signal_age_seconds: int
    price_improvement_cents: int
    order_style: OrderStyle = OrderStyle.TAKER
    exit_edge_bps: int = 200


@dataclass(frozen=True)
class RiskConfig:
    max_contracts_per_order: int
    max_order_notional_cents: int
    max_market_exposure_cents: int
    max_total_exposure_cents: int
    max_daily_loss_cents: int
    max_open_orders: int
    max_spread_cents: int
    min_top_level_contracts: int
    min_balance_cents: int
    cooldown_seconds: int


@dataclass(frozen=True)
class FeeConfig:
    """Kalshi fee rates. fee = ceil(rate * count * price * (1 - price)) per order."""

    taker_rate: Decimal
    maker_rate: Decimal

    def rate_for(self, style: OrderStyle) -> Decimal:
        return self.maker_rate if style is OrderStyle.MAKER else self.taker_rate


@dataclass(frozen=True)
class PaperConfig:
    starting_balance_cents: int
    slippage_cents: int


@dataclass(frozen=True)
class CollectorConfig:
    database_path: Path
    series: tuple[str, ...]
    interval_seconds: float
    orderbook_depth: int
    max_markets: int
    concurrency: int


@dataclass(frozen=True)
class Credentials:
    api_key_id: str
    private_key_path: Path


@dataclass(frozen=True)
class AppConfig:
    runtime: RuntimeConfig
    universe: UniverseConfig
    strategy: StrategyConfig
    risk: RiskConfig
    paper: PaperConfig
    fees: FeeConfig
    collector: CollectorConfig

    @property
    def rest_url(self) -> str:
        if self.runtime.environment == "demo":
            return "https://external-api.demo.kalshi.co/trade-api/v2"
        return "https://external-api.kalshi.com/trade-api/v2"

    def credentials(self) -> Credentials:
        key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if not key_id or not key_path:
            raise ConfigError(
                "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are required for "
                f"{self.runtime.environment} mode"
            )
        path = Path(key_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"private key does not exist: {path}")
        return Credentials(key_id, path)

    def assert_live_unlocked(self, cli_confirmation: bool) -> None:
        if self.runtime.environment != "live":
            return
        interlock = os.getenv("KALSHI_LIVE_CONFIRM", "")
        if not (
            self.runtime.allow_live and cli_confirmation and interlock == "I_ACCEPT_REAL_MONEY_RISK"
        ):
            raise ConfigError(
                "live mode is locked: set runtime.allow_live=true, pass --confirm-live, "
                "and set KALSHI_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK"
            )


T = TypeVar("T")


def _required(section: dict[str, object], key: str, kind: type[T]) -> T:
    value = section.get(key)
    if not isinstance(value, kind):
        raise ConfigError(f"{key} must be {kind.__name__}")
    return value


def _table(raw: dict[str, object], key: str) -> dict[str, object]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"missing or invalid configuration section: {key}")
    return cast(dict[str, object], value)


def _number(section: dict[str, object], key: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be numeric")
    return float(value)


def _integer(section: dict[str, object], key: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value


def _strings(section: dict[str, object], key: str) -> tuple[str, ...]:
    values = _required(section, key, list)
    if not all(isinstance(item, str) and item.strip() for item in values):
        raise ConfigError(f"{key} must contain non-empty strings")
    return tuple(dict.fromkeys(cast(str, item).strip() for item in values))


def _positive(name: str, value: int | float, *, zero_ok: bool = False) -> None:
    if value < 0 or (not zero_ok and value == 0):
        operator = "non-negative" if zero_ok else "positive"
        raise ConfigError(f"{name} must be {operator}")


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load {path}: {exc}") from exc

    rt = _table(raw, "runtime")
    univ = _table(raw, "universe")
    strat = _table(raw, "strategy")
    risk = _table(raw, "risk")
    paper = _table(raw, "paper")
    fees = _table(raw, "fees")
    collector_raw = raw.get("collector")
    collector = cast(dict[str, object], collector_raw) if isinstance(collector_raw, dict) else {}

    environment = _required(rt, "environment", str)
    if environment not in {"paper", "demo", "live"}:
        raise ConfigError("runtime.environment must be paper, demo, or live")
    runtime = RuntimeConfig(
        environment=cast(Environment, environment),
        poll_interval_seconds=_number(rt, "poll_interval_seconds"),
        database_path=Path(_required(rt, "database_path", str)),
        signal_path=Path(_required(rt, "signal_path", str)),
        log_level=_required(rt, "log_level", str),
        allow_live=_required(rt, "allow_live", bool),
        cancel_open_orders_on_shutdown=_required(rt, "cancel_open_orders_on_shutdown", bool),
    )
    universe = UniverseConfig(_strings(univ, "tickers"))
    order_style_raw = strat.get("order_style", "taker")
    if order_style_raw not in {"taker", "maker"}:
        raise ConfigError("strategy.order_style must be taker or maker")
    strategy = StrategyConfig(
        min_edge_bps=_integer(strat, "min_edge_bps"),
        min_probability=_number(strat, "min_probability"),
        max_probability=_number(strat, "max_probability"),
        max_signal_age_seconds=_integer(strat, "max_signal_age_seconds"),
        price_improvement_cents=_integer(strat, "price_improvement_cents"),
        order_style=OrderStyle(order_style_raw),
        exit_edge_bps=_integer(strat, "exit_edge_bps") if "exit_edge_bps" in strat else 200,
    )
    risk_config = RiskConfig(
        max_contracts_per_order=_integer(risk, "max_contracts_per_order"),
        max_order_notional_cents=_integer(risk, "max_order_notional_cents"),
        max_market_exposure_cents=_integer(risk, "max_market_exposure_cents"),
        max_total_exposure_cents=_integer(risk, "max_total_exposure_cents"),
        max_daily_loss_cents=_integer(risk, "max_daily_loss_cents"),
        max_open_orders=_integer(risk, "max_open_orders"),
        max_spread_cents=_integer(risk, "max_spread_cents"),
        min_top_level_contracts=_integer(risk, "min_top_level_contracts"),
        min_balance_cents=_integer(risk, "min_balance_cents"),
        cooldown_seconds=_integer(risk, "cooldown_seconds"),
    )
    paper_config = PaperConfig(
        starting_balance_cents=_integer(paper, "starting_balance_cents"),
        slippage_cents=_integer(paper, "slippage_cents"),
    )
    fee_config = FeeConfig(
        taker_rate=Decimal(str(_number(fees, "taker_rate"))),
        maker_rate=Decimal(str(_number(fees, "maker_rate"))),
    )
    collector_config = CollectorConfig(
        database_path=Path(str(collector.get("database_path", "data/history.db"))),
        series=_strings(collector, "series") if "series" in collector else (),
        interval_seconds=(
            _number(collector, "interval_seconds") if "interval_seconds" in collector else 60.0
        ),
        orderbook_depth=(
            _integer(collector, "orderbook_depth") if "orderbook_depth" in collector else 5
        ),
        max_markets=_integer(collector, "max_markets") if "max_markets" in collector else 200,
        concurrency=_integer(collector, "concurrency") if "concurrency" in collector else 4,
    )

    for name, value in (
        ("runtime.poll_interval_seconds", runtime.poll_interval_seconds),
        ("strategy.min_edge_bps", strategy.min_edge_bps),
        ("strategy.max_signal_age_seconds", strategy.max_signal_age_seconds),
        ("risk.max_contracts_per_order", risk_config.max_contracts_per_order),
        ("risk.max_order_notional_cents", risk_config.max_order_notional_cents),
        ("risk.max_market_exposure_cents", risk_config.max_market_exposure_cents),
        ("risk.max_total_exposure_cents", risk_config.max_total_exposure_cents),
        ("risk.max_daily_loss_cents", risk_config.max_daily_loss_cents),
        ("risk.max_open_orders", risk_config.max_open_orders),
        ("risk.min_balance_cents", risk_config.min_balance_cents),
        ("paper.starting_balance_cents", paper_config.starting_balance_cents),
        ("collector.interval_seconds", collector_config.interval_seconds),
        ("collector.orderbook_depth", collector_config.orderbook_depth),
        ("collector.max_markets", collector_config.max_markets),
        ("collector.concurrency", collector_config.concurrency),
    ):
        _positive(name, value)
    for name, value in (
        ("strategy.price_improvement_cents", strategy.price_improvement_cents),
        ("strategy.exit_edge_bps", strategy.exit_edge_bps),
        ("risk.max_spread_cents", risk_config.max_spread_cents),
        ("risk.min_top_level_contracts", risk_config.min_top_level_contracts),
        ("risk.cooldown_seconds", risk_config.cooldown_seconds),
        ("paper.slippage_cents", paper_config.slippage_cents),
        ("fees.taker_rate", float(fee_config.taker_rate)),
        ("fees.maker_rate", float(fee_config.maker_rate)),
    ):
        _positive(name, value, zero_ok=True)
    if fee_config.taker_rate >= 1 or fee_config.maker_rate >= 1:
        raise ConfigError("fee rates are fractions of notional and must be below 1")
    if not 0 < strategy.min_probability < strategy.max_probability < 1:
        raise ConfigError("strategy probability bounds must satisfy 0 < min < max < 1")
    if risk_config.max_order_notional_cents > risk_config.max_market_exposure_cents:
        raise ConfigError("max order notional cannot exceed max market exposure")
    if risk_config.max_market_exposure_cents > risk_config.max_total_exposure_cents:
        raise ConfigError("max market exposure cannot exceed max total exposure")
    return AppConfig(
        runtime, universe, strategy, risk_config, paper_config, fee_config, collector_config
    )
