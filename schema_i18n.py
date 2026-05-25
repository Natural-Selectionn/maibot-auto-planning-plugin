"""WebUI schema text for AutoPlanning config."""

from __future__ import annotations

from typing import Any


SECTION_DESCRIPTIONS: dict[str, str] = {
    "plugin": "插件基础配置",
    "schedule": "每日自动生成角色日程的规则",
    "inject": "回复时注入当前日程上下文的策略",
}

FIELD_LABELS: dict[tuple[str, str], str] = {
    ("plugin", "config_version"): "配置版本",
    ("schedule", "enabled"): "启用插件功能",
    ("schedule", "allowed_streams"): "聊天流白名单",
    ("schedule", "stream_discovery_platform"): "扫描平台",
    ("schedule", "generation_time"): "每日生成时间",
    ("schedule", "schedule_generation_model"): "日程生成模型",
    ("schedule", "persona_source"): "人设来源",
    ("schedule", "extra_generation_prompt"): "额外生成提示词",
    ("schedule", "max_tokens"): "最大输出 token",
    ("schedule", "activity_count_min"): "活动数量下限",
    ("schedule", "activity_count_max"): "活动数量上限",
    ("schedule", "wake_time"): "角色苏醒时间",
    ("schedule", "sleep_time"): "角色入睡时间",
    ("schedule", "history_message_limit"): "历史消息条数",
    ("schedule", "history_window_hours"): "历史消息时间窗口",
    ("schedule", "knowledge_search_limit"): "记忆检索条数",
    ("inject", "inject_window_minutes"): "注入时间窗口",
    ("inject", "max_injected_activities"): "最多注入活动数",
}

FIELD_HINTS: dict[tuple[str, str], str] = {
    ("plugin", "config_version"): "配置结构版本，用于插件升级时识别配置格式；通常不需要手动修改。",
    ("schedule", "enabled"): "关闭后不会自动生成日程，也不会在回复中注入日程上下文。",
    ("schedule", "allowed_streams"): (
        "启用日程的聊天流白名单。支持 all、session:<session_id>、"
        "<platform>:group:<group_id>、<platform>:private:<user_id>（eg:qq:group:123456）。空列表表示不启用任何聊天流。"
    ),
    ("schedule", "stream_discovery_platform"): "解析 all 或账号白名单时扫描的平台；all_platforms 表示扫描所有平台。",
    ("schedule", "generation_time"): "每天到达该时间后，为白名单聊天流生成当天缺失的日程。格式为 HH:MM。",
    ("schedule", "schedule_generation_model"): "生成日程使用的模型任务名；留空时使用默认模型。建议选择速度较快且输出稳定的模型。",
    ("schedule", "persona_source"): "system 使用 MaiBot 主程序人设，extra 只使用下方额外提示词，both 会按顺序拼接两者。",
    ("schedule", "extra_generation_prompt"): "附加到日程生成 prompt 的额外设定；persona_source 为 system 时不会使用这里的内容。",
    ("schedule", "max_tokens"): "日程生成 LLM 的最大输出 token 数。过低可能导致 JSON 或活动列表不完整。",
    ("schedule", "activity_count_min"): "每天日程活动数量的下限。",
    ("schedule", "activity_count_max"): "每天日程活动数量的上限。建议不小于活动数量下限。",
    ("schedule", "wake_time"): "角色每天通常醒来的时间。格式为 HH:MM。",
    ("schedule", "sleep_time"): "角色每天通常入睡的时间。格式为 HH:MM；可晚于午夜，例如 01:00。",
    ("schedule", "history_message_limit"): "生成日程时最多读取多少条最近聊天消息；设为 0 可不携带聊天历史。",
    ("schedule", "history_window_hours"): "生成日程时读取最近多少小时内的当前聊天流消息。例如 24 表示最近一天，48 表示最近两天。",
    ("schedule", "knowledge_search_limit"): "生成日程时检索当前聊天流相关记忆的条数；设为 0 可不携带知识库记忆。",
    ("inject", "inject_window_minutes"): "回复时会注入当前时间前后该分钟数范围内的活动。",
    ("inject", "max_injected_activities"): "每次回复最多注入几条活动，避免上下文过长。",
}

HIDDEN_FIELDS: set[tuple[str, str]] = {
    ("plugin", "config_version"),
}

NUMERIC_FIELD_LIMITS: dict[tuple[str, str], dict[str, int]] = {
    ("schedule", "max_tokens"): {"min": 1, "step": 1},
    ("schedule", "activity_count_min"): {"min": 1, "step": 1},
    ("schedule", "activity_count_max"): {"min": 1, "step": 1},
    ("schedule", "history_message_limit"): {"min": 0, "step": 1},
    ("schedule", "history_window_hours"): {"min": 1, "step": 1},
    ("schedule", "knowledge_search_limit"): {"min": 0, "step": 1},
    ("inject", "inject_window_minutes"): {"min": 1, "step": 1},
    ("inject", "max_injected_activities"): {"min": 1, "step": 1},
}


def apply_config_schema_i18n(schema: dict[str, Any]) -> dict[str, Any]:
    """Inject labels and hints expected by the current plugin WebUI."""

    sections = schema.get("sections")
    if not isinstance(sections, dict):
        return schema

    for section_name, section in sections.items():
        if not isinstance(section, dict):
            continue

        section_key = str(section_name)
        if section_key in SECTION_DESCRIPTIONS:
            section["description"] = SECTION_DESCRIPTIONS[section_key]

        fields = section.get("fields")
        if not isinstance(fields, dict):
            continue

        for field_name, field in fields.items():
            if not isinstance(field, dict):
                continue

            key = (section_key, str(field_name))
            if key in FIELD_LABELS:
                field["label"] = FIELD_LABELS[key]
            if key in FIELD_HINTS:
                field["hint"] = FIELD_HINTS[key]
            if key in HIDDEN_FIELDS:
                field["hidden"] = True
            if key in NUMERIC_FIELD_LIMITS:
                field.update(NUMERIC_FIELD_LIMITS[key])

    return schema
