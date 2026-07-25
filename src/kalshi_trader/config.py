from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar, cast

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
class PaperConfig:
    starting_balance_cents: int
    fee_bps: int
    slippage_cents: int


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
    strategy = StrategyConfig(
        min_edge_bps=_integer(strat, "min_edge_bps"),
        min_probability=_number(strat, "min_probability"),
        max_probability=_number(strat, "max_probability"),
        max_signal_age_seconds=_integer(strat, "max_signal_age_seconds"),
        price_improvement_cents=_integer(strat, "price_improvement_cents"),
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
        fee_bps=_integer(paper, "fee_bps"),
        slippage_cents=_integer(paper, "slippage_cents"),
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
    ):
        _positive(name, value)
    for name, value in (
        ("strategy.price_improvement_cents", strategy.price_improvement_cents),
        ("risk.max_spread_cents", risk_config.max_spread_cents),
        ("risk.min_top_level_contracts", risk_config.min_top_level_contracts),
        ("risk.cooldown_seconds", risk_config.cooldown_seconds),
        ("paper.fee_bps", paper_config.fee_bps),
        ("paper.slippage_cents", paper_config.slippage_cents),
    ):
        _positive(name, value, zero_ok=True)
    if not 0 < strategy.min_probability < strategy.max_probability < 1:
        raise ConfigError("strategy probability bounds must satisfy 0 < min < max < 1")
    if risk_config.max_order_notional_cents > risk_config.max_market_exposure_cents:
        raise ConfigError("max order notional cannot exceed max market exposure")
    if risk_config.max_market_exposure_cents > risk_config.max_total_exposure_cents:
        raise ConfigError("max market exposure cannot exceed max total exposure")
    return AppConfig(runtime, universe, strategy, risk_config, paper_config)
