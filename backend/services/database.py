from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import hashlib
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

from .trading_fees import estimate_a_share_trade_fee


ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env", override=False)
DEFAULT_DB_PATH = ROOT_DIR / "data" / "chanlun.db"
AUTH_SESSION_DAYS = 30
LEGACY_OWNER_USERNAME = "__legacy_unclaimed__"
BINANCE_CREDENTIAL_KEY_ENV = "BINANCE_CREDENTIAL_ENCRYPTION_KEY"
BINANCE_LEVEL_STRATEGIES = frozenset({"STRUCTURE_EXTREME", "CONFIRMED_PLATFORM"})
BINANCE_ENTRY_CONFIRMATION_MODES = frozenset({"RETEST_REQUIRED", "TRIGGER_ONLY"})
BINANCE_STRATEGY_MODES = frozenset({"MIDLINE", "SHORT_TERM"})
BINANCE_STRATEGY_ENGINES = frozenset({"CLASSIC", "MODEL"})
BINANCE_MODEL_BRANCHES = frozenset({"BEST", "STABLE"})
BINANCE_CONNECTION_MODES = frozenset({"REST", "WEBSOCKET"})
MAX_BINANCE_FUTURES_LEVERAGE = 20
BINANCE_ENTRY_CONFIRMATION_DEFAULT_MIGRATION = "binance-entry-confirmation-trigger-only-v1"
BINANCE_SCORE_CALIBRATION_MIGRATION = "binance-score-calibration-v1"
BINANCE_CONNECTION_SETTINGS_DEFAULTS = {
    "connectionMode": "REST",
}
BINANCE_STRATEGY_SETTINGS_DEFAULTS = {
    # Keep the established discipline engine as the safe default. The
    # sequence model is an explicit opt-in and can independently select its
    # validation-best or late-training-stable checkpoint.
    "strategyEngine": "CLASSIC",
    "modelBranch": "BEST",
    # Empty follows the latest completed run; a concrete id pins inference
    # and replay to a persisted training snapshot.
    "modelRunId": None,
    # MIDLINE preserves the established 4h -> 1h -> 15m workflow. SHORT_TERM
    # keeps 4h as a hidden risk veto and uses 1h/15m/5m for execution.
    "strategyMode": "MIDLINE",
    "levelStrategy": "STRUCTURE_EXTREME",
    # This is the risk budget for one position, not an account-wide stop.
    # Portfolio and daily limits below provide the account-level guardrails.
    "maxAccountLossRatio": 2.0,
    "maxPortfolioRiskRatio": 4.0,
    "maxSameSidePositions": 2,
    "dailyLossLimitRatio": 4.0,
    "rangeEdgeFraction": 0.25,
    # The source material uses room as a gate, not a universal 2R filter.
    # 1R is the minimum practical range for a short-period setup; trend plans
    # keep a slightly wider 1.5R baseline.
    "rangeMinimumTargetR": 1.0,
    "trendMinimumTargetR": 1.5,
    # 5m is an entry-timing aid.  The 4h/1h/15m plan may trigger directly;
    # a 5m retest is an explicit, stricter opt-in.
    "entryConfirmationMode": "TRIGGER_ONLY",
    "entryConfirmationExpiryBars": 3,
    "entryFailureExitBars": 3,
    "entryFailureBodyAtrMultiplier": 0.5,
    "trailingAtrMultiplier": 2.5,
    "structureStopAtrMultiplier": 0.28,
    # A separate minimum clearance beyond the defensive edge of the complete
    # trigger zone. This prevents a valid structural stop from being glued to
    # a signal-bar wick when the eventual entry is near that zone edge.
    "triggerZoneStopBufferAtrMultiplier": 0.5,
    "breakevenBufferAtrMultiplier": 0.05,
    "movingStopActivationR": 1.0,
    "nearTermMinimumTargetR": 0.5,
    "protectiveTakeProfitRatio": 25.0,
    "firstTakeProfitRatio": 50.0,
    "secondTakeProfitRatio": 75.0,
}
_BINANCE_FUTURES_HISTORY_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}
_BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK = 350
# A batch read keeps the SQL variable list well below SQLite's limit while
# avoiding one connection/query per contract.  The backtest chooses this
# chunk size explicitly so a 5m month does not become one giant Python list.
_BINANCE_FUTURES_HISTORY_READ_SYMBOL_CHUNK = 32


class DisciplineConfirmationRequired(ValueError):
    def __init__(self, violations: list[dict]) -> None:
        self.violations = violations
        super().__init__("该操作偏离交易纪律，需要明确确认后才能执行")


def db_path() -> Path:
    return Path(os.environ.get("CHANLUN_DB_PATH") or DEFAULT_DB_PATH)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_read_connection(*, row_factory=sqlite3.Row) -> Iterator[sqlite3.Connection]:
    """Open a tuned read-only connection for large local history scans.

    ``get_connection`` is intentionally a general read/write helper and
    enables WAL on every call.  Backtest preparation can issue thousands of
    rows-only reads, so it uses this separate helper to avoid that write-side
    setup and to give SQLite a useful per-connection cache.
    """

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = row_factory
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA cache_size=-65536")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                is_system INTEGER NOT NULL DEFAULT 0,
                notification_email TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_active_username ON users(is_system, username);

            CREATE TABLE IF NOT EXISTS auth_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_expiry ON auth_sessions(user_id, expires_at);

            CREATE TABLE IF NOT EXISTS user_ai_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT 'gpt-4.1-mini',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_ai_settings_updated ON user_ai_settings(updated_at);

            CREATE TABLE IF NOT EXISTS user_binance_credentials (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                network TEXT NOT NULL DEFAULT 'mainnet' CHECK (network = 'mainnet'),
                api_key_encrypted TEXT NOT NULL,
                api_secret_encrypted TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_binance_credentials_updated ON user_binance_credentials(updated_at);

            CREATE TABLE IF NOT EXISTS user_binance_connection_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                connection_mode TEXT NOT NULL DEFAULT 'REST'
                    CHECK (connection_mode IN ('REST', 'WEBSOCKET')),
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_binance_connection_settings_updated
                ON user_binance_connection_settings(updated_at);

            CREATE TABLE IF NOT EXISTS user_binance_ui_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                position_pnl_smoothing REAL NOT NULL DEFAULT 0.25
                    CHECK (position_pnl_smoothing >= 0 AND position_pnl_smoothing <= 1),
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_binance_ui_settings_updated
                ON user_binance_ui_settings(updated_at);

            CREATE TABLE IF NOT EXISTS user_binance_strategy_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                strategy_engine TEXT NOT NULL DEFAULT 'CLASSIC'
                    CHECK (strategy_engine IN ('CLASSIC', 'MODEL')),
                model_branch TEXT NOT NULL DEFAULT 'BEST'
                    CHECK (model_branch IN ('BEST', 'STABLE')),
                model_run_id TEXT,
                strategy_mode TEXT NOT NULL DEFAULT 'MIDLINE'
                    CHECK (strategy_mode IN ('MIDLINE', 'SHORT_TERM')),
                level_strategy TEXT NOT NULL DEFAULT 'STRUCTURE_EXTREME'
                    CHECK (level_strategy IN ('STRUCTURE_EXTREME', 'CONFIRMED_PLATFORM')),
                max_account_loss_ratio REAL NOT NULL DEFAULT 2,
                max_portfolio_risk_ratio REAL NOT NULL DEFAULT 4,
                max_same_side_positions INTEGER NOT NULL DEFAULT 2,
                daily_loss_limit_ratio REAL NOT NULL DEFAULT 4,
                range_edge_fraction REAL NOT NULL DEFAULT 0.25,
                range_minimum_target_r REAL NOT NULL DEFAULT 1,
                trend_minimum_target_r REAL NOT NULL DEFAULT 1.5,
                entry_confirmation_mode TEXT NOT NULL DEFAULT 'TRIGGER_ONLY'
                    CHECK (entry_confirmation_mode IN ('RETEST_REQUIRED', 'TRIGGER_ONLY')),
                entry_confirmation_expiry_bars INTEGER NOT NULL DEFAULT 3,
                entry_failure_exit_bars INTEGER NOT NULL DEFAULT 3,
                entry_failure_body_atr_multiplier REAL NOT NULL DEFAULT 0.5,
                trailing_atr_multiplier REAL NOT NULL DEFAULT 2.5,
                structure_stop_atr_multiplier REAL NOT NULL DEFAULT 0.28,
                trigger_zone_stop_buffer_atr_multiplier REAL NOT NULL DEFAULT 0.5,
                breakeven_buffer_atr_multiplier REAL NOT NULL DEFAULT 0.05,
                moving_stop_activation_r REAL NOT NULL DEFAULT 1,
                near_term_minimum_target_r REAL NOT NULL DEFAULT 0.5,
                protective_take_profit_ratio REAL NOT NULL DEFAULT 25,
                first_take_profit_ratio REAL NOT NULL DEFAULT 50,
                second_take_profit_ratio REAL NOT NULL DEFAULT 75,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_binance_strategy_settings_updated
                ON user_binance_strategy_settings(updated_at);

            CREATE TABLE IF NOT EXISTS user_binance_copy_trading_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                smart_money_top_trader_id TEXT NOT NULL DEFAULT '5132388877263187456',
                source_total_margin REAL NOT NULL DEFAULT 0 CHECK (source_total_margin >= 0),
                copy_multiplier REAL NOT NULL DEFAULT 1 CHECK (copy_multiplier > 0),
                smart_money_auth_encrypted TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_binance_copy_trading_settings_updated
                ON user_binance_copy_trading_settings(updated_at);

            CREATE TABLE IF NOT EXISTS user_binance_smart_money_trader_settings (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                top_trader_id TEXT NOT NULL,
                copy_multiplier REAL NOT NULL DEFAULT 1 CHECK (copy_multiplier > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, top_trader_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_binance_smart_money_trader_settings_updated
                ON user_binance_smart_money_trader_settings(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS user_binance_smart_money_position_follows (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                top_trader_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                position_side TEXT NOT NULL CHECK (position_side IN ('LONG', 'SHORT')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, top_trader_id, symbol, position_side)
            );
            CREATE INDEX IF NOT EXISTS idx_user_binance_smart_money_follows_updated
                ON user_binance_smart_money_position_follows(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS user_binance_futures_position_pnl_history (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                position_side TEXT NOT NULL CHECK (position_side IN ('LONG', 'SHORT')),
                recorded_at INTEGER NOT NULL,
                unrealized_pnl REAL NOT NULL,
                realized_pnl REAL,
                PRIMARY KEY (user_id, symbol, position_side, recorded_at)
            );
            CREATE INDEX IF NOT EXISTS idx_user_binance_futures_position_pnl_history
                ON user_binance_futures_position_pnl_history(user_id, symbol, position_side, recorded_at);

            CREATE TABLE IF NOT EXISTS binance_simulated_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
                market_mode TEXT NOT NULL CHECK (market_mode IN ('SPOT', 'FUTURES')),
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
                quantity REAL NOT NULL CHECK (quantity > 0),
                cost_price REAL NOT NULL CHECK (cost_price > 0),
                leverage REAL NOT NULL DEFAULT 1 CHECK (leverage >= 1 AND leverage <= 20),
                plan_json TEXT NOT NULL DEFAULT '{}',
                protected_stop REAL,
                moving_stop REAL,
                moving_stop_active INTEGER NOT NULL DEFAULT 0,
                moving_stop_activation_price REAL,
                moving_stop_activation_at TEXT,
                execution_status TEXT NOT NULL DEFAULT 'EXECUTING',
                stopped_at TEXT,
                stop_price REAL,
                stop_reason TEXT,
                dynamic_plan_json TEXT NOT NULL DEFAULT '{}',
                last_price REAL,
                last_unrealized_pnl REAL,
                last_unrealized_pnl_percent REAL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, network, market_mode, symbol, side)
            );
            CREATE INDEX IF NOT EXISTS idx_binance_simulated_positions_user_updated
                ON binance_simulated_positions(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS binance_futures_history_klines (
                network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                open_time INTEGER NOT NULL,
                close_time INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                quote_volume REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (network, symbol, interval, open_time)
            );
            CREATE INDEX IF NOT EXISTS idx_binance_futures_history_klines_range
                ON binance_futures_history_klines(network, symbol, interval, open_time);
            -- Batch replay reads one interval for many symbols.  This order
            -- lets SQLite scan that interval in symbol/time order without
            -- repeatedly jumping between the per-symbol ranges.
            CREATE INDEX IF NOT EXISTS idx_binance_futures_history_klines_interval_symbol_time
                ON binance_futures_history_klines(network, interval, symbol, open_time);

            CREATE TABLE IF NOT EXISTS binance_futures_history_coverage (
                network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                range_start_ms INTEGER NOT NULL,
                range_end_ms INTEGER NOT NULL,
                bar_count INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (network, symbol, interval, range_start_ms, range_end_ms),
                CHECK (range_end_ms > range_start_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_binance_futures_history_coverage_range
                ON binance_futures_history_coverage(network, symbol, interval, range_start_ms, range_end_ms);

            CREATE TABLE IF NOT EXISTS binance_futures_history_completeness_checks (
                network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                range_start_ms INTEGER NOT NULL,
                range_end_ms INTEGER NOT NULL,
                checked INTEGER NOT NULL DEFAULT 0 CHECK (checked IN (0, 1)),
                complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
                coverage_complete INTEGER NOT NULL DEFAULT 0 CHECK (coverage_complete IN (0, 1)),
                data_complete INTEGER NOT NULL DEFAULT 0 CHECK (data_complete IN (0, 1)),
                checked_at TEXT NOT NULL,
                PRIMARY KEY (network, symbol, interval, range_start_ms, range_end_ms),
                CHECK (range_end_ms > range_start_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_binance_futures_history_completeness_checks_range
                ON binance_futures_history_completeness_checks(network, range_start_ms, range_end_ms);

            CREATE TABLE IF NOT EXISTS binance_ml_training_runs (
                id TEXT PRIMARY KEY,
                network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
                status TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                dataset_json TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                artifact_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_binance_ml_training_runs_network_updated
                ON binance_ml_training_runs(network, updated_at DESC);

            CREATE TABLE IF NOT EXISTS securities (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                market TEXT,
                secid TEXT,
                secucode TEXT,
                is_st INTEGER DEFAULT 0,
                latest_price REAL,
                pct_change REAL,
                turnover_rate REAL,
                pe_dynamic REAL,
                pb REAL,
                total_market_cap REAL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_securities_name ON securities(name);
            CREATE INDEX IF NOT EXISTS idx_securities_st_price ON securities(is_st, latest_price);

            CREATE TABLE IF NOT EXISTS stock_snapshots (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                latest_price REAL,
                price_change REAL,
                pct_change REAL,
                open REAL,
                high REAL,
                low REAL,
                previous_close REAL,
                volume REAL,
                amount REAL,
                volume_ratio REAL,
                turnover_rate REAL,
                total_market_cap REAL,
                circulating_market_cap REAL,
                pe_dynamic REAL,
                pe_static REAL,
                pe_ttm REAL,
                pb REAL,
                nav_per_share REAL,
                raw_json TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS klines (
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                adjust TEXT NOT NULL,
                datetime TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                pct_change REAL,
                turnover_rate REAL,
                amount_unit TEXT,
                volume_unit TEXT,
                float_shares REAL,
                provider_turnover REAL,
                computed_turnover REAL,
                turnover_source TEXT,
                session TEXT,
                source TEXT,
                provider TEXT,
                completed INTEGER NOT NULL DEFAULT 1,
                quality_json TEXT,
                timestamp_quality TEXT,
                adjustment_version TEXT,
                raw_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, period, adjust, datetime)
            );
            CREATE INDEX IF NOT EXISTS idx_klines_symbol_period_datetime ON klines(symbol, period, adjust, datetime);

            CREATE TABLE IF NOT EXISTS current_klines (
                symbol TEXT NOT NULL,
                period TEXT NOT NULL,
                adjust TEXT NOT NULL,
                datetime TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                pct_change REAL,
                turnover_rate REAL,
                amount_unit TEXT,
                volume_unit TEXT,
                float_shares REAL,
                provider_turnover REAL,
                computed_turnover REAL,
                turnover_source TEXT,
                session TEXT,
                source TEXT,
                provider TEXT,
                completed INTEGER NOT NULL DEFAULT 1,
                quality_json TEXT,
                timestamp_quality TEXT,
                adjustment_version TEXT,
                raw_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, period, adjust, datetime)
            );
            CREATE INDEX IF NOT EXISTS idx_current_klines_symbol_period_datetime ON current_klines(symbol, period, adjust, datetime);

            CREATE TABLE IF NOT EXISTS daily_quotes (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                market TEXT,
                latest_price REAL,
                pct_change REAL,
                price_change REAL,
                volume REAL,
                amount REAL,
                turnover_rate REAL,
                pe_dynamic REAL,
                pb REAL,
                total_market_cap REAL,
                quote_minute TEXT NOT NULL,
                raw_json TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_daily_quotes_minute ON daily_quotes(quote_minute);
            CREATE INDEX IF NOT EXISTS idx_daily_quotes_pct_change ON daily_quotes(pct_change);
            CREATE INDEX IF NOT EXISTS idx_daily_quotes_amount ON daily_quotes(amount);

            CREATE TABLE IF NOT EXISTS portfolio_positions (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                name TEXT,
                cost_price REAL NOT NULL,
                shares INTEGER NOT NULL,
                note TEXT,
                trailing_stop REAL,
                trailing_peak REAL,
                trailing_peak_at TEXT,
                trailing_active INTEGER NOT NULL DEFAULT 0,
                trailing_stage TEXT NOT NULL DEFAULT 'INITIAL',
                trailing_activation_price REAL,
                trailing_activation_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, symbol)
            );

            CREATE TABLE IF NOT EXISTS watchlist_items (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                name TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, symbol)
            );

            CREATE TABLE IF NOT EXISTS stock_analysis_history (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                name TEXT,
                last_analyzed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, symbol)
            );
            CREATE INDEX IF NOT EXISTS idx_stock_analysis_history_user_time
                ON stock_analysis_history(user_id, last_analyzed_at DESC);

            CREATE TABLE IF NOT EXISTS portfolio_account (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                cash_balance REAL NOT NULL DEFAULT 100000,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS portfolio_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
                symbol TEXT NOT NULL,
                name TEXT,
                price REAL NOT NULL,
                shares INTEGER NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL,
                cash_before REAL NOT NULL,
                cash_after REAL NOT NULL,
                cost_before REAL,
                cost_after REAL,
                shares_before INTEGER NOT NULL DEFAULT 0,
                shares_after INTEGER NOT NULL DEFAULT 0,
                realized_profit REAL,
                note TEXT,
                discipline_override INTEGER NOT NULL DEFAULT 0,
                discipline_override_reasons TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discipline_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                trade_date TEXT,
                acknowledged_json TEXT NOT NULL,
                note TEXT,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS f10_reports (
                symbol TEXT NOT NULL,
                report_name TEXT NOT NULL,
                record_key TEXT NOT NULL,
                report_date TEXT,
                end_date TEXT,
                notice_date TEXT,
                page_number INTEGER DEFAULT 1,
                raw_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, report_name, record_key)
            );
            CREATE INDEX IF NOT EXISTS idx_f10_symbol_report ON f10_reports(symbol, report_name);

            CREATE TABLE IF NOT EXISTS fundamental_summary (
                symbol TEXT PRIMARY KEY,
                profile_json TEXT,
                valuation_json TEXT,
                finance_json TEXT,
                holder_json TEXT,
                themes_json TEXT,
                business_json TEXT,
                f10_status_json TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS f10_sync_status (
                symbol TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                total_reports INTEGER DEFAULT 0,
                completed_reports INTEGER DEFAULT 0,
                failed_reports INTEGER DEFAULT 0,
                failures_json TEXT,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_cache (
                provider TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                response_json TEXT NOT NULL,
                expires_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (provider, endpoint, cache_key)
            );
            CREATE INDEX IF NOT EXISTS idx_api_cache_expires ON api_cache(expires_at);

            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        _migrate_kline_contract(conn)
        _migrate_current_klines(conn)
        _migrate_personal_data_tables(conn)
        _migrate_portfolio_position_trailing_state(conn)
        _migrate_binance_connection_settings(conn)
        _migrate_binance_copy_trading_settings(conn)
        _migrate_binance_smart_money_trader_settings(conn)
        _migrate_user_notification_settings(conn)
        _migrate_binance_strategy_settings(conn)
        _migrate_binance_simulated_positions(conn)
        _migrate_binance_futures_history(conn)
        _migrate_binance_position_pnl_history(conn)


_PERSONAL_DATA_TABLES = (
    "portfolio_positions",
    "watchlist_items",
    "portfolio_account",
    "portfolio_trades",
    "discipline_journal",
)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()}


def _create_user_scoped_personal_tables(conn: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS portfolio_positions (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            name TEXT,
            cost_price REAL NOT NULL,
            shares INTEGER NOT NULL,
            note TEXT,
            trailing_stop REAL,
            trailing_peak REAL,
            trailing_peak_at TEXT,
            trailing_active INTEGER NOT NULL DEFAULT 0,
            trailing_stage TEXT NOT NULL DEFAULT 'INITIAL',
            trailing_activation_price REAL,
            trailing_activation_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, symbol)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS watchlist_items (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            name TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, symbol)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS portfolio_account (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            cash_balance REAL NOT NULL DEFAULT 100000,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS portfolio_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
            symbol TEXT NOT NULL,
            name TEXT,
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            fee REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL,
            cash_before REAL NOT NULL,
            cash_after REAL NOT NULL,
            cost_before REAL,
            cost_after REAL,
            shares_before INTEGER NOT NULL DEFAULT 0,
            shares_after INTEGER NOT NULL DEFAULT 0,
            realized_profit REAL,
            note TEXT,
            discipline_override INTEGER NOT NULL DEFAULT 0,
            discipline_override_reasons TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS discipline_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            trade_date TEXT,
            acknowledged_json TEXT NOT NULL,
            note TEXT,
            plan_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)


def _ensure_personal_data_indexes(conn: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE INDEX IF NOT EXISTS idx_portfolio_positions_user_updated
            ON portfolio_positions(user_id, updated_at DESC);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_watchlist_items_updated
            ON watchlist_items(user_id, updated_at DESC);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_portfolio_trades_created
            ON portfolio_trades(user_id, created_at DESC);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_portfolio_trades_symbol_created
            ON portfolio_trades(user_id, symbol, created_at DESC);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_discipline_journal_symbol_created
            ON discipline_journal(user_id, symbol, created_at DESC);
        """,
    )
    for statement in statements:
        conn.execute(statement)


def _legacy_column(columns: set[str], name: str, fallback: str) -> str:
    return f'"{name}"' if name in columns else fallback


def _ensure_legacy_owner(conn: sqlite3.Connection, timestamp: str) -> int:
    row = conn.execute(
        "SELECT id FROM users WHERE username = ? AND is_system = 1",
        (LEGACY_OWNER_USERNAME,),
    ).fetchone()
    if row:
        return int(row["id"])
    conn.execute(
        """
        INSERT INTO users (id, username, password_hash, is_system, created_at, updated_at)
        VALUES (0, ?, '!', 1, ?, ?)
        """,
        (LEGACY_OWNER_USERNAME, timestamp, timestamp),
    )
    return 0


def _migrate_personal_data_tables(conn: sqlite3.Connection) -> None:
    needs_migration = any("user_id" not in _table_columns(conn, table) for table in _PERSONAL_DATA_TABLES)
    if not needs_migration:
        _ensure_personal_data_indexes(conn)
        return

    timestamp = now_iso()
    legacy_tables = {table: f"{table}_legacy_user_scope" for table in _PERSONAL_DATA_TABLES}
    for index_name in (
        "idx_watchlist_items_updated",
        "idx_portfolio_trades_created",
        "idx_portfolio_trades_symbol_created",
        "idx_discipline_journal_symbol_created",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    for table, legacy_table in legacy_tables.items():
        if _table_exists(conn, legacy_table):
            raise RuntimeError(f"检测到未完成的个人数据迁移：{legacy_table}")
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy_table}"')

    _create_user_scoped_personal_tables(conn)
    legacy_user_id = _ensure_legacy_owner(conn, timestamp)

    positions = legacy_tables["portfolio_positions"]
    position_columns = _table_columns(conn, positions)
    conn.execute(
        f"""
        INSERT INTO portfolio_positions (
            user_id, symbol, name, cost_price, shares, note, created_at, updated_at
        )
        SELECT ?, {_legacy_column(position_columns, 'symbol', "''")},
            {_legacy_column(position_columns, 'name', 'NULL')},
            {_legacy_column(position_columns, 'cost_price', '0')},
            {_legacy_column(position_columns, 'shares', '0')},
            {_legacy_column(position_columns, 'note', 'NULL')},
            COALESCE({_legacy_column(position_columns, 'created_at', 'NULL')}, ?),
            COALESCE({_legacy_column(position_columns, 'updated_at', 'NULL')}, ?)
        FROM "{positions}"
        WHERE {_legacy_column(position_columns, 'symbol', "''")} <> ''
        """,
        (legacy_user_id, timestamp, timestamp),
    )

    watchlist = legacy_tables["watchlist_items"]
    watchlist_columns = _table_columns(conn, watchlist)
    conn.execute(
        f"""
        INSERT INTO watchlist_items (user_id, symbol, name, note, created_at, updated_at)
        SELECT ?, {_legacy_column(watchlist_columns, 'symbol', "''")},
            {_legacy_column(watchlist_columns, 'name', 'NULL')},
            {_legacy_column(watchlist_columns, 'note', 'NULL')},
            COALESCE({_legacy_column(watchlist_columns, 'created_at', 'NULL')}, ?),
            COALESCE({_legacy_column(watchlist_columns, 'updated_at', 'NULL')}, ?)
        FROM "{watchlist}"
        WHERE {_legacy_column(watchlist_columns, 'symbol', "''")} <> ''
        """,
        (legacy_user_id, timestamp, timestamp),
    )

    account = legacy_tables["portfolio_account"]
    account_columns = _table_columns(conn, account)
    conn.execute(
        f"""
        INSERT INTO portfolio_account (user_id, cash_balance, updated_at)
        SELECT ?, COALESCE({_legacy_column(account_columns, 'cash_balance', 'NULL')}, 100000),
            COALESCE({_legacy_column(account_columns, 'updated_at', 'NULL')}, ?)
        FROM "{account}"
        LIMIT 1
        """,
        (legacy_user_id, timestamp),
    )

    trades = legacy_tables["portfolio_trades"]
    trade_columns = _table_columns(conn, trades)
    conn.execute(
        f"""
        INSERT INTO portfolio_trades (
            id, user_id, action, symbol, name, price, shares, fee, amount, cash_before,
            cash_after, cost_before, cost_after, shares_before, shares_after,
            realized_profit, note, discipline_override, discipline_override_reasons, created_at
        )
        SELECT {_legacy_column(trade_columns, 'id', 'NULL')}, ?,
            {_legacy_column(trade_columns, 'action', "'BUY'")},
            {_legacy_column(trade_columns, 'symbol', "''")},
            {_legacy_column(trade_columns, 'name', 'NULL')},
            COALESCE({_legacy_column(trade_columns, 'price', 'NULL')}, 0),
            COALESCE({_legacy_column(trade_columns, 'shares', 'NULL')}, 0),
            COALESCE({_legacy_column(trade_columns, 'fee', 'NULL')}, 0),
            COALESCE({_legacy_column(trade_columns, 'amount', 'NULL')}, 0),
            COALESCE({_legacy_column(trade_columns, 'cash_before', 'NULL')}, 0),
            COALESCE({_legacy_column(trade_columns, 'cash_after', 'NULL')}, 0),
            {_legacy_column(trade_columns, 'cost_before', 'NULL')},
            {_legacy_column(trade_columns, 'cost_after', 'NULL')},
            COALESCE({_legacy_column(trade_columns, 'shares_before', 'NULL')}, 0),
            COALESCE({_legacy_column(trade_columns, 'shares_after', 'NULL')}, 0),
            {_legacy_column(trade_columns, 'realized_profit', 'NULL')},
            {_legacy_column(trade_columns, 'note', 'NULL')},
            COALESCE({_legacy_column(trade_columns, 'discipline_override', 'NULL')}, 0),
            COALESCE({_legacy_column(trade_columns, 'discipline_override_reasons', 'NULL')}, '[]'),
            COALESCE({_legacy_column(trade_columns, 'created_at', 'NULL')}, ?)
        FROM "{trades}"
        WHERE {_legacy_column(trade_columns, 'symbol', "''")} <> ''
        """,
        (legacy_user_id, timestamp),
    )

    journal = legacy_tables["discipline_journal"]
    journal_columns = _table_columns(conn, journal)
    conn.execute(
        f"""
        INSERT INTO discipline_journal (
            id, user_id, symbol, action, trade_date, acknowledged_json, note, plan_json, created_at
        )
        SELECT {_legacy_column(journal_columns, 'id', 'NULL')}, ?,
            {_legacy_column(journal_columns, 'symbol', "''")},
            {_legacy_column(journal_columns, 'action', "''")},
            {_legacy_column(journal_columns, 'trade_date', 'NULL')},
            COALESCE({_legacy_column(journal_columns, 'acknowledged_json', 'NULL')}, '[]'),
            {_legacy_column(journal_columns, 'note', 'NULL')},
            COALESCE({_legacy_column(journal_columns, 'plan_json', 'NULL')}, '{{}}'),
            COALESCE({_legacy_column(journal_columns, 'created_at', 'NULL')}, ?)
        FROM "{journal}"
        WHERE {_legacy_column(journal_columns, 'symbol', "''")} <> ''
        """,
        (legacy_user_id, timestamp),
    )

    has_legacy_data = any(
        conn.execute(f'SELECT 1 FROM "{legacy_table}" LIMIT 1').fetchone() is not None
        for legacy_table in legacy_tables.values()
    )
    if has_legacy_data:
        conn.execute(
            """
            INSERT INTO app_metadata (key, value, updated_at)
            VALUES ('legacy_personal_data_pending', '1', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (timestamp,),
        )
    else:
        conn.execute("DELETE FROM users WHERE id = ?", (legacy_user_id,))

    for legacy_table in legacy_tables.values():
        conn.execute(f'DROP TABLE "{legacy_table}"')
    _ensure_personal_data_indexes(conn)


def _migrate_portfolio_position_trailing_state(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "portfolio_positions"):
        return
    columns = _table_columns(conn, "portfolio_positions")
    additions = {
        "trailing_stop": "REAL",
        "trailing_peak": "REAL",
        "trailing_peak_at": "TEXT",
        "trailing_active": "INTEGER NOT NULL DEFAULT 0",
        "trailing_stage": "TEXT NOT NULL DEFAULT 'INITIAL'",
        "trailing_activation_price": "REAL",
        "trailing_activation_at": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE portfolio_positions ADD COLUMN {column} {definition}")


def _migrate_binance_simulated_positions(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "binance_simulated_positions"):
        return
    columns = _table_columns(conn, "binance_simulated_positions")
    if "leverage" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN leverage REAL NOT NULL DEFAULT 1")
    if "plan_json" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}'")
    if "execution_status" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'EXECUTING'")
    if "stopped_at" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN stopped_at TEXT")
    if "stop_price" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN stop_price REAL")
    if "stop_reason" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN stop_reason TEXT")
    if "dynamic_plan_json" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN dynamic_plan_json TEXT NOT NULL DEFAULT '{}'")
    if "last_price" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN last_price REAL")
    if "last_unrealized_pnl" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN last_unrealized_pnl REAL")
    if "last_unrealized_pnl_percent" not in columns:
        conn.execute("ALTER TABLE binance_simulated_positions ADD COLUMN last_unrealized_pnl_percent REAL")
    _migrate_binance_simulated_positions_leverage_constraint(conn)
    conn.execute(
        "UPDATE binance_simulated_positions SET leverage = 1 WHERE leverage IS NULL OR leverage < 1"
    )
    conn.execute(
        "UPDATE binance_simulated_positions SET leverage = ? WHERE leverage > ?",
        (MAX_BINANCE_FUTURES_LEVERAGE, MAX_BINANCE_FUTURES_LEVERAGE),
    )
    conn.execute(
        "UPDATE binance_simulated_positions SET plan_json = '{}' WHERE plan_json IS NULL OR plan_json = ''"
    )
    conn.execute(
        "UPDATE binance_simulated_positions SET execution_status = 'EXECUTING' WHERE execution_status IS NULL OR execution_status = ''"
    )
    conn.execute(
        "UPDATE binance_simulated_positions SET dynamic_plan_json = '{}' WHERE dynamic_plan_json IS NULL OR dynamic_plan_json = ''"
    )


def _migrate_binance_position_pnl_history(conn: sqlite3.Connection) -> None:
    """Keep the realized component used to build each position PnL point."""

    if not _table_exists(conn, "user_binance_futures_position_pnl_history"):
        return
    if "realized_pnl" not in _table_columns(conn, "user_binance_futures_position_pnl_history"):
        conn.execute(
            "ALTER TABLE user_binance_futures_position_pnl_history ADD COLUMN realized_pnl REAL"
        )


def _migrate_binance_simulated_positions_leverage_constraint(conn: sqlite3.Connection) -> None:
    """Rebuild older position tables whose SQLite check still stops at 10x.

    SQLite cannot alter a CHECK constraint in place.  Reusing the existing
    table definition keeps any columns added by later migrations intact while
    replacing only the leverage bound, then restores ordinary indexes/triggers.
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("binance_simulated_positions",),
    ).fetchone()
    table_sql = str(row["sql"] if row else "")
    if not re.search(r"leverage\s*>=\s*1\s+AND\s+leverage\s*<=\s*10\b", table_sql, flags=re.IGNORECASE):
        return

    upgraded_sql = re.sub(
        r"(leverage\s*>=\s*1\s+AND\s+leverage\s*<=\s*)10\b",
        r"\g<1>20",
        table_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if upgraded_sql == table_sql:
        return

    temporary_name = "binance_simulated_positions__leverage20"
    conn.execute(f'DROP TABLE IF EXISTS "{temporary_name}"')
    temporary_sql = re.sub(
        r"(?i)(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?)binance_simulated_positions([\"`\]]?)",
        rf"\g<1>{temporary_name}\g<2>",
        upgraded_sql,
        count=1,
    )
    if temporary_sql == upgraded_sql:
        raise RuntimeError("无法迁移 Binance 持仓表杠杆约束")

    related_objects = conn.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL
        """,
        ("binance_simulated_positions",),
    ).fetchall()
    conn.execute(temporary_sql)
    columns = [str(item["name"]) for item in conn.execute('PRAGMA table_info("binance_simulated_positions")').fetchall()]
    if not columns:
        raise RuntimeError("Binance 持仓表迁移后没有可复制字段")
    column_sql = ", ".join(f'"{name.replace(chr(34), chr(34) * 2)}"' for name in columns)
    conn.execute(
        f'INSERT INTO "{temporary_name}" ({column_sql}) SELECT {column_sql} FROM "binance_simulated_positions"'
    )
    conn.execute('DROP TABLE "binance_simulated_positions"')
    conn.execute(f'ALTER TABLE "{temporary_name}" RENAME TO "binance_simulated_positions"')
    for item in related_objects:
        object_sql = str(item["sql"] or "").strip()
        if object_sql:
            conn.execute(object_sql)


def _migrate_binance_strategy_settings(conn: sqlite3.Connection) -> None:
    """Extend strategy settings and restore the previous direct-trigger default."""

    if not _table_exists(conn, "user_binance_strategy_settings"):
        return
    columns = _table_columns(conn, "user_binance_strategy_settings")
    if "strategy_engine" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_strategy_settings "
            "ADD COLUMN strategy_engine TEXT NOT NULL DEFAULT 'CLASSIC'"
        )
    if "model_branch" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_strategy_settings "
            "ADD COLUMN model_branch TEXT NOT NULL DEFAULT 'BEST'"
        )
    if "model_run_id" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_strategy_settings "
            "ADD COLUMN model_run_id TEXT"
        )
    if "strategy_mode" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_strategy_settings "
            "ADD COLUMN strategy_mode TEXT NOT NULL DEFAULT 'MIDLINE'"
        )
    if "level_strategy" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_strategy_settings "
            "ADD COLUMN level_strategy TEXT NOT NULL DEFAULT 'STRUCTURE_EXTREME'"
        )
    if "protective_take_profit_ratio" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_strategy_settings "
            "ADD COLUMN protective_take_profit_ratio REAL NOT NULL DEFAULT 25"
        )
    additions = (
        ("max_portfolio_risk_ratio", "REAL NOT NULL DEFAULT 4"),
        ("max_same_side_positions", "INTEGER NOT NULL DEFAULT 2"),
        ("daily_loss_limit_ratio", "REAL NOT NULL DEFAULT 4"),
        ("range_edge_fraction", "REAL NOT NULL DEFAULT 0.25"),
        ("range_minimum_target_r", "REAL NOT NULL DEFAULT 1"),
        ("trend_minimum_target_r", "REAL NOT NULL DEFAULT 1.5"),
        ("entry_confirmation_mode", "TEXT NOT NULL DEFAULT 'TRIGGER_ONLY'"),
        ("entry_confirmation_expiry_bars", "INTEGER NOT NULL DEFAULT 3"),
        ("entry_failure_exit_bars", "INTEGER NOT NULL DEFAULT 3"),
        ("entry_failure_body_atr_multiplier", "REAL NOT NULL DEFAULT 0.5"),
        ("near_term_minimum_target_r", "REAL NOT NULL DEFAULT 0.5"),
        ("trigger_zone_stop_buffer_atr_multiplier", "REAL NOT NULL DEFAULT 0.5"),
    )
    for column, definition in additions:
        if column not in columns:
            conn.execute(
                "ALTER TABLE user_binance_strategy_settings "
                f"ADD COLUMN {column} {definition}"
            )
    conn.execute(
        """
        UPDATE user_binance_strategy_settings
        SET strategy_engine = 'CLASSIC'
        WHERE strategy_engine IS NULL OR UPPER(TRIM(strategy_engine)) NOT IN ('CLASSIC', 'MODEL')
        """
    )
    conn.execute(
        """
        UPDATE user_binance_strategy_settings
        SET model_branch = 'BEST'
        WHERE model_branch IS NULL OR UPPER(TRIM(model_branch)) NOT IN ('BEST', 'STABLE')
        """
    )
    conn.execute(
        """
        UPDATE user_binance_strategy_settings
        SET strategy_mode = 'MIDLINE'
        WHERE strategy_mode IS NULL
           OR TRIM(strategy_mode) = ''
           OR UPPER(strategy_mode) NOT IN ('MIDLINE', 'SHORT_TERM')
        """
    )
    conn.execute(
        """
        UPDATE user_binance_strategy_settings
        SET level_strategy = 'STRUCTURE_EXTREME'
        WHERE level_strategy IS NULL
           OR TRIM(level_strategy) = ''
           OR UPPER(level_strategy) NOT IN ('STRUCTURE_EXTREME', 'CONFIRMED_PLATFORM')
        """
    )
    conn.execute(
        """
        UPDATE user_binance_strategy_settings
        SET entry_confirmation_mode = 'TRIGGER_ONLY'
        WHERE entry_confirmation_mode IS NULL
           OR TRIM(entry_confirmation_mode) = ''
           OR UPPER(entry_confirmation_mode) NOT IN ('RETEST_REQUIRED', 'TRIGGER_ONLY')
        """
    )
    migration = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?",
        (BINANCE_ENTRY_CONFIRMATION_DEFAULT_MIGRATION,),
    ).fetchone()
    if migration is None:
        # RETEST_REQUIRED was temporarily made the default by an earlier
        # release. Convert those legacy default rows once, while preserving a
        # deliberate RETEST_REQUIRED choice after this migration is recorded.
        conn.execute(
            """
            UPDATE user_binance_strategy_settings
            SET entry_confirmation_mode = 'TRIGGER_ONLY'
            WHERE UPPER(TRIM(entry_confirmation_mode)) = 'RETEST_REQUIRED'
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (BINANCE_ENTRY_CONFIRMATION_DEFAULT_MIGRATION, now_iso()),
        )
    score_migration = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?",
        (BINANCE_SCORE_CALIBRATION_MIGRATION,),
    ).fetchone()
    if score_migration is None:
        # Values equal to the previous built-in defaults are legacy baseline
        # values. Keep deliberate custom settings (for example 2.5R) intact.
        conn.execute(
            """
            UPDATE user_binance_strategy_settings
            SET range_minimum_target_r = 1.0
            WHERE range_minimum_target_r = 2.0
            """
        )
        conn.execute(
            """
            UPDATE user_binance_strategy_settings
            SET trend_minimum_target_r = 1.5
            WHERE trend_minimum_target_r = 2.0
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (BINANCE_SCORE_CALIBRATION_MIGRATION, now_iso()),
        )


def _migrate_binance_connection_settings(conn: sqlite3.Connection) -> None:
    """Create the per-account transport preference for older databases."""

    if not _table_exists(conn, "user_binance_connection_settings"):
        conn.execute(
            """
            CREATE TABLE user_binance_connection_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                connection_mode TEXT NOT NULL DEFAULT 'REST'
                    CHECK (connection_mode IN ('REST', 'WEBSOCKET')),
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_binance_connection_settings_updated
                ON user_binance_connection_settings(updated_at)
            """
        )
        return
    columns = _table_columns(conn, "user_binance_connection_settings")
    if "connection_mode" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_connection_settings "
            "ADD COLUMN connection_mode TEXT NOT NULL DEFAULT 'REST'"
        )
    if "updated_at" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_connection_settings "
            "ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        UPDATE user_binance_connection_settings
        SET connection_mode = 'REST'
        WHERE connection_mode IS NULL
           OR TRIM(connection_mode) = ''
           OR UPPER(connection_mode) NOT IN ('REST', 'WEBSOCKET')
        """
    )


def _migrate_binance_copy_trading_settings(conn: sqlite3.Connection) -> None:
    """Add encrypted per-user Smart Money web login state to older databases."""

    if not _table_exists(conn, "user_binance_copy_trading_settings"):
        return
    columns = _table_columns(conn, "user_binance_copy_trading_settings")
    if "smart_money_top_trader_id" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_copy_trading_settings "
            "ADD COLUMN smart_money_top_trader_id TEXT NOT NULL DEFAULT '5132388877263187456'"
        )
    if "smart_money_auth_encrypted" not in columns:
        conn.execute(
            "ALTER TABLE user_binance_copy_trading_settings "
            "ADD COLUMN smart_money_auth_encrypted TEXT NOT NULL DEFAULT ''"
        )


def _migrate_binance_smart_money_trader_settings(conn: sqlite3.Connection) -> None:
    """Move the legacy selected-trader multiplier into per-trader settings."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_binance_smart_money_trader_settings (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            top_trader_id TEXT NOT NULL,
            copy_multiplier REAL NOT NULL DEFAULT 1 CHECK (copy_multiplier > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, top_trader_id)
        );
        CREATE INDEX IF NOT EXISTS idx_user_binance_smart_money_trader_settings_updated
            ON user_binance_smart_money_trader_settings(user_id, updated_at DESC);
        """
    )
    if not _table_exists(conn, "user_binance_copy_trading_settings"):
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO user_binance_smart_money_trader_settings
            (user_id, top_trader_id, copy_multiplier, created_at, updated_at)
        SELECT user_id, smart_money_top_trader_id, copy_multiplier, updated_at, updated_at
        FROM user_binance_copy_trading_settings
        WHERE TRIM(COALESCE(smart_money_top_trader_id, '')) <> ''
        """
    )


def _migrate_user_notification_settings(conn: sqlite3.Connection) -> None:
    """Keep notification delivery user-scoped across A-share and Binance."""

    columns = _table_columns(conn, "users")
    if "notification_email" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN notification_email TEXT NOT NULL DEFAULT ''")
    if _table_exists(conn, "user_binance_copy_trading_settings") and "smart_money_notification_email" in _table_columns(conn, "user_binance_copy_trading_settings"):
        conn.execute(
            """
            UPDATE users
            SET notification_email = COALESCE((
                SELECT smart_money_notification_email
                FROM user_binance_copy_trading_settings
                WHERE user_binance_copy_trading_settings.user_id = users.id
            ), '')
            WHERE TRIM(COALESCE(notification_email, '')) = ''
              AND EXISTS (
                SELECT 1
                FROM user_binance_copy_trading_settings
                WHERE user_binance_copy_trading_settings.user_id = users.id
                  AND TRIM(COALESCE(smart_money_notification_email, '')) <> ''
              )
            """
        )


def _migrate_binance_futures_history(conn: sqlite3.Connection) -> None:
    """Add historical quote turnover without rebuilding the large K-line table."""

    if not _table_exists(conn, "binance_futures_history_klines"):
        return
    columns = _table_columns(conn, "binance_futures_history_klines")
    if "quote_volume" not in columns:
        conn.execute("ALTER TABLE binance_futures_history_klines ADD COLUMN quote_volume REAL")


def _normalize_username(value: object) -> str:
    username = str(value or "").strip()
    if len(username) < 2 or len(username) > 32:
        raise ValueError("用户名长度需为 2 到 32 个字符")
    if username.startswith("__") or any(ord(char) < 32 for char in username):
        raise ValueError("用户名包含不支持的字符")
    return username


def _validate_password(value: object) -> str:
    password = str(value or "")
    if len(password) < 8 or len(password) > 128:
        raise ValueError("密码长度需为 8 到 128 个字符")
    return password


def _normalize_notification_email(value: object) -> str:
    email = str(value or "").strip()
    if email and (len(email) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
        raise ValueError("提醒邮箱格式无效")
    return email


def _user_profile(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "createdAt": row["created_at"],
    }


def get_user(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, created_at FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
    return _user_profile(row) if row else None


def get_user_notification_settings(user_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT notification_email, updated_at FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
    if not row:
        raise ValueError("用户不存在")
    return {
        "email": str(row["notification_email"] or "").strip(),
        "updatedAt": row["updated_at"],
    }


def save_user_notification_settings(user_id: int, value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    email = _normalize_notification_email(source.get("email"))
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE users SET notification_email = ?, updated_at = ? WHERE id = ? AND is_system = 0",
            (email, now_iso(), int(user_id)),
        )
    if not cursor.rowcount:
        raise ValueError("用户不存在")
    return get_user_notification_settings(user_id)


def register_user(username: object, password: object) -> dict:
    normalized_username = _normalize_username(username)
    valid_password = _validate_password(password)
    timestamp = now_iso()
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (username, password_hash, is_system, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (normalized_username, generate_password_hash(valid_password), timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已被使用") from exc
        user_id = int(cursor.lastrowid)
        _claim_legacy_personal_data(conn, user_id, timestamp)
        row = conn.execute(
            "SELECT id, username, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _user_profile(row) if row else {}


def authenticate_user(username: object, password: object) -> dict | None:
    normalized_username = str(username or "").strip()
    if not normalized_username or not isinstance(password, str):
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE username = ? AND is_system = 0",
            (normalized_username,),
        ).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return None
    return _user_profile(row)


def create_auth_session(user_id: int) -> dict:
    profile = get_user(user_id)
    if not profile:
        raise ValueError("用户不存在")
    token = secrets.token_urlsafe(32)
    timestamp = now_iso()
    expires_at = (datetime.now() + timedelta(days=AUTH_SESSION_DAYS)).isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (user_id, token_hash, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(user_id), _token_hash(token), expires_at, timestamp),
        )
    return {"token": token, "expiresAt": expires_at}


def get_user_by_session_token(token: object) -> dict | None:
    if not isinstance(token, str) or not token.strip():
        return None
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.username, users.created_at
            FROM auth_sessions
            JOIN users ON users.id = auth_sessions.user_id
            WHERE auth_sessions.token_hash = ?
              AND auth_sessions.revoked_at IS NULL
              AND auth_sessions.expires_at > ?
              AND users.is_system = 0
            """,
            (_token_hash(token.strip()), now_iso()),
        ).fetchone()
    return _user_profile(row) if row else None


def revoke_auth_session(token: object) -> bool:
    if not isinstance(token, str) or not token.strip():
        return False
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE auth_sessions
            SET revoked_at = ?
            WHERE token_hash = ? AND revoked_at IS NULL
            """,
            (now_iso(), _token_hash(token.strip())),
        )
    return cursor.rowcount > 0


DEFAULT_AI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_AI_MODEL = "gpt-4.1-mini"


def _normalize_ai_base_url(value: object) -> str:
    base_url = str(value or "").strip()
    if len(base_url) > 512:
        raise ValueError("AI Base URL 长度不能超过 512 个字符")
    return base_url or DEFAULT_AI_BASE_URL


def _normalize_ai_model(value: object) -> str:
    model = str(value or "").strip()
    if len(model) > 128:
        raise ValueError("AI 模型名称长度不能超过 128 个字符")
    return model or DEFAULT_AI_MODEL


def _ai_settings_profile(row: sqlite3.Row | None, include_secret: bool = False) -> dict:
    base_url = row["base_url"] if row else DEFAULT_AI_BASE_URL
    model = row["model"] if row else DEFAULT_AI_MODEL
    api_key = row["api_key"] if row else ""
    profile = {
        "baseUrl": base_url or DEFAULT_AI_BASE_URL,
        "model": model or DEFAULT_AI_MODEL,
        "apiKeyConfigured": bool(api_key),
        "updatedAt": row["updated_at"] if row else None,
    }
    if include_secret:
        profile["apiKey"] = api_key or ""
    return profile


def get_user_ai_settings(user_id: int, include_secret: bool = False) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT base_url, api_key, model, updated_at
            FROM user_ai_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _ai_settings_profile(row, include_secret=include_secret)


def save_user_ai_settings(
    user_id: int,
    *,
    base_url: object = None,
    model: object = None,
    api_key: object = None,
) -> dict:
    normalized_base_url = _normalize_ai_base_url(base_url)
    normalized_model = _normalize_ai_model(model)
    timestamp = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
        if not user:
            raise ValueError("用户不存在")

        existing = conn.execute(
            "SELECT api_key FROM user_ai_settings WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        source = value if isinstance(value, dict) else {}
        next_api_key = existing["api_key"] if existing else ""
        if api_key is not None:
            next_api_key = str(api_key).strip()
            if len(next_api_key) > 4096:
                raise ValueError("AI API Key 长度不能超过 4096 个字符")

        conn.execute(
            """
            INSERT INTO user_ai_settings (user_id, base_url, api_key, model, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                base_url = excluded.base_url,
                api_key = excluded.api_key,
                model = excluded.model,
                updated_at = excluded.updated_at
            """,
            (int(user_id), normalized_base_url, next_api_key, normalized_model, timestamp),
        )
        row = conn.execute(
            """
            SELECT base_url, api_key, model, updated_at
            FROM user_ai_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _ai_settings_profile(row)


def default_binance_strategy_settings() -> dict[str, Any]:
    """Return a fresh copy of the user-facing Binance strategy defaults."""

    return dict(BINANCE_STRATEGY_SETTINGS_DEFAULTS)


def normalize_binance_strategy_settings(value: object = None) -> dict[str, Any]:
    """Validate strategy settings and keep all target ratios cumulative."""

    source = value if isinstance(value, dict) else {}
    settings = default_binance_strategy_settings()
    integer_keys = {
        "maxSameSidePositions",
        "entryConfirmationExpiryBars",
        "entryFailureExitBars",
    }
    for key in settings:
        if key not in source or source[key] is None or source[key] == "":
            continue
        if key == "levelStrategy":
            strategy = str(source[key]).strip().upper()
            if strategy not in BINANCE_LEVEL_STRATEGIES:
                raise ValueError("点位策略路线必须选择结构极值或确认平台/区域")
            settings[key] = strategy
            continue
        if key == "strategyMode":
            strategy_mode = str(source[key]).strip().upper()
            if strategy_mode not in BINANCE_STRATEGY_MODES:
                raise ValueError("策略模式必须选择中线模式或短线模式")
            settings[key] = strategy_mode
            continue
        if key == "strategyEngine":
            engine = str(source[key]).strip().upper()
            if engine not in BINANCE_STRATEGY_ENGINES:
                raise ValueError("策略引擎必须选择经典策略或时序模型")
            settings[key] = engine
            continue
        if key == "modelBranch":
            branch = str(source[key]).strip().upper()
            if branch not in BINANCE_MODEL_BRANCHES:
                raise ValueError("时序模型分支必须选择最佳性能或稳定末期")
            settings[key] = branch
            continue
        if key == "modelRunId":
            run_id = str(source[key]).strip() if source[key] is not None else ""
            if len(run_id) > 160:
                raise ValueError("时序模型训练任务标识过长")
            settings[key] = run_id or None
            continue
        if key == "entryConfirmationMode":
            mode = str(source[key]).strip().upper()
            if mode not in BINANCE_ENTRY_CONFIRMATION_MODES:
                raise ValueError("入场确认方式必须选择回测确认或仅触发")
            settings[key] = mode
            continue
        if isinstance(source[key], bool):
            raise ValueError(f"{key}必须是数字")
        try:
            number = float(source[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key}必须是数字") from None
        if not math.isfinite(number):
            raise ValueError(f"{key}必须是有限数字")
        if key in integer_keys:
            if not number.is_integer():
                raise ValueError(f"{key}必须是整数")
            settings[key] = int(number)
        else:
            settings[key] = number

    if not 0 < settings["maxAccountLossRatio"] <= 100:
        raise ValueError("单笔结构风险比例必须在 0% 到 100% 之间")
    if not 0 < settings["maxPortfolioRiskRatio"] <= 100:
        raise ValueError("组合结构风险上限必须在 0% 到 100% 之间")
    if not 1 <= settings["maxSameSidePositions"] <= 20:
        raise ValueError("同向持仓上限必须在 1 到 20 之间")
    if not 0 < settings["dailyLossLimitRatio"] <= 100:
        raise ValueError("单日风险上限必须在 0% 到 100% 之间")
    if not 0.05 <= settings["rangeEdgeFraction"] < 0.5:
        raise ValueError("区间边缘比例必须在 0.05 到 0.5 之间")
    if not 0.5 <= settings["rangeMinimumTargetR"] <= 10:
        raise ValueError("区间最小目标 R 必须在 0.5 到 10 之间")
    if not 0.5 <= settings["trendMinimumTargetR"] <= 10:
        raise ValueError("趋势最小目标 R 必须在 0.5 到 10 之间")
    if not 1 <= settings["entryConfirmationExpiryBars"] <= 8:
        raise ValueError("入场确认有效K线数必须在 1 到 8 之间")
    if not 1 <= settings["entryFailureExitBars"] <= 8:
        raise ValueError("入场失败复核K线数必须在 1 到 8 之间")
    if not 0.1 <= settings["entryFailureBodyAtrMultiplier"] <= 5:
        raise ValueError("失败退出实体 ATR 倍数必须在 0.1 到 5 之间")
    if not 0 < settings["trailingAtrMultiplier"] <= 20:
        raise ValueError("移动止损 ATR 倍数必须在 0 到 20 之间")
    if not 0 <= settings["structureStopAtrMultiplier"] <= 10:
        raise ValueError("结构止损 ATR 缓冲必须在 0 到 10 之间")
    if not 0.1 <= settings["triggerZoneStopBufferAtrMultiplier"] <= 10:
        raise ValueError("触发区防假突破缓冲必须在 0.1 到 10 之间")
    if not 0 <= settings["breakevenBufferAtrMultiplier"] <= 10:
        raise ValueError("保本缓冲 ATR 倍数必须在 0 到 10 之间")
    if not 0 < settings["movingStopActivationR"] <= 20:
        raise ValueError("移动止损启动 R 倍数必须在 0 到 20 之间")
    if not 0.05 <= settings["nearTermMinimumTargetR"] <= 5:
        raise ValueError("近端止盈最低 R 必须在 0.05 到 5 之间")
    if not 1 <= settings["protectiveTakeProfitRatio"] < settings["firstTakeProfitRatio"] < 100:
        raise ValueError("近端保护止盈累计比例必须不低于 1%，且小于第一止盈累计比例")
    if not 1 <= settings["firstTakeProfitRatio"] < 100:
        raise ValueError("第一止盈累计比例必须在 1% 到 99% 之间")
    if not settings["firstTakeProfitRatio"] < settings["secondTakeProfitRatio"] < 100:
        raise ValueError("第二止盈累计比例必须大于第一止盈且小于 100%")
    return settings


def _binance_strategy_settings_profile(row: sqlite3.Row | None) -> dict:
    if not row:
        return {**default_binance_strategy_settings(), "updatedAt": None}
    columns = set(row.keys())
    profile = {
        "strategyEngine": (
            str(row["strategy_engine"]).strip().upper()
            if "strategy_engine" in columns and str(row["strategy_engine"] or "").strip().upper() in BINANCE_STRATEGY_ENGINES
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["strategyEngine"]
        ),
        "modelBranch": (
            str(row["model_branch"]).strip().upper()
            if "model_branch" in columns and str(row["model_branch"] or "").strip().upper() in BINANCE_MODEL_BRANCHES
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["modelBranch"]
        ),
        "modelRunId": (
            str(row["model_run_id"]).strip() or None
            if "model_run_id" in columns and row["model_run_id"] is not None
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["modelRunId"]
        ),
        "strategyMode": (
            str(row["strategy_mode"]).strip().upper()
            if "strategy_mode" in columns and str(row["strategy_mode"] or "").strip().upper() in BINANCE_STRATEGY_MODES
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["strategyMode"]
        ),
        "levelStrategy": (
            str(row["level_strategy"]).strip().upper()
            if "level_strategy" in columns and str(row["level_strategy"] or "").strip().upper() in BINANCE_LEVEL_STRATEGIES
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["levelStrategy"]
        ),
        "maxAccountLossRatio": float(row["max_account_loss_ratio"]),
        "maxPortfolioRiskRatio": (
            float(row["max_portfolio_risk_ratio"])
            if "max_portfolio_risk_ratio" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["maxPortfolioRiskRatio"]
        ),
        "maxSameSidePositions": (
            int(row["max_same_side_positions"])
            if "max_same_side_positions" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["maxSameSidePositions"]
        ),
        "dailyLossLimitRatio": (
            float(row["daily_loss_limit_ratio"])
            if "daily_loss_limit_ratio" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["dailyLossLimitRatio"]
        ),
        "rangeEdgeFraction": (
            float(row["range_edge_fraction"])
            if "range_edge_fraction" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["rangeEdgeFraction"]
        ),
        "rangeMinimumTargetR": (
            float(row["range_minimum_target_r"])
            if "range_minimum_target_r" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["rangeMinimumTargetR"]
        ),
        "trendMinimumTargetR": (
            float(row["trend_minimum_target_r"])
            if "trend_minimum_target_r" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["trendMinimumTargetR"]
        ),
        "entryConfirmationMode": (
            str(row["entry_confirmation_mode"] or "").strip().upper()
            if "entry_confirmation_mode" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["entryConfirmationMode"]
        ),
        "entryConfirmationExpiryBars": (
            int(row["entry_confirmation_expiry_bars"])
            if "entry_confirmation_expiry_bars" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["entryConfirmationExpiryBars"]
        ),
        "entryFailureExitBars": (
            int(row["entry_failure_exit_bars"])
            if "entry_failure_exit_bars" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["entryFailureExitBars"]
        ),
        "entryFailureBodyAtrMultiplier": (
            float(row["entry_failure_body_atr_multiplier"])
            if "entry_failure_body_atr_multiplier" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["entryFailureBodyAtrMultiplier"]
        ),
        "trailingAtrMultiplier": float(row["trailing_atr_multiplier"]),
        "structureStopAtrMultiplier": float(row["structure_stop_atr_multiplier"]),
        "triggerZoneStopBufferAtrMultiplier": (
            float(row["trigger_zone_stop_buffer_atr_multiplier"])
            if "trigger_zone_stop_buffer_atr_multiplier" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["triggerZoneStopBufferAtrMultiplier"]
        ),
        "breakevenBufferAtrMultiplier": float(row["breakeven_buffer_atr_multiplier"]),
        "movingStopActivationR": float(row["moving_stop_activation_r"]),
        "nearTermMinimumTargetR": (
            float(row["near_term_minimum_target_r"])
            if "near_term_minimum_target_r" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["nearTermMinimumTargetR"]
        ),
        "protectiveTakeProfitRatio": (
            float(row["protective_take_profit_ratio"])
            if "protective_take_profit_ratio" in columns
            else BINANCE_STRATEGY_SETTINGS_DEFAULTS["protectiveTakeProfitRatio"]
        ),
        "firstTakeProfitRatio": float(row["first_take_profit_ratio"]),
        "secondTakeProfitRatio": float(row["second_take_profit_ratio"]),
    }
    try:
        normalized = normalize_binance_strategy_settings(profile)
    except ValueError:
        normalized = default_binance_strategy_settings()
    return {**normalized, "updatedAt": row["updated_at"]}


def get_user_binance_strategy_settings(user_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT strategy_engine, model_branch, model_run_id, strategy_mode, level_strategy, max_account_loss_ratio, max_portfolio_risk_ratio,
                   max_same_side_positions, daily_loss_limit_ratio, range_edge_fraction,
                   range_minimum_target_r, trend_minimum_target_r, entry_confirmation_mode,
                   entry_confirmation_expiry_bars, entry_failure_exit_bars,
                   entry_failure_body_atr_multiplier, trailing_atr_multiplier,
                   structure_stop_atr_multiplier, trigger_zone_stop_buffer_atr_multiplier,
                   breakeven_buffer_atr_multiplier,
                   moving_stop_activation_r, near_term_minimum_target_r, protective_take_profit_ratio,
                   first_take_profit_ratio,
                   second_take_profit_ratio, updated_at
            FROM user_binance_strategy_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _binance_strategy_settings_profile(row)


def save_user_binance_strategy_settings(user_id: int, value: object) -> dict:
    settings = normalize_binance_strategy_settings(value)
    timestamp = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        conn.execute(
            """
            INSERT INTO user_binance_strategy_settings (
                user_id, strategy_engine, model_branch, model_run_id, strategy_mode, level_strategy, max_account_loss_ratio, max_portfolio_risk_ratio,
                max_same_side_positions, daily_loss_limit_ratio, range_edge_fraction,
                range_minimum_target_r, trend_minimum_target_r, entry_confirmation_mode,
                entry_confirmation_expiry_bars, entry_failure_exit_bars,
                entry_failure_body_atr_multiplier, trailing_atr_multiplier,
                structure_stop_atr_multiplier, trigger_zone_stop_buffer_atr_multiplier,
                breakeven_buffer_atr_multiplier,
                moving_stop_activation_r, near_term_minimum_target_r, protective_take_profit_ratio,
                first_take_profit_ratio,
                second_take_profit_ratio, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                strategy_engine = excluded.strategy_engine,
                model_branch = excluded.model_branch,
                model_run_id = excluded.model_run_id,
                strategy_mode = excluded.strategy_mode,
                level_strategy = excluded.level_strategy,
                max_account_loss_ratio = excluded.max_account_loss_ratio,
                max_portfolio_risk_ratio = excluded.max_portfolio_risk_ratio,
                max_same_side_positions = excluded.max_same_side_positions,
                daily_loss_limit_ratio = excluded.daily_loss_limit_ratio,
                range_edge_fraction = excluded.range_edge_fraction,
                range_minimum_target_r = excluded.range_minimum_target_r,
                trend_minimum_target_r = excluded.trend_minimum_target_r,
                entry_confirmation_mode = excluded.entry_confirmation_mode,
                entry_confirmation_expiry_bars = excluded.entry_confirmation_expiry_bars,
                entry_failure_exit_bars = excluded.entry_failure_exit_bars,
                entry_failure_body_atr_multiplier = excluded.entry_failure_body_atr_multiplier,
                trailing_atr_multiplier = excluded.trailing_atr_multiplier,
                structure_stop_atr_multiplier = excluded.structure_stop_atr_multiplier,
                trigger_zone_stop_buffer_atr_multiplier = excluded.trigger_zone_stop_buffer_atr_multiplier,
                breakeven_buffer_atr_multiplier = excluded.breakeven_buffer_atr_multiplier,
                moving_stop_activation_r = excluded.moving_stop_activation_r,
                near_term_minimum_target_r = excluded.near_term_minimum_target_r,
                protective_take_profit_ratio = excluded.protective_take_profit_ratio,
                first_take_profit_ratio = excluded.first_take_profit_ratio,
                second_take_profit_ratio = excluded.second_take_profit_ratio,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                settings["strategyEngine"],
                settings["modelBranch"],
                settings["modelRunId"],
                settings["strategyMode"],
                settings["levelStrategy"],
                settings["maxAccountLossRatio"],
                settings["maxPortfolioRiskRatio"],
                settings["maxSameSidePositions"],
                settings["dailyLossLimitRatio"],
                settings["rangeEdgeFraction"],
                settings["rangeMinimumTargetR"],
                settings["trendMinimumTargetR"],
                settings["entryConfirmationMode"],
                settings["entryConfirmationExpiryBars"],
                settings["entryFailureExitBars"],
                settings["entryFailureBodyAtrMultiplier"],
                settings["trailingAtrMultiplier"],
                settings["structureStopAtrMultiplier"],
                settings["triggerZoneStopBufferAtrMultiplier"],
                settings["breakevenBufferAtrMultiplier"],
                settings["movingStopActivationR"],
                settings["nearTermMinimumTargetR"],
                settings["protectiveTakeProfitRatio"],
                settings["firstTakeProfitRatio"],
                settings["secondTakeProfitRatio"],
                timestamp,
            ),
        )
        row = conn.execute(
            """
            SELECT strategy_engine, model_branch, model_run_id, strategy_mode, level_strategy, max_account_loss_ratio, max_portfolio_risk_ratio,
                   max_same_side_positions, daily_loss_limit_ratio, range_edge_fraction,
                   range_minimum_target_r, trend_minimum_target_r, entry_confirmation_mode,
                   entry_confirmation_expiry_bars, entry_failure_exit_bars,
                   entry_failure_body_atr_multiplier, trailing_atr_multiplier,
                   structure_stop_atr_multiplier, trigger_zone_stop_buffer_atr_multiplier,
                   breakeven_buffer_atr_multiplier,
                   moving_stop_activation_r, near_term_minimum_target_r, protective_take_profit_ratio,
                   first_take_profit_ratio,
                   second_take_profit_ratio, updated_at
            FROM user_binance_strategy_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _binance_strategy_settings_profile(row)


BINANCE_COPY_TRADING_SETTINGS_DEFAULTS = {
    "topTraderId": "5132388877263187456",
    "sourceTotalMargin": 0.0,
    "copyMultiplier": 1,
}
SMART_MONEY_AUTH_KEYS = (
    "cookie",
    "csrfToken",
    "deviceInfo",
    "fvideoId",
    "fvideoToken",
    "xPassthroughToken",
    "referer",
)


def normalize_binance_copy_trading_settings(value: object = None) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    settings = dict(BINANCE_COPY_TRADING_SETTINGS_DEFAULTS)
    if source.get("topTraderId") not in (None, ""):
        top_trader_id = str(source["topTraderId"]).strip()
        if not top_trader_id.isdigit() or len(top_trader_id) > 32:
            raise ValueError("聪明钱用户 ID 格式无效")
        settings["topTraderId"] = top_trader_id
    if "sourceTotalMargin" in source and source["sourceTotalMargin"] not in (None, ""):
        if isinstance(source["sourceTotalMargin"], bool):
            raise ValueError("sourceTotalMargin必须是数字")
        try:
            source_total_margin = float(source["sourceTotalMargin"])
        except (TypeError, ValueError):
            raise ValueError("sourceTotalMargin必须是数字") from None
        if not math.isfinite(source_total_margin):
            raise ValueError("sourceTotalMargin必须是有限数字")
        settings["sourceTotalMargin"] = source_total_margin
    if "copyMultiplier" in source and source["copyMultiplier"] not in (None, ""):
        if isinstance(source["copyMultiplier"], bool):
            raise ValueError("跟单倍率必须是 1 到 10 的整数")
        try:
            copy_multiplier = float(source["copyMultiplier"])
        except (TypeError, ValueError):
            raise ValueError("跟单倍率必须是 1 到 10 的整数") from None
        if not math.isfinite(copy_multiplier) or not copy_multiplier.is_integer():
            raise ValueError("跟单倍率必须是 1 到 10 的整数")
        settings["copyMultiplier"] = int(copy_multiplier)
    if settings["sourceTotalMargin"] < 0:
        raise ValueError("聪明钱总保证金不能小于 0")
    if not 1 <= settings["copyMultiplier"] <= 10:
        raise ValueError("跟单倍率必须是 1 到 10 的整数")
    return settings


def _binance_copy_trading_settings_profile(row: sqlite3.Row | None, copy_multipliers: dict[str, float] | None = None) -> dict:
    multipliers = copy_multipliers or {}
    if row is None:
        return {
            **BINANCE_COPY_TRADING_SETTINGS_DEFAULTS,
            "copyMultipliers": multipliers,
            "smartMoneyAuthConfigured": False,
            "smartMoneyAuthUpdatedAt": None,
            "updatedAt": None,
        }
    top_trader_id = str(row["smart_money_top_trader_id"] or BINANCE_COPY_TRADING_SETTINGS_DEFAULTS["topTraderId"])
    return {
        "sourceTotalMargin": float(row["source_total_margin"]),
        "topTraderId": top_trader_id,
        "copyMultiplier": float(multipliers.get(top_trader_id, 1.0)),
        "copyMultipliers": multipliers,
        "smartMoneyAuthConfigured": bool(str(row["smart_money_auth_encrypted"] or "").strip()),
        "smartMoneyAuthUpdatedAt": row["updated_at"] if str(row["smart_money_auth_encrypted"] or "").strip() else None,
        "updatedAt": row["updated_at"],
    }


def _normalize_smart_money_auth(value: object) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    max_lengths = {"cookie": 20000, "csrfToken": 2048, "deviceInfo": 4096, "fvideoId": 512, "fvideoToken": 4096, "xPassthroughToken": 4096, "referer": 512}
    normalized: dict[str, str] = {}
    for key in SMART_MONEY_AUTH_KEYS:
        raw = source.get(key)
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        if len(text) > max_lengths[key]:
            raise ValueError(f"Smart Money {key}长度过长")
        normalized[key] = text
    return normalized


def _decrypt_smart_money_auth(encrypted: object) -> dict[str, str]:
    value = str(encrypted or "").strip()
    if not value:
        return {}
    try:
        payload = json.loads(_binance_fernet().decrypt(value.encode("ascii")).decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Smart Money 登录态无法解密，请重新保存") from exc
    return _normalize_smart_money_auth(payload)


def get_user_binance_smart_money_auth(user_id: int, *, include_secrets: bool = False) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT smart_money_auth_encrypted, updated_at FROM user_binance_copy_trading_settings WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    encrypted = row["smart_money_auth_encrypted"] if row else ""
    return {
        "configured": bool(str(encrypted or "").strip()),
        "updatedAt": row["updated_at"] if row and str(encrypted or "").strip() else None,
        **(_decrypt_smart_money_auth(encrypted) if include_secrets else {}),
    }


def list_user_binance_smart_money_auth() -> list[dict]:
    """Return decrypted Smart Money auth for the background refresh only."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, smart_money_top_trader_id, smart_money_auth_encrypted FROM user_binance_copy_trading_settings "
            "WHERE TRIM(COALESCE(smart_money_auth_encrypted, '')) <> ''"
        ).fetchall()
    result = []
    for row in rows:
        try:
            auth = _decrypt_smart_money_auth(row["smart_money_auth_encrypted"])
        except RuntimeError:
            continue
        if auth:
            result.append({
                "userId": int(row["user_id"]),
                "topTraderId": str(row["smart_money_top_trader_id"] or BINANCE_COPY_TRADING_SETTINGS_DEFAULTS["topTraderId"]),
                "auth": auth,
            })
    return result


def _user_binance_smart_money_copy_multipliers(conn: sqlite3.Connection, user_id: int) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT top_trader_id, copy_multiplier
        FROM user_binance_smart_money_trader_settings
        WHERE user_id = ?
        """,
        (int(user_id),),
    ).fetchall()
    return {str(row["top_trader_id"]): float(row["copy_multiplier"]) for row in rows}


def ensure_user_binance_smart_money_trader_settings(user_id: int, top_trader_ids: list[object]) -> None:
    """Create default multipliers for newly discovered subscriptions."""

    trader_ids = list(dict.fromkeys(
        str(value or "").strip()
        for value in top_trader_ids
        if str(value or "").strip()
    ))
    if not trader_ids:
        return
    timestamp = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        conn.executemany(
            """
            INSERT OR IGNORE INTO user_binance_smart_money_trader_settings
                (user_id, top_trader_id, copy_multiplier, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            [(int(user_id), trader_id, timestamp, timestamp) for trader_id in trader_ids],
        )


def get_user_binance_copy_trading_settings(user_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT smart_money_top_trader_id, source_total_margin, copy_multiplier, smart_money_auth_encrypted, updated_at
            FROM user_binance_copy_trading_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
        copy_multipliers = _user_binance_smart_money_copy_multipliers(conn, user_id)
    return _binance_copy_trading_settings_profile(row, copy_multipliers)


def list_user_binance_smart_money_position_follows(
    user_id: int,
    top_trader_id: object | None = None,
) -> list[dict]:
    trader_filter = str(top_trader_id or "").strip()
    query = """
        SELECT top_trader_id, symbol, position_side, created_at, updated_at
        FROM user_binance_smart_money_position_follows
        WHERE user_id = ?
    """
    params: list[object] = [int(user_id)]
    if trader_filter:
        query += " AND top_trader_id = ?"
        params.append(trader_filter)
    query += " ORDER BY updated_at DESC, symbol ASC, position_side ASC"
    with get_connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [
        {
            "topTraderId": str(row["top_trader_id"]),
            "symbol": str(row["symbol"]),
            "positionSide": str(row["position_side"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        for row in rows
    ]


def set_user_binance_smart_money_position_follow(
    user_id: int,
    *,
    top_trader_id: object,
    symbol: object,
    position_side: object,
    enabled: bool,
) -> list[dict]:
    trader_id = str(top_trader_id or "").strip()
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_side = str(position_side or "").strip().upper()
    if not trader_id:
        raise ValueError("聪明钱用户 ID 不能为空")
    if not normalized_symbol:
        raise ValueError("合约不能为空")
    if normalized_side not in {"LONG", "SHORT"}:
        raise ValueError("持仓方向必须是 LONG 或 SHORT")
    timestamp = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        if enabled:
            conn.execute(
                """
                INSERT INTO user_binance_smart_money_position_follows
                    (user_id, top_trader_id, symbol, position_side, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, top_trader_id, symbol, position_side) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (int(user_id), trader_id, normalized_symbol, normalized_side, timestamp, timestamp),
            )
        else:
            conn.execute(
                """
                DELETE FROM user_binance_smart_money_position_follows
                WHERE user_id = ? AND top_trader_id = ? AND symbol = ? AND position_side = ?
                """,
                (int(user_id), trader_id, normalized_symbol, normalized_side),
            )
    return list_user_binance_smart_money_position_follows(user_id, trader_id)


def _binance_futures_position_history_key(position: object) -> tuple[str, str] | None:
    if not isinstance(position, dict):
        return None
    symbol = str(position.get("symbol") or position.get("pair") or "").strip().upper()
    if not symbol:
        return None
    raw_side = str(position.get("side") or position.get("positionSide") or "").strip().upper()
    if raw_side in {"BUY", "LONG"}:
        side = "LONG"
    elif raw_side in {"SELL", "SHORT"}:
        side = "SHORT"
    else:
        try:
            quantity = float(position.get("quantity") or position.get("positionAmt") or position.get("amount") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity == 0:
            return None
        side = "LONG" if quantity > 0 else "SHORT"
    try:
        quantity = abs(float(position.get("quantity") or position.get("positionAmt") or position.get("amount") or 0))
    except (TypeError, ValueError):
        quantity = 0
    return (symbol, side) if quantity > 0 else None


BINANCE_POSITION_PNL_HISTORY_INTERVAL_MS = 60_000


def list_user_binance_futures_position_pnl_history(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol, position_side, recorded_at, unrealized_pnl
            FROM user_binance_futures_position_pnl_history
            WHERE user_id = ?
            ORDER BY symbol ASC, position_side ASC, recorded_at ASC
            """,
            (int(user_id),),
        ).fetchall()
    grouped_by_minute: dict[tuple[str, str], dict[int, float]] = {}
    for row in rows:
        key = (str(row["symbol"]), str(row["position_side"]))
        minute = int(row["recorded_at"]) // BINANCE_POSITION_PNL_HISTORY_INTERVAL_MS * BINANCE_POSITION_PNL_HISTORY_INTERVAL_MS
        # Rows are ordered by timestamp, so legacy sub-minute samples collapse
        # to the latest value without changing their stored history.
        grouped_by_minute.setdefault(key, {})[minute] = float(row["unrealized_pnl"])

    grouped: dict[tuple[str, str], list[dict]] = {}
    for key, minute_values in grouped_by_minute.items():
        points: list[dict] = []
        for minute, pnl in sorted(minute_values.items()):
            if points:
                previous = points[-1]
                missing_minute = previous["recordedAt"] + BINANCE_POSITION_PNL_HISTORY_INTERVAL_MS
                while missing_minute < minute:
                    points.append({"recordedAt": missing_minute, "unrealizedPnl": previous["unrealizedPnl"]})
                    missing_minute += BINANCE_POSITION_PNL_HISTORY_INTERVAL_MS
            points.append({"recordedAt": minute, "unrealizedPnl": pnl})
        grouped[key] = points
    return [
        {"symbol": symbol, "positionSide": position_side, "points": points}
        for (symbol, position_side), points in grouped.items()
    ]


def record_user_binance_futures_position_pnl_history(
    user_id: int,
    positions: object,
    recorded_at: int | None = None,
) -> list[dict]:
    timestamp = int(recorded_at or time.time() * 1000)
    timestamp = timestamp // BINANCE_POSITION_PNL_HISTORY_INTERVAL_MS * BINANCE_POSITION_PNL_HISTORY_INTERVAL_MS
    active: dict[tuple[str, str], float] = {}
    realized_components: dict[tuple[str, str], float] = {}
    active_keys: set[tuple[str, str]] = set()
    for position in positions if isinstance(positions, list) else []:
        key = _binance_futures_position_history_key(position)
        if key is None:
            continue
        active_keys.add(key)
        try:
            unrealized_pnl = float(
                position.get("unrealizedProfit")
                if position.get("unrealizedProfit") is not None
                else position.get("unrealizedPnl")
                if position.get("unrealizedPnl") is not None
                else position.get("unRealizedProfit") or 0
            )
        except (TypeError, ValueError):
            continue
        if not math.isfinite(unrealized_pnl):
            continue
        raw_realized = position.get("realizedPnl")
        try:
            realized_pnl = float(raw_realized) if raw_realized not in (None, "") else None
        except (TypeError, ValueError):
            realized_pnl = None
        if realized_pnl is not None and not math.isfinite(realized_pnl):
            realized_pnl = None
        # A temporarily unavailable trade-history response must not erase the
        # last known realized component and make the total curve jump down.
        realized_components[key] = realized_pnl if realized_pnl is not None else math.nan
        active[key] = unrealized_pnl
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (int(user_id),)).fetchone():
            return []
        if active_keys:
            clauses = " OR ".join("(symbol = ? AND position_side = ?)" for _ in active_keys)
            params: list[object] = [int(user_id)]
            for symbol, side in active_keys:
                params.extend([symbol, side])
            conn.execute(
                f"DELETE FROM user_binance_futures_position_pnl_history WHERE user_id = ? AND NOT ({clauses})",
                tuple(params),
            )
            for key, realized_pnl in list(realized_components.items()):
                if not math.isnan(realized_pnl):
                    continue
                previous = conn.execute(
                    """
                    SELECT realized_pnl
                    FROM user_binance_futures_position_pnl_history
                    WHERE user_id = ? AND symbol = ? AND position_side = ?
                    ORDER BY recorded_at DESC
                    LIMIT 1
                    """,
                    (int(user_id), key[0], key[1]),
                ).fetchone()
                try:
                    fallback = float(previous["realized_pnl"]) if previous and previous["realized_pnl"] is not None else 0.0
                except (TypeError, ValueError):
                    fallback = 0.0
                realized_components[key] = fallback
            conn.executemany(
                """
                INSERT INTO user_binance_futures_position_pnl_history
                    (user_id, symbol, position_side, recorded_at, unrealized_pnl, realized_pnl)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, symbol, position_side, recorded_at) DO UPDATE SET
                    unrealized_pnl = excluded.unrealized_pnl,
                    realized_pnl = excluded.realized_pnl
                """,
                [
                    (
                        int(user_id),
                        symbol,
                        side,
                        timestamp,
                        unrealized_pnl + realized_components[(symbol, side)],
                        realized_components[(symbol, side)],
                    )
                    for (symbol, side), unrealized_pnl in active.items()
                ],
            )
        else:
            conn.execute(
                "DELETE FROM user_binance_futures_position_pnl_history WHERE user_id = ?",
                (int(user_id),),
            )
    return list_user_binance_futures_position_pnl_history(user_id)


def save_user_binance_copy_trading_settings(user_id: int, value: object) -> dict:
    settings = normalize_binance_copy_trading_settings(value)
    source = value if isinstance(value, dict) else {}
    auth_payload = value.get("smartMoneyAuth") if isinstance(value, dict) else None
    direct_auth = {key: value.get(key) for key in SMART_MONEY_AUTH_KEYS} if isinstance(value, dict) else {}
    if isinstance(auth_payload, dict):
        direct_auth.update(auth_payload)
    has_auth_update = any(key in direct_auth and direct_auth[key] not in (None, "") for key in SMART_MONEY_AUTH_KEYS)
    has_copy_multiplier_update = "copyMultiplier" in source and source.get("copyMultiplier") not in (None, "")
    timestamp = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        existing = conn.execute(
            "SELECT smart_money_top_trader_id, source_total_margin, copy_multiplier, smart_money_auth_encrypted FROM user_binance_copy_trading_settings WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if isinstance(value, dict) and value.get("clearSmartMoneyAuth"):
            encrypted_auth = ""
        elif has_auth_update:
            existing_auth = _decrypt_smart_money_auth(existing["smart_money_auth_encrypted"]) if existing else {}
            existing_auth.update(_normalize_smart_money_auth(direct_auth))
            if not existing_auth.get("cookie") or not existing_auth.get("csrfToken"):
                raise ValueError("Smart Money 登录态至少需要 Cookie 和 csrftoken")
            encrypted_auth = _binance_fernet().encrypt(json.dumps(existing_auth, ensure_ascii=False).encode("utf-8")).decode("ascii") if existing_auth else ""
        else:
            encrypted_auth = str(existing["smart_money_auth_encrypted"] or "") if existing else ""
        source_total_margin = float(existing["source_total_margin"] or 0) if existing and "sourceTotalMargin" not in source else settings["sourceTotalMargin"]
        top_trader_id = settings["topTraderId"]
        legacy_copy_multiplier = float(existing["copy_multiplier"] or 1) if existing else 1.0
        conn.execute(
            """
            INSERT INTO user_binance_copy_trading_settings (
                user_id, smart_money_top_trader_id, source_total_margin, copy_multiplier, smart_money_auth_encrypted, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                smart_money_top_trader_id = excluded.smart_money_top_trader_id,
                source_total_margin = excluded.source_total_margin,
                smart_money_auth_encrypted = excluded.smart_money_auth_encrypted,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                top_trader_id,
                source_total_margin,
                legacy_copy_multiplier,
                encrypted_auth,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO user_binance_smart_money_trader_settings
                (user_id, top_trader_id, copy_multiplier, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (int(user_id), top_trader_id, timestamp, timestamp),
        )
        if has_copy_multiplier_update:
            conn.execute(
                """
                UPDATE user_binance_smart_money_trader_settings
                SET copy_multiplier = ?, updated_at = ?
                WHERE user_id = ? AND top_trader_id = ?
                """,
                (settings["copyMultiplier"], timestamp, int(user_id), top_trader_id),
            )
        row = conn.execute(
            """
            SELECT smart_money_top_trader_id, source_total_margin, copy_multiplier, smart_money_auth_encrypted, updated_at
            FROM user_binance_copy_trading_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
        copy_multipliers = _user_binance_smart_money_copy_multipliers(conn, user_id)
    return _binance_copy_trading_settings_profile(row, copy_multipliers)


def default_binance_connection_settings() -> dict[str, str]:
    """Return a fresh copy of the per-account transport defaults."""

    return dict(BINANCE_CONNECTION_SETTINGS_DEFAULTS)


def normalize_binance_connection_settings(value: object = None) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    raw_mode = source.get("connectionMode", source.get("mode", "REST"))
    mode = str(raw_mode or "REST").strip().upper().replace(" ", "")
    if mode in {"WS", "WEBSOCKET", "SOCKET"}:
        mode = "WEBSOCKET"
    elif mode in {"REST", "HTTP"}:
        mode = "REST"
    else:
        raise ValueError("连接方式必须选择 REST 或 WebSocket")
    return {"connectionMode": mode}


def _binance_connection_settings_profile(row: sqlite3.Row | None) -> dict[str, str | None]:
    if not row:
        return {**default_binance_connection_settings(), "updatedAt": None}
    raw_mode = str(row["connection_mode"] or "REST").strip().upper()
    mode = raw_mode if raw_mode in BINANCE_CONNECTION_MODES else "REST"
    return {
        "connectionMode": mode,
        "updatedAt": row["updated_at"],
    }


def get_user_binance_connection_settings(user_id: int) -> dict[str, str | None]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT connection_mode, updated_at
            FROM user_binance_connection_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _binance_connection_settings_profile(row)


def save_user_binance_connection_settings(user_id: int, value: object) -> dict[str, str | None]:
    settings = normalize_binance_connection_settings(value)
    timestamp = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        conn.execute(
            """
            INSERT INTO user_binance_connection_settings (user_id, connection_mode, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                connection_mode = excluded.connection_mode,
                updated_at = excluded.updated_at
            """,
            (int(user_id), settings["connectionMode"], timestamp),
        )
        row = conn.execute(
            """
            SELECT connection_mode, updated_at
            FROM user_binance_connection_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _binance_connection_settings_profile(row)


def default_binance_ui_settings() -> dict[str, float]:
    return {"positionPnlSmoothing": 0.25}


def normalize_binance_ui_settings(value: object = None) -> dict[str, float]:
    source = value if isinstance(value, dict) else {}
    raw_value = source.get("positionPnlSmoothing", 0.25)
    try:
        smoothing = float(raw_value)
    except (TypeError, ValueError):
        raise ValueError("盈亏走势平滑值无效")
    if not math.isfinite(smoothing) or smoothing < 0 or smoothing > 1:
        raise ValueError("盈亏走势平滑值必须在 0 到 1 之间")
    return {"positionPnlSmoothing": round(smoothing, 4)}


def _binance_ui_settings_profile(row: sqlite3.Row | None) -> dict[str, float | str | None]:
    if not row:
        return {**default_binance_ui_settings(), "updatedAt": None}
    try:
        smoothing = float(row["position_pnl_smoothing"])
    except (TypeError, ValueError):
        smoothing = 0.25
    if not math.isfinite(smoothing) or smoothing < 0 or smoothing > 1:
        smoothing = 0.25
    return {
        "positionPnlSmoothing": smoothing,
        "updatedAt": row["updated_at"],
    }


def get_user_binance_ui_settings(user_id: int) -> dict[str, float | str | None]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT position_pnl_smoothing, updated_at
            FROM user_binance_ui_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _binance_ui_settings_profile(row)


def save_user_binance_ui_settings(user_id: int, value: object) -> dict[str, float | str | None]:
    settings = normalize_binance_ui_settings(value)
    timestamp = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        conn.execute(
            """
            INSERT INTO user_binance_ui_settings (user_id, position_pnl_smoothing, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                position_pnl_smoothing = excluded.position_pnl_smoothing,
                updated_at = excluded.updated_at
            """,
            (int(user_id), settings["positionPnlSmoothing"], timestamp),
        )
        row = conn.execute(
            """
            SELECT position_pnl_smoothing, updated_at
            FROM user_binance_ui_settings
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _binance_ui_settings_profile(row)


def list_user_binance_connection_modes() -> dict[int, str]:
    """Return transport preferences without decrypting account secrets."""

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id, connection_mode
            FROM user_binance_connection_settings
            """
        ).fetchall()
    return {
        int(row["user_id"]): (
            str(row["connection_mode"] or "REST").strip().upper()
            if str(row["connection_mode"] or "REST").strip().upper() in BINANCE_CONNECTION_MODES
            else "REST"
        )
        for row in rows
    }


def _binance_fernet() -> Fernet:
    raw_key = str(os.environ.get(BINANCE_CREDENTIAL_KEY_ENV) or "").strip()
    if not raw_key:
        raise RuntimeError(f"未配置 {BINANCE_CREDENTIAL_KEY_ENV}，无法安全保存 Binance 凭据")
    try:
        return Fernet(raw_key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{BINANCE_CREDENTIAL_KEY_ENV} 格式无效") from exc


def ensure_binance_credential_storage() -> None:
    _binance_fernet()


def _normalize_binance_network(value: object) -> str:
    network = str(value or "mainnet").strip().lower()
    if network != "mainnet":
        raise ValueError("Binance 账户 API 仅支持主网")
    return "mainnet"


def _normalize_binance_credential(value: object, label: str) -> str:
    credential = str(value or "").strip()
    if not credential:
        raise ValueError(f"请填写 Binance {label}")
    if len(credential) > 256:
        raise ValueError(f"Binance {label}长度不能超过 256 个字符")
    return credential


def _mask_binance_key(value: str) -> str:
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _binance_credentials_profile(row: sqlite3.Row | None, api_key: str = "") -> dict:
    requires_reconfiguration = bool(row and row["network"] != "mainnet")
    return {
        "configured": bool(row) and not requires_reconfiguration,
        "network": "mainnet",
        "apiKeyMasked": _mask_binance_key(api_key) if api_key else "",
        "updatedAt": row["updated_at"] if row else None,
        "requiresReconfiguration": requires_reconfiguration,
    }


def _decrypt_binance_credential(fernet: Fernet, encrypted: str, label: str) -> str:
    try:
        return fernet.decrypt(str(encrypted).encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"Binance {label}无法解密，请重新连接 API") from exc


def get_user_binance_credentials(user_id: int, include_secrets: bool = False) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT network, api_key_encrypted, api_secret_encrypted, updated_at
            FROM user_binance_credentials
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    if not row:
        profile = _binance_credentials_profile(None)
        profile["connectionMode"] = get_user_binance_connection_settings(user_id)["connectionMode"]
        return profile

    profile = _binance_credentials_profile(row)
    profile["connectionMode"] = get_user_binance_connection_settings(user_id)["connectionMode"]
    if not profile["configured"]:
        return profile
    fernet = _binance_fernet()
    api_key = _decrypt_binance_credential(fernet, row["api_key_encrypted"], "API Key")
    profile["apiKeyMasked"] = _mask_binance_key(api_key)
    if include_secrets:
        profile["apiKey"] = api_key
        profile["apiSecret"] = _decrypt_binance_credential(fernet, row["api_secret_encrypted"], "Secret Key")
    return profile


def save_user_binance_credentials(
    user_id: int,
    *,
    network: object,
    api_key: object,
    api_secret: object,
) -> dict:
    normalized_network = _normalize_binance_network(network)
    normalized_api_key = _normalize_binance_credential(api_key, "API Key")
    normalized_api_secret = _normalize_binance_credential(api_secret, "Secret Key")
    fernet = _binance_fernet()
    timestamp = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = ? AND is_system = 0",
            (int(user_id),),
        ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        conn.execute(
            """
            INSERT INTO user_binance_credentials (
                user_id, network, api_key_encrypted, api_secret_encrypted, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                network = excluded.network,
                api_key_encrypted = excluded.api_key_encrypted,
                api_secret_encrypted = excluded.api_secret_encrypted,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                normalized_network,
                fernet.encrypt(normalized_api_key.encode("utf-8")).decode("ascii"),
                fernet.encrypt(normalized_api_secret.encode("utf-8")).decode("ascii"),
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT network, api_key_encrypted, api_secret_encrypted, updated_at FROM user_binance_credentials WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    return {
        "configured": True,
        "network": "mainnet",
        "apiKeyMasked": _mask_binance_key(normalized_api_key),
        "updatedAt": row["updated_at"],
        "requiresReconfiguration": False,
        "connectionMode": get_user_binance_connection_settings(user_id)["connectionMode"],
    }


def list_user_binance_credentials_for_refresh() -> list[dict]:
    """Return decrypted mainnet credentials for the backend account refresher."""

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.user_id, c.network, c.api_key_encrypted, c.api_secret_encrypted,
                   COALESCE(s.connection_mode, 'REST') AS connection_mode
            FROM user_binance_credentials AS c
            LEFT JOIN user_binance_connection_settings AS s ON s.user_id = c.user_id
            WHERE c.network = 'mainnet'
            """
        ).fetchall()
    fernet = _binance_fernet()
    credentials = []
    for row in rows:
        try:
            credentials.append(
                {
                    "userId": int(row["user_id"]),
                    "network": "mainnet",
                    "apiKey": _decrypt_binance_credential(fernet, row["api_key_encrypted"], "API Key"),
                    "apiSecret": _decrypt_binance_credential(fernet, row["api_secret_encrypted"], "Secret Key"),
                    "connectionMode": (
                        str(row["connection_mode"] or "REST").strip().upper()
                        if str(row["connection_mode"] or "REST").strip().upper() in BINANCE_CONNECTION_MODES
                        else "REST"
                    ),
                }
            )
        except RuntimeError:
            continue
    return credentials


def delete_user_binance_credentials(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM user_binance_credentials WHERE user_id = ?", (int(user_id),))
    return cursor.rowcount > 0


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _claim_legacy_personal_data(conn: sqlite3.Connection, user_id: int, timestamp: str) -> None:
    pending = conn.execute(
        "SELECT value FROM app_metadata WHERE key = 'legacy_personal_data_pending'"
    ).fetchone()
    if not pending or pending["value"] != "1":
        return
    legacy_owner = conn.execute(
        "SELECT id FROM users WHERE username = ? AND is_system = 1",
        (LEGACY_OWNER_USERNAME,),
    ).fetchone()
    if not legacy_owner:
        return
    legacy_user_id = int(legacy_owner["id"])
    for table_name in _PERSONAL_DATA_TABLES:
        conn.execute(f'UPDATE "{table_name}" SET user_id = ? WHERE user_id = ?', (user_id, legacy_user_id))
    conn.execute("DELETE FROM users WHERE id = ?", (legacy_user_id,))
    conn.execute(
        "UPDATE app_metadata SET value = 'claimed', updated_at = ? WHERE key = 'legacy_personal_data_pending'",
        (timestamp,),
    )


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def is_fresh(updated_at: str | None, max_age: timedelta) -> bool:
    if not updated_at:
        return False
    try:
        return datetime.fromisoformat(updated_at) >= datetime.now() - max_age
    except ValueError:
        return False


def upsert_securities(items: list[dict]) -> None:
    if not items:
        return
    updated_at = now_iso()
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO securities (
                symbol, name, market, secid, secucode, is_st, latest_price, pct_change,
                turnover_rate, pe_dynamic, pb, total_market_cap, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                name=excluded.name,
                market=excluded.market,
                secid=excluded.secid,
                secucode=excluded.secucode,
                is_st=excluded.is_st,
                latest_price=excluded.latest_price,
                pct_change=excluded.pct_change,
                turnover_rate=excluded.turnover_rate,
                pe_dynamic=excluded.pe_dynamic,
                pb=excluded.pb,
                total_market_cap=excluded.total_market_cap,
                updated_at=excluded.updated_at
            """,
            [
                (
                    item.get("symbol"),
                    item.get("name"),
                    item.get("market"),
                    item.get("secid"),
                    item.get("secucode"),
                    1 if item.get("isSt") else 0,
                    item.get("latestPrice"),
                    item.get("pctChange"),
                    item.get("turnoverRate"),
                    item.get("peDynamic"),
                    item.get("pb"),
                    item.get("totalMarketCap"),
                    updated_at,
                )
                for item in items
            ],
        )


def list_securities() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM securities ORDER BY symbol").fetchall()
    return [_security_item(row) for row in rows]


def get_security(symbol: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM securities WHERE symbol = ?", (str(symbol or "").strip(),)).fetchone()
    return _security_item(row) if row else None


def search_securities(keyword: str, limit: int = 20) -> list[dict]:
    word = f"%{keyword.lower()}%"
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM securities
            WHERE lower(symbol) LIKE ? OR lower(name) LIKE ?
            ORDER BY symbol
            LIMIT ?
            """,
            (word, word, limit),
        ).fetchall()
    return [_security_item(row) for row in rows]


def price_limited_non_st(min_price: float, max_price: float) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM securities
            WHERE is_st = 0 AND latest_price IS NOT NULL AND latest_price > 0
              AND latest_price >= ? AND latest_price <= ?
            ORDER BY RANDOM()
            """,
            (min_price, max_price),
        ).fetchall()
    return [_security_item(row) for row in rows]


def securities_are_fresh(max_age: timedelta) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count, MAX(updated_at) AS updated_at FROM securities").fetchone()
    return bool(row and row["count"] and is_fresh(row["updated_at"], max_age))


def upsert_snapshot(snapshot: dict) -> None:
    if not snapshot.get("symbol"):
        return
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO stock_snapshots (
                symbol, name, latest_price, price_change, pct_change, open, high, low,
                previous_close, volume, amount, volume_ratio, turnover_rate, total_market_cap,
                circulating_market_cap, pe_dynamic, pe_static, pe_ttm, pb, nav_per_share,
                raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                name=excluded.name,
                latest_price=excluded.latest_price,
                price_change=excluded.price_change,
                pct_change=excluded.pct_change,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                previous_close=excluded.previous_close,
                volume=excluded.volume,
                amount=excluded.amount,
                volume_ratio=excluded.volume_ratio,
                turnover_rate=excluded.turnover_rate,
                total_market_cap=excluded.total_market_cap,
                circulating_market_cap=excluded.circulating_market_cap,
                pe_dynamic=excluded.pe_dynamic,
                pe_static=excluded.pe_static,
                pe_ttm=excluded.pe_ttm,
                pb=excluded.pb,
                nav_per_share=excluded.nav_per_share,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                snapshot.get("symbol"),
                snapshot.get("name"),
                snapshot.get("latestPrice"),
                snapshot.get("priceChange"),
                snapshot.get("pctChange"),
                snapshot.get("open"),
                snapshot.get("high"),
                snapshot.get("low"),
                snapshot.get("previousClose"),
                snapshot.get("volume"),
                snapshot.get("amount"),
                snapshot.get("volumeRatio"),
                snapshot.get("turnoverRate"),
                snapshot.get("totalMarketCap"),
                snapshot.get("circulatingMarketCap"),
                snapshot.get("peDynamic"),
                snapshot.get("peStatic"),
                snapshot.get("peTtm"),
                snapshot.get("pb"),
                snapshot.get("navPerShare"),
                json.dumps(snapshot, ensure_ascii=False),
                now_iso(),
            ),
        )


def get_snapshot(symbol: str, max_age: timedelta | None = None) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM stock_snapshots WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        return None
    if max_age and not is_fresh(row["updated_at"], max_age):
        return None
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "latestPrice": row["latest_price"],
        "priceChange": row["price_change"],
        "pctChange": row["pct_change"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "previousClose": row["previous_close"],
        "volume": row["volume"],
        "amount": row["amount"],
        "volumeRatio": row["volume_ratio"],
        "turnoverRate": row["turnover_rate"],
        "totalMarketCap": row["total_market_cap"],
        "circulatingMarketCap": row["circulating_market_cap"],
        "peDynamic": row["pe_dynamic"],
        "peStatic": row["pe_static"],
        "peTtm": row["pe_ttm"],
        "pb": row["pb"],
        "navPerShare": row["nav_per_share"],
        "updatedAt": row["updated_at"],
    }


def upsert_klines(symbol: str, period: str, adjust: str, items: list[dict]) -> None:
    if not items:
        return
    updated_at = now_iso()
    with get_connection() as conn:
        historical_rows = []
        current_rows = []
        today = datetime.now().strftime("%Y-%m-%d")
        for item in items:
            row = _kline_db_row(symbol, period, adjust, item, updated_at)
            if str(item.get("date", "")).startswith(today):
                current_rows.append(row)
            else:
                historical_rows.append(row)
        _upsert_kline_rows(conn, "klines", historical_rows)
        _upsert_kline_rows(conn, "current_klines", current_rows)


def query_klines(symbol: str, period: str, adjust: str, start_date: str = "", end_date: str = "", max_age: timedelta | None = None) -> list[dict]:
    where = "symbol = ? AND period = ? AND adjust = ?"
    params: list[object] = [symbol, period, adjust]
    if start_date:
        where += " AND datetime >= ?"
        params.append(_date_key(start_date))
    if end_date:
        where += " AND datetime <= ?"
        params.append(_date_key(end_date, end=True))
    query = f"""
        SELECT * FROM klines WHERE {where}
        UNION ALL
        SELECT * FROM current_klines WHERE {where}
        ORDER BY datetime
    """
    with get_connection() as conn:
        rows = conn.execute(query, params + params).fetchall()
    if not rows:
        return []
    if max_age and not is_fresh(max(row["updated_at"] for row in rows), max_age):
        return []
    if not _klines_cover_requested_range(rows, period, start_date, end_date):
        return []
    return [_kline_item(row) for row in rows]


def list_cached_klines(symbol: str, period: str, adjust: str, limit: int = 800) -> list[dict]:
    """Read local K-lines without freshness or full-range coverage requirements."""

    safe_limit = max(1, min(int(limit), 2_000))
    query = """
        WITH cached AS (
            SELECT * FROM klines WHERE symbol = ? AND period = ? AND adjust = ?
            UNION ALL
            SELECT * FROM current_klines WHERE symbol = ? AND period = ? AND adjust = ?
        )
        SELECT * FROM cached
        ORDER BY datetime DESC
        LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.execute(query, (symbol, period, adjust, symbol, period, adjust, safe_limit)).fetchall()
    return [_kline_item(row) for row in reversed(rows)]


def get_binance_futures_history_missing_ranges(
    network: str,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
) -> list[tuple[int, int]]:
    """Return portions of a fixed futures history window not yet fetched successfully.

    Coverage is tracked separately from rows because a contract can have no
    candles before it was listed. A successful empty response still closes that
    portion of the requested window and should not be requested forever.
    """

    safe_network, safe_symbol, safe_interval, safe_start, safe_end = _binance_history_scope(
        network,
        symbol,
        interval,
        start_time,
        end_time,
    )
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT range_start_ms, range_end_ms
            FROM binance_futures_history_coverage
            WHERE network = ? AND symbol = ? AND interval = ?
              AND range_end_ms > ? AND range_start_ms < ?
            ORDER BY range_start_ms, range_end_ms
            """,
            (safe_network, safe_symbol, safe_interval, safe_start, safe_end),
        ).fetchall()

    missing: list[tuple[int, int]] = []
    cursor = safe_start
    for row in rows:
        covered_start = max(safe_start, int(row["range_start_ms"]))
        covered_end = min(safe_end, int(row["range_end_ms"]))
        if covered_end <= cursor:
            continue
        if covered_start > cursor:
            missing.append((cursor, covered_start))
        cursor = max(cursor, covered_end)
        if cursor >= safe_end:
            break
    if cursor < safe_end:
        missing.append((cursor, safe_end))
    return missing


def get_binance_futures_history_completeness_checks(
    network: str,
    symbols: list[str],
    intervals: tuple[str, ...] | list[str],
    start_time: int,
    end_time: int,
) -> dict[tuple[str, str], dict]:
    """Read persisted completeness checks for one fixed history window."""

    safe_network = str(network or "").strip().lower()
    safe_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    safe_intervals = list(dict.fromkeys(
        str(interval or "").strip()
        for interval in intervals
        if str(interval or "").strip() in _BINANCE_FUTURES_HISTORY_INTERVAL_MS
    ))
    try:
        safe_start = int(start_time)
        safe_end = int(end_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("Binance 历史 K 线时间范围无效") from exc
    if safe_network not in {"mainnet", "testnet"} or safe_start <= 0 or safe_end <= safe_start:
        raise ValueError("Binance 历史 K 线范围无效")
    if not safe_symbols or not safe_intervals:
        return {}

    result: dict[tuple[str, str], dict] = {}
    interval_placeholders = ", ".join("?" for _ in safe_intervals)
    with get_connection() as conn:
        for offset in range(0, len(safe_symbols), _BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK):
            chunk = safe_symbols[offset:offset + _BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK]
            symbol_placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT symbol, interval, range_start_ms, range_end_ms,
                       checked, complete, coverage_complete, data_complete, checked_at
                FROM binance_futures_history_completeness_checks
                WHERE network = ? AND symbol IN ({symbol_placeholders})
                  AND interval IN ({interval_placeholders})
                  AND range_start_ms = ? AND range_end_ms = ?
                """,
                [safe_network, *chunk, *safe_intervals, safe_start, safe_end],
            ).fetchall()
            for row in rows:
                result[(str(row["symbol"]), str(row["interval"]))] = {
                    "network": safe_network,
                    "symbol": str(row["symbol"]),
                    "interval": str(row["interval"]),
                    "rangeStart": int(row["range_start_ms"]),
                    "rangeEnd": int(row["range_end_ms"]),
                    "checked": bool(row["checked"]),
                    "complete": bool(row["complete"]),
                    "coverageComplete": bool(row["coverage_complete"]),
                    "dataComplete": bool(row["data_complete"]),
                    "checkedAt": str(row["checked_at"] or ""),
                }
    return result


def get_binance_futures_history_completeness_check(
    network: str,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
) -> dict | None:
    """Read one persisted completeness check for a fixed history window."""

    safe_network, safe_symbol, safe_interval, safe_start, safe_end = _binance_history_scope(
        network,
        symbol,
        interval,
        start_time,
        end_time,
    )
    return get_binance_futures_history_completeness_checks(
        safe_network,
        [safe_symbol],
        [safe_interval],
        safe_start,
        safe_end,
    ).get((safe_symbol, safe_interval))


def upsert_binance_futures_history_completeness_checks(
    network: str,
    items: list[dict],
    start_time: int,
    end_time: int,
) -> None:
    """Persist the result of a batch completeness inspection."""

    safe_network = str(network or "").strip().lower()
    try:
        safe_start = int(start_time)
        safe_end = int(end_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("Binance 历史 K 线时间范围无效") from exc
    if safe_network not in {"mainnet", "testnet"} or safe_start <= 0 or safe_end <= safe_start:
        raise ValueError("Binance 历史 K 线范围无效")

    checked_at = now_iso()
    rows: list[tuple] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        interval = str(item.get("interval") or "").strip()
        if not symbol or len(symbol) > 32 or interval not in _BINANCE_FUTURES_HISTORY_INTERVAL_MS:
            continue
        rows.append(
            (
                safe_network,
                symbol,
                interval,
                safe_start,
                safe_end,
                int(bool(item.get("checked"))),
                int(bool(item.get("complete"))),
                int(bool(item.get("coverageComplete"))),
                int(bool(item.get("dataComplete"))),
                str(item.get("checkedAt") or checked_at),
            )
        )
    if not rows:
        return

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO binance_futures_history_completeness_checks (
                network, symbol, interval, range_start_ms, range_end_ms,
                checked, complete, coverage_complete, data_complete, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, symbol, interval, range_start_ms, range_end_ms) DO UPDATE SET
                checked = excluded.checked,
                complete = excluded.complete,
                coverage_complete = excluded.coverage_complete,
                data_complete = excluded.data_complete,
                checked_at = excluded.checked_at
            """,
            rows,
        )


def upsert_binance_futures_history_completeness_check(
    network: str,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    complete: bool = False,
    checked: bool = True,
    coverage_complete: bool = False,
    data_complete: bool = False,
) -> None:
    """Persist one completeness check; the batch API is used internally."""

    upsert_binance_futures_history_completeness_checks(
        network,
        [{
            "symbol": symbol,
            "interval": interval,
            "complete": complete,
            "checked": checked,
            "coverageComplete": coverage_complete,
            "dataComplete": data_complete,
        }],
        start_time,
        end_time,
    )


def upsert_binance_futures_history_chunk(
    network: str,
    symbol: str,
    interval: str,
    items: list[dict],
    range_start_ms: int,
    range_end_ms: int,
) -> None:
    """Atomically persist one inspected Binance historical range and its rows."""

    safe_network, safe_symbol, safe_interval, safe_start, safe_end = _binance_history_scope(
        network,
        symbol,
        interval,
        range_start_ms,
        range_end_ms,
    )
    updated_at = now_iso()
    rows: list[tuple] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            open_time = int(item["openTime"])
            close_time = int(item["closeTime"])
            open_price = float(item["open"])
            high_price = float(item["high"])
            low_price = float(item["low"])
            close_price = float(item["close"])
            volume = float(item["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        if open_time < safe_start or open_time >= safe_end or close_time < open_time:
            continue
        try:
            quote_volume = float(item.get("quoteVolume")) if item.get("quoteVolume") is not None else None
        except (TypeError, ValueError):
            quote_volume = None
        if quote_volume is not None and quote_volume < 0:
            quote_volume = None
        rows.append(
            (
                safe_network,
                safe_symbol,
                safe_interval,
                open_time,
                close_time,
                open_price,
                high_price,
                low_price,
                close_price,
                volume,
                quote_volume,
                updated_at,
            )
        )

    with get_connection() as conn:
        if rows:
            conn.executemany(
                """
                INSERT INTO binance_futures_history_klines (
                    network, symbol, interval, open_time, close_time,
                    open, high, low, close, volume, quote_volume, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(network, symbol, interval, open_time) DO UPDATE SET
                    close_time = excluded.close_time,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    quote_volume = excluded.quote_volume,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        conn.execute(
            """
            INSERT INTO binance_futures_history_coverage (
                network, symbol, interval, range_start_ms, range_end_ms,
                bar_count, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(network, symbol, interval, range_start_ms, range_end_ms) DO UPDATE SET
                bar_count = excluded.bar_count,
                completed_at = excluded.completed_at
            """,
            (safe_network, safe_symbol, safe_interval, safe_start, safe_end, len(rows), updated_at),
        )
        conn.execute(
            """
            UPDATE binance_futures_history_completeness_checks
            SET checked = 0,
                complete = 0,
                coverage_complete = 0,
                data_complete = 0,
                checked_at = ?
            WHERE network = ? AND symbol = ? AND interval = ?
              AND range_end_ms > ? AND range_start_ms < ?
            """,
            (updated_at, safe_network, safe_symbol, safe_interval, safe_start, safe_end),
        )


def list_binance_futures_history_klines(
    network: str,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
) -> list[dict]:
    """Read locally persisted Binance futures candles in chronological order."""

    safe_network, safe_symbol, safe_interval, safe_start, safe_end = _binance_history_scope(
        network,
        symbol,
        interval,
        start_time,
        end_time,
    )
    with get_read_connection() as conn:
        rows = conn.execute(
            """
            SELECT open_time, close_time, open, high, low, close, volume, quote_volume
            FROM binance_futures_history_klines
            WHERE network = ? AND symbol = ? AND interval = ?
              AND open_time >= ? AND open_time < ?
            ORDER BY open_time
            """,
            (safe_network, safe_symbol, safe_interval, safe_start, safe_end),
        ).fetchall()
    return [_binance_history_row_to_item(row) for row in rows]


def list_latest_binance_futures_history_klines(
    network: str,
    symbol: str,
    interval: str,
    limit: int,
) -> list[dict]:
    """Read the latest locally persisted candles without any network call."""

    safe_network = str(network or "").strip().lower()
    safe_symbol = str(symbol or "").strip().upper()
    safe_interval = str(interval or "").strip()
    safe_limit = max(1, min(int(limit or 1), 1500))
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("Binance 历史 K 线网络无效")
    if not safe_symbol or len(safe_symbol) > 32:
        raise ValueError("Binance 历史 K 线交易对无效")
    if safe_interval not in _BINANCE_FUTURES_HISTORY_INTERVAL_MS:
        raise ValueError("Binance 历史 K 线周期无效")
    with get_read_connection() as conn:
        rows = conn.execute(
            """
            SELECT open_time, close_time, open, high, low, close, volume, quote_volume
            FROM binance_futures_history_klines
            WHERE network = ? AND symbol = ? AND interval = ?
            ORDER BY open_time DESC
            LIMIT ?
            """,
            (safe_network, safe_symbol, safe_interval, safe_limit),
        ).fetchall()
    return [_binance_history_row_to_item(row) for row in reversed(rows)]


def list_latest_binance_futures_market_volumes(network: str, limit: int) -> list[dict]:
    """Return contracts ranked by latest locally stored 15m notional turnover."""

    safe_network = str(network or "").strip().lower()
    safe_limit = max(1, min(int(limit or 1), 1000))
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("Binance 历史 K 线网络无效")
    with get_read_connection() as conn:
        rows = conn.execute(
            """
            SELECT history.symbol,
                   COALESCE(NULLIF(history.quote_volume, 0), history.volume * history.close, 0) AS quote_volume,
                   history.close, history.open_time
            FROM binance_futures_history_klines AS history
            INNER JOIN (
                SELECT symbol, MAX(open_time) AS latest_open_time
                FROM binance_futures_history_klines
                WHERE network = ? AND interval = '15m'
                GROUP BY symbol
            ) AS latest
              ON latest.symbol = history.symbol AND latest.latest_open_time = history.open_time
            WHERE history.network = ? AND history.interval = '15m'
            ORDER BY COALESCE(NULLIF(history.quote_volume, 0), history.volume * history.close, 0) DESC,
                     history.symbol ASC
            LIMIT ?
            """,
            (safe_network, safe_network, safe_limit),
        ).fetchall()
    return [
        {
            "symbol": str(row["symbol"]),
            "quoteVolume": float(row["quote_volume"] or 0),
            "lastPrice": float(row["close"] or 0),
            "updatedAt": int(row["open_time"] or 0),
        }
        for row in rows
    ]


def list_binance_futures_history_symbols(
    network: str,
    intervals: tuple[str, ...] | list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
) -> list[str]:
    """List symbols already present in the local futures-history corpus.

    This deliberately inspects only SQLite.  The backtest uses it as a
    read-only fallback when Binance has rejected a public market-list request,
    so an existing historical corpus can still be replayed without adding
    more requests to a rate-limited IP.
    """

    safe_network = str(network or "").strip().lower()
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("Binance 历史 K 线网络无效")
    safe_intervals = sorted({str(item or "").strip() for item in (intervals or []) if str(item or "").strip()})
    if any(interval not in _BINANCE_FUTURES_HISTORY_INTERVAL_MS for interval in safe_intervals):
        raise ValueError("Binance 历史 K 线周期无效")
    if (start_time is None) != (end_time is None):
        raise ValueError("Binance 历史 K 线时间范围无效")
    bounds: list[int] = []
    if start_time is not None and end_time is not None:
        try:
            safe_start = int(start_time)
            safe_end = int(end_time)
        except (TypeError, ValueError) as exc:
            raise ValueError("Binance 历史 K 线时间范围无效") from exc
        if safe_start <= 0 or safe_end <= safe_start:
            raise ValueError("Binance 历史 K 线时间范围无效")
        bounds = [safe_start, safe_end]

    clauses = ["network = ?"]
    parameters: list[object] = [safe_network]
    if safe_intervals:
        placeholders = ", ".join("?" for _ in safe_intervals)
        clauses.append(f"interval IN ({placeholders})")
        parameters.extend(safe_intervals)
    if bounds:
        clauses.append("open_time >= ? AND open_time < ?")
        parameters.extend(bounds)
    query = "SELECT DISTINCT symbol FROM binance_futures_history_klines WHERE " + " AND ".join(clauses) + " ORDER BY symbol"
    with get_read_connection() as conn:
        rows = conn.execute(query, parameters).fetchall()
    return [str(row["symbol"]) for row in rows]


def list_binance_futures_history_coverage_symbols(
    network: str,
    intervals: tuple[str, ...] | list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
) -> list[str]:
    """List symbols represented by persisted history coverage metadata.

    Coverage is a compact index of downloaded ranges, so this fallback avoids
    scanning tens of millions of candle rows when the exchange market list is
    temporarily unavailable.
    """

    safe_network = str(network or "").strip().lower()
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("Binance 历史 K 线网络无效")
    safe_intervals = sorted({str(item or "").strip() for item in (intervals or []) if str(item or "").strip()})
    if any(interval not in _BINANCE_FUTURES_HISTORY_INTERVAL_MS for interval in safe_intervals):
        raise ValueError("Binance 历史 K 线周期无效")
    if (start_time is None) != (end_time is None):
        raise ValueError("Binance 历史 K 线时间范围无效")
    clauses = ["network = ?"]
    parameters: list[object] = [safe_network]
    if safe_intervals:
        placeholders = ", ".join("?" for _ in safe_intervals)
        clauses.append(f"interval IN ({placeholders})")
        parameters.extend(safe_intervals)
    if start_time is not None and end_time is not None:
        try:
            safe_start = int(start_time)
            safe_end = int(end_time)
        except (TypeError, ValueError) as exc:
            raise ValueError("Binance 历史 K 线时间范围无效") from exc
        if safe_start <= 0 or safe_end <= safe_start:
            raise ValueError("Binance 历史 K 线时间范围无效")
        clauses.append("range_end_ms > ? AND range_start_ms < ?")
        parameters.extend((safe_start, safe_end))
    query = "SELECT DISTINCT symbol FROM binance_futures_history_coverage WHERE " + " AND ".join(clauses) + " ORDER BY symbol"
    with get_read_connection() as conn:
        rows = conn.execute(query, parameters).fetchall()
    return [str(row["symbol"]) for row in rows]


def list_complete_binance_futures_history_symbols(
    network: str,
    intervals: tuple[str, ...] | list[str],
    start_time: int,
    end_time: int,
) -> list[str]:
    """Return symbols with one contiguous, complete local series per interval.

    The calculation is intentionally read-only.  It lets a replay prove that
    a fixed corpus is sufficient after an exchange rate limit without writing
    completeness metadata or attempting another public request.
    """

    safe_network = str(network or "").strip().lower()
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("Binance 历史 K 线网络无效")
    safe_intervals = sorted({str(item or "").strip() for item in intervals if str(item or "").strip()})
    if not safe_intervals or any(interval not in _BINANCE_FUTURES_HISTORY_INTERVAL_MS for interval in safe_intervals):
        raise ValueError("Binance 历史 K 线周期无效")
    try:
        safe_start = int(start_time)
        safe_end = int(end_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("Binance 历史 K 线时间范围无效") from exc
    if safe_start <= 0 or safe_end <= safe_start:
        raise ValueError("Binance 历史 K 线时间范围无效")

    expected_counts: dict[str, int] = {}
    for interval in safe_intervals:
        interval_ms = _BINANCE_FUTURES_HISTORY_INTERVAL_MS[interval]
        duration = safe_end - safe_start
        if safe_start % interval_ms or safe_end % interval_ms or duration % interval_ms:
            raise ValueError("Binance 固定历史范围必须按 K 线周期对齐")
        expected_counts[interval] = duration // interval_ms

    placeholders = ", ".join("?" for _ in safe_intervals)
    with get_read_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT symbol, interval,
                   COUNT(*) AS bar_count,
                   MIN(open_time) AS first_open_time,
                   MAX(open_time) AS last_open_time,
                   SUM(CASE
                       WHEN close_time = open_time + CASE interval
                           WHEN '1m' THEN 59999
                           WHEN '5m' THEN 299999
                           WHEN '15m' THEN 899999
                           WHEN '1h' THEN 3599999
                           WHEN '4h' THEN 14399999
                       END THEN 1 ELSE 0 END) AS valid_time_count
            FROM binance_futures_history_klines
            WHERE network = ? AND interval IN ({placeholders})
              AND open_time >= ? AND open_time < ?
            GROUP BY symbol, interval
            """,
            [safe_network, *safe_intervals, safe_start, safe_end],
        ).fetchall()

    valid_by_symbol: dict[str, set[str]] = {}
    for row in rows:
        interval = str(row["interval"])
        interval_ms = _BINANCE_FUTURES_HISTORY_INTERVAL_MS[interval]
        expected_count = expected_counts[interval]
        if (
            int(row["bar_count"] or 0) != expected_count
            or int(row["first_open_time"] or -1) != safe_start
            or int(row["last_open_time"] or -1) != safe_end - interval_ms
            or int(row["valid_time_count"] or 0) != expected_count
        ):
            continue
        valid_by_symbol.setdefault(str(row["symbol"]), set()).add(interval)
    required = set(safe_intervals)
    return sorted(symbol for symbol, available in valid_by_symbol.items() if available == required)


def list_binance_futures_history_klines_batch(
    network: str,
    symbols: list[str] | tuple[str, ...],
    interval: str,
    start_time: int,
    end_time: int,
    *,
    symbol_chunk_size: int = _BINANCE_FUTURES_HISTORY_READ_SYMBOL_CHUNK,
) -> dict[str, list[dict]]:
    """Read one interval for several symbols using a shared read connection.

    The history table is keyed by ``network, symbol, interval, open_time``.
    Keeping symbol in the ``IN`` clause lets SQLite seek the existing range
    index for each requested symbol, while the shared connection removes the
    connection/WAL setup cost that made replay preparation disproportionately
    slow.  Results are split into bounded symbol chunks so loading a month of
    5m candles never requires materialising the whole universe in one query.
    """

    safe_network = str(network or "").strip().lower()
    safe_interval = str(interval or "").strip()
    try:
        safe_start = int(start_time)
        safe_end = int(end_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("Binance 历史 K 线时间范围无效") from exc
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("Binance 历史 K 线网络无效")
    if safe_interval not in _BINANCE_FUTURES_HISTORY_INTERVAL_MS:
        raise ValueError("Binance 历史 K 线周期无效")
    if safe_start <= 0 or safe_end <= safe_start:
        raise ValueError("Binance 历史 K 线时间范围无效")

    safe_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if any(len(symbol) > 32 for symbol in safe_symbols):
        raise ValueError("Binance 历史 K 线交易对无效")
    result: dict[str, list[dict]] = {symbol: [] for symbol in safe_symbols}
    if not safe_symbols:
        return result

    try:
        chunk_size = max(1, min(int(symbol_chunk_size), _BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK))
    except (TypeError, ValueError):
        chunk_size = _BINANCE_FUTURES_HISTORY_READ_SYMBOL_CHUNK

    with get_read_connection() as conn:
        for offset in range(0, len(safe_symbols), chunk_size):
            chunk = safe_symbols[offset:offset + chunk_size]
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT symbol, open_time, close_time, open, high, low, close, volume, quote_volume
                FROM binance_futures_history_klines
                WHERE network = ? AND interval = ?
                  AND symbol IN ({placeholders})
                  AND open_time >= ? AND open_time < ?
                ORDER BY symbol, open_time
                """,
                [safe_network, safe_interval, *chunk, safe_start, safe_end],
            ).fetchall()
            for row in rows:
                symbol = str(row["symbol"])
                if symbol in result:
                    result[symbol].append(_binance_history_row_to_item(row))
    return result


def list_binance_futures_history_klines_batch_rows(
    network: str,
    symbols: list[str] | tuple[str, ...],
    interval: str,
    start_time: int,
    end_time: int,
    *,
    symbol_chunk_size: int = _BINANCE_FUTURES_HISTORY_READ_SYMBOL_CHUNK,
) -> dict[str, list[tuple]]:
    """Read a batch as tuples for replay's zero-copy-ish compaction path.

    The public dictionary-shaped batch reader is useful to API callers, but
    converting millions of SQLite rows to dictionaries only to immediately
    convert them back into compact numeric arrays is expensive.  Backtest
    preparation uses this tuple variant and compacts each batch in the worker
    before releasing the temporary rows.

    Each tuple is ``(open_time, close_time, open, high, low, close, volume)``.
    """

    safe_network, safe_symbols, safe_interval, safe_start, safe_end, chunk_size = _prepare_binance_history_batch(
        network, symbols, interval, start_time, end_time, symbol_chunk_size
    )
    result: dict[str, list[tuple]] = {symbol: [] for symbol in safe_symbols}
    if not safe_symbols:
        return result

    with get_read_connection(row_factory=None) as conn:
        for offset in range(0, len(safe_symbols), chunk_size):
            chunk = safe_symbols[offset:offset + chunk_size]
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                    SELECT symbol, open_time, close_time, open, high, low, close, volume, quote_volume
                FROM binance_futures_history_klines
                WHERE network = ? AND interval = ?
                  AND symbol IN ({placeholders})
                  AND open_time >= ? AND open_time < ?
                ORDER BY symbol, open_time
                """,
                [safe_network, safe_interval, *chunk, safe_start, safe_end],
            ).fetchall()
            for row in rows:
                symbol = str(row[0])
                if symbol in result:
                     result[symbol].append(tuple(row[1:]))
    return result


def _prepare_binance_history_batch(
    network: str,
    symbols: list[str] | tuple[str, ...],
    interval: str,
    start_time: int,
    end_time: int,
    symbol_chunk_size: int,
) -> tuple[str, list[str], str, int, int, int]:
    safe_network = str(network or "").strip().lower()
    safe_interval = str(interval or "").strip()
    try:
        safe_start = int(start_time)
        safe_end = int(end_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("Binance 历史 K 线时间范围无效") from exc
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("Binance 历史 K 线网络无效")
    if safe_interval not in _BINANCE_FUTURES_HISTORY_INTERVAL_MS:
        raise ValueError("Binance 历史 K 线周期无效")
    if safe_start <= 0 or safe_end <= safe_start:
        raise ValueError("Binance 历史 K 线时间范围无效")

    safe_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if any(len(symbol) > 32 for symbol in safe_symbols):
        raise ValueError("Binance 历史 K 线交易对无效")
    try:
        chunk_size = max(1, min(int(symbol_chunk_size), _BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK))
    except (TypeError, ValueError):
        chunk_size = _BINANCE_FUTURES_HISTORY_READ_SYMBOL_CHUNK
    return safe_network, safe_symbols, safe_interval, safe_start, safe_end, chunk_size


def _binance_history_row_to_item(row: sqlite3.Row) -> dict:
    return {
        "openTime": int(row["open_time"]),
        "closeTime": int(row["close_time"]),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
        "quoteVolume": float(row["quote_volume"]) if row["quote_volume"] is not None else None,
    }


def summarize_binance_futures_history_completeness(
    network: str,
    symbols: list[str],
    intervals: tuple[str, ...] | list[str],
    start_time: int,
    end_time: int,
) -> dict:
    """Strictly summarize every requested futures K-line series.

    A completed check is persisted for the exact fixed window. Only records
    that were checked and complete are reused; missing or failed records are
    inspected again against both coverage ranges and the actual candle rows.
    """

    safe_network = str(network or "").strip().lower()
    safe_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    safe_intervals = list(dict.fromkeys(
        str(interval or "").strip()
        for interval in intervals
        if str(interval or "").strip() in _BINANCE_FUTURES_HISTORY_INTERVAL_MS
    ))
    try:
        safe_start = int(start_time)
        safe_end = int(end_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("Binance 历史 K 线时间范围无效") from exc
    if safe_network not in {"mainnet", "testnet"} or safe_start <= 0 or safe_end <= safe_start:
        raise ValueError("Binance 历史 K 线范围无效")

    series = {
        (symbol, interval): {
            "symbol": symbol,
            "interval": interval,
            "coverageComplete": False,
            "dataComplete": False,
            "checked": False,
            "complete": False,
            "checkCached": False,
            "checkedAt": "",
        }
        for symbol in safe_symbols
        for interval in safe_intervals
    }
    if not series:
        return {
            "network": safe_network,
            "startTime": safe_start,
            "endTime": safe_end,
            "symbolCount": len(safe_symbols),
            "requiredSeries": 0,
            "checkedSeries": 0,
            "coverageCompleteSeries": 0,
            "dataCompleteSeries": 0,
            "completeSeries": 0,
            "percent": 0,
            "intervals": [],
            "incomplete": [],
        }

    checks = get_binance_futures_history_completeness_checks(
        safe_network,
        safe_symbols,
        safe_intervals,
        safe_start,
        safe_end,
    )
    pending_keys: set[tuple[str, str]] = set()
    for key, item in series.items():
        check = checks.get(key)
        if check and check["checked"] and check["complete"]:
            item.update(
                coverageComplete=True,
                dataComplete=True,
                checked=True,
                complete=True,
                checkCached=True,
                checkedAt=check.get("checkedAt") or "",
            )
        else:
            pending_keys.add(key)
            if check:
                item["checkedAt"] = check.get("checkedAt") or ""

    coverage_by_series: dict[tuple[str, str], list[tuple[int, int]]] = {}
    data_by_series: dict[tuple[str, str], sqlite3.Row] = {}
    coverage_complete_keys: set[tuple[str, str]] = set()
    if pending_keys:
        pending_symbols = sorted({symbol for symbol, _ in pending_keys})
        pending_intervals = [interval for interval in safe_intervals if any(key[1] == interval for key in pending_keys)]

        # Coverage is a compact range index. Read it first and use it to
        # decide which series deserve an expensive candle-row scan. In a
        # partially filled fixed corpus this avoids walking every 5m/1m row
        # just to conclude that the requested range was never downloaded.
        with get_connection() as conn:
            for offset in range(0, len(pending_symbols), _BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK):
                chunk = pending_symbols[offset:offset + _BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK]
                symbol_placeholders = ", ".join("?" for _ in chunk)
                interval_placeholders = ", ".join("?" for _ in pending_intervals)
                coverage_rows = conn.execute(
                    f"""
                    SELECT symbol, interval, range_start_ms, range_end_ms
                    FROM binance_futures_history_coverage
                    WHERE network = ? AND symbol IN ({symbol_placeholders})
                      AND interval IN ({interval_placeholders})
                      AND range_end_ms > ? AND range_start_ms < ?
                    ORDER BY symbol, interval, range_start_ms, range_end_ms
                    """,
                    [safe_network, *chunk, *pending_intervals, safe_start, safe_end],
                ).fetchall()
                for row in coverage_rows:
                    key = (str(row["symbol"]), str(row["interval"]))
                    if key in pending_keys:
                        coverage_by_series.setdefault(key, []).append(
                            (int(row["range_start_ms"]), int(row["range_end_ms"]))
                        )

        # Resolve coverage before reading candle rows. A coverage range can
        # represent a successful empty exchange response, so it remains
        # separate from the data-complete check below.
        for key in sorted(pending_keys):
            cursor = safe_start
            for range_start, range_end in coverage_by_series.get(key, []):
                if range_end <= cursor:
                    continue
                if range_start > cursor:
                    break
                cursor = max(cursor, range_end)
                if cursor >= safe_end:
                    coverage_complete_keys.add(key)
                    break

    # Only coverage-complete series need an actual row scan. Grouping by
    # interval avoids a broad symbol x interval query touching unrequested
    # 5m/1m partitions in a mostly macro-complete corpus.
    if coverage_complete_keys:
        with get_connection() as conn:
            for interval in safe_intervals:
                interval_symbols = sorted(
                    symbol for symbol, item_interval in coverage_complete_keys
                    if item_interval == interval
                )
                if not interval_symbols:
                    continue
                interval_ms = _BINANCE_FUTURES_HISTORY_INTERVAL_MS[interval]
                for offset in range(0, len(interval_symbols), _BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK):
                    chunk = interval_symbols[offset:offset + _BINANCE_FUTURES_HISTORY_SYMBOL_QUERY_CHUNK]
                    symbol_placeholders = ", ".join("?" for _ in chunk)
                    data_rows = conn.execute(
                        f"""
                        SELECT
                            symbol,
                            COUNT(*) AS row_count,
                            MIN(open_time) AS first_open_time,
                            MAX(open_time) AS last_open_time,
                            MAX(close_time) AS last_close_time,
                            SUM(CASE
                                WHEN ((open_time - ?) % ?) != 0
                                  OR close_time IS NULL
                                  OR close_time != open_time + ? - 1
                                  OR open IS NULL OR high IS NULL OR low IS NULL
                                  OR close IS NULL OR volume IS NULL
                                  OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                                  OR high < low OR volume < 0
                                THEN 1 ELSE 0
                            END) AS invalid_row_count
                        FROM binance_futures_history_klines
                        WHERE network = ? AND interval = ?
                          AND symbol IN ({symbol_placeholders})
                          AND open_time >= ? AND open_time < ?
                        GROUP BY symbol
                        """,
                        [
                            safe_start,
                            interval_ms,
                            interval_ms,
                            safe_network,
                            interval,
                            *chunk,
                            safe_start,
                            safe_end,
                        ],
                    ).fetchall()
                    for row in data_rows:
                        key = (str(row["symbol"]), interval)
                        if key in pending_keys:
                            data_by_series[key] = row

    checked_at = now_iso()
    check_rows: list[dict] = []
    for key in sorted(pending_keys):
        item = series[key]
        item["coverageComplete"] = key in coverage_complete_keys

        row = data_by_series.get(key)
        duration = _BINANCE_FUTURES_HISTORY_INTERVAL_MS[key[1]]
        range_is_aligned = (safe_end - safe_start) % duration == 0
        expected_count = (safe_end - safe_start) // duration if range_is_aligned else -1
        if row and range_is_aligned:
            item["dataComplete"] = (
                int(row["row_count"] or 0) == expected_count
                and int(row["first_open_time"] or -1) == safe_start
                and int(row["last_open_time"] or -1) == safe_end - duration
                and int(row["last_close_time"] or -1) == safe_end - 1
                and int(row["invalid_row_count"] or 0) == 0
            )
        item["checked"] = True
        item["checkedAt"] = checked_at
        item["complete"] = bool(item["coverageComplete"] and item["dataComplete"])
        check_rows.append(item)

    if check_rows:
        upsert_binance_futures_history_completeness_checks(
            safe_network,
            check_rows,
            safe_start,
            safe_end,
        )

    interval_summary = []
    for interval in safe_intervals:
        interval_items = [item for item in series.values() if item["interval"] == interval]
        interval_summary.append(
            {
                "interval": interval,
                "total": len(interval_items),
                "checked": sum(1 for item in interval_items if item["checked"]),
                "coverageComplete": sum(1 for item in interval_items if item["coverageComplete"]),
                "dataComplete": sum(1 for item in interval_items if item["dataComplete"]),
                "complete": sum(1 for item in interval_items if item["complete"]),
            }
        )
    values = list(series.values())
    coverage_complete = sum(1 for item in values if item["coverageComplete"])
    data_complete = sum(1 for item in values if item["dataComplete"])
    complete = sum(1 for item in values if item["complete"])
    checked = sum(1 for item in values if item["checked"])
    required = len(values)
    incomplete = [item for item in values if not item["complete"]]
    return {
        "network": safe_network,
        "startTime": safe_start,
        "endTime": safe_end,
        "symbolCount": len(safe_symbols),
        "requiredSeries": required,
        "checkedSeries": checked,
        "coverageCompleteSeries": coverage_complete,
        "dataCompleteSeries": data_complete,
        "completeSeries": complete,
        "percent": round(complete / required * 100, 2) if required else 0,
        "intervals": interval_summary,
        "incomplete": incomplete[:50],
    }


def _binance_history_scope(
    network: str,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
) -> tuple[str, str, str, int, int]:
    safe_network = str(network or "").strip().lower()
    safe_symbol = str(symbol or "").strip().upper()
    safe_interval = str(interval or "").strip()
    try:
        safe_start = int(start_time)
        safe_end = int(end_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("Binance 历史 K 线时间范围无效") from exc
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("Binance 历史 K 线网络无效")
    if not safe_symbol or len(safe_symbol) > 32:
        raise ValueError("Binance 历史 K 线交易对无效")
    if safe_interval not in _BINANCE_FUTURES_HISTORY_INTERVAL_MS:
        raise ValueError("Binance 历史 K 线周期无效")
    if safe_start <= 0 or safe_end <= safe_start:
        raise ValueError("Binance 历史 K 线时间范围无效")
    return safe_network, safe_symbol, safe_interval, safe_start, safe_end


def create_binance_ml_training_run(
    run_id: str,
    network: str,
    config: dict | None = None,
) -> dict:
    safe_id = str(run_id or "").strip()
    safe_network = str(network or "").strip().lower()
    if not safe_id:
        raise ValueError("模型训练任务标识无效")
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("模型训练网络无效")
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO binance_ml_training_runs (
                id, network, status, config_json, dataset_json, metrics_json,
                artifact_path, error, created_at, updated_at
            ) VALUES (?, ?, 'QUEUED', ?, '{}', '{}', NULL, NULL, ?, ?)
            """,
            (safe_id, safe_network, _json_dump(config or {}), timestamp, timestamp),
        )
    return get_binance_ml_training_run(safe_id) or {}


def update_binance_ml_training_run(
    run_id: str,
    *,
    status: str | None = None,
    dataset: dict | None = None,
    metrics: dict | None = None,
    artifact_path: str | None = None,
    error: str | None = None,
) -> dict | None:
    safe_id = str(run_id or "").strip()
    if not safe_id:
        raise ValueError("模型训练任务标识无效")
    columns: list[str] = []
    parameters: list[object] = []
    if status is not None:
        columns.append("status = ?")
        parameters.append(str(status).strip().upper()[:32])
    if dataset is not None:
        columns.append("dataset_json = ?")
        parameters.append(_json_dump(dataset))
    if metrics is not None:
        columns.append("metrics_json = ?")
        parameters.append(_json_dump(metrics))
    if artifact_path is not None:
        columns.append("artifact_path = ?")
        parameters.append(str(artifact_path).strip() or None)
    if error is not None:
        columns.append("error = ?")
        parameters.append(str(error).strip() or None)
    columns.append("updated_at = ?")
    parameters.append(now_iso())
    parameters.append(safe_id)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE binance_ml_training_runs SET {', '.join(columns)} WHERE id = ?",
            parameters,
        )
    return get_binance_ml_training_run(safe_id)


def get_binance_ml_training_run(run_id: str) -> dict | None:
    safe_id = str(run_id or "").strip()
    if not safe_id:
        return None
    with get_read_connection() as conn:
        row = conn.execute(
            """
            SELECT id, network, status, config_json, dataset_json, metrics_json,
                   artifact_path, error, created_at, updated_at
            FROM binance_ml_training_runs
            WHERE id = ?
            """,
            (safe_id,),
        ).fetchone()
    return _binance_ml_training_run_row(row) if row else None


def get_latest_binance_ml_training_run(
    network: str = "mainnet",
    *,
    completed_only: bool = False,
) -> dict | None:
    safe_network = str(network or "").strip().lower()
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("模型训练网络无效")
    clauses = ["network = ?"]
    parameters: list[object] = [safe_network]
    if completed_only:
        clauses.append("status = 'COMPLETED'")
    with get_read_connection() as conn:
        row = conn.execute(
            f"""
            SELECT id, network, status, config_json, dataset_json, metrics_json,
                   artifact_path, error, created_at, updated_at
            FROM binance_ml_training_runs
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            parameters,
        ).fetchone()
    return _binance_ml_training_run_row(row) if row else None


def list_binance_ml_training_runs(network: str = "mainnet", limit: int = 12) -> list[dict]:
    safe_network = str(network or "").strip().lower()
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("模型训练网络无效")
    try:
        safe_limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        safe_limit = 12
    with get_read_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, network, status, config_json, dataset_json, metrics_json,
                   artifact_path, error, created_at, updated_at
            FROM binance_ml_training_runs
            WHERE network = ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (safe_network, safe_limit),
        ).fetchall()
    return [_binance_ml_training_run_row(row) for row in rows]


def _binance_ml_training_run_row(row: sqlite3.Row) -> dict:
    return {
        "id": str(row["id"]),
        "network": str(row["network"]),
        "status": str(row["status"]),
        "config": _json_load(row["config_json"]),
        "dataset": _json_load(row["dataset_json"]),
        "metrics": _json_load(row["metrics_json"]),
        "artifactPath": row["artifact_path"],
        "error": row["error"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: object) -> dict:
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def upsert_daily_quotes(items: list[dict], quote_minute: str) -> None:
    if not items:
        return
    updated_at = now_iso()
    with get_connection() as conn:
        _write_daily_quotes(conn, items, quote_minute, updated_at)


def replace_daily_quotes(items: list[dict], quote_minute: str) -> None:
    """Atomically replace the market snapshot after completeness validation."""

    if not items:
        return
    updated_at = now_iso()
    with get_connection() as conn:
        conn.execute("DELETE FROM daily_quotes")
        _write_daily_quotes(conn, items, quote_minute, updated_at)


def _write_daily_quotes(conn: sqlite3.Connection, items: list[dict], quote_minute: str, updated_at: str) -> None:
    conn.executemany(
        """
        INSERT INTO daily_quotes (
            symbol, name, market, latest_price, pct_change, price_change, volume,
            amount, turnover_rate, pe_dynamic, pb, total_market_cap, quote_minute,
            raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            name=excluded.name,
            market=excluded.market,
            latest_price=excluded.latest_price,
            pct_change=excluded.pct_change,
            price_change=excluded.price_change,
            volume=excluded.volume,
            amount=excluded.amount,
            turnover_rate=excluded.turnover_rate,
            pe_dynamic=excluded.pe_dynamic,
            pb=excluded.pb,
            total_market_cap=excluded.total_market_cap,
            quote_minute=excluded.quote_minute,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        [
            (
                item.get("symbol"),
                item.get("name"),
                item.get("market"),
                item.get("latestPrice"),
                item.get("pctChange"),
                item.get("priceChange"),
                item.get("volume"),
                item.get("amount"),
                item.get("turnoverRate"),
                item.get("peDynamic"),
                item.get("pb"),
                item.get("totalMarketCap"),
                quote_minute,
                json.dumps(item, ensure_ascii=False),
                updated_at,
            )
            for item in items
            if item.get("symbol")
        ],
    )


def daily_quote_cache_status() -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count, MAX(quote_minute) AS quote_minute, MAX(updated_at) AS updated_at FROM daily_quotes"
        ).fetchone()
    return {
        "count": int(row["count"] or 0) if row else 0,
        "quoteMinute": row["quote_minute"] if row else None,
        "updatedAt": row["updated_at"] if row else None,
    }


def get_app_metadata(key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_app_metadata(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO app_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, datetime.now().isoformat(timespec="seconds")),
        )


def list_daily_quotes(symbols: list[str] | None = None) -> list[dict]:
    with get_connection() as conn:
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            rows = conn.execute(
                f"SELECT * FROM daily_quotes WHERE symbol IN ({placeholders}) ORDER BY symbol",
                symbols,
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM daily_quotes ORDER BY symbol").fetchall()
    return [_daily_quote_item(row) for row in rows]


def _personal_user_id(user_id: object) -> int:
    try:
        normalized = int(user_id)
    except (TypeError, ValueError):
        raise ValueError("缺少有效用户身份") from None
    if normalized <= 0:
        raise ValueError("缺少有效用户身份")
    return normalized


def _normalize_binance_futures_leverage(value: object) -> float:
    try:
        leverage = float(value)
    except (TypeError, ValueError):
        raise ValueError("杠杆必须是数字") from None
    if not math.isfinite(leverage) or leverage < 1 or leverage > MAX_BINANCE_FUTURES_LEVERAGE:
        raise ValueError(f"杠杆必须在 1 到 {MAX_BINANCE_FUTURES_LEVERAGE} 倍之间")
    return leverage


def insert_binance_simulated_position(user_id: int, position: dict) -> dict:
    owner_id = _personal_user_id(user_id)
    timestamp = now_iso()
    leverage = _normalize_binance_futures_leverage(position.get("leverage", 1))
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO binance_simulated_positions (
                    user_id, network, market_mode, symbol, side, quantity, cost_price,
                    leverage, plan_json, protected_stop, moving_stop, moving_stop_active,
                    moving_stop_activation_price, moving_stop_activation_at,
                    execution_status, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    position["network"],
                    position["marketMode"],
                    position["symbol"],
                    position["side"],
                    position["quantity"],
                    position["costPrice"],
                    leverage,
                    json.dumps(position.get("plan") or {}, ensure_ascii=False, separators=(",", ":")),
                    position.get("executionStatus") or "EXECUTING",
                    position.get("note", ""),
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("相同网络、市场、交易对和方向的持仓计划监控已存在") from exc
        position_id = cursor.lastrowid
    return get_binance_simulated_position(owner_id, position_id) or {}


def update_binance_simulated_position(user_id: int, position_id: int, position: dict) -> dict:
    owner_id = _personal_user_id(user_id)
    timestamp = now_iso()
    leverage = _normalize_binance_futures_leverage(position.get("leverage", 1))
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """
                UPDATE binance_simulated_positions
                SET network = ?, market_mode = ?, symbol = ?, side = ?,
                    quantity = ?, cost_price = ?, leverage = ?,
                    plan_json = COALESCE(?, plan_json), note = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    position["network"],
                    position["marketMode"],
                    position["symbol"],
                    position["side"],
                    position["quantity"],
                    position["costPrice"],
                    leverage,
                    json.dumps(position["plan"], ensure_ascii=False, separators=(",", ":"))
                    if position.get("plan") is not None
                    else None,
                    position.get("note", ""),
                    timestamp,
                    int(position_id),
                    owner_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("相同网络、市场、交易对和方向的持仓计划监控已存在") from exc
        if cursor.rowcount == 0:
            raise ValueError("持仓计划监控不存在")
    return get_binance_simulated_position(owner_id, position_id) or {}


def merge_binance_simulated_position_plan_snapshot(
    user_id: int,
    position_id: int,
    updates: dict,
) -> dict | None:
    """Merge immutable execution-plan fields without replacing the saved plan."""

    owner_id = _personal_user_id(user_id)
    if not isinstance(updates, dict) or not updates:
        return get_binance_simulated_position(owner_id, position_id)
    timestamp = now_iso()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT plan_json FROM binance_simulated_positions WHERE id = ? AND user_id = ?",
            (int(position_id), owner_id),
        ).fetchone()
        if not row:
            return None
        plan = _loads(row["plan_json"], {})
        if not isinstance(plan, dict):
            plan = {}
        plan.update({key: value for key, value in updates.items() if value is not None})
        conn.execute(
            """
            UPDATE binance_simulated_positions
            SET plan_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (json.dumps(plan, ensure_ascii=False, separators=(",", ":")), timestamp, int(position_id), owner_id),
        )
    return get_binance_simulated_position(owner_id, position_id)


def list_binance_simulated_positions(user_id: int) -> list[dict]:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM binance_simulated_positions
            WHERE user_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (owner_id,),
        ).fetchall()
    return [_binance_simulated_position_item(row) for row in rows]


def list_binance_simulated_positions_for_refresh() -> list[tuple[int, dict]]:
    """Return every active holding-monitor plan for the backend refresh worker."""

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM binance_simulated_positions
            WHERE execution_status IN ('PENDING_ENTRY', 'EXECUTING')
            ORDER BY updated_at ASC, id ASC
            """
        ).fetchall()
    return [(int(row["user_id"]), _binance_simulated_position_item(row)) for row in rows]


def get_binance_simulated_position(user_id: int, position_id: int) -> dict | None:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM binance_simulated_positions WHERE user_id = ? AND id = ?",
            (owner_id, int(position_id)),
        ).fetchone()
    return _binance_simulated_position_item(row) if row else None


def update_binance_simulated_position_state(
    user_id: int,
    position_id: int,
    *,
    protected_stop: float | None,
    moving_stop: float | None,
    moving_stop_active: bool,
    moving_stop_activation_price: float | None,
    moving_stop_activation_at: str | None,
    dynamic_plan: dict | None = None,
    current_price: float | None = None,
    unrealized_pnl: float | None = None,
    unrealized_pnl_percent: float | None = None,
) -> dict | None:
    owner_id = _personal_user_id(user_id)
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE binance_simulated_positions
            SET protected_stop = ?, moving_stop = ?, moving_stop_active = ?,
                moving_stop_activation_price = ?, moving_stop_activation_at = ?,
                dynamic_plan_json = COALESCE(?, dynamic_plan_json),
                last_price = COALESCE(?, last_price),
                last_unrealized_pnl = COALESCE(?, last_unrealized_pnl),
                last_unrealized_pnl_percent = COALESCE(?, last_unrealized_pnl_percent),
                updated_at = ?
            WHERE id = ? AND user_id = ? AND execution_status = 'EXECUTING'
            """,
            (
                protected_stop,
                moving_stop,
                1 if moving_stop_active else 0,
                moving_stop_activation_price,
                moving_stop_activation_at,
                json.dumps(dynamic_plan, ensure_ascii=False, separators=(",", ":")) if dynamic_plan is not None else None,
                current_price,
                unrealized_pnl,
                unrealized_pnl_percent,
                timestamp,
                int(position_id),
                owner_id,
            ),
        )
    return get_binance_simulated_position(owner_id, position_id)


def update_binance_simulated_position_monitoring_metadata(
    user_id: int,
    position_id: int,
    *,
    execution_status: str | None = None,
    dynamic_plan: dict | None = None,
    clear_live_metrics: bool = False,
) -> dict | None:
    """Persist monitor-stage metadata without treating a missing account row as an exit."""

    if execution_status not in (None, "PENDING_ENTRY", "EXECUTING"):
        raise ValueError("无效的持仓计划监控状态")
    owner_id = _personal_user_id(user_id)
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE binance_simulated_positions
            SET execution_status = COALESCE(?, execution_status),
                dynamic_plan_json = COALESCE(?, dynamic_plan_json),
                last_price = CASE WHEN ? THEN NULL ELSE last_price END,
                last_unrealized_pnl = CASE WHEN ? THEN NULL ELSE last_unrealized_pnl END,
                last_unrealized_pnl_percent = CASE WHEN ? THEN NULL ELSE last_unrealized_pnl_percent END,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND execution_status IN ('PENDING_ENTRY', 'EXECUTING')
            """,
            (
                execution_status,
                json.dumps(dynamic_plan, ensure_ascii=False, separators=(",", ":")) if dynamic_plan is not None else None,
                1 if clear_live_metrics else 0,
                1 if clear_live_metrics else 0,
                1 if clear_live_metrics else 0,
                timestamp,
                int(position_id),
                owner_id,
            ),
        )
    return get_binance_simulated_position(owner_id, position_id)


def sync_binance_simulated_position_live_values(
    user_id: int,
    position_id: int,
    *,
    quantity: float | None = None,
    cost_price: float | None = None,
) -> dict | None:
    """Copy quantity and entry price from a matching live futures position."""

    owner_id = _personal_user_id(user_id)
    if quantity is None and cost_price is None:
        return get_binance_simulated_position(owner_id, position_id)
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE binance_simulated_positions
            SET quantity = COALESCE(?, quantity),
                cost_price = COALESCE(?, cost_price),
                updated_at = ?
            WHERE id = ? AND user_id = ? AND execution_status = 'EXECUTING'
            """,
            (quantity, cost_price, timestamp, int(position_id), owner_id),
        )
    return get_binance_simulated_position(owner_id, position_id)


def mark_binance_simulated_position_stopped(
    user_id: int,
    position_id: int,
    *,
    stop_price: float | None,
    stop_reason: str,
    dynamic_plan: dict,
    current_price: float | None,
    unrealized_pnl: float | None,
    unrealized_pnl_percent: float | None,
) -> dict | None:
    owner_id = _personal_user_id(user_id)
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE binance_simulated_positions
            SET execution_status = 'STOPPED', stopped_at = COALESCE(stopped_at, ?),
                stop_price = COALESCE(stop_price, ?), stop_reason = COALESCE(stop_reason, ?),
                dynamic_plan_json = ?, last_price = ?,
                last_unrealized_pnl = ?, last_unrealized_pnl_percent = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND execution_status = 'EXECUTING'
            """,
            (
                timestamp,
                stop_price,
                stop_reason,
                json.dumps(dynamic_plan, ensure_ascii=False, separators=(",", ":")),
                current_price,
                unrealized_pnl,
                unrealized_pnl_percent,
                timestamp,
                int(position_id),
                owner_id,
            ),
        )
    return get_binance_simulated_position(owner_id, position_id)


def restore_binance_simulated_position(user_id: int, position_id: int) -> dict | None:
    """Resume a stopped holding monitor and reset its transient state."""

    owner_id = _personal_user_id(user_id)
    timestamp = now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE binance_simulated_positions
            SET protected_stop = NULL, moving_stop = NULL, moving_stop_active = 0,
                moving_stop_activation_price = NULL, moving_stop_activation_at = NULL,
                execution_status = 'EXECUTING', stopped_at = NULL, stop_price = NULL,
                stop_reason = NULL, dynamic_plan_json = '{}', last_price = NULL,
                last_unrealized_pnl = NULL, last_unrealized_pnl_percent = NULL,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND execution_status = 'STOPPED'
            """,
            (timestamp, int(position_id), owner_id),
        )
    return get_binance_simulated_position(owner_id, position_id) if cursor.rowcount else None


def delete_binance_simulated_position(user_id: int, position_id: int) -> bool:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM binance_simulated_positions WHERE user_id = ? AND id = ?",
            (owner_id, int(position_id)),
        )
    return cursor.rowcount > 0


def upsert_portfolio_position(user_id: int, position: dict) -> dict:
    owner_id = _personal_user_id(user_id)
    symbol = str(position.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    updated_at = now_iso()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT created_at FROM portfolio_positions WHERE user_id = ? AND symbol = ?",
            (owner_id, symbol),
        ).fetchone()
        created_at = existing["created_at"] if existing else updated_at
        conn.execute(
            """
            INSERT INTO portfolio_positions (
                user_id, symbol, name, cost_price, shares, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                name=excluded.name,
                cost_price=excluded.cost_price,
                shares=excluded.shares,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                owner_id,
                symbol,
                position.get("name"),
                position.get("costPrice"),
                position.get("shares"),
                position.get("note"),
                created_at,
                updated_at,
            ),
        )
    return get_portfolio_position(owner_id, symbol) or {}


def list_portfolio_positions(user_id: int) -> list[dict]:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio_positions WHERE user_id = ? ORDER BY updated_at DESC, symbol",
            (owner_id,),
        ).fetchall()
    return [_portfolio_item(row) for row in rows]


def get_portfolio_position(user_id: int, symbol: str) -> dict | None:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_positions WHERE user_id = ? AND symbol = ?",
            (owner_id, symbol),
        ).fetchone()
    return _portfolio_item(row) if row else None


def update_portfolio_position_trailing_state(user_id: int, symbol: str, state: dict) -> dict | None:
    """Persist an A-share holding trail without ever loosening protection."""

    owner_id = _personal_user_id(user_id)
    safe_symbol = str(symbol or "").strip()
    if not safe_symbol or not isinstance(state, dict):
        return get_portfolio_position(owner_id, safe_symbol) if safe_symbol else None

    def positive(value: object) -> float | None:
        try:
            number = float(value)
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    stage_order = {"INITIAL": 0, "BREAKEVEN": 1, "ATR_TRAILING": 2}
    requested_stop = positive(state.get("stopPrice"))
    requested_peak = positive(state.get("peakPrice"))
    requested_stage = str(state.get("state") or "INITIAL").strip().upper()
    if requested_stage not in stage_order:
        requested_stage = "INITIAL"
    requested_active = bool(state.get("active"))
    requested_activation_price = positive(state.get("activationPrice"))
    requested_activation_at = state.get("activationAt") or None
    requested_peak_at = state.get("peakAt") or None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_positions WHERE user_id = ? AND symbol = ?",
            (owner_id, safe_symbol),
        ).fetchone()
        if not row:
            return None
        columns = set(row.keys())
        current_stop = positive(row["trailing_stop"]) if "trailing_stop" in columns else None
        current_peak = positive(row["trailing_peak"]) if "trailing_peak" in columns else None
        current_stage = str(row["trailing_stage"] or "INITIAL").strip().upper() if "trailing_stage" in columns else "INITIAL"
        if current_stage not in stage_order:
            current_stage = "INITIAL"
        current_active = bool(row["trailing_active"]) if "trailing_active" in columns else False
        effective_stop = max(value for value in (current_stop, requested_stop) if value is not None) if current_stop or requested_stop else None
        effective_peak = max(value for value in (current_peak, requested_peak) if value is not None) if current_peak or requested_peak else None
        effective_stage = requested_stage if stage_order[requested_stage] >= stage_order[current_stage] else current_stage
        effective_active = current_active or requested_active
        activation_price = row["trailing_activation_price"] if "trailing_activation_price" in columns else None
        activation_at = row["trailing_activation_at"] if "trailing_activation_at" in columns else None
        peak_at = row["trailing_peak_at"] if "trailing_peak_at" in columns else None
        if activation_price is None and requested_activation_price is not None:
            activation_price = requested_activation_price
        if not activation_at and requested_activation_at:
            activation_at = requested_activation_at
        if requested_peak is not None and (current_peak is None or requested_peak >= current_peak):
            peak_at = requested_peak_at or peak_at
        conn.execute(
            """
            UPDATE portfolio_positions
            SET trailing_stop = ?, trailing_peak = ?, trailing_peak_at = ?, trailing_active = ?,
                trailing_stage = ?, trailing_activation_price = ?, trailing_activation_at = ?, updated_at = ?
            WHERE user_id = ? AND symbol = ?
            """,
            (
                effective_stop,
                effective_peak,
                peak_at,
                int(effective_active),
                effective_stage,
                activation_price,
                activation_at,
                now_iso(),
                owner_id,
                safe_symbol,
            ),
        )
    return get_portfolio_position(owner_id, safe_symbol)


def delete_portfolio_position(user_id: int, symbol: str) -> bool:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM portfolio_positions WHERE user_id = ? AND symbol = ?",
            (owner_id, symbol),
        )
    return cursor.rowcount > 0


def upsert_watchlist_item(user_id: int, item: dict) -> dict:
    owner_id = _personal_user_id(user_id)
    symbol = str(item.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    updated_at = now_iso()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT created_at FROM watchlist_items WHERE user_id = ? AND symbol = ?",
            (owner_id, symbol),
        ).fetchone()
        created_at = existing["created_at"] if existing else updated_at
        conn.execute(
            """
            INSERT INTO watchlist_items (user_id, symbol, name, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                name=excluded.name,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                owner_id,
                symbol,
                item.get("name"),
                item.get("note"),
                created_at,
                updated_at,
            ),
        )
    return get_watchlist_item(owner_id, symbol) or {}


def list_watchlist_items(user_id: int) -> list[dict]:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist_items WHERE user_id = ? ORDER BY updated_at DESC, symbol",
            (owner_id,),
        ).fetchall()
    return [_watchlist_item(row) for row in rows]


def get_watchlist_item(user_id: int, symbol: str) -> dict | None:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM watchlist_items WHERE user_id = ? AND symbol = ?",
            (owner_id, symbol),
        ).fetchone()
    return _watchlist_item(row) if row else None


def delete_watchlist_item(user_id: int, symbol: str) -> bool:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM watchlist_items WHERE user_id = ? AND symbol = ?",
            (owner_id, symbol),
        )
    return cursor.rowcount > 0


def upsert_stock_analysis_history(user_id: int, item: dict) -> dict:
    owner_id = _personal_user_id(user_id)
    symbol = str(item.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    name = str(item.get("name") or "").strip()
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO stock_analysis_history (user_id, symbol, name, last_analyzed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                name=excluded.name,
                last_analyzed_at=excluded.last_analyzed_at
            """,
            (owner_id, symbol, name, timestamp),
        )
    return get_stock_analysis_history_item(owner_id, symbol) or {}


def list_stock_analysis_history(user_id: int, limit: int = 10) -> list[dict]:
    owner_id = _personal_user_id(user_id)
    safe_limit = max(1, min(int(limit), 10))
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM stock_analysis_history
            WHERE user_id = ?
            ORDER BY last_analyzed_at DESC, symbol
            LIMIT ?
            """,
            (owner_id, safe_limit),
        ).fetchall()
    return [_stock_analysis_history_item(row) for row in rows]


def get_stock_analysis_history_item(user_id: int, symbol: str) -> dict | None:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM stock_analysis_history
            WHERE user_id = ? AND symbol = ?
            """,
            (owner_id, str(symbol or "").strip()),
        ).fetchone()
    return _stock_analysis_history_item(row) if row else None


def delete_stock_analysis_history(user_id: int, symbol: str) -> bool:
    owner_id = _personal_user_id(user_id)
    normalized_symbol = str(symbol or "").strip()
    if not normalized_symbol:
        return False
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM stock_analysis_history WHERE user_id = ? AND symbol = ?",
            (owner_id, normalized_symbol),
        )
    return cursor.rowcount > 0


def get_portfolio_account(user_id: int) -> dict:
    owner_id = _personal_user_id(user_id)
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO portfolio_account (user_id, cash_balance, updated_at)
            VALUES (?, 100000, ?)
            """,
            (owner_id, timestamp),
        )
        row = conn.execute(
            "SELECT cash_balance, updated_at FROM portfolio_account WHERE user_id = ?",
            (owner_id,),
        ).fetchone()
    return {
        "cashBalance": round(float(row["cash_balance"] or 0), 2),
        "updatedAt": row["updated_at"],
    }


def set_portfolio_cash(user_id: int, cash_balance: float) -> dict:
    owner_id = _personal_user_id(user_id)
    cash = round(float(cash_balance), 2)
    if cash < 0:
        raise ValueError("现金余额不能小于 0")
    if cash > 100_000_000:
        raise ValueError("现金余额不能超过 1 亿元")
    updated_at = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_account (user_id, cash_balance, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                cash_balance=excluded.cash_balance,
                updated_at=excluded.updated_at
            """,
            (owner_id, cash, updated_at),
        )
    return get_portfolio_account(owner_id)


def execute_portfolio_trade(user_id: int, trade: dict) -> dict:
    owner_id = _personal_user_id(user_id)
    action = str(trade.get("action") or "").strip().upper()
    symbol = str(trade.get("symbol") or "").strip()
    name = str(trade.get("name") or "").strip()
    note = str(trade.get("note") or "").strip()
    discipline_override = trade.get("disciplineOverride") is True
    raw_fee = trade.get("fee")
    try:
        price = round(float(trade.get("price")), 4)
        shares = int(trade.get("shares"))
        fee = round(float(raw_fee), 2) if raw_fee is not None else None
    except (TypeError, ValueError):
        raise ValueError("成交价、股数和手续费必须为有效数字") from None

    if action not in {"BUY", "SELL"}:
        raise ValueError("交易动作必须为 BUY 或 SELL")
    if not symbol:
        raise ValueError("股票代码不能为空")
    if price <= 0:
        raise ValueError("成交价必须大于 0")
    if shares <= 0:
        raise ValueError("成交股数必须大于 0")
    if fee is not None and fee < 0:
        raise ValueError("手续费不能小于 0")

    amount = round(price * shares, 2)
    if fee is None:
        fee = estimate_a_share_trade_fee(action, symbol, amount)["total"]
    created_at = now_iso()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        account_row = conn.execute(
            "SELECT cash_balance FROM portfolio_account WHERE user_id = ?",
            (owner_id,),
        ).fetchone()
        if not account_row:
            conn.execute(
                "INSERT INTO portfolio_account (user_id, cash_balance, updated_at) VALUES (?, 100000, ?)",
                (owner_id, created_at),
            )
            cash_before = 100_000.0
        else:
            cash_before = float(account_row["cash_balance"] or 0)

        position = conn.execute(
            "SELECT * FROM portfolio_positions WHERE user_id = ? AND symbol = ?",
            (owner_id, symbol),
        ).fetchone()
        shares_before = int(position["shares"]) if position else 0
        cost_before = float(position["cost_price"]) if position else None
        position_name = name or (position["name"] if position else "") or symbol
        discipline_override_applied = False
        discipline_override_reasons: list[dict] = []

        if action == "BUY":
            if shares % 100 != 0:
                raise ValueError("买入股数必须为 100 股的整数倍")
            cash_after = round(cash_before - amount - fee, 2)
            if cash_after < -0.0001:
                raise ValueError("可用现金不足，无法执行该买入计划")
            if position and price <= float(position["cost_price"]):
                discipline_override_reasons = [
                    {
                        "code": "AVERAGING_DOWN",
                        "message": "加仓价格未高于当前均摊成本，属于向下摊平。",
                        "price": price,
                        "costPrice": cost_before,
                    }
                ]
                if not discipline_override:
                    raise DisciplineConfirmationRequired(discipline_override_reasons)
                discipline_override_applied = True
            shares_after = shares_before + shares
            cost_after = round(
                (((cost_before or 0) * shares_before) + amount + fee) / shares_after,
                4,
            )
            realized_profit = None
            conn.execute(
                """
                INSERT INTO portfolio_positions (
                    user_id, symbol, name, cost_price, shares, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, symbol) DO UPDATE SET
                    name=excluded.name,
                    cost_price=excluded.cost_price,
                    shares=excluded.shares,
                    note=excluded.note,
                    updated_at=excluded.updated_at
                """,
                (
                    owner_id,
                    symbol,
                    position_name,
                    cost_after,
                    shares_after,
                    note or (position["note"] if position else ""),
                    position["created_at"] if position else created_at,
                    created_at,
                ),
            )
        else:
            if not position:
                raise ValueError("没有可卖出的持仓")
            if shares > shares_before:
                raise ValueError("卖出股数不能超过当前持仓")
            if amount < fee:
                raise ValueError("手续费不能高于卖出金额")
            cash_after = round(cash_before + amount - fee, 2)
            shares_after = shares_before - shares
            cost_after = cost_before if shares_after > 0 else None
            realized_profit = round((price - (cost_before or 0)) * shares - fee, 2)
            if shares_after > 0:
                conn.execute(
                    """
                    UPDATE portfolio_positions
                    SET name = ?, shares = ?, note = ?, updated_at = ?
                    WHERE user_id = ? AND symbol = ?
                    """,
                    (position_name, shares_after, note or (position["note"] or ""), created_at, owner_id, symbol),
                )
            else:
                conn.execute(
                    "DELETE FROM portfolio_positions WHERE user_id = ? AND symbol = ?",
                    (owner_id, symbol),
                )

        conn.execute(
            "UPDATE portfolio_account SET cash_balance = ?, updated_at = ? WHERE user_id = ?",
            (cash_after, created_at, owner_id),
        )
        cursor = conn.execute(
            """
            INSERT INTO portfolio_trades (
                user_id, action, symbol, name, price, shares, fee, amount, cash_before, cash_after,
                cost_before, cost_after, shares_before, shares_after, realized_profit, note,
                discipline_override, discipline_override_reasons, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                action,
                symbol,
                position_name,
                price,
                shares,
                fee,
                amount,
                cash_before,
                cash_after,
                cost_before,
                cost_after,
                shares_before,
                shares_after,
                realized_profit,
                note,
                1 if discipline_override_applied else 0,
                json.dumps(discipline_override_reasons, ensure_ascii=False),
                created_at,
            ),
        )
        trade_id = cursor.lastrowid

    return get_portfolio_trade(owner_id, trade_id) or {}


def list_portfolio_trades(user_id: int, limit: int = 30) -> list[dict]:
    owner_id = _personal_user_id(user_id)
    safe_limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio_trades WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, safe_limit),
        ).fetchall()
    return [_portfolio_trade_item(row) for row in rows]


def get_portfolio_trade(user_id: int, trade_id: int) -> dict | None:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_trades WHERE user_id = ? AND id = ?",
            (owner_id, trade_id),
        ).fetchone()
    return _portfolio_trade_item(row) if row else None


def create_discipline_journal(user_id: int, entry: dict) -> dict:
    owner_id = _personal_user_id(user_id)
    symbol = str(entry.get("symbol") or "").strip()
    action = str(entry.get("action") or "").strip()
    plan = entry.get("plan") or {}
    if not symbol or not action:
        raise ValueError("symbol and action are required")
    acknowledged = entry.get("acknowledged") or []
    if not isinstance(acknowledged, list):
        raise ValueError("acknowledged must be a list")
    created_at = now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO discipline_journal (
                user_id, symbol, action, trade_date, acknowledged_json, note, plan_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                symbol,
                action,
                str(entry.get("tradeDate") or ""),
                json.dumps(acknowledged, ensure_ascii=False),
                str(entry.get("note") or "").strip(),
                json.dumps(plan, ensure_ascii=False),
                created_at,
            ),
        )
        journal_id = cursor.lastrowid
    return get_discipline_journal_entry(owner_id, journal_id) or {}


def list_discipline_journal(user_id: int, limit: int = 20) -> list[dict]:
    owner_id = _personal_user_id(user_id)
    safe_limit = max(1, min(int(limit), 100))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM discipline_journal WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, safe_limit),
        ).fetchall()
    return [_discipline_journal_item(row) for row in rows]


def get_discipline_journal_entry(user_id: int, journal_id: int) -> dict | None:
    owner_id = _personal_user_id(user_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM discipline_journal WHERE user_id = ? AND id = ?",
            (owner_id, journal_id),
        ).fetchone()
    return _discipline_journal_item(row) if row else None


def upsert_f10_report_rows(symbol: str, report_name: str, rows: list[dict], page_number: int = 1) -> int:
    if not rows:
        return 0
    updated_at = now_iso()
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO f10_reports (
                symbol, report_name, record_key, report_date, end_date, notice_date,
                page_number, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, report_name, record_key) DO UPDATE SET
                report_date=excluded.report_date,
                end_date=excluded.end_date,
                notice_date=excluded.notice_date,
                page_number=excluded.page_number,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            [
                (
                    symbol,
                    report_name,
                    _record_key(row, index),
                    _clean_date(row.get("REPORT_DATE")),
                    _clean_date(row.get("END_DATE")),
                    _clean_date(row.get("NOTICE_DATE")),
                    page_number,
                    json.dumps(row, ensure_ascii=False),
                    updated_at,
                )
                for index, row in enumerate(rows)
            ],
        )
    return len(rows)


def get_f10_report_rows(symbol: str, report_name: str, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT raw_json FROM f10_reports
            WHERE symbol = ? AND report_name = ?
            ORDER BY COALESCE(report_date, end_date, notice_date, updated_at) DESC
            LIMIT ?
            """,
            (symbol, report_name, limit),
        ).fetchall()
    return [json.loads(row["raw_json"]) for row in rows]


def save_fundamental_summary(symbol: str, summary: dict) -> dict:
    payload = {
        "profile": summary.get("profile") or {},
        "valuation": summary.get("valuation") or {},
        "finance": summary.get("finance") or [],
        "holder": summary.get("holder") or [],
        "themes": summary.get("themes") or [],
        "business": summary.get("business") or {},
        "f10Status": summary.get("f10Status") or {},
    }
    updated_at = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO fundamental_summary (
                symbol, profile_json, valuation_json, finance_json, holder_json,
                themes_json, business_json, f10_status_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                profile_json=excluded.profile_json,
                valuation_json=excluded.valuation_json,
                finance_json=excluded.finance_json,
                holder_json=excluded.holder_json,
                themes_json=excluded.themes_json,
                business_json=excluded.business_json,
                f10_status_json=excluded.f10_status_json,
                updated_at=excluded.updated_at
            """,
            (
                symbol,
                json.dumps(payload["profile"], ensure_ascii=False),
                json.dumps(payload["valuation"], ensure_ascii=False),
                json.dumps(payload["finance"], ensure_ascii=False),
                json.dumps(payload["holder"], ensure_ascii=False),
                json.dumps(payload["themes"], ensure_ascii=False),
                json.dumps(payload["business"], ensure_ascii=False),
                json.dumps(payload["f10Status"], ensure_ascii=False),
                updated_at,
            ),
        )
    payload["symbol"] = symbol
    payload["updatedAt"] = updated_at
    return payload


def get_fundamental_summary(symbol: str, max_age: timedelta | None = None) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM fundamental_summary WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        return None
    if max_age and not is_fresh(row["updated_at"], max_age):
        return None
    return {
        "symbol": symbol,
        "profile": _loads(row["profile_json"], {}),
        "valuation": _loads(row["valuation_json"], {}),
        "finance": _loads(row["finance_json"], []),
        "holder": _loads(row["holder_json"], []),
        "themes": _loads(row["themes_json"], []),
        "business": _loads(row["business_json"], {}),
        "f10Status": _loads(row["f10_status_json"], {}),
        "updatedAt": row["updated_at"],
    }


def set_f10_status(symbol: str, status: str, total: int, completed: int, failures: list[dict], started_at: str | None = None, finished_at: str | None = None) -> dict:
    payload = {
        "symbol": symbol,
        "status": status,
        "totalReports": total,
        "completedReports": completed,
        "failedReports": len(failures),
        "failures": failures,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "updatedAt": now_iso(),
    }
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO f10_sync_status (
                symbol, status, total_reports, completed_reports, failed_reports,
                failures_json, started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                status=excluded.status,
                total_reports=excluded.total_reports,
                completed_reports=excluded.completed_reports,
                failed_reports=excluded.failed_reports,
                failures_json=excluded.failures_json,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                updated_at=excluded.updated_at
            """,
            (
                symbol,
                status,
                total,
                completed,
                len(failures),
                json.dumps(failures, ensure_ascii=False),
                started_at,
                finished_at,
                payload["updatedAt"],
            ),
        )
    return payload


def get_f10_status(symbol: str) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM f10_sync_status WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        return {
            "symbol": symbol,
            "status": "not_started",
            "totalReports": 0,
            "completedReports": 0,
            "failedReports": 0,
            "failures": [],
            "updatedAt": None,
        }
    return {
        "symbol": symbol,
        "status": row["status"],
        "totalReports": row["total_reports"],
        "completedReports": row["completed_reports"],
        "failedReports": row["failed_reports"],
        "failures": _loads(row["failures_json"], []),
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "updatedAt": row["updated_at"],
    }


def set_api_cache(provider: str, endpoint: str, cache_key: str, response: dict, expires_at: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO api_cache (provider, endpoint, cache_key, response_json, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, endpoint, cache_key) DO UPDATE SET
                response_json=excluded.response_json,
                expires_at=excluded.expires_at,
                updated_at=excluded.updated_at
            """,
            (provider, endpoint, cache_key, json.dumps(response, ensure_ascii=False), expires_at, now_iso()),
        )


def get_api_cache(provider: str, endpoint: str, cache_key: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT response_json, expires_at FROM api_cache
            WHERE provider = ? AND endpoint = ? AND cache_key = ?
            """,
            (provider, endpoint, cache_key),
        ).fetchone()
    if not row:
        return None
    if row["expires_at"]:
        try:
            if datetime.fromisoformat(row["expires_at"]) < datetime.now():
                return None
        except ValueError:
            return None
    return _loads(row["response_json"], None)


def delete_expired_api_cache() -> int:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM api_cache WHERE expires_at IS NOT NULL AND expires_at < ?", (now_iso(),))
        return cursor.rowcount


_KLINE_CONTRACT_COLUMNS = {
    "amount_unit": "TEXT",
    "volume_unit": "TEXT",
    "float_shares": "REAL",
    "provider_turnover": "REAL",
    "computed_turnover": "REAL",
    "turnover_source": "TEXT",
    "session": "TEXT",
    "source": "TEXT",
    "provider": "TEXT",
    "completed": "INTEGER NOT NULL DEFAULT 1",
    "quality_json": "TEXT",
    "timestamp_quality": "TEXT",
    "adjustment_version": "TEXT",
}


def _migrate_kline_contract(conn: sqlite3.Connection) -> None:
    """Add structured cache metadata without invalidating existing databases."""

    for table in ("klines", "current_klines"):
        columns = _table_columns(conn, table)
        for name, declaration in _KLINE_CONTRACT_COLUMNS.items():
            if name not in columns:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}')


def _migrate_current_klines(conn: sqlite3.Connection) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute("SELECT * FROM klines WHERE datetime >= ? AND datetime <= ?", (today, f"{today} 23:59")).fetchall()
    if not rows:
        return
    _upsert_kline_rows(
        conn,
        "current_klines",
        [
            (
                row["symbol"],
                row["period"],
                row["adjust"],
                row["datetime"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
                row["amount"],
                row["pct_change"],
                row["turnover_rate"],
                row["amount_unit"],
                row["volume_unit"],
                row["float_shares"],
                row["provider_turnover"],
                row["computed_turnover"],
                row["turnover_source"],
                row["session"],
                row["source"],
                row["provider"],
                row["completed"],
                row["quality_json"],
                row["timestamp_quality"],
                row["adjustment_version"],
                row["raw_json"],
                row["updated_at"],
            )
            for row in rows
        ],
    )
    conn.execute("DELETE FROM klines WHERE datetime >= ? AND datetime <= ?", (today, f"{today} 23:59"))


def _kline_db_row(symbol: str, period: str, adjust: str, item: dict, updated_at: str) -> tuple:
    amount = item.get("amount")
    volume = item.get("volume")
    float_shares = item.get("floatShares", item.get("float_shares"))
    provider_turnover = item.get("turnoverRate", item.get("providerTurnover", item.get("turnover_rate")))
    computed_turnover = item.get("computedTurnover")
    if computed_turnover is None:
        try:
            if amount is not None and float_shares and float(float_shares) > 0:
                computed_turnover = float(amount) / float(float_shares)
        except (TypeError, ValueError):
            computed_turnover = None
    turnover_source = item.get("turnoverSource") or ("PROVIDER" if provider_turnover is not None else "COMPUTED" if computed_turnover is not None else "UNKNOWN")
    return (
        symbol,
        period,
        adjust,
        item.get("date"),
        item.get("open"),
        item.get("high"),
        item.get("low"),
        item.get("close"),
        item.get("volume"),
        item.get("amount"),
        item.get("pctChange"),
        item.get("turnoverRate"),
        item.get("amountUnit", item.get("amount_unit")),
        item.get("volumeUnit", item.get("volume_unit")),
        float_shares,
        provider_turnover,
        computed_turnover,
        turnover_source,
        item.get("session"),
        item.get("source", item.get("provider")),
        item.get("provider"),
        1 if item.get("completed", item.get("isCompleted", True)) else 0,
        json.dumps(item.get("quality", item.get("qualityFlags", [])), ensure_ascii=False),
        item.get("timestampQuality", "VALID" if item.get("date") else "UNKNOWN"),
        item.get("adjustmentVersion", item.get("adjustment", adjust)),
        json.dumps(item, ensure_ascii=False),
        updated_at,
    )


def _upsert_kline_rows(conn: sqlite3.Connection, table: str, rows: list[tuple]) -> None:
    if not rows:
        return
    if table not in {"klines", "current_klines"}:
        raise ValueError("invalid kline table")
    placeholders = ", ".join("?" for _ in range(27))
    conn.executemany(
        f"""
        INSERT INTO {table} (
            symbol, period, adjust, datetime, open, high, low, close,
            volume, amount, pct_change, turnover_rate,
            amount_unit, volume_unit, float_shares, provider_turnover, computed_turnover,
            turnover_source, session, source, provider, completed, quality_json,
            timestamp_quality, adjustment_version, raw_json, updated_at
        ) VALUES ({placeholders})
        ON CONFLICT(symbol, period, adjust, datetime) DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume,
            amount=excluded.amount,
            pct_change=excluded.pct_change,
            turnover_rate=excluded.turnover_rate,
            amount_unit=excluded.amount_unit,
            volume_unit=excluded.volume_unit,
            float_shares=excluded.float_shares,
            provider_turnover=excluded.provider_turnover,
            computed_turnover=excluded.computed_turnover,
            turnover_source=excluded.turnover_source,
            session=excluded.session,
            source=excluded.source,
            provider=excluded.provider,
            completed=excluded.completed,
            quality_json=excluded.quality_json,
            timestamp_quality=excluded.timestamp_quality,
            adjustment_version=excluded.adjustment_version,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        rows,
    )


def _klines_cover_requested_range(rows: list[sqlite3.Row], period: str, start_date: str, end_date: str) -> bool:
    if not rows:
        return False
    first = str(rows[0]["datetime"])
    last = str(rows[-1]["datetime"])
    if start_date and _date_gap(first[:10], _date_key(start_date, end=False)[:10]) > 10:
        return False
    if end_date:
        end_key = _date_key(end_date, end=True)
        today = datetime.now().strftime("%Y-%m-%d")
        if _date_key(end_date).startswith(today):
            return any(str(row["datetime"]).startswith(today) for row in rows)
        if _date_gap(end_key[:10], last[:10]) > 10:
            return False
    if period == "101":
        return _daily_rows_are_continuous_enough(rows, start_date, end_date)
    return _intraday_rows_are_continuous_enough(rows, start_date, end_date)


def _daily_rows_are_continuous_enough(rows: list[sqlite3.Row], start_date: str, end_date: str) -> bool:
    if len(rows) < 2:
        return True
    dates = [pd[:10] for pd in (str(row["datetime"]) for row in rows)]
    for previous, current in zip(dates, dates[1:]):
        gap = (datetime.fromisoformat(current) - datetime.fromisoformat(previous)).days
        if gap > 10:
            return False
    return True


def _intraday_rows_are_continuous_enough(rows: list[sqlite3.Row], start_date: str, end_date: str) -> bool:
    if len(rows) < 2:
        return True
    dates = sorted({str(row["datetime"])[:10] for row in rows})
    for previous, current in zip(dates, dates[1:]):
        gap = (datetime.fromisoformat(current) - datetime.fromisoformat(previous)).days
        if gap > 10:
            return False
    return True


def _date_gap(later: str, earlier: str) -> int:
    try:
        return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).days
    except ValueError:
        return 9999


def _security_item(row: sqlite3.Row) -> dict:
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "label": f'{row["symbol"]} - {row["name"]}',
        "market": row["market"],
        "secid": row["secid"],
        "secucode": row["secucode"],
        "isSt": bool(row["is_st"]),
        "latestPrice": row["latest_price"],
        "pctChange": row["pct_change"],
        "turnoverRate": row["turnover_rate"],
        "peDynamic": row["pe_dynamic"],
        "pb": row["pb"],
        "totalMarketCap": row["total_market_cap"],
        "updatedAt": row["updated_at"],
    }


def _kline_item(row: sqlite3.Row) -> dict:
    columns = set(row.keys())
    def value(name: str, fallback: object = None):
        return row[name] if name in columns else fallback

    quality_raw = value("quality_json")
    return {
        "date": row["datetime"],
        "open": float(row["open"] or 0),
        "high": float(row["high"] or 0),
        "low": float(row["low"] or 0),
        "close": float(row["close"] or 0),
        "volume": float(row["volume"] or 0),
        "amount": float(row["amount"] or 0),
        "pctChange": float(row["pct_change"] or 0),
        "turnoverRate": row["turnover_rate"],
        "amountUnit": value("amount_unit"),
        "volumeUnit": value("volume_unit"),
        "floatShares": value("float_shares"),
        "providerTurnover": value("provider_turnover"),
        "computedTurnover": value("computed_turnover"),
        "turnoverSource": value("turnover_source", "UNKNOWN"),
        "session": value("session", "unknown"),
        "source": value("source", "unknown"),
        "provider": value("provider"),
        "completed": bool(value("completed", 1)),
        "qualityFlags": _json_value(quality_raw, []),
        "timestampQuality": value("timestamp_quality", "UNKNOWN"),
        "adjustmentVersion": value("adjustment_version"),
    }


def _daily_quote_item(row: sqlite3.Row) -> dict:
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "label": f'{row["symbol"]} - {row["name"]}' if row["name"] else row["symbol"],
        "market": row["market"],
        "latestPrice": row["latest_price"],
        "pctChange": row["pct_change"],
        "priceChange": row["price_change"],
        "volume": row["volume"],
        "amount": row["amount"],
        "turnoverRate": row["turnover_rate"],
        "peDynamic": row["pe_dynamic"],
        "pb": row["pb"],
        "totalMarketCap": row["total_market_cap"],
        "quoteMinute": row["quote_minute"],
        "updatedAt": row["updated_at"],
    }


def _portfolio_item(row: sqlite3.Row) -> dict:
    columns = set(row.keys())
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "label": f'{row["symbol"]} - {row["name"]}' if row["name"] else row["symbol"],
        "entryPrice": row["cost_price"],
        "costPrice": row["cost_price"],
        "shares": row["shares"],
        "note": row["note"] or "",
        "trailingStop": row["trailing_stop"] if "trailing_stop" in columns else None,
        "trailingPeak": row["trailing_peak"] if "trailing_peak" in columns else None,
        "trailingPeakAt": row["trailing_peak_at"] if "trailing_peak_at" in columns else None,
        "trailingActive": bool(row["trailing_active"]) if "trailing_active" in columns else False,
        "trailingStage": row["trailing_stage"] if "trailing_stage" in columns else "INITIAL",
        "trailingActivationPrice": row["trailing_activation_price"] if "trailing_activation_price" in columns else None,
        "trailingActivationAt": row["trailing_activation_at"] if "trailing_activation_at" in columns else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _binance_simulated_position_item(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "network": row["network"],
        "marketMode": row["market_mode"],
        "symbol": row["symbol"],
        "side": row["side"],
        "quantity": row["quantity"],
        "costPrice": row["cost_price"],
        "leverage": row["leverage"],
        "planSnapshot": _loads(row["plan_json"], {}),
        "protectedStop": row["protected_stop"],
        "movingStop": row["moving_stop"],
        "movingStopActive": bool(row["moving_stop_active"]),
        "movingStopActivationPrice": row["moving_stop_activation_price"],
        "movingStopActivationAt": row["moving_stop_activation_at"],
        "executionStatus": row["execution_status"] or "EXECUTING",
        "stoppedAt": row["stopped_at"],
        "stopPrice": row["stop_price"],
        "stopReason": row["stop_reason"] or "",
        "dynamicPlan": _loads(row["dynamic_plan_json"], {}),
        "lastPrice": row["last_price"],
        "lastUnrealizedPnl": row["last_unrealized_pnl"],
        "lastUnrealizedPnlPercent": row["last_unrealized_pnl_percent"],
        "note": row["note"] or "",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _watchlist_item(row: sqlite3.Row) -> dict:
    return {
        "symbol": row["symbol"],
        "name": row["name"] or "",
        "label": f'{row["symbol"]} - {row["name"]}' if row["name"] else row["symbol"],
        "note": row["note"] or "",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _stock_analysis_history_item(row: sqlite3.Row) -> dict:
    return {
        "symbol": row["symbol"],
        "name": row["name"] or "",
        "label": f'{row["symbol"]} - {row["name"]}' if row["name"] else row["symbol"],
        "lastAnalyzedAt": row["last_analyzed_at"],
    }


def _portfolio_trade_item(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "action": row["action"],
        "symbol": row["symbol"],
        "name": row["name"] or "",
        "price": float(row["price"] or 0),
        "shares": int(row["shares"] or 0),
        "fee": float(row["fee"] or 0),
        "amount": float(row["amount"] or 0),
        "cashBefore": float(row["cash_before"] or 0),
        "cashAfter": float(row["cash_after"] or 0),
        "costBefore": float(row["cost_before"]) if row["cost_before"] is not None else None,
        "costAfter": float(row["cost_after"]) if row["cost_after"] is not None else None,
        "sharesBefore": int(row["shares_before"] or 0),
        "sharesAfter": int(row["shares_after"] or 0),
        "realizedProfit": float(row["realized_profit"]) if row["realized_profit"] is not None else None,
        "note": row["note"] or "",
        "disciplineOverride": bool(row["discipline_override"]),
        "disciplineOverrideReasons": _json_value(row["discipline_override_reasons"], []),
        "createdAt": row["created_at"],
    }


def _discipline_journal_item(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "action": row["action"],
        "tradeDate": row["trade_date"],
        "acknowledged": _json_value(row["acknowledged_json"], []),
        "note": row["note"] or "",
        "plan": _json_value(row["plan_json"], {}),
        "createdAt": row["created_at"],
    }


def _json_value(raw: str | None, fallback: object) -> object:
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _record_key(row: dict, index: int) -> str:
    for key in ("SECURITY_CODE", "SECUCODE", "REPORT_DATE", "END_DATE", "NOTICE_DATE", "HOLDER_RANK", "BOARD_CODE", "ORG_CODE"):
        value = row.get(key)
        if value is not None and value != "":
            break
    parts = [
        str(row.get("SECURITY_CODE") or row.get("SECUCODE") or ""),
        str(row.get("REPORT_DATE") or row.get("END_DATE") or row.get("NOTICE_DATE") or ""),
        str(row.get("HOLDER_RANK") or row.get("BOARD_CODE") or row.get("ORG_CODE") or index),
    ]
    return "|".join(parts)


def _clean_date(value: object) -> str | None:
    if value in (None, "", "-"):
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def _date_key(value: str, end: bool = False) -> str:
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}{' 23:59' if end else ''}"
    return text


def _loads(value: str | None, fallback):
    try:
        return json.loads(value) if value else fallback
    except json.JSONDecodeError:
        return fallback


if __name__ == "__main__":
    init_db()
    print(db_path())
