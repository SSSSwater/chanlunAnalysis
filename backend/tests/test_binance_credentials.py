from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from backend.services import database as db
from backend.services import binance_account_worker as account_worker


class BinanceCredentialStorageTests(unittest.TestCase):
    def test_credentials_are_encrypted_and_scoped_to_each_user(self):
        encryption_key = Fernet.generate_key().decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "credentials.db")
            with patch.dict(
                os.environ,
                {
                    "CHANLUN_DB_PATH": database_path,
                    "BINANCE_CREDENTIAL_ENCRYPTION_KEY": encryption_key,
                },
                clear=False,
            ):
                db.init_db()
                first_user = db.register_user("first-binance-user", "password-123")
                second_user = db.register_user("second-binance-user", "password-123")

                saved = db.save_user_binance_credentials(
                    first_user["id"],
                    network="mainnet",
                    api_key="first-public-key",
                    api_secret="first-private-key",
                )
                first_profile = db.get_user_binance_credentials(first_user["id"])
                first_secrets = db.get_user_binance_credentials(first_user["id"], include_secrets=True)
                second_profile = db.get_user_binance_credentials(second_user["id"])

                self.assertTrue(saved["configured"])
                self.assertTrue(first_profile["configured"])
                self.assertEqual(first_profile["apiKeyMasked"], "firs...-key")
                self.assertNotIn("apiSecret", first_profile)
                self.assertEqual(first_secrets["apiKey"], "first-public-key")
                self.assertEqual(first_secrets["apiSecret"], "first-private-key")
                self.assertFalse(second_profile["configured"])

                with db.get_connection() as conn:
                    row = conn.execute(
                        "SELECT api_key_encrypted, api_secret_encrypted FROM user_binance_credentials WHERE user_id = ?",
                        (first_user["id"],),
                    ).fetchone()
                self.assertNotEqual(row["api_key_encrypted"], "first-public-key")
                self.assertNotEqual(row["api_secret_encrypted"], "first-private-key")

    def test_account_refresh_only_uses_saved_mainnet_credentials(self):
        encryption_key = Fernet.generate_key().decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "credentials.db")
            with patch.dict(
                os.environ,
                {
                    "CHANLUN_DB_PATH": database_path,
                    "BINANCE_CREDENTIAL_ENCRYPTION_KEY": encryption_key,
                },
                clear=False,
            ):
                db.init_db()
                user = db.register_user("refresh-user", "password-123")
                db.save_user_binance_credentials(
                    user["id"],
                    network="mainnet",
                    api_key="refresh-public-key",
                    api_secret="refresh-private-key",
                )

                with patch.object(account_worker, "get_binance_account_snapshot", return_value={"futures": {}}) as snapshot:
                    refreshed = account_worker.refresh_binance_accounts_once()

                self.assertEqual(refreshed, 1)
                snapshot.assert_called_once_with("mainnet", "refresh-public-key", "refresh-private-key")

    def test_account_credentials_reject_non_mainnet_storage(self):
        encryption_key = Fernet.generate_key().decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "credentials.db")
            with patch.dict(
                os.environ,
                {
                    "CHANLUN_DB_PATH": database_path,
                    "BINANCE_CREDENTIAL_ENCRYPTION_KEY": encryption_key,
                },
                clear=False,
            ):
                db.init_db()
                user = db.register_user("mainnet-only-user", "password-123")
                with self.assertRaises(ValueError):
                    db.save_user_binance_credentials(
                        user["id"],
                        network="testnet",
                        api_key="public-key",
                        api_secret="private-key",
                    )

    def test_missing_master_key_is_rejected_before_saving(self):
        with patch.dict(os.environ, {"BINANCE_CREDENTIAL_ENCRYPTION_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                db.ensure_binance_credential_storage()

    def test_strategy_settings_are_scoped_validated_and_defaulted_per_user(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "strategy-settings.db")
            with patch.dict(os.environ, {"CHANLUN_DB_PATH": database_path}, clear=False):
                db.init_db()
                first_user = db.register_user("first-strategy-user", "password-123")
                second_user = db.register_user("second-strategy-user", "password-123")

                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["levelStrategy"], "STRUCTURE_EXTREME")
                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["maxAccountLossRatio"], 2.0)
                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["maxPortfolioRiskRatio"], 4.0)
                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["maxSameSidePositions"], 2)
                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["rangeMinimumTargetR"], 1.0)
                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["trendMinimumTargetR"], 1.5)
                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["nearTermMinimumTargetR"], 0.5)
                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["triggerZoneStopBufferAtrMultiplier"], 0.5)
                self.assertEqual(db.get_user_binance_strategy_settings(first_user["id"])["entryConfirmationMode"], "TRIGGER_ONLY")
                self.assertEqual(db.get_user_binance_strategy_settings(second_user["id"])["firstTakeProfitRatio"], 50.0)

                saved = db.save_user_binance_strategy_settings(
                    first_user["id"],
                    {
                        "levelStrategy": "CONFIRMED_PLATFORM",
                        "maxAccountLossRatio": 7.5,
                        "maxPortfolioRiskRatio": 8,
                        "maxSameSidePositions": 3,
                        "dailyLossLimitRatio": 6,
                        "rangeEdgeFraction": 0.2,
                        "rangeMinimumTargetR": 2.5,
                        "trendMinimumTargetR": 2.0,
                        "entryConfirmationMode": "TRIGGER_ONLY",
                        "entryConfirmationExpiryBars": 2,
                        "entryFailureExitBars": 4,
                        "entryFailureBodyAtrMultiplier": 0.8,
                        "trailingAtrMultiplier": 3,
                        "structureStopAtrMultiplier": 0.4,
                        "triggerZoneStopBufferAtrMultiplier": 0.8,
                        "breakevenBufferAtrMultiplier": 0.1,
                        "movingStopActivationR": 1.5,
                        "nearTermMinimumTargetR": 0.75,
                        "firstTakeProfitRatio": 40,
                        "secondTakeProfitRatio": 80,
                    },
                )

                self.assertEqual(saved["levelStrategy"], "CONFIRMED_PLATFORM")
                self.assertEqual(saved["maxAccountLossRatio"], 7.5)
                self.assertEqual(saved["maxPortfolioRiskRatio"], 8.0)
                self.assertEqual(saved["maxSameSidePositions"], 3)
                self.assertEqual(saved["dailyLossLimitRatio"], 6.0)
                self.assertEqual(saved["rangeEdgeFraction"], 0.2)
                self.assertEqual(saved["rangeMinimumTargetR"], 2.5)
                self.assertEqual(saved["trendMinimumTargetR"], 2.0)
                self.assertEqual(saved["nearTermMinimumTargetR"], 0.75)
                self.assertEqual(saved["entryConfirmationMode"], "TRIGGER_ONLY")
                self.assertEqual(saved["entryConfirmationExpiryBars"], 2)
                self.assertEqual(saved["entryFailureExitBars"], 4)
                self.assertEqual(saved["entryFailureBodyAtrMultiplier"], 0.8)
                self.assertEqual(saved["triggerZoneStopBufferAtrMultiplier"], 0.8)
                self.assertEqual(saved["firstTakeProfitRatio"], 40.0)
                self.assertEqual(saved["secondTakeProfitRatio"], 80.0)
                self.assertEqual(db.get_user_binance_strategy_settings(second_user["id"])["secondTakeProfitRatio"], 75.0)
                with self.assertRaisesRegex(ValueError, "第二止盈累计比例"):
                    db.save_user_binance_strategy_settings(
                        first_user["id"],
                        {"firstTakeProfitRatio": 80, "secondTakeProfitRatio": 40},
                    )
                with self.assertRaisesRegex(ValueError, "点位策略路线"):
                    db.save_user_binance_strategy_settings(
                        first_user["id"],
                        {"levelStrategy": "UNSUPPORTED"},
                    )
                with self.assertRaisesRegex(ValueError, "入场确认方式"):
                    db.save_user_binance_strategy_settings(
                        first_user["id"],
                        {"entryConfirmationMode": "UNSUPPORTED"},
                    )
                with self.assertRaisesRegex(ValueError, "趋势最小目标 R"):
                    db.save_user_binance_strategy_settings(
                        first_user["id"],
                        {"trendMinimumTargetR": 0.25},
                    )
                with self.assertRaisesRegex(ValueError, "触发区防假突破缓冲"):
                    db.save_user_binance_strategy_settings(
                        first_user["id"],
                        {"triggerZoneStopBufferAtrMultiplier": 0.05},
                    )

    def test_legacy_retest_default_is_migrated_once_but_explicit_retest_is_preserved(self):
        encryption_key = Fernet.generate_key().decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "strategy-migration.db")
            with patch.dict(
                os.environ,
                {
                    "CHANLUN_DB_PATH": database_path,
                    "BINANCE_CREDENTIAL_ENCRYPTION_KEY": encryption_key,
                },
                clear=False,
            ):
                db.init_db()
                user = db.register_user("strategy-migration-user", "password-123")
                db.save_user_binance_strategy_settings(
                    user["id"],
                    {"entryConfirmationMode": "RETEST_REQUIRED"},
                )
                with db.get_connection() as conn:
                    conn.execute(
                        "DELETE FROM schema_migrations WHERE name = ?",
                        (db.BINANCE_ENTRY_CONFIRMATION_DEFAULT_MIGRATION,),
                    )

                db.init_db()
                self.assertEqual(
                    db.get_user_binance_strategy_settings(user["id"])["entryConfirmationMode"],
                    "TRIGGER_ONLY",
                )

                db.save_user_binance_strategy_settings(
                    user["id"],
                    {"entryConfirmationMode": "RETEST_REQUIRED"},
                )
                db.init_db()
                self.assertEqual(
                    db.get_user_binance_strategy_settings(user["id"])["entryConfirmationMode"],
                    "RETEST_REQUIRED",
                )

    def test_futures_position_pnl_history_is_user_scoped_and_removed_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "pnl-history.db")
            with patch.dict(os.environ, {"CHANLUN_DB_PATH": database_path}, clear=False):
                db.init_db()
                first_user = db.register_user("pnl-history-first", "password-123")
                second_user = db.register_user("pnl-history-second", "password-123")
                db.record_user_binance_futures_position_pnl_history(
                    first_user["id"],
                    [{
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "1",
                        "unrealizedProfit": "2.5",
                        "realizedPnl": "1.25",
                    }],
                    60_001,
                )
                db.record_user_binance_futures_position_pnl_history(
                    second_user["id"],
                    [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "1", "unrealizedProfit": "-1"}],
                    60_001,
                )
                db.record_user_binance_futures_position_pnl_history(
                    first_user["id"],
                    [{
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "0.5",
                        "unrealizedProfit": "1.0",
                        "realizedPnl": None,
                    }],
                    119_999,
                )
                points = db.list_user_binance_futures_position_pnl_history(first_user["id"])[0]["points"]
                self.assertEqual(points, [{"recordedAt": 60_000, "unrealizedPnl": 2.25}])
                self.assertEqual(
                    db.list_user_binance_futures_position_pnl_history(second_user["id"])[0]["points"][0]["unrealizedPnl"],
                    -1.0,
                )
                self.assertEqual(
                    db.record_user_binance_futures_position_pnl_history(first_user["id"], [], 120_000),
                    [],
                )

    def test_position_pnl_history_fills_missing_minutes_without_rewriting_legacy_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "pnl-minute-history.db")
            with patch.dict(os.environ, {"CHANLUN_DB_PATH": database_path}, clear=False):
                db.init_db()
                user = db.register_user("pnl-minute-history", "password-123")
                with db.get_connection() as conn:
                    conn.executemany(
                        """
                        INSERT INTO user_binance_futures_position_pnl_history
                            (user_id, symbol, position_side, recorded_at, unrealized_pnl, realized_pnl)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (user["id"], "BTCUSDT", "LONG", 60_010, 1.0, None),
                            (user["id"], "BTCUSDT", "LONG", 60_050, 2.0, None),
                            (user["id"], "BTCUSDT", "LONG", 180_000, 4.0, None),
                        ],
                    )

                points = db.list_user_binance_futures_position_pnl_history(user["id"])[0]["points"]

                self.assertEqual(points, [
                    {"recordedAt": 60_000, "unrealizedPnl": 2.0},
                    {"recordedAt": 120_000, "unrealizedPnl": 2.0},
                    {"recordedAt": 180_000, "unrealizedPnl": 4.0},
                ])
                with db.get_connection() as conn:
                    stored_count = conn.execute(
                        "SELECT COUNT(*) FROM user_binance_futures_position_pnl_history WHERE user_id = ?",
                        (user["id"],),
                    ).fetchone()[0]
                self.assertEqual(stored_count, 3)

    def test_binance_ui_settings_are_user_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "ui-settings.db")
            with patch.dict(os.environ, {"CHANLUN_DB_PATH": database_path}, clear=False):
                db.init_db()
                first_user = db.register_user("ui-settings-first", "password-123")
                second_user = db.register_user("ui-settings-second", "password-123")
                self.assertEqual(db.get_user_binance_ui_settings(first_user["id"])["positionPnlSmoothing"], 0.25)
                db.save_user_binance_ui_settings(first_user["id"], {"positionPnlSmoothing": 0.8})
                self.assertEqual(db.get_user_binance_ui_settings(first_user["id"])["positionPnlSmoothing"], 0.8)
                self.assertEqual(db.get_user_binance_ui_settings(second_user["id"])["positionPnlSmoothing"], 0.25)


if __name__ == "__main__":
    unittest.main()
