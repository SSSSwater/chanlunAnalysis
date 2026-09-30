from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as app_module
from backend.services.update_log import parse_update_log, read_update_log


MARKDOWN = """# 更新日志

## v2.0.01 - 2026-08-25

### 新功能

- 第一项变化
- 第二项变化

## v2.0 - 2026-08-20

### 初始版本

- 基础功能
"""


class UpdateLogTests(unittest.TestCase):
    def test_parse_markdown_returns_versioned_entries(self):
        updates = parse_update_log(MARKDOWN)

        self.assertEqual(updates[0]["version"], "2.0.01")
        self.assertEqual(updates[0]["title"], "新功能")
        self.assertEqual(updates[0]["items"], ["第一项变化", "第二项变化"])
        self.assertEqual(updates[1]["date"], "2026-08-20")

    def test_update_log_endpoint_returns_current_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update-log.md"
            path.write_text(MARKDOWN, encoding="utf-8")
            with patch("backend.services.update_log.UPDATE_LOG_PATH", path):
                response = app_module.app.test_client().get("/api/update-log")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["currentVersion"], "2.0.01")
        self.assertEqual(payload["currentUpdate"]["title"], "新功能")
        self.assertEqual(len(payload["previousUpdates"]), 1)

    def test_update_log_endpoint_uses_last_valid_payload_during_file_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update-log.md"
            path.write_text(MARKDOWN, encoding="utf-8")
            with patch("backend.services.update_log.UPDATE_LOG_PATH", path):
                first = app_module.app.test_client().get("/api/update-log")
                path.unlink()
                second = app_module.app.test_client().get("/api/update-log")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["currentVersion"], "2.0.01")

    def test_invalid_markdown_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_update_log("# 更新日志\n\n没有版本")

    def test_read_update_log_reports_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            read_update_log(Path("missing-update-log.md"))

    def test_update_log_endpoint_returns_json_for_file_read_failure(self):
        with patch("backend.app.read_update_log", side_effect=OSError("暂时无法读取更新日志")):
            response = app_module.app.test_client().get("/api/update-log")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["message"], "暂时无法读取更新日志")


if __name__ == "__main__":
    unittest.main()
