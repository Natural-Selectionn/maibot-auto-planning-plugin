from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础配置"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    config_version: str = Field(default="0.3.0", description="配置版本")


class ScheduleSection(PluginConfigBase):
    """日程生成设置"""

    __ui_label__ = "日程生成"

    enabled: bool = Field(default=True, description="是否启用自动日程生成")
    allowed_streams: list[str] = Field(
        default_factory=list,
        description="启用日程的聊天流白名单。支持 all、session:<session_id>、<platform>:group:<group_id>、<platform>:private:<user_id>",
    )
    stream_discovery_platform: str = Field(
        default="all_platforms",
        description="解析 all 或账号白名单时扫描的平台，all_platforms 表示所有平台",
    )
    generation_time: str = Field(
        default="01:30",
        description="每日生成时间 (HH:MM)",
        json_schema_extra={"placeholder": "01:30"},
    )
    schedule_generation_model: str = Field(
        default="",
        description="生成日程使用的模型任务名，空字符串表示使用默认模型",
    )
    persona_source: str = Field(
        default="both",
        description="人物设定来源: system | extra | both",
    )
    extra_generation_prompt: str = Field(
        default="",
        description="注入到日程生成 prompt 中的额外提示词",
        json_schema_extra={"x-widget": "textarea"},
    )
    max_tokens: int = Field(default=16000, description="日程生成 LLM 的最大输出 token 数")
    activity_count_min: int = Field(default=8, description="每日活动数量下限")
    activity_count_max: int = Field(default=14, description="每日活动数量上限")
    wake_time: str = Field(default="08:30", description="角色苏醒时间 (HH:MM)")
    sleep_time: str = Field(default="01:00", description="角色入睡时间 (HH:MM)")
    history_message_limit: int = Field(default=30, description="生成日程时读取的最近消息条数")
    history_window_hours: int = Field(default=24, description="生成日程时读取最近多少小时内的聊天消息")
    knowledge_search_limit: int = Field(default=5, description="生成日程时检索当前聊天流相关记忆的条数")


class InjectSection(PluginConfigBase):
    """注入策略设置"""

    __ui_label__ = "注入策略"

    inject_window_minutes: int = Field(default=90, description="注入时间窗口 (分钟)")
    max_injected_activities: int = Field(default=3, description="最多注入活动数量")


class AutoPlanningConfig(PluginConfigBase):
    """AutoPlanning 插件完整配置"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    inject: InjectSection = Field(default_factory=InjectSection)
