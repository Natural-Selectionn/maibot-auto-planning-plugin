from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, time
from typing import Any

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from . import schedule_engine, schedule_store
from .config_model import AutoPlanningConfig
from .injector import inject_schedule_into_reply
from .schema_i18n import apply_config_schema_i18n

logger = logging.getLogger(__name__)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _resolve_tool_stream_id(explicit_session_id: str, kwargs: dict[str, Any]) -> str:
    """Prefer MaiBot's injected runtime stream id over model-supplied tool args."""
    return (
        _normalize_text(kwargs.get("stream_id"))
        or _normalize_text(kwargs.get("chat_id"))
        or _normalize_text(explicit_session_id)
    )


def _recent_bounds(hours: int) -> tuple[float, float]:
    now = datetime.now()
    start = now - timedelta(hours=max(1, int(hours)))
    return start.timestamp(), now.timestamp()


def _parse_generation_time(value: str) -> time | None:
    try:
        return datetime.strptime(_normalize_text(value), "%H:%M").time()
    except ValueError:
        return None


def _unwrap_capability_failure(value: Any) -> Any:
    if isinstance(value, dict) and value.get("success") is False:
        error = _normalize_text(value.get("error"))
        raise RuntimeError(error or "能力调用失败")
    return value


def _message_timestamp_text(message: dict[str, Any]) -> str:
    raw_timestamp = message.get("timestamp")
    try:
        timestamp = datetime.fromtimestamp(float(raw_timestamp))
    except (TypeError, ValueError, OSError):
        return ""
    return timestamp.strftime("%H:%M")


def _message_user_name(message: dict[str, Any]) -> str:
    info = message.get("message_info")
    if not isinstance(info, dict):
        return "未知用户"
    user_info = info.get("user_info")
    if not isinstance(user_info, dict):
        return "未知用户"
    return (
        _normalize_text(user_info.get("user_cardname"))
        or _normalize_text(user_info.get("user_nickname"))
        or _normalize_text(user_info.get("user_id"))
        or "未知用户"
    )


def _message_text(message: dict[str, Any]) -> str:
    text = _normalize_text(message.get("processed_plain_text"))
    if text:
        return text
    raw_message = message.get("raw_message")
    if isinstance(raw_message, dict):
        components = raw_message.get("components")
        if isinstance(components, list):
            parts: list[str] = []
            for component in components:
                if not isinstance(component, dict):
                    continue
                part = _normalize_text(component.get("text") or component.get("content"))
                if part:
                    parts.append(part)
            if parts:
                return " ".join(parts)
    return ""


def _build_readable_history(messages: list[Any], limit: int) -> str:
    selected = messages[-limit:] if limit > 0 else messages
    lines: list[str] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        text = _message_text(item)
        if not text:
            continue
        timestamp = _message_timestamp_text(item)
        user_name = _message_user_name(item)
        prefix = f"[{timestamp}] " if timestamp else ""
        lines.append(f"{prefix}{user_name}说：{text}")
    return "\n".join(lines)


def _is_plugin_enabled(config: AutoPlanningConfig | None) -> bool:
    return bool(config and config.plugin.enabled)


class AutoPlanningPlugin(MaiBotPlugin):
    config_model = AutoPlanningConfig

    # ── 生命周期 ──────────────────────────────────────────

    @classmethod
    def build_config_schema(
        cls,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> dict[str, object]:
        schema = super().build_config_schema(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )
        return apply_config_schema_i18n(schema)

    async def on_load(self) -> None:
        self._scheduler_task: asyncio.Task[None] | None = None
        self._start_scheduler()
        schedule_engine._clean_old_llm_logs()
        logger.info("AutoPlanningPlugin 已加载，定时器已启动")

    async def on_unload(self) -> None:
        self._stop_scheduler()
        logger.info("AutoPlanningPlugin 已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        del config_data, version
        if scope == "self" and self.config:
            logger.info("AutoPlanningPlugin 配置已热更新: allowed_streams=%s", self.config.schedule.allowed_streams)

    # ── 定时器 ────────────────────────────────────────────

    def _start_scheduler(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def _stop_scheduler(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self._scheduler_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("定时器异常")
            await asyncio.sleep(60)

    async def _scheduler_tick(self) -> None:
        if self.config is None:
            return
        cfg = self.config.schedule
        if not _is_plugin_enabled(self.config):
            return

        now = datetime.now()
        generation_time = _parse_generation_time(cfg.generation_time)
        if generation_time is None:
            if getattr(self, "_last_invalid_generation_time", None) != cfg.generation_time:
                self._last_invalid_generation_time = cfg.generation_time
                logger.warning("generation_time 配置无效，应为 HH:MM: %s", cfg.generation_time)
            return

        if now.time() < generation_time:
            return

        await self._generate_all_schedules()

    async def _generate_all_schedules(self) -> None:
        """为白名单中的聊天流生成今日日程。"""
        streams = await self._resolve_enabled_streams()
        if not streams:
            logger.info("未匹配到启用的聊天流，跳过自动日程生成")
            return

        today_str = datetime.now().strftime("%Y-%m-%d")
        missing_streams: list[dict[str, Any]] = []
        for stream in streams:
            session_id = _normalize_text(stream.get("session_id") or stream.get("stream_id"))
            if not session_id:
                continue
            if schedule_store.usable_schedule_exists(session_id, today_str):
                continue
            missing_streams.append(stream)

        if not missing_streams:
            logger.debug("所有启用聊天流均已有今日日程: date=%s streams=%s", today_str, len(streams))
            return

        logger.info(
            "开始自动生成缺失日程: date=%s missing=%s enabled=%s",
            today_str,
            len(missing_streams),
            len(streams),
        )
        for stream in missing_streams:
            session_id = _normalize_text(stream.get("session_id") or stream.get("stream_id"))
            try:
                await self._generate_for_session(session_id, stream_info=stream)
            except Exception:
                logger.exception("聊天流日程生成失败: session=%s", session_id)

    async def _generate_for_session(self, session_id: str, stream_info: dict[str, Any] | None = None) -> None:
        if self.config is None:
            return
        cfg = self.config.schedule

        persona = await self._resolve_persona()
        if stream_info is None:
            stream_info = await self._find_stream_by_session_id(session_id)
        knowledge = await self._resolve_knowledge(session_id, stream_info)
        history = await self._resolve_history_context(session_id)

        logger.info("开始生成日程: session=%s", session_id)

        schedule = await schedule_engine.generate_daily_schedule(
            self.ctx,
            session_id,
            persona=persona,
            activity_count_min=cfg.activity_count_min,
            activity_count_max=cfg.activity_count_max,
            wake_time=cfg.wake_time,
            sleep_time=cfg.sleep_time,
            model=cfg.schedule_generation_model,
            knowledge_context=knowledge,
            history_context=history,
            stream_info=stream_info,
            max_tokens=cfg.max_tokens,
        )
        if schedule is not None:
            schedule_store.save_schedule(session_id, schedule)
            logger.info("已生成 %s 日程: %d 个活动", session_id, len(schedule.get("activities", [])))
        else:
            logger.error("日程生成彻底失败: session=%s", session_id)

    async def _resolve_persona(self) -> str:
        if self.config is None:
            return ""
        source = self.config.schedule.persona_source
        if source == "extra":
            extra = str(self.config.schedule.extra_generation_prompt or "").strip()
            return extra
        system = ""
        if source in ("system", "both"):
            try:
                system = str(
                    await self.ctx.config.get("personality.personality", "")
                    or ""
                ).strip()
            except Exception:
                logger.debug("读取全局人设失败，将仅使用额外提示词")
        extra = ""
        if source == "both":
            extra = str(self.config.schedule.extra_generation_prompt or "").strip()
        if system and extra:
            return f"{system}\n\n额外设定：{extra}"
        return system or extra

    async def _resolve_knowledge(self, session_id: str, stream_info: dict[str, Any] | None) -> str:
        if self.config is None:
            return ""
        cfg = self.config.schedule
        try:
            limit = max(0, int(cfg.knowledge_search_limit))
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return ""

        terms = [
            "角色最近的习惯",
            "角色偏好",
            "日程相关约定",
            "当前聊天流发生的事件",
        ]
        group_id = ""
        user_id = ""
        if stream_info:
            if stream_info.get("group_id"):
                group_id = _normalize_text(stream_info.get("group_id"))
            if stream_info.get("user_id"):
                user_id = _normalize_text(stream_info.get("user_id"))
        query = "；".join(terms)
        try:
            call_capability = getattr(self.ctx, "call_capability", None)
            if callable(call_capability):
                result = await call_capability(
                    "knowledge.search",
                    query=query,
                    limit=limit,
                    mode="aggregate",
                    chat_id=session_id,
                    group_id=group_id,
                    user_id=user_id,
                    respect_filter=True,
                )
            else:
                result = await self.ctx.knowledge.search(query, limit=limit)
        except Exception:
            logger.debug("知识库检索失败，日程生成将不携带记忆上下文")
            return ""

        if isinstance(result, dict):
            if result.get("success") is False:
                logger.debug("知识库检索返回失败，日程生成将不携带记忆上下文: %s", result.get("error", ""))
                return ""
            result = result.get("content", "")
        return str(result or "").strip()

    async def _resolve_history_context(self, session_id: str) -> str:
        if self.config is None:
            return ""
        limit = max(0, int(self.config.schedule.history_message_limit))
        if limit <= 0:
            return ""
        try:
            window_hours = max(1, int(self.config.schedule.history_window_hours))
        except (TypeError, ValueError):
            window_hours = 24
        start_ts, end_ts = _recent_bounds(window_hours)
        try:
            messages = await self.ctx.message.get_by_time_in_chat(
                session_id,
                str(start_ts),
                str(end_ts),
                limit=limit,
                limit_mode="latest",
                filter_mai=False,
                filter_command=True,
            )
            messages = _unwrap_capability_failure(messages)
            if not messages:
                return ""
            if not isinstance(messages, list):
                logger.debug("聊天历史返回类型异常，日程生成将不携带历史上下文: type=%s", type(messages).__name__)
                return ""
            readable = _build_readable_history(messages, limit)
        except Exception:
            logger.debug("读取聊天历史失败，日程生成将不携带历史上下文")
            return ""
        return str(readable or "").strip()

    async def _resolve_enabled_streams(self) -> list[dict[str, Any]]:
        if self.config is None:
            return []
        cfg = self.config.schedule
        raw_entries = [_normalize_text(item) for item in cfg.allowed_streams]
        entries = [item for item in raw_entries if item]
        if not entries:
            logger.info("聊天流白名单为空，不启用任何聊天流")
            return []

        streams = await self._list_known_streams(cfg.stream_discovery_platform)
        if not streams:
            logger.info("未发现可用于匹配白名单的聊天流")
            return []

        if any(item.lower() == "all" for item in entries):
            logger.info("聊天流白名单为 all，启用所有已知聊天流: %s", len(streams))
            return streams

        matched: dict[str, dict[str, Any]] = {}
        for stream in streams:
            session_id = _normalize_text(stream.get("session_id") or stream.get("stream_id"))
            if not session_id:
                continue
            keys = self._stream_match_keys(stream)
            if any(entry in keys for entry in entries):
                matched[session_id] = stream

        missing = [entry for entry in entries if not any(entry in self._stream_match_keys(stream) for stream in matched.values())]
        if missing:
            logger.info("以下白名单项未匹配到已知聊天流: %s", missing)
        logger.info("白名单匹配完成: matched=%s total=%s", len(matched), len(entries))
        return list(matched.values())

    async def _list_known_streams(self, platform: str) -> list[dict[str, Any]]:
        try:
            streams = await self.ctx.chat.get_all_streams(platform or "all_platforms")
        except Exception:
            logger.debug("读取聊天流列表失败")
            return []
        if not isinstance(streams, list):
            return []
        return [item for item in streams if isinstance(item, dict)]

    def _stream_match_keys(self, stream: dict[str, Any]) -> set[str]:
        platform = _normalize_text(stream.get("platform") or "qq")
        session_id = _normalize_text(stream.get("session_id") or stream.get("stream_id"))
        group_id = _normalize_text(stream.get("group_id"))
        user_id = _normalize_text(stream.get("user_id"))
        keys: set[str] = set()
        if session_id:
            keys.add(f"session:{session_id}")
            keys.add(session_id)
        if group_id:
            keys.add(f"{platform}:group:{group_id}")
        if user_id:
            keys.add(f"{platform}:private:{user_id}")
        return keys

    async def _find_stream_by_session_id(self, session_id: str) -> dict[str, Any] | None:
        if self.config is None:
            return None
        streams = await self._list_known_streams(self.config.schedule.stream_discovery_platform)
        for stream in streams:
            if _normalize_text(stream.get("session_id") or stream.get("stream_id")) == session_id:
                return stream
        return None

    async def _is_session_enabled(self, session_id: str) -> bool:
        streams = await self._resolve_enabled_streams()
        return any(
            _normalize_text(stream.get("session_id") or stream.get("stream_id")) == session_id
            for stream in streams
        )

    # ── HookHandler ───────────────────────────────────────

    @HookHandler(
        "maisaka.planner.after_response",
        name="inject_schedule_into_reply",
        description="在 planner 决定调用 reply 后，将当前日程上下文注入 reference_info",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_schedule_into_reply_handler(
        self, **kwargs: Any
    ) -> dict[str, Any]:
        config = self.config
        if not _is_plugin_enabled(config):
            return {"action": "continue"}

        raw_tool_calls = kwargs.get("tool_calls")
        session_id = str(kwargs.get("session_id", "") or "").strip()
        if not isinstance(raw_tool_calls, list) or not session_id:
            return {"action": "continue"}
        if not await self._is_session_enabled(session_id):
            logger.info("聊天流未启用日程注入，跳过: session=%s", session_id)
            return {"action": "continue"}

        modified_tool_calls = inject_schedule_into_reply(
            raw_tool_calls,
            session_id,
            window_minutes=config.inject.inject_window_minutes,
            max_activities=config.inject.max_injected_activities,
        )
        if modified_tool_calls is raw_tool_calls:
            return {"action": "continue"}

        result = dict(kwargs)
        result["tool_calls"] = modified_tool_calls
        return {"action": "continue", "modified_kwargs": result}

    # ── Command ────────────────────────────────────────────

    @Command(
        "generate_schedule",
        description="手动生成当日日程",
        pattern=r"^/generate_schedule",
    )
    async def handle_generate_schedule_command(
        self, stream_id: str = "", **kwargs: Any
    ) -> tuple[bool, str, int]:
        del kwargs
        if not stream_id:
            return True, "无法获取当前聊天流", 0
        if not _is_plugin_enabled(self.config):
            return True, "日程生成功能未启用", 0
        if not await self._is_session_enabled(stream_id):
            return True, "当前聊天流未在 AutoPlanning 白名单中启用", 0
        asyncio.create_task(self._generate_for_session(stream_id))
        return True, "正在后台生成今日日程...", 2

    # ── Tools ─────────────────────────────────────────────

    @Tool(
        "query_schedule",
        brief_description="查询角色某一天的日程状态",
        detailed_description="当用户自然询问角色今天、过去或未来某天在做什么、准备做什么时使用。返回结果应作为角色扮演参考，由回复模型自然转述，不要机械逐条播报。\n\n参数说明：\n- date：YYYY-MM-DD 格式的日期字符串，必填。\n- session_id：当前聊天流 ID，通常由运行时提供；如无可留空。",
        parameters=[
            ToolParameterInfo(
                name="date",
                param_type=ToolParamType.STRING,
                description="要查询的日期 (YYYY-MM-DD)",
                required=True,
            ),
            ToolParameterInfo(
                name="session_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID，通常由运行时提供",
                required=False,
            ),
        ],
    )
    async def handle_query_schedule(self, date: str, session_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if not _is_plugin_enabled(self.config):
            return {"success": True, "schedule": "AutoPlanning 已禁用"}
        session_id = _resolve_tool_stream_id(session_id, kwargs)
        if not session_id:
            return {"success": False, "error": "无法获取当前聊天流"}
        if not await self._is_session_enabled(session_id):
            logger.info("query_schedule 跳过未启用聊天流: session=%s", session_id)
            return {"success": True, "schedule": "当前聊天流未启用日程系统"}
        data = schedule_store.load_schedule(session_id, date)
        if data is None:
            # 检查 pending_commitments
            commitments = schedule_store.get_pending_commitments(session_id)
            matching = [c for c in commitments if str(c.get("date", "") or "").strip() == date]
            if matching:
                lines = ["该日期暂无完整日程，但有以下约定："]
                for c in matching:
                    lines.append(f"- {c.get('time', '')} {c.get('title', '')}".strip())
                return {"success": True, "schedule": "\n".join(lines)}
            return {"success": True, "schedule": f"{date} 暂无日程安排"}

        activities = data.get("activities", [])
        if not activities:
            matching = [
                c for c in data.get("pending_commitments", [])
                if str(c.get("date", "") or "").strip() == date
            ]
            if matching:
                lines = ["该日期暂无完整日程，但有以下约定："]
                for c in matching:
                    lines.append(f"- {c.get('time', '')} {c.get('title', '')}".strip())
                return {"success": True, "schedule": "\n".join(lines)}
            return {"success": True, "schedule": f"{date} 暂无日程安排"}

        lines = [f"{date} 日程："]
        for act in activities:
            line = f"- {act.get('start', '')}-{act.get('end', '')} {act.get('title', '')}"
            if act.get("mood"):
                line += f" | 心情: {act['mood']}"
            if act.get("notes"):
                line += f" | {act['notes']}"
            lines.append(line)
        return {"success": True, "schedule": "\n".join(lines)}

    @Tool(
        "update_schedule",
        brief_description="根据对话请求维护角色日程",
        detailed_description="当用户邀请角色今天一起做某事、临时改变计划、或预约未来计划时使用。工具会先根据人设和当前日程判断角色是否接受；若角色没有接受，日程不变。\n\n参数说明：\n- description：日程相关请求的自然语言描述，例如「下午2点一起学习」。\n- session_id：当前聊天流 ID，通常由运行时提供；如无可留空。",
        parameters=[
            ToolParameterInfo(
                name="description",
                param_type=ToolParamType.STRING,
                description="日程变更的自然语言描述",
                required=True,
            ),
            ToolParameterInfo(
                name="session_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID，通常由运行时提供",
                required=False,
            ),
        ],
    )
    async def handle_update_schedule(self, description: str, session_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if not _is_plugin_enabled(self.config):
            return {"success": False, "error": "AutoPlanning 已禁用"}
        session_id = _resolve_tool_stream_id(session_id, kwargs)
        if not session_id:
            return {"success": False, "error": "无法获取当前聊天流"}
        if not await self._is_session_enabled(session_id):
            return {"success": False, "error": "当前聊天流未启用日程系统"}

        description = (description or "").strip()
        if not description:
            return {"success": False, "error": "描述不能为空"}

        cfg = self.config.schedule
        persona = await self._resolve_persona()

        async def _do_generate(merged_description: str) -> None:
            await self._do_regenerate_schedule(
                session_id,
                merged_description,
                persona=persona,
                model=cfg.schedule_generation_model,
                activity_count_min=cfg.activity_count_min,
                activity_count_max=cfg.activity_count_max,
                max_tokens=cfg.max_tokens,
            )

        schedule_engine.task_queue.submit(session_id, description, _do_generate)

        return {
            "success": True,
            "message": "日程更新请求已接收，角色会根据人设和当前日程判断是否接受；若未接受则日程不变。",
        }

    async def _do_regenerate_schedule(
        self,
        session_id: str,
        description: str,
        *,
        persona: str,
        model: str,
        activity_count_min: int,
        activity_count_max: int,
        max_tokens: int,
    ) -> None:
        try:
            schedule = await schedule_engine.regenerate_schedule(
                self.ctx,
                session_id,
                description,
                persona=persona,
                model=model,
                activity_count_min=activity_count_min,
                activity_count_max=activity_count_max,
                max_tokens=max_tokens,
            )
            if schedule is not None:
                schedule_store.save_schedule(session_id, schedule)
                logger.info("已重生成 %s 日程: %d 个活动", session_id, len(schedule.get("activities", [])))
            else:
                logger.info("日程未改变: session=%s", session_id)
        except Exception:
            logger.exception("日程重生成异常: session=%s", session_id)


def create_plugin() -> AutoPlanningPlugin:
    return AutoPlanningPlugin()
