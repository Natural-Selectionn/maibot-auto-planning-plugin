# AutoPlanning

为麦麦（MaiBot）角色扮演 AI 自动生成每日日程，并在回复时注入当前时间附近的日程上下文，提升角色扮演的真实感与沉浸度。

## 功能

- **自动日程生成**：每天定时（默认 01:30）调用 LLM，根据角色人设、聊天记录、知识库记忆自动生成当天的日程安排
- **回复注入**：在角色回复时，将当前时间附近的日程活动注入到上下文，让角色行动更贴近时间线
- **日程查询工具**：对话中可自然询问角色"今天在做什么"，通过 `query_schedule` 工具查询
- **日程更新工具**：当用户邀请角色做某事时，通过 `update_schedule` 工具角色会根据人设和当前日程判断是否接受
- **跨天活动支持**：支持凌晨跨天的睡眠/活动，昨天的跨天尾项不会在今天的日程中重复
- **未来预约**：用户可预约未来某天的活动，生成当天日程时自动纳入
- **聊天流隔离**：针对每个群聊的上下文和未来预约，生成独立的日程

## 配置说明

插件安装后可在麦麦配置界面中调整以下参数：

### 日程生成

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否启用 |
| `allowed_streams` | `[]` | 聊天流白名单，支持 `all`、`session:<id>`、`<platform>:group:<id>`、`<platform>:private:<id>` |
| `generation_time` | `01:30` | 每日自动生成时间 |
| `persona_source` | `both` | 人设来源：`system`采用maibot的设置 / `extra` 采用插件的设置 / `both` 按顺序拼接前两者|
| `wake_time` | `08:30` | 角色苏醒时间 |
| `sleep_time` | `01:00` | 角色入睡时间 |
| `activity_count_min` | `8` | 每日活动最少数量 |
| `activity_count_max` | `14` | 每日活动最多数量 |
| `schedule_generation_model` | `""` | 生成日程使用的模型（如 planner replyer 建议速度快的模型）|
| `extra_generation_prompt` | `""` | 注入日程生成的额外提示词，在`system`下无效 |
| `history_message_limit` | `30` | 生成时最多读取的最近消息条数 |
| `history_window_hours` | `24` | 生成时读取最近多少小时内的聊天消息 |
| `knowledge_search_limit` | `5` | 检索相关记忆的条数 |

### replyer提示词注入策略

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `inject_window_minutes` | `90` | 注入时间窗口（分钟），当前时间前后各 90 分钟内的活动会被注入 |
| `max_injected_activities` | `3` | 每次回复最多注入几条日程活动 |

## 命令

| 命令 | 说明 |
|------|------|
| `/generate_schedule` | 手动触发当天日程生成 |

## 依赖

- MaiBot SDK >= 2.4.0
- 需要以下能力：`chat.get_all_streams`、`llm.generate`、`knowledge.search`、`message.get_by_time_in_chat`、`config.get`

## 协议

MIT
