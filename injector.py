"""日程注入模块。

职责:
  - 在 planner 决定调用 reply 后, 将当前时间附近的日程上下文注入到 reply 的 reference_info 中。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from . import schedule_store

logger = logging.getLogger(__name__)


def get_current_schedule(session_id: str) -> list[dict[str, Any]]:
    """获取当前聊天流今天和昨天的日程活动。

    同时读取昨天是为了支持 23:00-01:00 这类跨天活动在今天凌晨继续注入。
    """
    today = datetime.now().date()
    schedule_items: list[dict[str, Any]] = []
    for schedule_date in (today - timedelta(days=1), today):
        data = schedule_store.load_schedule(session_id, schedule_date.strftime("%Y-%m-%d"))
        if data is None:
            continue
        activities = data.get("activities", [])
        if not isinstance(activities, list):
            continue
        for act in activities:
            if not isinstance(act, dict):
                continue
            item = dict(act)
            item["_schedule_date"] = schedule_date.strftime("%Y-%m-%d")
            schedule_items.append(item)
    return schedule_items


def find_reply_tool_call(tool_calls: list[dict[str, Any]]) -> int | None:
    for i, tc in enumerate(tool_calls):
        func = tc.get("function", {})
        if isinstance(func, dict) and func.get("name") == "reply":
            return i
    return None


def build_schedule_reference_text(
    schedule: list[dict[str, Any]],
    *,
    window_minutes: int = 90,
    max_activities: int = 3,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
    window = timedelta(minutes=max(0, int(window_minutes)))

    nearby: list[tuple[tuple[int, float], dict[str, Any], tuple[datetime, datetime]]] = []
    for act in schedule:
        title = act.get("title")
        if not title:
            continue
        try:
            start_dt, end_dt = _activity_interval(act, fallback_date=now.date())
        except (TypeError, ValueError):
            continue
        if start_dt - window <= now <= end_dt + window:
            ongoing = start_dt <= now <= end_dt
            if ongoing:
                distance_seconds = 0.0
            elif now < start_dt:
                distance_seconds = (start_dt - now).total_seconds()
            else:
                distance_seconds = (now - end_dt).total_seconds()
            nearby.append(((0 if ongoing else 1, distance_seconds), act, (start_dt, end_dt)))

    nearby.sort(key=lambda item: item[0])
    selected = [(act, interval) for _, act, interval in nearby[:max_activities]]
    if not selected:
        return ""

    lines = ["【角色日程参考】"]
    lines.append("以下是角色当前的日程状态，仅供理解角色状态和语气。")
    lines.append("除非对话自然相关，否则不要主动强调或逐条说明。")
    for act, (start_dt, end_dt) in selected:
        line = f"- {act['start']}-{act['end']}"
        if end_dt.date() > start_dt.date():
            line += "(次日)"
        line += f" {act['title']}"
        if act.get("mood"):
            line += f" | 心情: {act['mood']}"
        lines.append(line)
    return "\n".join(lines)


def inject_schedule_into_reply(
    tool_calls: list[dict[str, Any]],
    session_id: str,
    *,
    window_minutes: int = 90,
    max_activities: int = 3,
) -> list[dict[str, Any]]:
    reply_idx = find_reply_tool_call(tool_calls)
    if reply_idx is None:
        return tool_calls

    schedule_text = build_schedule_reference_text(
        get_current_schedule(session_id),
        window_minutes=window_minutes,
        max_activities=max_activities,
    )
    if not schedule_text:
        return tool_calls

    modified = [dict(tc) for tc in tool_calls]
    tc = modified[reply_idx]
    func = dict(tc["function"])
    args = dict(func.get("arguments", {}))

    existing_ref = str(args.get("reference_info", "") or "").strip()
    if existing_ref:
        args["reference_info"] = f"{existing_ref}\n\n{schedule_text}"
    else:
        args["reference_info"] = schedule_text

    func["arguments"] = args
    tc["function"] = func
    return modified


def _time_to_minutes(time_str: str) -> int:
    h, m = time_str.strip().split(":")
    return int(h) * 60 + int(m)


def _activity_interval(act: dict[str, Any], *, fallback_date: date) -> tuple[datetime, datetime]:
    schedule_date = _parse_date(str(act.get("_schedule_date", "") or ""), fallback=fallback_date)
    start_time = _parse_time(str(act.get("start", "") or ""))
    end_time = _parse_time(str(act.get("end", "") or ""))
    start_dt = datetime.combine(schedule_date, start_time)
    end_dt = datetime.combine(schedule_date, end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def _parse_date(date_str: str, *, fallback: date) -> date:
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return fallback


def _parse_time(time_str: str):
    h, m = time_str.strip().split(":")
    h_int = int(h)
    m_int = int(m)
    if not (0 <= h_int <= 23 and 0 <= m_int <= 59):
        raise ValueError(f"invalid time: {time_str}")
    return datetime.strptime(f"{h_int:02d}:{m_int:02d}", "%H:%M").time()
