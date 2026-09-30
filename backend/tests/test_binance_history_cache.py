import os
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.services.binance_client import BinanceApiError
from backend.services import binance_strategy_backtest as backtest
from backend.services import database as db


def _bar(open_time: int, close_time: int) -> dict:
    return {
        "openTime": open_time,
        "closeTime": close_time,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "10",
    }


def _bars(start_time: int, end_time: int, interval_ms: int) -> list[dict]:
    return [
        _bar(open_time, open_time + interval_ms - 1)
        for open_time in range(start_time, end_time, interval_ms)
    ]


class BinanceHistoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.environ = patch.dict(os.environ, {"CHANLUN_DB_PATH": os.path.join(self.tempdir.name, "history.db")})
        self.environ.start()
        db.init_db()

    def tearDown(self):
        self.environ.stop()
        self.tempdir.cleanup()

    def test_history_coverage_keeps_only_uninspected_ranges_missing(self):
        db.upsert_binance_futures_history_chunk("mainnet", "BTCUSDT", "1m", [_bar(100, 159)], 100, 160)
        self.assertEqual(
            db.get_binance_futures_history_missing_ranges("mainnet", "BTCUSDT", "1m", 100, 400),
            [(160, 400)],
        )

        # A successful empty response is still coverage. The contract may not
        # have existed during this section, so it must not be retried forever.
        db.upsert_binance_futures_history_chunk("mainnet", "BTCUSDT", "1m", [], 160, 250)
        db.upsert_binance_futures_history_chunk("mainnet", "BTCUSDT", "1m", [_bar(250, 309)], 250, 400)

        self.assertEqual(
            db.get_binance_futures_history_missing_ranges("mainnet", "BTCUSDT", "1m", 100, 400),
            [],
        )
        rows = db.list_binance_futures_history_klines("mainnet", "BTCUSDT", "1m", 100, 400)
        self.assertEqual([row["openTime"] for row in rows], [100, 250])

    def test_batch_history_read_groups_symbols_in_one_local_read(self):
        db.upsert_binance_futures_history_chunk(
            "mainnet", "BTCUSDT", "5m", [_bar(100, 279), _bar(280, 459)], 100, 500
        )
        db.upsert_binance_futures_history_chunk(
            "mainnet", "ETHUSDT", "5m", [_bar(100, 279)], 100, 500
        )

        result = db.list_binance_futures_history_klines_batch(
            "mainnet", ["ETHUSDT", "BTCUSDT", "MISSINGUSDT"], "5m", 100, 500
        )

        self.assertEqual([row["openTime"] for row in result["BTCUSDT"]], [100, 280])
        self.assertEqual([row["openTime"] for row in result["ETHUSDT"]], [100])
        self.assertEqual(result["MISSINGUSDT"], [])

    def test_repeat_preparation_does_not_refetch_a_completed_fixed_scope(self):
        fixed_values = {
            "FIXED_HISTORY_START_TIME": 100,
            "FIXED_HISTORY_END_TIME": 400,
            "FIXED_HISTORY_INTERVALS": ("1m",),
            "HISTORY_DOWNLOAD_WORKERS": 1,
        }
        response = {"items": [_bar(100, 159)], "stale": False}
        with patch.multiple(backtest, **fixed_values), patch.object(
            backtest,
            "get_futures_historical_klines",
            return_value=response,
        ) as fetch:
            first = backtest._ensure_fixed_history("test-job", "mainnet", [{"symbol": "BTCUSDT"}])
            second = backtest._ensure_fixed_history("test-job", "mainnet", [{"symbol": "BTCUSDT"}])

        self.assertTrue(first["ready"])
        self.assertTrue(second["ready"])
        self.assertEqual(fetch.call_count, 1)

    def test_rate_limited_download_stops_and_keeps_written_pages(self):
        fixed_values = {
            "FIXED_HISTORY_START_TIME": 100,
            "FIXED_HISTORY_END_TIME": 220,
            "FIXED_HISTORY_INTERVALS": ("1m",),
            "HISTORY_DOWNLOAD_WORKERS": 1,
            "HISTORY_REQUEST_MIN_INTERVAL_SECONDS": 0,
            "HISTORY_PAGE_LIMIT": 1,
        }
        with patch.multiple(backtest, **fixed_values), patch.object(
            backtest,
            "get_futures_historical_klines",
            side_effect=[
                {"items": [_bar(100, 159)], "stale": False},
                BinanceApiError("Too many requests", status_code=429, exchange_code=-1003),
            ],
        ) as fetch, patch.object(backtest.time, "sleep") as sleep:
            result = backtest._ensure_fixed_history("test-job", "mainnet", [{"symbol": "BTCUSDT"}])

        self.assertFalse(result["ready"])
        self.assertTrue(result["rateLimited"])
        self.assertEqual(result["rateLimitMessage"], "Too many requests")
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_not_called()
        rows = db.list_binance_futures_history_klines("mainnet", "BTCUSDT", "1m", 100, 220)
        self.assertEqual([row["openTime"] for row in rows], [100])

    def test_history_fill_job_never_loads_or_replays_history(self):
        prepared = {
            "ready": True,
            "universeCount": 1,
            "missingRangeCount": 0,
            "failedScopes": [],
            "rateLimited": False,
            "rateLimitMessage": "",
        }

        def run_immediately(function, *args, **kwargs):
            function(*args, **kwargs)

        with patch.object(
            backtest,
            "get_all_futures_markets",
            return_value={"items": [{"symbol": "BTCUSDT"}]},
        ), patch.object(
            backtest,
            "_ensure_fixed_history",
            return_value=prepared,
        ), patch.object(
            backtest,
            "_load_macro_history",
        ) as load_macro, patch.object(
            backtest,
            "_run_replay",
        ) as replay, patch.object(
            backtest._job_executor,
            "submit",
            side_effect=run_immediately,
        ):
            job = backtest.start_fixed_history_fill("mainnet")

        self.assertEqual(job["status"], "COMPLETED")
        self.assertTrue(job["result"]["historyReady"])
        load_macro.assert_not_called()
        replay.assert_not_called()

    def test_history_status_uses_partial_local_coverage_when_market_list_is_unavailable(self):
        fixed_values = {
            "FIXED_HISTORY_START_TIME": 100,
            "FIXED_HISTORY_END_TIME": 400,
            "FIXED_HISTORY_INTERVALS": ("1m", "5m"),
        }
        db.upsert_binance_futures_history_chunk(
            "mainnet", "LOCALUSDT", "1m", [_bar(100, 159)], 100, 400
        )
        with patch.multiple(backtest, **fixed_values), patch.object(
            backtest,
            "get_all_futures_markets",
            side_effect=BinanceApiError("market list unavailable", status_code=503),
        ):
            backtest._history_market_universe_cache.clear()
            backtest._history_completeness_cache.clear()
            result = backtest.get_fixed_history_completeness("mainnet")

        self.assertEqual(result["symbolCount"], 1)
        self.assertEqual(result["requiredSeries"], 2)
        self.assertEqual(result["historySource"], "LOCAL_HISTORY")
        self.assertTrue(result["offlineOnly"])
        self.assertEqual(result["completeSeries"], 0)
        self.assertEqual({item["interval"] for item in result["incomplete"]}, {"1m", "5m"})

    def test_partial_history_universe_is_not_reused_for_strict_replay_resolution(self):
        fixed_values = {
            "FIXED_HISTORY_START_TIME": 300_000,
            "FIXED_HISTORY_END_TIME": 900_000,
            "FIXED_HISTORY_INTERVALS": ("1m", "5m"),
            "EARLIEST_BACKTEST_START_TIME": 300_000,
            "DEFAULT_BACKTEST_START_TIME": 300_000,
        }
        db.upsert_binance_futures_history_chunk(
            "mainnet",
            "PARTIALUSDT",
            "1m",
            [_bar(300_000, 359_999), _bar(360_000, 419_999)],
            300_000,
            900_000,
        )
        with patch.multiple(backtest, **fixed_values), patch.object(
            backtest,
            "get_all_futures_markets",
            side_effect=BinanceApiError("market list unavailable", status_code=503),
        ):
            backtest._history_market_universe_cache.clear()
            partial = backtest._get_fixed_history_market_universe("mainnet", allow_partial_local=True)
            self.assertEqual(partial["symbols"], ["PARTIALUSDT"])

            with self.assertRaises(BinanceApiError):
                backtest._get_fixed_history_market_universe("mainnet", allow_partial_local=False)

    def test_history_status_falls_back_to_local_coverage_after_market_lookup_timeout(self):
        db.upsert_binance_futures_history_chunk(
            "mainnet",
            "TIMEOUTUSDT",
            "1m",
            [_bar(300_000, 359_999)],
            300_000,
            900_000,
        )

        def slow_market_lookup(_network):
            time.sleep(0.1)
            return {"items": [{"symbol": "TIMEOUTUSDT"}]}

        with patch.multiple(
            backtest,
            FIXED_HISTORY_START_TIME=300_000,
            FIXED_HISTORY_END_TIME=900_000,
            FIXED_HISTORY_INTERVALS=("1m",),
            HISTORY_MARKET_LOOKUP_TIMEOUT_SECONDS=0.01,
        ), patch.object(backtest, "get_all_futures_markets", side_effect=slow_market_lookup):
            backtest._history_market_universe_cache.clear()
            universe = backtest._get_fixed_history_market_universe("mainnet", allow_partial_local=True)

        self.assertEqual(universe["source"], "LOCAL_HISTORY")
        self.assertEqual(universe["symbols"], ["TIMEOUTUSDT"])

    def test_position_table_migration_raises_the_legacy_leverage_check_to_twenty(self):
        with db.get_connection() as conn:
            conn.execute("DROP TABLE binance_simulated_positions")
            conn.execute(
                """
                CREATE TABLE binance_simulated_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    network TEXT NOT NULL CHECK (network IN ('mainnet', 'testnet')),
                    market_mode TEXT NOT NULL CHECK (market_mode IN ('SPOT', 'FUTURES')),
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
                    quantity REAL NOT NULL CHECK (quantity > 0),
                    cost_price REAL NOT NULL CHECK (cost_price > 0),
                    leverage REAL NOT NULL DEFAULT 1 CHECK (leverage >= 1 AND leverage <= 10),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, network, market_mode, symbol, side)
                )
                """
            )
            db._migrate_binance_simulated_positions(conn)
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'binance_simulated_positions'"
            ).fetchone()["sql"]
            self.assertIn("leverage <= 20", str(table_sql))
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("migration-user", "hash", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("migration-user",)).fetchone()["id"]
            conn.execute(
                """
                INSERT INTO binance_simulated_positions (
                    user_id, network, market_mode, symbol, side, quantity, cost_price,
                    leverage, note, created_at, updated_at
                ) VALUES (?, 'mainnet', 'FUTURES', 'BTCUSDT', 'LONG', 1, 100, 20, '', ?, ?)
                """,
                (user_id, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )

    def test_history_fill_uses_partial_local_coverage_when_market_list_is_rate_limited(self):
        prepared = {
            "ready": True,
            "universeCount": 1,
            "missingRangeCount": 0,
            "failedScopes": [],
            "rateLimited": False,
            "rateLimitMessage": "",
        }

        def run_immediately(function, *args, **kwargs):
            function(*args, **kwargs)

        db.upsert_binance_futures_history_chunk(
            "mainnet", "LOCALUSDT", "1m", [_bar(100, 159)], 100, 400
        )
        with patch.multiple(
            backtest,
            FIXED_HISTORY_START_TIME=100,
            FIXED_HISTORY_END_TIME=400,
            FIXED_HISTORY_INTERVALS=("1m", "5m"),
        ), patch.object(
            backtest,
            "get_all_futures_markets",
            side_effect=BinanceApiError("market list unavailable", status_code=503),
        ), patch.object(backtest, "_ensure_fixed_history", return_value=prepared) as ensure, patch.object(
            backtest._job_executor,
            "submit",
            side_effect=run_immediately,
        ):
            backtest._history_market_universe_cache.clear()
            job = backtest.start_fixed_history_fill("mainnet")

        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(ensure.call_args.args[1], "mainnet")
        self.assertEqual(ensure.call_args.args[2][0]["symbol"], "LOCALUSDT")

    def test_completeness_requires_coverage_and_every_expected_candle(self):
        start_time = 1_704_067_200_000
        end_time = start_time + 10 * 60_000
        for symbol in ("BTCUSDT", "ETHUSDT"):
            for interval, interval_ms in (("1m", 60_000), ("5m", 300_000)):
                db.upsert_binance_futures_history_chunk(
                    "mainnet",
                    symbol,
                    interval,
                    _bars(start_time, end_time, interval_ms),
                    start_time,
                    end_time,
                )

        result = db.summarize_binance_futures_history_completeness(
            "mainnet", ["BTCUSDT", "ETHUSDT"], ["1m", "5m"], start_time, end_time
        )

        self.assertEqual(result["requiredSeries"], 4)
        self.assertEqual(result["coverageCompleteSeries"], 4)
        self.assertEqual(result["dataCompleteSeries"], 4)
        self.assertEqual(result["completeSeries"], 4)
        self.assertEqual(result["percent"], 100)
        self.assertEqual(result["incomplete"], [])

    def test_completeness_rejects_empty_coverage_missing_bars_and_invalid_timestamps(self):
        start_time = 1_704_067_200_000
        end_time = start_time + 6 * 60_000
        complete = _bars(start_time, end_time, 60_000)
        db.upsert_binance_futures_history_chunk("mainnet", "COVERAGEUSDT", "1m", [], start_time, end_time)
        db.upsert_binance_futures_history_chunk(
            "mainnet", "GAPUSDT", "1m", [complete[0], *complete[2:]], start_time, end_time
        )
        malformed = [dict(item) for item in complete]
        malformed[-1]["closeTime"] = malformed[-1]["openTime"] + 58_000
        db.upsert_binance_futures_history_chunk("mainnet", "BADTIMEUSDT", "1m", malformed, start_time, end_time)

        result = db.summarize_binance_futures_history_completeness(
            "mainnet",
            ["COVERAGEUSDT", "GAPUSDT", "BADTIMEUSDT"],
            ["1m"],
            start_time,
            end_time,
        )

        self.assertEqual(result["coverageCompleteSeries"], 3)
        self.assertEqual(result["dataCompleteSeries"], 0)
        self.assertEqual(result["completeSeries"], 0)
        self.assertEqual(result["percent"], 0)
        self.assertEqual(
            {(item["symbol"], item["interval"]) for item in result["incomplete"]},
            {("COVERAGEUSDT", "1m"), ("GAPUSDT", "1m"), ("BADTIMEUSDT", "1m")},
        )

    def test_complete_check_is_persisted_and_reused_without_rescanning_rows(self):
        start_time = 1_704_067_200_000
        end_time = start_time + 6 * 60_000
        db.upsert_binance_futures_history_chunk(
            "mainnet", "CACHEDUSDT", "1m", _bars(start_time, end_time, 60_000), start_time, end_time
        )

        first = db.summarize_binance_futures_history_completeness(
            "mainnet", ["CACHEDUSDT"], ["1m"], start_time, end_time
        )
        check = db.get_binance_futures_history_completeness_check(
            "mainnet", "CACHEDUSDT", "1m", start_time, end_time
        )
        self.assertEqual(first["completeSeries"], 1)
        self.assertIsNotNone(check)
        self.assertTrue(check["checked"])
        self.assertTrue(check["complete"])

        # A direct out-of-band change is intentionally invisible to the next
        # call: a checked-complete fixed series is served from the check table.
        with db.get_connection() as conn:
            conn.execute(
                """
                UPDATE binance_futures_history_klines
                SET close_time = close_time - 1
                WHERE network = ? AND symbol = ? AND interval = ? AND open_time = ?
                """,
                ("mainnet", "CACHEDUSDT", "1m", start_time),
            )

        second = db.summarize_binance_futures_history_completeness(
            "mainnet", ["CACHEDUSDT"], ["1m"], start_time, end_time
        )
        self.assertEqual(second["completeSeries"], 1)
        self.assertTrue(second["incomplete"] == [])

    def test_incomplete_check_is_rechecked_after_missing_bar_is_saved(self):
        start_time = 1_704_067_200_000
        end_time = start_time + 6 * 60_000
        complete = _bars(start_time, end_time, 60_000)
        db.upsert_binance_futures_history_chunk(
            "mainnet", "RECHECKUSDT", "1m", [complete[0], *complete[2:]], start_time, end_time
        )

        first = db.summarize_binance_futures_history_completeness(
            "mainnet", ["RECHECKUSDT"], ["1m"], start_time, end_time
        )
        self.assertEqual(first["completeSeries"], 0)
        self.assertTrue(first["incomplete"][0]["checked"])

        db.upsert_binance_futures_history_chunk(
            "mainnet", "RECHECKUSDT", "1m", [complete[1]], start_time, end_time
        )
        second = db.summarize_binance_futures_history_completeness(
            "mainnet", ["RECHECKUSDT"], ["1m"], start_time, end_time
        )
        self.assertEqual(second["completeSeries"], 1)
        self.assertEqual(second["incomplete"], [])

    def test_fixed_history_status_uses_all_active_contracts_without_starting_downloads(self):
        start_time = 1_704_067_200_000
        end_time = start_time + 6 * 60_000
        db.upsert_binance_futures_history_chunk(
            "testnet", "STATUSUSDT", "1m", _bars(start_time, end_time, 60_000), start_time, end_time
        )
        backtest._invalidate_fixed_history_completeness("testnet")
        with patch.multiple(
            backtest,
            FIXED_HISTORY_START_TIME=start_time,
            FIXED_HISTORY_END_TIME=end_time,
            FIXED_HISTORY_INTERVALS=("1m",),
        ), patch.object(
            backtest,
            "get_all_futures_markets",
            return_value={"items": [{"symbol": "STATUSUSDT"}], "updatedAt": 123, "stale": False},
        ) as markets:
            result = backtest.get_fixed_history_completeness("testnet")
            cached = backtest.get_fixed_history_completeness("testnet")

        self.assertEqual(result["symbolCount"], 1)
        self.assertEqual(result["completeSeries"], 1)
        self.assertEqual(result["requiredSeries"], 1)
        self.assertEqual(result["percent"], 100)
        self.assertEqual(result["marketUpdatedAt"], 123)
        self.assertEqual(cached, result)
        markets.assert_called_once_with("testnet")

    def test_replay_universe_uses_complete_local_history_without_a_market_request(self):
        start_time = 60_000
        end_time = 180_000
        db.upsert_binance_futures_history_chunk(
            "mainnet",
            "LOCALUSDT",
            "1m",
            [_bar(start_time, 119_999), _bar(120_000, 179_999)],
            start_time,
            end_time,
        )
        with backtest._history_completeness_lock:
            backtest._replay_history_market_universe_cache.clear()
        with patch.multiple(
            backtest,
            FIXED_HISTORY_START_TIME=start_time,
            FIXED_HISTORY_END_TIME=end_time,
            FIXED_HISTORY_INTERVALS=("1m",),
            EARLIEST_BACKTEST_START_TIME=start_time,
            DEFAULT_BACKTEST_START_TIME=start_time,
            BACKTEST_INTERVAL_MS=60_000,
        ), patch.object(backtest, "get_all_futures_markets") as markets:
            universe = backtest._get_replay_market_universe("mainnet")

        self.assertEqual(universe["source"], "LOCAL_HISTORY")
        self.assertTrue(universe["offlineOnly"])
        self.assertEqual(universe["symbols"], ["LOCALUSDT"])
        self.assertEqual(universe["quotes"][0]["symbol"], "LOCALUSDT")
        markets.assert_not_called()

    def test_fixed_history_excludes_contracts_onboarded_after_window_start(self):
        with patch.object(backtest, "FIXED_HISTORY_START_TIME", 1_000):
            selected, excluded = backtest._select_fixed_history_quotes([
                {"symbol": "OLDUSDT", "onboardDate": 999},
                {"symbol": "ATSTARTUSDT", "onboardDate": 1_000},
                {"symbol": "NEWUSDT", "onboardDate": 1_001},
                {"symbol": "UNKNOWNUSDT"},
            ])

        self.assertEqual([item["symbol"] for item in selected], ["OLDUSDT", "ATSTARTUSDT", "UNKNOWNUSDT"])
        self.assertEqual([item["symbol"] for item in excluded], ["NEWUSDT"])
        self.assertEqual(excluded[0]["reason"], "上线时间晚于固定历史区间开始时间，无法覆盖整段 6 至 8 月数据")

    def test_history_missing_scopes_follow_download_priority(self):
        with patch.multiple(
            backtest,
            FIXED_HISTORY_START_TIME=100,
            FIXED_HISTORY_END_TIME=400,
            FIXED_HISTORY_INTERVALS=("1m", "5m", "15m", "1h", "4h"),
        ), patch.object(
            db,
            "get_binance_futures_history_completeness_checks",
            return_value={},
        ), patch.object(
            db,
            "get_binance_futures_history_missing_ranges",
            return_value=[(100, 400)],
        ):
            scopes = backtest._history_missing_scopes("mainnet", [{"symbol": "BTCUSDT"}])

        self.assertEqual(
            [scope["interval"] for scope in scopes],
            ["4h", "1h", "15m", "5m", "1m"],
        )
