from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from kalshi_trader.api import KalshiClient
from kalshi_trader.auth import RequestSigner
from kalshi_trader.backtest import BacktestError, load_backtest_rows, run_backtest
from kalshi_trader.broker import KalshiBroker, PaperBroker
from kalshi_trader.config import AppConfig, ConfigError, load_config
from kalshi_trader.engine import TradingEngine
from kalshi_trader.logging import configure_logging
from kalshi_trader.signals import SignalError, load_signals
from kalshi_trader.state import StateStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kalshi-trader", description="Risk-first Kalshi execution framework"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="run the trading engine")
    run.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    run.add_argument("--once", action="store_true", help="run one polling cycle")
    run.add_argument(
        "--confirm-live",
        action="store_true",
        help="one of three required production trading interlocks",
    )
    doctor = subcommands.add_parser("doctor", help="validate configuration and local inputs")
    doctor.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    status = subcommands.add_parser("status", help="show local engine state")
    status.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    kill = subcommands.add_parser("kill-switch", help="read or change the persistent kill switch")
    kill.add_argument("setting", choices=("on", "off", "status"))
    kill.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    backtest = subcommands.add_parser("backtest", help="backtest timestamped probability signals")
    backtest.add_argument("dataset", type=Path)
    backtest.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    backtest.add_argument("--output", type=Path)
    return parser


def _broker(config: AppConfig, state: StateStore) -> PaperBroker | KalshiBroker:
    if config.runtime.environment == "paper":
        return PaperBroker(KalshiClient(config.rest_url), state, config.paper)
    credentials = config.credentials()
    signer = RequestSigner(credentials.api_key_id, credentials.private_key_path)
    return KalshiBroker(KalshiClient(config.rest_url, signer=signer), state)


async def _run(config: AppConfig, state: StateStore, *, once: bool) -> None:
    engine = TradingEngine(config, _broker(config, state), state)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, engine.stop)
    await engine.run(once=once)


def _doctor(config: AppConfig) -> dict[str, object]:
    signals = load_signals(config.runtime.signal_path)
    checks: dict[str, object] = {
        "environment": config.runtime.environment,
        "database_parent": str(config.runtime.database_path.parent),
        "signal_count": len(signals),
        "universe_count": len(config.universe.tickers or tuple(signals)),
        "live_orders_enabled": config.runtime.allow_live,
    }
    config.runtime.database_path.parent.mkdir(parents=True, exist_ok=True)
    if config.runtime.environment != "paper":
        credentials = config.credentials()
        RequestSigner(credentials.api_key_id, credentials.private_key_path)
        checks["credentials"] = "loaded"
    return checks


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        configure_logging(config.runtime.log_level)
        if args.command == "doctor":
            print(json.dumps(_doctor(config), indent=2, sort_keys=True))
            return
        if args.command == "backtest":
            report = run_backtest(config, load_backtest_rows(args.dataset))
            rendered = report.to_json()
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered + "\n", encoding="utf-8")
            print(rendered)
            return
        state = StateStore(config.runtime.database_path)
        try:
            if args.command == "status":
                print(json.dumps(state.status_summary(), indent=2, sort_keys=True))
            elif args.command == "kill-switch":
                if args.setting != "status":
                    state.set_kill_switch(args.setting == "on")
                print("on" if state.kill_switch_active() else "off")
            elif args.command == "run":
                config.assert_live_unlocked(args.confirm_live)
                asyncio.run(_run(config, state, once=args.once))
        finally:
            state.close()
    except (BacktestError, ConfigError, SignalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
