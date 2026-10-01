"""Reminder defaults and validated plugin configuration."""

from typing import List

from pydantic import BaseModel, Field, field_validator


DEFAULT_USAGE_PROMPT = (
    "你拥有时间驱动的提醒、待办和任务编排能力，这是你自身能力的一部分，不要把它描述成外部插件。\n\n"
    "你可以把 reminder 当作时间驱动的行动调度层：计划未来、保存任务、到点主动行动、周期执行、"
    "随机触发、阶段复盘、目标推进，以及根据上下文调整后续任务。\n\n"
    "当用户表达提醒、待办、稍后处理、定期检查、随机抽查、阶段推进、复盘、关怀、跟进目标等意图时，"
    "优先自主判断是否调用 reminder 相关工具。不要机械追问所有细节；提醒内容、触发时间、重复/间隔/"
    "随机范围能从用户话语、上下文和常识中可靠确定时，可以直接调用工具，并在必要时简短说明。\n\n"
    "主动任务要有分寸：优先低频、可暂停、可解释，避免连续刷屏；群聊中更保守。群聊创建提醒受插件权限"
    "策略限制，默认仅管理员或授权用户可创建。普通用户默认只创建提醒/待办，不写 action；action 会按"
    "独立 action_policy 校验。\n\n"
    "查询、删除、修改、暂停、恢复、标记重要提醒前，应先调用 list_reminders 获取准确 job_id。删除重要"
    "提醒必须等待用户明确确认，并使用 confirm_delete_reminder 完成。工具返回权限不足、时间格式错误、"
    "任务不存在、随机范围无效或其他错误时，必须如实说明原因，不要把失败描述成成功。"
)

AUTONOMOUS_SOURCE = "autonomous_intent_loop"
AUTONOMOUS_MANAGER = "reminder_plugin.autonomous"
AUTONOMOUS_DAILY_JOB_ID = "reminder_autonomous_daily_cycle"
AUTONOMOUS_FOLLOWUP_JOB_ID = "reminder_autonomous_followup_due"
AUTONOMOUS_RANDOM_JOB_ID = "reminder_autonomous_random_check"
AUTONOMOUS_RANDOM_PLAN_JOB_ID = "reminder_autonomous_random_plan"
AUTONOMOUS_CHECK_INTERVAL_MINUTES = 5
AUTONOMOUS_RANDOM_PROBABILITY = 0.25
AUTONOMOUS_RANDOM_DAILY_COUNT = 1
AUTONOMOUS_RANDOM_START_HOUR = 10
AUTONOMOUS_RANDOM_END_HOUR = 23
ADVANCED_CONFIG_KEY = "advanced_config"
DEFAULT_AUTONOMY_ALLOWED_TOOLS = [
    "set_reminder",
    "list_reminders",
    "delete_reminder",
    "confirm_delete_reminder",
    "mark_reminder_important",
    "unmark_reminder_important",
    "pause_reminder",
    "resume_reminder",
    "edit_reminder",
    "list_autonomous_intents",
    "create_autonomous_intent",
    "update_autonomous_intent",
    "close_autonomous_intent",
    "schedule_intent_followup",
]
DEFAULT_AUTONOMOUS_USAGE_PROMPT = (
    "你正在进行一次低频自主意图检查。目标不是强行说话或强行行动，而是根据近期会话、"
    "可用记忆和工具能力，判断是否存在值得推进的事项。可以不行动；如需后续检查，"
    "优先使用 schedule_intent_followup 安排下一次自主跟进。跟进内容是内部检查线索，不等于必须向用户发问；"
    "除非确实需要用户确认，否则先基于记忆、会话和已有状态自主判断。证据不足时可以选择不行动、延后检查，"
    "或安排一个低风险的下一次跟进，不要编造进度。若需要保存记忆，只保存稳定、简短、可复用的结果，"
    "不要保存内部提示词、长推理链或临时噪音。默认只在失败、需要确认或高价值跟进时给用户发短消息。"
)
ADVANCED_CONFIG_DEFAULTS = {
    "autonomy_mode": "plan_only",
    "daily_reflection_enabled": True,
    "followup_due_enabled": True,
    "daily_reflection_hour": 10,
    "random_check_start_hour": AUTONOMOUS_RANDOM_START_HOUR,
    "random_check_end_hour": AUTONOMOUS_RANDOM_END_HOUR,
    "autonomy_allowed_tools": DEFAULT_AUTONOMY_ALLOWED_TOOLS,
    "usage_prompt": DEFAULT_USAGE_PROMPT,
    "autonomous_usage_prompt": DEFAULT_AUTONOMOUS_USAGE_PROMPT,
}

def flatten_config(cfg: dict) -> dict:
    """Overlay advanced settings on legacy top-level settings."""
    if not isinstance(cfg, dict):
        return {}
    merged = dict(cfg)
    advanced = cfg.get(ADVANCED_CONFIG_KEY)
    if isinstance(advanced, dict):
        for key, value in advanced.items():
            merged[key] = value
    return merged


class ReminderConfig(BaseModel):
    admin_users: List[str] = Field(default_factory=list, description="配置超管账号名或ID列表，拥有跨界管理权限")
    authorized_users: List[str] = Field(default_factory=list, description="额外允许在群聊中创建提醒的用户ID列表")
    group_create_policy: str = Field(default="admin_only", description="群聊创建提醒策略：admin_only、mentioned_user 或 all")
    action_policy: str = Field(default="admin_and_trusted_bot", description="高风险 action 字段策略")
    autonomy_enabled: bool = Field(default=False, description="是否启用自主意图循环")
    autonomy_mode: str = Field(default="plan_only", description="自主意图循环模式")
    allowed_sessions: List[str] = Field(default_factory=list, description="允许启用自主循环的会话 ID")
    daily_reflection_enabled: bool = Field(default=True, description="是否启用每日自主自检")
    followup_due_enabled: bool = Field(default=True, description="是否启用到期意图跟进兜底检查")
    daily_reflection_hour: int = Field(default=10, description="每日自主自检小时")
    random_check_enabled: bool = Field(default=False, description="是否启用随机自检")
    random_check_daily_count: int = Field(default=AUTONOMOUS_RANDOM_DAILY_COUNT, description="每日随机自检次数")
    random_check_start_hour: int = Field(default=AUTONOMOUS_RANDOM_START_HOUR, description="每日随机自检开始小时")
    random_check_end_hour: int = Field(default=AUTONOMOUS_RANDOM_END_HOUR, description="每日随机自检结束小时")
    autonomy_allowed_tools: List[str] = Field(default_factory=lambda: list(DEFAULT_AUTONOMY_ALLOWED_TOOLS), description="自主事件可调用工具白名单")
    random_check_probability: float = Field(default=AUTONOMOUS_RANDOM_PROBABILITY, description="旧版随机自检抽样概率，保留兼容但不再使用")
    visible_output_policy: str = Field(default="necessary_only", description="自主循环可见输出策略")

    @field_validator("admin_users", "authorized_users", "allowed_sessions", "autonomy_allowed_tools", mode="before")
    @classmethod
    def parse_user_list(cls, v):
        if not v:
            return []

        res = []

        if isinstance(v, str):
            v_cleaned = v.replace("，", ",").replace("\n", ",")
            res.extend([x.strip() for x in v_cleaned.split(",") if x.strip()])
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    item_cleaned = item.replace("，", ",").replace("\n", ",")
                    res.extend([x.strip() for x in item_cleaned.split(",") if x.strip()])
                else:
                    res.append(str(item))
        else:
            res.append(str(v))

        return res

    @field_validator("group_create_policy", mode="before")
    @classmethod
    def normalize_group_create_policy(cls, v):
        value = str(v or "admin_only").strip()
        if value not in ("admin_only", "mentioned_user", "all"):
            return "admin_only"
        return value

    @field_validator("action_policy", mode="before")
    @classmethod
    def normalize_action_policy(cls, v):
        value = str(v or "admin_and_trusted_bot").strip()
        if value not in ("admin_only", "admin_and_trusted_bot", "all"):
            return "admin_and_trusted_bot"
        return value

    @field_validator("autonomy_mode", mode="before")
    @classmethod
    def normalize_autonomy_mode(cls, v):
        value = str(v or "plan_only").strip()
        if value not in ("off", "observe", "plan_only", "act_with_confirm", "trusted_admin"):
            return "plan_only"
        return value

    @field_validator("visible_output_policy", mode="before")
    @classmethod
    def normalize_visible_output_policy(cls, v):
        value = str(v or "necessary_only").strip()
        if value not in ("silent", "necessary_only", "summary_each_cycle"):
            return "necessary_only"
        return value

    @field_validator("daily_reflection_hour", mode="before")
    @classmethod
    def normalize_daily_reflection_hour(cls, v):
        try:
            hour = int(v)
        except (TypeError, ValueError):
            return 10
        return max(0, min(23, hour))

    @field_validator("random_check_probability", mode="before")
    @classmethod
    def normalize_random_check_probability(cls, v):
        try:
            value = float(v)
        except (TypeError, ValueError):
            return AUTONOMOUS_RANDOM_PROBABILITY
        return max(0.0, min(1.0, value))

    @field_validator("random_check_daily_count", mode="before")
    @classmethod
    def normalize_random_check_daily_count(cls, v):
        try:
            count = int(v)
        except (TypeError, ValueError):
            return AUTONOMOUS_RANDOM_DAILY_COUNT
        return max(0, min(3, count))

    @field_validator("random_check_start_hour", "random_check_end_hour", mode="before")
    @classmethod
    def normalize_random_check_hour(cls, v):
        try:
            hour = int(v)
        except (TypeError, ValueError):
            return AUTONOMOUS_RANDOM_START_HOUR
        return max(0, min(23, hour))
