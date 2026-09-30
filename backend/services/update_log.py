from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from threading import RLock


ROOT_DIR = Path(__file__).resolve().parents[2]
UPDATE_LOG_PATH = ROOT_DIR / "update-log.md"
VERSION_HEADING = re.compile(r"^##\s+v([^\s-]+)\s+-\s+(\d{4}-\d{2}-\d{2})\s*$")
_UPDATE_LOG_CACHE_LOCK = RLock()
_UPDATE_LOG_CACHE: dict | None = None
_UPDATE_LOG_CACHE_PATH: Path | None = None


def _clean_markdown_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def parse_update_log(markdown: str) -> list[dict]:
    updates: list[dict] = []
    current: dict | None = None
    current_items: list[str] = []

    def finish_current() -> None:
        if current is None:
            return
        current["items"] = current_items.copy()
        if current["title"] or current["items"]:
            updates.append(current.copy())

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        version_match = VERSION_HEADING.match(line)
        if version_match:
            finish_current()
            current = {
                "version": _clean_markdown_text(version_match.group(1)),
                "date": version_match.group(2),
                "title": "",
                "items": [],
            }
            current_items = []
            continue
        if current is None or not line:
            continue
        if line.startswith("### "):
            current["title"] = _clean_markdown_text(line[4:])
            continue
        item_match = re.match(r"^[-*+]\s+(.+)$", line)
        if item_match:
            current_items.append(_clean_markdown_text(item_match.group(1)))

    finish_current()
    if not updates:
        raise ValueError("更新日志没有可解析的版本记录")
    if any(not item["version"] or not item["date"] or not item["title"] for item in updates):
        raise ValueError("更新日志存在缺少版本、日期或标题的记录")
    return updates


def read_update_log(path: Path | None = None) -> dict:
    global _UPDATE_LOG_CACHE, _UPDATE_LOG_CACHE_PATH
    source_path = Path(path or UPDATE_LOG_PATH)
    try:
        markdown = source_path.read_text(encoding="utf-8")
        updates = parse_update_log(markdown)
    except FileNotFoundError as exc:
        if path is None:
            with _UPDATE_LOG_CACHE_LOCK:
                if _UPDATE_LOG_CACHE is not None and _UPDATE_LOG_CACHE_PATH == source_path:
                    return deepcopy(_UPDATE_LOG_CACHE)
        raise FileNotFoundError(f"更新日志文件不存在：{source_path}") from exc
    except (OSError, UnicodeError, ValueError):
        # The file can be briefly unavailable while a deployment or local
        # update replaces it. Keep the last valid default-file payload usable.
        if path is None:
            with _UPDATE_LOG_CACHE_LOCK:
                if _UPDATE_LOG_CACHE is not None and _UPDATE_LOG_CACHE_PATH == source_path:
                    return deepcopy(_UPDATE_LOG_CACHE)
        raise

    payload = {
        "currentVersion": updates[0]["version"],
        "currentUpdate": updates[0],
        "previousUpdates": updates[1:],
        "updates": updates,
    }
    if path is None:
        with _UPDATE_LOG_CACHE_LOCK:
            _UPDATE_LOG_CACHE = deepcopy(payload)
            _UPDATE_LOG_CACHE_PATH = source_path
    return payload
