"""Parameterized entry point for the local Binance futures strategy backtest."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import database as db
from backend.services.binance_strategy_backtest import (
    DEFAULT_BACKTEST_END_TIME,
    DEFAULT_BACKTEST_START_TIME,
    MAX_LEVERAGE_CAP,
    get_current_strategy_backtest,
    start_current_strategy_backtest,
)


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
STRATEGY_ARGUMENTS = (
    "strategyEngine",
    "modelBranch",
    "strategyMode",
    "levelStrategy",
    "maxAccountLossRatio",
    "maxPortfolioRiskRatio",
    "maxSameSidePositions",
    "dailyLossLimitRatio",
    "rangeEdgeFraction",
    "rangeMinimumTargetR",
    "trendMinimumTargetR",
    "entryConfirmationMode",
    "entryConfirmationExpiryBars",
    "entryFailureExitBars",
    "entryFailureBodyAtrMultiplier",
    "trailingAtrMultiplier",
    "structureStopAtrMultiplier",
    "triggerZoneStopBufferAtrMultiplier",
    "breakevenBufferAtrMultiplier",
    "movingStopActivationR",
    "protectiveTakeProfitRatio",
    "firstTakeProfitRatio",
    "secondTakeProfitRatio",
)
LEVERAGE_ARGUMENTS = ("leverageMode", "leverage", "maxLeverage")


def _positive_float(value: str) -> float:
    number = float(value)
    if not number > 0:
        raise argparse.ArgumentTypeError("必须是正数")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _local_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE).isoformat()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a reproducible Binance futures replay with explicit strategy and execution parameters.",
    )
    parser.add_argument("--network", choices=("mainnet", "testnet"), default="mainnet")
    parser.add_argument("--mode", choices=("RANGE", "RANDOM"), default="RANGE")
    parser.add_argument(
        "--start-time",
        type=_local_timestamp,
        default=datetime.fromtimestamp(DEFAULT_BACKTEST_START_TIME / 1000, LOCAL_TIMEZONE).isoformat(),
        help="Range start; ISO datetime without a timezone is interpreted as Asia/Shanghai",
    )
    parser.add_argument(
        "--end-time",
        type=_local_timestamp,
        default=datetime.fromtimestamp(DEFAULT_BACKTEST_END_TIME / 1000, LOCAL_TIMEZONE).isoformat(),
        help="Range end; ISO datetime without a timezone is interpreted as Asia/Shanghai",
    )
    parser.add_argument("--random-days", type=_positive_int, default=7)

    strategy = parser.add_argument_group("strategy settings")
    strategy.add_argument("--strategy-engine", choices=("CLASSIC", "MODEL"), default=None, help="经典策略或时序模型引擎")
    strategy.add_argument("--model-branch", choices=("BEST", "STABLE"), default=None, help="时序模型分支：验证最佳或稳定末期")
    strategy.add_argument("--strategy-mode", choices=("MIDLINE", "SHORT_TERM"), default=None, help="中线或短线纪律策略模式")
    strategy.add_argument("--level-strategy", choices=("STRUCTURE_EXTREME", "CONFIRMED_PLATFORM"))
    strategy.add_argument("--max-account-loss-ratio", type=float)
    strategy.add_argument("--max-portfolio-risk-ratio", type=float)
    strategy.add_argument("--max-same-side-positions", type=_positive_int)
    strategy.add_argument("--daily-loss-limit-ratio", type=float)
    strategy.add_argument("--range-edge-fraction", type=float)
    strategy.add_argument("--range-minimum-target-r", type=float)
    strategy.add_argument("--trend-minimum-target-r", type=float)
    strategy.add_argument("--entry-confirmation-mode", choices=("RETEST_REQUIRED", "TRIGGER_ONLY"))
    strategy.add_argument("--entry-confirmation-expiry-bars", type=_positive_int)
    strategy.add_argument("--entry-failure-exit-bars", type=_positive_int)
    strategy.add_argument("--entry-failure-body-atr-multiplier", type=float)
    strategy.add_argument("--trailing-atr-multiplier", type=float)
    strategy.add_argument("--structure-stop-atr-multiplier", type=float)
    strategy.add_argument("--trigger-zone-stop-buffer-atr-multiplier", type=float)
    strategy.add_argument("--breakeven-buffer-atr-multiplier", type=float)
    strategy.add_argument("--moving-stop-activation-r", type=float)
    strategy.add_argument("--protective-take-profit-ratio", type=float)
    strategy.add_argument("--first-take-profit-ratio", type=float)
    strategy.add_argument("--second-take-profit-ratio", type=float)
    strategy.add_argument(
        "--use-user-settings",
        action="store_true",
        help="Use a saved user settings snapshot as the base, then apply explicit strategy arguments",
    )
    strategy.add_argument("--user-id", type=int, default=1)

    execution = parser.add_argument_group("execution settings")
    execution.add_argument("--initial-balance", type=_positive_float, default=5.0)
    execution.add_argument(
        "--position-margin-fraction",
        type=_positive_float,
        default=0.25,
        help="Maximum margin per managed position as a fraction of equity (0.25 = 25%%)",
    )
    execution.add_argument("--max-managed-positions", type=_positive_int, default=4)
    execution.add_argument("--scan-limit", type=_positive_int, default=100)
    execution.add_argument(
        "--max-leverage",
        type=_positive_int,
        default=MAX_LEVERAGE_CAP,
        help=f"Integer leverage cap; the backend also enforces a hard maximum of {MAX_LEVERAGE_CAP}x",
    )
    execution.add_argument(
        "--leverage-mode",
        choices=("RISK_BUDGET", "FIXED"),
        help="RISK_BUDGET derives leverage from stop distance; FIXED uses --leverage and sizes margin to the risk budget",
    )
    execution.add_argument(
        "--leverage",
        type=_positive_int,
        help=f"Fixed integer leverage (1-{MAX_LEVERAGE_CAP}); supplying this option selects FIXED mode unless --leverage-mode is provided",
    )
    execution.add_argument(
        "--model-evaluation-mode",
        choices=("OFF", "COMPARE"),
        default="OFF",
        help="COMPARE runs the baseline and model-filtered replay on the model's untouched test interval",
    )
    execution.add_argument("--poll-interval", type=_positive_float, default=1.0)
    execution.add_argument("--dry-run", action="store_true", help="Validate settings and print the run configuration")
    return parser


def _strategy_settings(args: argparse.Namespace) -> dict[str, Any]:
    base = db.get_user_binance_strategy_settings(args.user_id) if args.use_user_settings else db.default_binance_strategy_settings()
    overrides = {
        "strategyEngine": args.strategy_engine,
        "modelBranch": args.model_branch,
        "strategyMode": args.strategy_mode,
        "levelStrategy": args.level_strategy,
        "maxAccountLossRatio": args.max_account_loss_ratio,
        "maxPortfolioRiskRatio": args.max_portfolio_risk_ratio,
        "maxSameSidePositions": args.max_same_side_positions,
        "dailyLossLimitRatio": args.daily_loss_limit_ratio,
        "rangeEdgeFraction": args.range_edge_fraction,
        "rangeMinimumTargetR": args.range_minimum_target_r,
        "trendMinimumTargetR": args.trend_minimum_target_r,
        "entryConfirmationMode": args.entry_confirmation_mode,
        "entryConfirmationExpiryBars": args.entry_confirmation_expiry_bars,
        "entryFailureExitBars": args.entry_failure_exit_bars,
        "entryFailureBodyAtrMultiplier": args.entry_failure_body_atr_multiplier,
        "trailingAtrMultiplier": args.trailing_atr_multiplier,
        "structureStopAtrMultiplier": args.structure_stop_atr_multiplier,
        "triggerZoneStopBufferAtrMultiplier": args.trigger_zone_stop_buffer_atr_multiplier,
        "breakevenBufferAtrMultiplier": args.breakeven_buffer_atr_multiplier,
        "movingStopActivationR": args.moving_stop_activation_r,
        "protectiveTakeProfitRatio": args.protective_take_profit_ratio,
        "firstTakeProfitRatio": args.first_take_profit_ratio,
        "secondTakeProfitRatio": args.second_take_profit_ratio,
    }
    return db.normalize_binance_strategy_settings({**base, **{key: value for key, value in overrides.items() if value is not None}})


def _backtest_options(args: argparse.Namespace) -> dict[str, Any]:
    options: dict[str, Any] = {
        "mode": args.mode,
        "initialBalance": args.initial_balance,
        "positionMarginFraction": args.position_margin_fraction,
        "maxManagedPositions": args.max_managed_positions,
        "scanLimit": args.scan_limit,
        "maxLeverage": args.max_leverage,
        "modelEvaluationMode": args.model_evaluation_mode,
    }
    if args.strategy_mode is not None:
        options["strategyMode"] = args.strategy_mode
    if args.mode == "RANDOM":
        options["days"] = args.random_days
    else:
        options["startTime"] = args.start_time
        options["endTime"] = args.end_time
    if args.leverage_mode is not None:
        options["leverageMode"] = args.leverage_mode
    if args.leverage is not None:
        options["leverage"] = args.leverage
    return options


def _print_job_summary(job: dict[str, Any]) -> None:
    result = job.get("result") or job.get("replayResult") or {}
    print(f"Job: {job.get('id')}")
    print(f"Status: {job.get('status')}")
    print(f"Window: {result.get('startTime')} -> {result.get('endTime')}")
    print(f"Strategy: {(result.get('strategyMode') or (job.get('backtestOptions') or {}).get('strategyMode') or 'MIDLINE')} · timeframe {result.get('timeframe') or '15m'}")
    print(f"Equity: {result.get('initialBalance')} -> {result.get('finalEquity')}")
    print(f"PnL: {result.get('totalPnl')} ({result.get('totalPnlPercent')}%)")
    print(f"Opened: {(result.get('summary') or {}).get('openedCount', 0)}")
    print(f"Record: {result.get('recordFile') or job.get('recordFile') or 'not persisted yet'}")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    strategy_settings = _strategy_settings(args)
    backtest_options = _backtest_options(args)
    if args.position_margin_fraction > 1:
        parser.error("--position-margin-fraction cannot exceed 1")
    if args.max_leverage > MAX_LEVERAGE_CAP:
        parser.error(f"--max-leverage cannot exceed {MAX_LEVERAGE_CAP}")
    if args.leverage is not None and args.leverage > MAX_LEVERAGE_CAP:
        parser.error(f"--leverage cannot exceed {MAX_LEVERAGE_CAP}")
    if args.leverage_mode == "FIXED" and args.leverage is None:
        parser.error("--leverage-mode FIXED requires --leverage")
    if args.poll_interval > 60:
        parser.error("--poll-interval cannot exceed 60 seconds")

    print("Strategy settings:")
    for key in STRATEGY_ARGUMENTS:
        print(f"  {key}: {strategy_settings[key]}")
    print("Backtest options:")
    for key, value in backtest_options.items():
        print(f"  {key}: {value}")
    if args.dry_run:
        return 0

    job = start_current_strategy_backtest(
        args.network,
        backtest_options=backtest_options,
        strategy_settings=strategy_settings,
    )
    print(f"Started backtest job {job['id']}")
    last_message: str | None = None
    while True:
        current = get_current_strategy_backtest(job["id"])
        if current is None:
            print("Backtest job expired")
            return 1
        progress = current.get("progress") or {}
        message = str(progress.get("message") or current.get("status") or "")
        if message != last_message:
            print(message)
            last_message = message
        if current.get("status") == "COMPLETED":
            _print_job_summary(current)
            return 0
        if current.get("status") in {"FAILED", "RATE_LIMITED", "HISTORY_INCOMPLETE"}:
            _print_job_summary(current)
            print(f"Error: {current.get('error') or current.get('progress', {}).get('message') or current.get('status')}")
            return 2
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
