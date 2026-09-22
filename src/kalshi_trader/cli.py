from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from dataclasses import replace
from pathlib import Path

from kalshi_trader.api import KalshiClient
from kalshi_trader.auth import RequestSigner
from kalshi_trader.backtest import BacktestError, load_backtest_rows, run_backtest
from kalshi_trader.broker import KalshiBroker, PaperBroker
from kalshi_trader.calibration import CalibrationError, evaluate, fit_platt, load_scored_rows
from kalshi_trader.collector import Collector, export_history
from kalshi_trader.config import AppConfig, ConfigError, load_config
from kalshi_trader.engine import TradingEngine
from kalshi_trader.history import HistoryStore
from kalshi_trader.logging import configure_logging
from kalshi_trader.models import build_model, external_tickers, load_contexts, write_signals
from kalshi_trader.models.external import ExternalInputError
from kalshi_trader.models.structural import implied_overround
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
    collect = subcommands.add_parser(
        "collect", help="snapshot public order books and settlement results into history.db"
    )
    collect.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    collect.add_argument("--once", action="store_true", help="run one collection cycle")
    collect.add_argument(
        "--series", action="append", default=[], help="extra series ticker (repeatable)"
    )
    export = subcommands.add_parser(
        "export-history", help="write collected snapshots as a backtest CSV"
    )
    export.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    export.add_argument("--output", type=Path, default=Path("data/history.csv"))
    export.add_argument(
        "--require-signal",
        action="store_true",
        help="only rows that had a recorded fair_probability at observation time",
    )
    export.add_argument(
        "--include-unresolved", action="store_true", help="also export markets with no result yet"
    )
    history = subcommands.add_parser("history", help="summarize the collected history database")
    history.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    sig = subcommands.add_parser(
        "signal", help="run the configured model over live markets and write the signal CSV"
    )
    sig.add_argument("--config", type=Path, default=Path("config/paper.toml"))
    sig.add_argument("--series", action="append", default=[], help="extra series (repeatable)")
    sig.add_argument("--external", type=Path, help="external probabilities/odds CSV")
    sig.add_argument("--output", type=Path, help="defaults to runtime.signal_path")
    sig.add_argument(
        "--dry-run", action="store_true", help="print predictions without writing the file"
    )
    ev = subcommands.add_parser(
        "evaluate", help="score fair_probability against results in a history/backtest CSV"
    )
    ev.add_argument("dataset", type=Path)
    ev.add_argument("--all-observations", action="store_true", help="score every row, not first")
    ev.add_argument("--fit-calibration", type=Path, help="write Platt a,b to this JSON path")
    ev.add_argument("--output", type=Path)
    return parser


def _broker(config: AppConfig, state: StateStore) -> PaperBroker | KalshiBroker:
    if config.runtime.environment == "paper":
        return PaperBroker(KalshiClient(config.rest_url), state, config.paper, config.fees)
    credentials = config.credentials()
    signer = RequestSigner(credentials.api_key_id, credentials.private_key_path)
    return KalshiBroker(KalshiClient(config.rest_url, signer=signer), state)


async def _collect(config: AppConfig, *, once: bool, extra_series: list[str]) -> None:
    store = HistoryStore(config.collector.database_path)
    series = tuple(dict.fromkeys((*config.collector.series, *extra_series)))
    collector = Collector(
        KalshiClient(config.rest_url),
        store,
        replace(config.collector, series=series),
        tickers=config.universe.tickers,
        signal_path=config.runtime.signal_path,
    )
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, collector.stop)
    try:
        await collector.run(once=once)
    finally:
        store.close()


async def _signal(config: AppConfig, args: argparse.Namespace) -> dict[str, object]:
    model = build_model(config.model, external_input=args.external)
    series = tuple(dict.fromkeys((*config.model.series, *args.series)))
    tickers = tuple(
        dict.fromkeys((*config.universe.tickers, *external_tickers(config.model, args.external)))
    )
    client = KalshiClient(config.rest_url)
    try:
        contexts = await load_contexts(
            client,
            series=series,
            tickers=tickers,
            max_markets=config.model.max_markets,
            depth=config.model.orderbook_depth,
        )
    finally:
        await client.close()
    predictions = model.predict(contexts)
    summary: dict[str, object] = {
        "model_version": model.version,
        "markets_scored": len(predictions),
        "markets_seen": len(contexts),
        "overround_by_event": {
            event: f"{value:+.4f}" for event, value in implied_overround(contexts).items()
        },
    }
    mids = {c.ticker: c.mid for c in contexts}
    summary["predictions"] = {
        ticker: {
            "fair": f"{p:.4f}",
            "mid": f"{mids[ticker]:.4f}" if mids.get(ticker) is not None else None,
        }
        for ticker, p in sorted(predictions.items())
    }
    if not args.dry_run:
        output = args.output or config.runtime.signal_path
        write_signals(predictions, output, model_version=model.version)
        summary["written"] = str(output)
    return summary


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
        if args.command == "collect":
            asyncio.run(_collect(config, once=args.once, extra_series=args.series))
            return
        if args.command == "export-history":
            store = HistoryStore(config.collector.database_path)
            try:
                written = export_history(
                    store,
                    args.output,
                    require_signal=args.require_signal,
                    resolved_only=not args.include_unresolved,
                )
            finally:
                store.close()
            print(json.dumps({"output": str(args.output), "rows": written}, indent=2))
            return
        if args.command == "history":
            store = HistoryStore(config.collector.database_path)
            try:
                print(json.dumps(store.summary(), indent=2, sort_keys=True))
            finally:
                store.close()
            return
        if args.command == "signal":
            print(json.dumps(asyncio.run(_signal(config, args)), indent=2, sort_keys=True))
            return
        if args.command == "evaluate":
            rows = load_scored_rows(args.dataset, first_per_ticker=not args.all_observations)
            calibration = evaluate(rows)
            rendered = calibration.to_json()
            if args.fit_calibration:
                calibrator = fit_platt(rows)
                calibrator.save(args.fit_calibration)
                rendered = rendered.rstrip("}\n") + (
                    f',\n  "fitted_calibration": {{"a": {calibrator.a:.6f}, '
                    f'"b": {calibrator.b:.6f}, "path": "{args.fit_calibration}"}}\n}}'
                )
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
    except (
        BacktestError,
        CalibrationError,
        ConfigError,
        ExternalInputError,
        SignalError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
