"""日程生成引擎。

职责:
  - 构造 LLM prompt 并调用生成日程
  - 解析 LLM 返回的 JSON 日程
  - 重生成（update_schedule）的队列执行
  - 降级链: 重试 → 空（失败时宁可不注入）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import schedule_store

logger = logging.getLogger(__name__)

_MAX_RETRIES = 1


# ── prompt 构造 ──────────────────────────────────────────

def _build_generation_prompt(
    *,
    persona: str,
    date_info: str,
    activity_count_min: int,
    activity_count_max: int,
    wake_time: str,
    sleep_time: str,
    continuity_context: str,
    pending_commitments: list[dict[str, Any]] | None,
    knowledge_context: str,
    history_context: str,
    stream_info: dict[str, Any] | None,
) -> str:
    sections: list[str] = []

    if persona:
        sections.append(f"【角色人设】\n{persona}")

    sections.append(f"【日期信息】\n{date_info}")

    if stream_info:
        stream_lines = [
            f"- 聊天流 ID: {stream_info.get('session_id', '') or stream_info.get('stream_id', '')}",
            f"- 平台: {stream_info.get('platform', '')}",
            f"- 类型: {stream_info.get('chat_type', '')}",
        ]
        if stream_info.get("group_id"):
            stream_lines.append(f"- 群号: {stream_info.get('group_id')}")
        if stream_info.get("user_id"):
            stream_lines.append(f"- 用户账号: {stream_info.get('user_id')}")
        sections.append("【当前聊天流】\n" + "\n".join(line for line in stream_lines if line.strip()))

    if knowledge_context:
        sections.append(f"【记忆参考】\n以下是角色从对话中积累的知识，可作为日程生成的参考：\n{knowledge_context}")

    if history_context:
        sections.append(f"【当天聊天摘要/上文】\n以下内容仅来自当前聊天流，用于理解今天发生过什么：\n{history_context}")

    if continuity_context:
        sections.append(continuity_context)

    if pending_commitments:
        lines = ["以下是需要纳入今天日程的约定："]
        for item in pending_commitments:
            time_str = item.get("time", "")
            title = item.get("title", "")
            notes = item.get("notes", "")
            line = f"- {time_str} {title}"
            if notes:
                line += f" ({notes})"
            lines.append(line)
        sections.append("\n".join(lines))

    target_activity_count = _target_activity_count(activity_count_min, activity_count_max)
    sleep_guidance = ""
    if wake_time and sleep_time:
        sleep_guidance = (
            f"- 角色通常在 {wake_time} 左右苏醒，在 {sleep_time} 左右入睡；"
            "必须把睡眠/休息作为日程活动写入，而不是省略\n"
        )

    sections.append(
        f"【生成要求】\n"
        f"- 目标生成 {target_activity_count} 个日常活动；数量可略有浮动，但不要极端偏离\n"
        f"- 日程必须覆盖 24 小时，从一天开始到一天结束，包含睡眠、清醒后的日常、工作/学习/休闲和夜间收束\n"
        f"{sleep_guidance}"
        f"- 支持跨天活动：如果活动从当天夜间延续到次日凌晨，可以写成 start 晚于 end，例如 23:00-01:00 表示次日 01:00 结束\n"
        f"- 如果入睡时间在凌晨附近，睡前活动和睡眠活动很可能跨过午夜，请用跨天时间段表达，不要强行截断成两条生硬活动\n"
        f"- 昨天已经跨到今天凌晨的活动，只能写在开始那一天；今天不要再次写这段活动本身，而要从它结束后的新状态开始安排\n"
        f"- 如果昨天最后一项是跨天睡眠，今天的第一项应是起床/洗漱/早餐等新活动，而不是再次写一条睡眠\n"
        f"- 活动应该像真实人类的一天，有轻重缓急和空档\n"
        f"- 避免全是「和用户聊天」——角色有自己的生活\n"
        f"- 每个活动格式: {{\"start\": \"HH:MM\", \"end\": \"HH:MM\", \"title\": \"...\", \"mood\": \"...\", \"notes\": \"...\"}}\n"
        f"- 如果有约定需要纳入，安排到合适时间段，并把 source 设为 \"commitment\"\n"
        f"- 其他活动的 source 设为 \"llm\"\n"
        f"- 严格输出 JSON，不要 Markdown 代码块或额外文字\n"
        f"- 输出格式: {{\"activities\": [...]}}"
    )

    return "\n\n".join(sections)


def _build_regeneration_prompt(
    *,
    persona: str,
    current_activities: list[dict[str, str]],
    description: str,
    date_info: str,
) -> str:
    sections: list[str] = []

    if persona:
        sections.append(f"【角色人设】\n{persona}")

    sections.append(f"【日期信息】\n{date_info}")

    lines = ["【当前日程】"]
    for act in current_activities:
        lines.append(f"- {act.get('start', '')}-{act.get('end', '')} {act.get('title', '')}")
    sections.append("\n".join(lines))

    sections.append(
        f"【日程变更请求】\n{description}\n\n"
        "【要求】\n"
        "- 将上述变更融入当前日程，调整受影响的活动（推移、缩短或替换）\n"
        "- 如果变更请求没有指定精确时间，请根据角色人设和当天空档合理选择\n"
        "- 如果请求涉及未来日期，请放入 pending_commitments 列表，不动当天日程\n"
        "- 如果没有实质性日程变更，返回当前日程不变\n"
        "- 输出完整 JSON，格式与当前日程相同\n"
        "- pending_commitments 中每条需要 date、title 字段，time 和 notes 可选"
    )

    return "\n\n".join(sections)


# ── JSON 解析 ─────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _parse_schedule_json(text: str) -> dict[str, Any] | None:
    """从 LLM 返回文本中提取日程 JSON，支持多级降级。"""
    text = text.strip()

    # 尝试直接解析
    result = _try_parse_json(text)
    if result is not None:
        return result

    # 尝试提取 ```json``` 代码块
    for match in _JSON_BLOCK_RE.finditer(text):
        result = _try_parse_json(match.group(1))
        if result is not None:
            return result

    # 尝试提取第一个 JSON 对象
    match = _JSON_OBJECT_RE.search(text)
    if match:
        result = _try_parse_json(match.group(0))
        if result is not None:
            return result

    return None


def _try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _target_activity_count(activity_count_min: int, activity_count_max: int) -> int:
    low = max(1, int(activity_count_min))
    high = max(low, int(activity_count_max))
    return (low + high) // 2


def _build_yesterday_continuity_context(
    yesterday_activities: list[dict[str, str]] | None,
    *,
    wake_time: str,
    sleep_time: str,
) -> str:
    """把昨天的收尾整理成今天的衔接提示。"""
    if not yesterday_activities:
        return ""

    activities = [act for act in yesterday_activities if isinstance(act, dict)]
    if not activities:
        return ""

    tail = activities[-2:] if len(activities) >= 2 else activities
    last = activities[-1]
    last_start = str(last.get("start", "") or "").strip()
    last_end = str(last.get("end", "") or "").strip()
    last_title = str(last.get("title", "") or "").strip()

    lines = [
        "【昨日收束与今日衔接】",
        "昨天和今天是一条连续时间轴。跨过午夜的活动只归属开始那一天，今天不要重复昨天尾项。",
    ]

    if tail:
        lines.append("昨日尾部参考：")
        for act in tail:
            start = str(act.get("start", "") or "").strip()
            end = str(act.get("end", "") or "").strip()
            title = str(act.get("title", "") or "").strip()
            if not start or not end or not title:
                continue
            suffix = "（跨天）" if _is_cross_day_time_range(start, end) else ""
            lines.append(f"- {start}-{end}{suffix} {title}")

    if last_start and last_end and last_title:
        if _is_cross_day_time_range(last_start, last_end):
            lines.append(f"- 昨日最后一项 {last_start}-{last_end} {last_title} 是跨天尾项，今天不要再写它。")
            if sleep_time:
                lines.append(
                    f"- 如果这是一段睡眠，今天首项应从醒来后的新活动开始，通常是衔接上一段睡眠苏醒时间的起床/洗漱/早餐，而不是再写一条睡眠。"
                )
        else:
            lines.append(f"- 昨日最后一项 {last_start}-{last_end} {last_title} 已在昨天结束，今天从它结束后的新活动开始。")

    lines.append("- 今天只写今天新开始的活动，不要把昨天已经写过的跨天活动重新抄进今天。")
    return "\n".join(lines)


def _validate_schedule(
    data: dict[str, Any],
    activity_count_min: int,
    activity_count_max: int,
) -> bool:
    """最小校验集。"""
    return _schedule_validation_error(data, activity_count_min, activity_count_max) is None


def _schedule_validation_error(
    data: dict[str, Any],
    activity_count_min: int,
    activity_count_max: int,
) -> str | None:
    """返回硬校验失败原因；校验通过时返回 None。"""
    activities = data.get("activities")
    if not isinstance(activities, list):
        return "activities 字段不存在或不是列表"
    for idx, act in enumerate(activities):
        if not isinstance(act, dict):
            return f"activities[{idx}] 不是对象"
        for key in ("start", "end", "title"):
            if not isinstance(act.get(key), str) or not act[key].strip():
                return f"activities[{idx}].{key} 缺失或不是非空字符串"
        if not _is_valid_time(act["start"]) or not _is_valid_time(act["end"]):
            return f"activities[{idx}] 时间格式无效: start={act['start']!r}, end={act['end']!r}"
    return None


def _schedule_quality_warning(
    data: dict[str, Any],
    activity_count_min: int,
    activity_count_max: int,
    yesterday_activities: list[dict[str, str]] | None = None,
) -> str | None:
    """返回可接受的质量问题；不会阻止保存日程。"""
    activities = data.get("activities")
    if not isinstance(activities, list):
        return None
    count = len(activities)
    if not (activity_count_min <= count <= activity_count_max):
        return f"activities 数量为 {count}，不在 {activity_count_min}-{activity_count_max} 范围内，结构可用，已接受"
    overlap_warning = _first_activity_overlaps_yesterday_tail(activities, yesterday_activities)
    if overlap_warning:
        return overlap_warning
    return None


def _first_activity_overlaps_yesterday_tail(
    activities: list[Any],
    yesterday_activities: list[dict[str, str]] | None,
) -> str | None:
    if not activities or not yesterday_activities:
        return None
    first = activities[0]
    if not isinstance(first, dict):
        return None
    previous = [act for act in yesterday_activities if isinstance(act, dict)]
    if not previous:
        return None
    last = previous[-1]
    last_start = str(last.get("start", "") or "").strip()
    last_end = str(last.get("end", "") or "").strip()
    if not _is_cross_day_time_range(last_start, last_end):
        return None

    first_start = str(first.get("start", "") or "").strip()
    first_end = str(first.get("end", "") or "").strip()
    first_title = str(first.get("title", "") or "").strip()
    last_title = str(last.get("title", "") or "").strip()
    if not first_start or not first_end:
        return None

    if _time_ranges_overlap(first_start, first_end, last_start, last_end):
        return (
            "今天第一项可能与昨天跨天尾项重叠: "
            f"yesterday={last_start}-{last_end} {last_title}, "
            f"today_first={first_start}-{first_end} {first_title}"
        )
    return None


def _time_ranges_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    try:
        a_start = _time_to_minutes(start_a)
        a_end = _time_to_minutes(end_a)
        b_start = _time_to_minutes(start_b)
        b_end = _time_to_minutes(end_b)
    except (TypeError, ValueError):
        return False
    if a_end <= a_start:
        a_end += 24 * 60
    if b_end <= b_start:
        b_end += 24 * 60
    # 昨天跨天尾项映射到今天凌晨部分。
    if b_start > b_end - 24 * 60:
        b_start -= 24 * 60
    today_overlap_start = max(0, b_start)
    today_overlap_end = b_end - 24 * 60 if b_end > 24 * 60 else b_end
    if today_overlap_end <= today_overlap_start:
        today_overlap_start = 0
    return max(a_start, today_overlap_start) < min(a_end, today_overlap_end)


def _is_valid_time(s: str) -> bool:
    parts = s.strip().split(":")
    if len(parts) != 2:
        return False
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


def _is_cross_day_time_range(start: str, end: str) -> bool:
    try:
        return _time_to_minutes(end) <= _time_to_minutes(start)
    except (TypeError, ValueError):
        return False


def _time_to_minutes(time_str: str) -> int:
    h, m = time_str.strip().split(":")
    return int(h) * 60 + int(m)


# ── LLM 调用 ─────────────────────────────────────────────

async def generate_daily_schedule(
    ctx: Any,
    session_id: str,
    *,
    persona: str = "",
    activity_count_min: int = 8,
    activity_count_max: int = 15,
    wake_time: str = "",
    sleep_time: str = "",
    model: str = "",
    knowledge_context: str = "",
    history_context: str = "",
    stream_info: dict[str, Any] | None = None,
    max_tokens: int = 16000,
) -> dict[str, Any] | None:
    """生成每日日程。

    Returns: 成功返回日程 dict，失败返回 None。
    """
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    date_info = f"日期：{date_str} ({weekday_names[today.weekday()]})"

    yesterday_data = schedule_store.load_yesterdays_schedule(session_id)
    yesterday_activities = None
    if yesterday_data and isinstance(yesterday_data.get("activities"), list):
        yesterday_activities = yesterday_data["activities"]

    continuity_context = _build_yesterday_continuity_context(
        yesterday_activities,
        wake_time=wake_time,
        sleep_time=sleep_time,
    )

    pending = schedule_store.peek_pending_commitments(session_id, date_str)

    prompt = _build_generation_prompt(
        persona=persona,
        date_info=date_info,
        activity_count_min=activity_count_min,
        activity_count_max=activity_count_max,
        wake_time=wake_time,
        sleep_time=sleep_time,
        continuity_context=continuity_context,
        pending_commitments=pending or None,
        knowledge_context=knowledge_context,
        history_context=history_context,
        stream_info=stream_info,
    )

    schedule = await _call_llm_with_retry(
        ctx, prompt=prompt, model=model,
        activity_count_min=activity_count_min,
        activity_count_max=activity_count_max,
        yesterday_activities=yesterday_activities,
        max_tokens=max_tokens,
    )

    if schedule is not None:
        schedule["date"] = date_str
        schedule["generated_at"] = today.isoformat(timespec="seconds")
        if "pending_commitments" not in schedule:
            schedule["pending_commitments"] = []
        if pending:
            schedule_store.consume_pending_commitments(session_id, date_str)
            logger.info("已消费 %s 条当天预约: session=%s date=%s", len(pending), session_id, date_str)
        return schedule

    logger.warning("日程生成失败，不写入兜底日程: session=%s", session_id)
    return None


async def regenerate_schedule(
    ctx: Any,
    session_id: str,
    description: str,
    *,
    persona: str = "",
    model: str = "",
    activity_count_min: int = 8,
    activity_count_max: int = 15,
    max_tokens: int = 16000,
) -> dict[str, Any] | None:
    """根据自然语言描述重生成当天日程（update_schedule 调用）。"""
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    date_info = f"日期：{today_str} ({weekday_names[today.weekday()]})"

    current_data = schedule_store.load_todays_schedule(session_id)
    if current_data is None:
        current_activities = []
        existing_pending = []
    else:
        current_activities = current_data.get("activities", [])
        existing_pending = current_data.get("pending_commitments", [])

    parsed = await _decide_schedule_update(
        ctx,
        description=description,
        date_info=date_info,
        persona=persona,
        current_activities=current_activities,
        model=model,
    )
    if parsed is None:
        logger.info("日程修改判定失败，保持当前日程不变: session=%s", session_id)
        return None

    decision = str(parsed.get("decision", "") or "").strip().lower()
    if decision in {"reject", "decline", "ignore", "no_change"}:
        logger.info("角色未接受日程修改请求，日程不变: session=%s reason=%s", session_id, parsed.get("reason", ""))
        return None

    if decision == "future":
        target_date = parsed.get("date", "")
        if not target_date:
            target_date = _infer_future_date(parsed.get("raw_date", description), today_str)
        if target_date <= today_str:
            logger.info("未来预约日期无效，日程不变: session=%s date=%s", session_id, target_date)
            return None
        commitment = {
            "date": target_date,
            "time": parsed.get("time", ""),
            "title": parsed.get("title", description),
            "notes": parsed.get("notes", ""),
            "reason": parsed.get("reason", ""),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        schedule_store.add_pending_commitment(session_id, commitment)
        logger.info("已记录未来预约: session=%s date=%s title=%s", session_id, target_date, commitment["title"])
        return None

    if decision != "today":
        logger.info("未知日程修改判定，日程不变: session=%s decision=%s", session_id, decision)
        return None

    if current_data is None:
        logger.info("当前聊天流暂无今日日程，无法修改今天日程: session=%s", session_id)
        return None

    enriched_parts = []
    if parsed.get("title"):
        enriched_parts.append(f"角色决定接受或调整今天的活动：{parsed['title']}")
    if parsed.get("time"):
        enriched_parts.append(f"时间：{parsed['time']}")
    if parsed.get("notes"):
        enriched_parts.append(f"备注：{parsed['notes']}")
    if parsed.get("reason"):
        enriched_parts.append(f"角色判断理由：{parsed['reason']}")
    enriched_parts.append(f"原始请求：{description}")
    description = "\n".join(enriched_parts)

    prompt = _build_regeneration_prompt(
        persona=persona,
        current_activities=current_activities,
        description=description,
        date_info=date_info,
    )

    schedule = await _call_llm_with_retry(
        ctx, prompt=prompt, model=model,
        activity_count_min=activity_count_min,
        activity_count_max=activity_count_max,
        max_tokens=max_tokens,
    )

    if schedule is not None:
        schedule["date"] = today_str
        schedule["generated_at"] = today.isoformat(timespec="seconds")
        if "pending_commitments" not in schedule:
            schedule["pending_commitments"] = existing_pending
        return schedule

    logger.warning("日程重生成失败，保持当前日程不变: session=%s", session_id)
    return None


# ── update_schedule 防抖队列 ──────────────────────────────

class _TaskQueue:
    """按 session 隔离的任务队列，FIFO 顺序执行，同一 session 同时只跑一个。"""

    def __init__(self) -> None:
        self._items: dict[str, list[tuple[str, Any]]] = {}
        self._running: dict[str, bool] = {}

    def submit(self, session_id: str, description: str, callback: Any) -> None:
        if session_id not in self._items:
            self._items[session_id] = []
        self._items[session_id].append((description, callback))
        if not self._running.get(session_id):
            self._running[session_id] = True
            asyncio.create_task(self._worker(session_id))

    async def _worker(self, session_id: str) -> None:
        try:
            while True:
                items = self._items.get(session_id)
                if not items:
                    break
                description, callback = items.pop(0)
                try:
                    await callback(description)
                except Exception:
                    logger.exception("队列任务失败: session=%s", session_id)
        finally:
            self._running.pop(session_id, None)


task_queue = _TaskQueue()


# ── 日期解析 ─────────────────────────────────────────────

async def _decide_schedule_update(
    ctx: Any,
    *,
    description: str,
    date_info: str,
    persona: str,
    current_activities: list[dict[str, Any]],
    model: str,
) -> dict[str, Any] | None:
    """让 LLM 根据人设和当前日程判断是否接受日程变更。"""
    lines = []
    for act in current_activities:
        lines.append(f"- {act.get('start', '')}-{act.get('end', '')} {act.get('title', '')}")
    current_text = "\n".join(lines) if lines else "暂无可靠日程"
    prompt = (
        f"【角色人设】\n{persona or '未提供'}\n\n"
        f"【日期信息】\n{date_info}\n\n"
        f"【当前日程】\n{current_text}\n\n"
        f"【用户请求】\n{description}\n\n"
        "请扮演该角色，判断角色是否会接受这个日程相关请求。\n"
        "不要机械接受用户安排；如果不符合人设、时间冲突太强、语气像玩笑或没有明确计划，可以拒绝或不改日程。\n"
        "decision 只能是以下值之一：\n"
        "- today: 角色接受或调整今天的安排，需要修改今天日程\n"
        "- future: 角色接受未来某天的预约，只记录预约，不修改今天日程\n"
        "- reject: 角色没有接受，日程不变\n\n"
        "以 JSON 格式返回：\n"
        '{"decision": "today/future/reject", "date": "YYYY-MM-DD 或空", '
        '"time": "HH:MM 或空", "title": "简短标题", "notes": "备注", '
        '"reason": "角色判断理由", "raw_date": "用户原文中的日期描述"}\n'
        "日期必须根据【日期信息】中的真实日期推断。\n"
        "只输出 JSON，不要其他文字。"
    )
    try:
        result = await ctx.llm.generate(
            prompt=prompt,
            model=model,
            temperature=0.3,
            max_tokens=2000,
        )
    except Exception as exc:
        logger.warning("日期解析 LLM 调用失败: %s", exc)
        _log_llm_call("update_decision", prompt, "", model, success=False)
        return None

    response_text = ""
    if isinstance(result, dict):
        response_text = str(result.get("response", "") or "").strip()
    if not response_text:
        _log_llm_call("update_decision", prompt, "", model, success=False)
        return None

    data = _parse_schedule_json(response_text)
    success = isinstance(data, dict)
    _log_llm_call("update_decision", prompt, response_text, model, success=success)
    return data if success else None


def _infer_future_date(raw_date: str, today_str: str) -> str:
    """LLM 未给出具体日期时的简单推断。"""
    from datetime import timedelta
    today = datetime.strptime(today_str, "%Y-%m-%d")
    mapping = {
        "明天": 1, "明日": 1,
        "后天": 2, "後天": 2,
        "大后天": 3, "大後天": 3,
    }
    for kw, offset in mapping.items():
        if kw in raw_date:
            return (today + timedelta(days=offset)).strftime("%Y-%m-%d")
    # 兜底：今天的日期（有空字符串被定时生成处理）
    return today_str


_LLM_LOG_DIR = Path(__file__).resolve().parent / "llm_logs"
_LOG_RETENTION_DAYS = 7


def _log_llm_call(
    call_type: str,
    prompt: str,
    response_text: str,
    model: str,
    success: bool,
) -> None:
    """将 LLM 调用的 prompt 和响应写入日志文件。"""
    try:
        _LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        status = "ok" if success else "fail"
        filename = f"{status}_{call_type}_{timestamp}.txt"
        path = _LLM_LOG_DIR / filename
        content = (
            f"=== MODEL ===\n{model or 'default'}\n\n"
            f"=== SUCCESS ===\n{success}\n\n"
            f"=== PROMPT ===\n{prompt}\n\n"
            f"=== RESPONSE ===\n{response_text}"
        )
        path.write_text(content, encoding="utf-8")
    except OSError:
        pass


def _clean_old_llm_logs() -> None:
    """删除超过保留期的 LLM 日志。"""
    if not _LLM_LOG_DIR.is_dir():
        return
    cutoff = datetime.now() - timedelta(days=_LOG_RETENTION_DAYS)
    for path in _LLM_LOG_DIR.iterdir():
        if not path.suffix == ".txt":
            continue
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
        except OSError:
            pass


async def _call_llm_with_retry(
    ctx: Any,
    *,
    prompt: str,
    model: str,
    activity_count_min: int,
    activity_count_max: int,
    yesterday_activities: list[dict[str, str]] | None = None,
    max_tokens: int = 16000,
) -> dict[str, Any] | None:
    """带重试的 LLM 调用 + JSON 解析 + 校验。"""
    last_response_text = ""
    for attempt in range(1 + _MAX_RETRIES):
        try:
            result = await ctx.llm.generate(
                prompt=prompt,
                model=model,
                temperature=0.7,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            logger.warning("LLM 调用失败 (尝试 %d/%d): %s", attempt + 1, 1 + _MAX_RETRIES, exc)
            continue

        response_text = ""
        if isinstance(result, dict):
            response_text = str(result.get("response", "") or "").strip()
        last_response_text = response_text
        if not response_text:
            logger.warning("LLM 返回空响应 (尝试 %d/%d)", attempt + 1, 1 + _MAX_RETRIES)
            continue

        data = _parse_schedule_json(response_text)
        if data is None:
            logger.warning("LLM 返回 JSON 解析失败 (尝试 %d/%d)", attempt + 1, 1 + _MAX_RETRIES)
            continue

        # 顶层可能是 {"activities": [...]} 或直接就是 activities 列表
        activities = data.get("activities")
        if activities is None and isinstance(data, list):
            data = {"activities": data}
            activities = data.get("activities")

        validation_error = _schedule_validation_error(data, activity_count_min, activity_count_max)
        if validation_error is None:
            quality_warning = _schedule_quality_warning(
                data,
                activity_count_min,
                activity_count_max,
                yesterday_activities=yesterday_activities,
            )
            if quality_warning:
                logger.warning("LLM 返回日程存在质量偏差: %s", quality_warning)
            _log_llm_call("schedule_generation", prompt, response_text, model, success=True)
            return data

        logger.warning(
            "LLM 返回日程校验失败 (尝试 %d/%d): %s",
            attempt + 1,
            1 + _MAX_RETRIES,
            validation_error,
        )
        _log_llm_call("schedule_generation", prompt, response_text, model, success=False)

    if last_response_text:
        _log_llm_call("schedule_generation", prompt, last_response_text, model, success=False)

    return None


