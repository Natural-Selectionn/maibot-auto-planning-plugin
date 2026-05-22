"""日程持久化模块。

职责:
  - 按 session_id + date 读写日程 JSON 文件
  - 管理 pending_commitments 的增删消费
  - 不包含任何 LLM 调用逻辑
"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_SCHEDULE_AGE_DAYS = 7
_SANITIZE_PATTERN = re.compile(r"[^a-zA-Z0-9_.\-]")


def _sanitize(name: str) -> str:
    return _SANITIZE_PATTERN.sub("_", str(name).strip() or "unknown")


def _store_dir() -> Path:
    return Path(__file__).resolve().parent / "schedules"


def _file_path(session_id: str, date_str: str) -> Path:
    return _store_dir() / f"{_sanitize(session_id)}_{date_str}.json"


# ── 底层读写 ──────────────────────────────────────────

def load_schedule(session_id: str, date_str: str) -> dict[str, Any] | None:
    """加载指定 session 和日期的日程，不存在时返回 None。"""
    path = _file_path(session_id, date_str)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("加载日程文件失败 %s: %s", path, exc)
        return None


def save_schedule(session_id: str, data: dict[str, Any]) -> None:
    """保存日程到文件，自动根据 data["date"] 定位。"""
    date_str = str(data.get("date", "") or "").strip()
    if not date_str:
        raise ValueError("日程数据缺少 date 字段")
    path = _file_path(session_id, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_todays_schedule(session_id: str) -> dict[str, Any] | None:
    """加载今天的有日程。"""
    return load_schedule(session_id, datetime.now().strftime("%Y-%m-%d"))


def load_yesterdays_schedule(session_id: str) -> dict[str, Any] | None:
    """加载昨天的日程。"""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return load_schedule(session_id, yesterday)


def load_or_fallback_todays_schedule(session_id: str) -> dict[str, Any] | None:
    """加载今天日程；不存在时回落到该聊天流昨天日程。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. session 独立今日
    data = load_schedule(session_id, today_str)
    if data is not None:
        return data

    # 2. session 独立昨日
    data = load_schedule(session_id, yesterday_str)
    if data is not None:
        return data

    return None


def schedule_exists(session_id: str, date_str: str) -> bool:
    return _file_path(session_id, date_str).is_file()


def usable_schedule_exists(session_id: str, date_str: str) -> bool:
    """判断指定日期是否存在可用于注入/查询的完整日程。"""
    data = load_schedule(session_id, date_str)
    if data is None:
        return False
    activities = data.get("activities")
    return isinstance(activities, list) and len(activities) > 0


# ── pending_commitments 管理 ───────────────────────────

def get_pending_commitments(session_id: str) -> list[dict[str, Any]]:
    """获取所有未来的 pending_commitments，跨所有已存储的日程文件扫描。

    也检查今天文件中未消费的条目。
    """
    commitments: list[dict[str, Any]] = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    store_dir = _store_dir()
    if not store_dir.is_dir():
        return commitments
    prefix = f"{_sanitize(session_id)}_"
    for path in sorted(store_dir.iterdir()):
        if not path.name.startswith(prefix) or not path.suffix == ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get("pending_commitments", []):
            item_date = str(item.get("date", "") or "").strip()
            if item_date and item_date >= today_str:
                commitments.append(item)
    return commitments


def consume_pending_commitments(session_id: str, date_str: str) -> list[dict[str, Any]]:
    """提取目标日期的 pending_commitments 并标记已消费。

    扫描所有文件，移除 date == date_str 的条目并写回。
    """
    consumed: list[dict[str, Any]] = []
    today_str = datetime.now().strftime("%Y-%m-%d")
    store_dir = _store_dir()
    if not store_dir.is_dir():
        return consumed
    prefix = f"{_sanitize(session_id)}_"
    for path in sorted(store_dir.iterdir()):
        if not path.name.startswith(prefix) or not path.suffix == ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        remaining: list[dict[str, Any]] = []
        changed = False
        for item in data.get("pending_commitments", []):
            item_date = str(item.get("date", "") or "").strip()
            if item_date == date_str:
                consumed.append(deepcopy(item))
                changed = True
            elif item_date and item_date >= today_str:
                remaining.append(item)
        if changed:
            data["pending_commitments"] = remaining
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return consumed


def peek_pending_commitments(session_id: str, date_str: str) -> list[dict[str, Any]]:
    """读取目标日期的 pending_commitments，不修改文件。"""
    commitments: list[dict[str, Any]] = []
    store_dir = _store_dir()
    if not store_dir.is_dir():
        return commitments
    prefix = f"{_sanitize(session_id)}_"
    for path in sorted(store_dir.iterdir()):
        if not path.name.startswith(prefix) or not path.suffix == ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get("pending_commitments", []):
            item_date = str(item.get("date", "") or "").strip()
            if item_date == date_str:
                commitments.append(deepcopy(item))
    return commitments


def add_pending_commitment(session_id: str, commitment: dict[str, Any]) -> None:
    """追加一条 pending_commitment 到目标日期的日程文件。

    如果目标日期文件不存在，创建一个空日程骨架。
    """
    target_date = str(commitment.get("date", "") or "").strip()
    if not _is_date_string(target_date):
        target_date = datetime.now().strftime("%Y-%m-%d")
        logger.warning("pending_commitment 缺少有效 date，回退写入今天文件: session=%s", session_id)
    data = load_schedule(session_id, target_date)
    if data is None:
        data = {
            "date": target_date,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "activities": [],
            "pending_commitments": [],
        }
    if "pending_commitments" not in data:
        data["pending_commitments"] = []
    data["pending_commitments"].append(commitment)
    save_schedule(session_id, data)


def _is_date_string(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def delete_old_schedules(session_id: str, max_age_days: int = _MAX_SCHEDULE_AGE_DAYS) -> int:
    """删除超过 max_age_days 天的旧日程文件。"""
    cutoff = datetime.now() - timedelta(days=max_age_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    deleted = 0
    store_dir = _store_dir()
    if not store_dir.is_dir():
        return 0
    prefix = f"{_sanitize(session_id)}_"
    for path in store_dir.iterdir():
        if not path.name.startswith(prefix) or not path.suffix == ".json":
            continue
        date_part = path.stem[len(prefix):]
        if date_part < cutoff_str:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted
