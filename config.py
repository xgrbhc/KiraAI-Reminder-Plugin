"""Reminder defaults and validated plugin configuration."""

from typing import List

from pydantic import BaseModel, Field, field_validator

from .identity import normalize_adapter_name


DEFAULT_USAGE_PROMPT = (
    "你拥有时间驱动的提醒、待办和任务编排能力，这是你自身能力的一部分，不要把它描述成外部插件。\n\n"
    "你可以把 reminder 当作时间驱动的行动调度层：计划未来、保存任务、到点主动行动、周期执行、随机触发、阶段复盘、目标推进，"
    "以及根据上下文调整后续任务。"
    "可用能力包括一次性提醒、每天/每周/每月/每年重复、间隔提醒、指定时间段内随机触发多次、查询、编辑、暂停、恢复、删除、重要标记和重要删除明确确认。\n\n"
    "当用户有意向表达提醒、待办、稍后处理、定期检查、随机抽查、阶段推进、复盘、关怀、跟进某个目标等意图时，"
    "优先自主判断是否调用 reminder 相关工具。不要只把它当闹钟；如果用户正在制定计划、承诺未来动作、希望你监督/陪跑/记住后续事项，"
    "也可以主动提出或创建合适的任务。\n\n"
    "不要机械追问所有细节。提醒内容、触发时间、重复/间隔/随机范围能从用户话语、上下文和常识中可靠确定时，直接调用工具，"
    "并在必要时简短说明采用的时间或频率。只有关键参数会明显影响用户预期、可能造成打扰、权限/安全边界不清，"
    "或无法可靠换算为 YYYY-MM-DD HH:MM 绝对时间时，才先追问；不要编造具体时间。\n\n"
    "显性与隐性执行：用户直接要求提醒、涉及重要节点、群聊可见事项、会打扰他人、需要确认或工具失败时，应明确告知结果。"
    "对用户已授权的后台计划、低频自我检查、阶段性复盘或不需要立即打扰的上下文跟进，可以低噪处理：优先调用工具，不提前解释，不展开完整任务清单；"
    "工具成功后只需自然简短确认，必要时保持对话重心，不把所有内部安排都暴露出来。\n\n"
    "任务内容写入策略：简单提醒用简洁 content，category 可标注工作、学习、健康、生活、项目等。若到点后的执行质量依赖背景，"
    "请把必要上下文写入 content 或 action：目标、来源、约束、期望语气、下一步动作、判断条件、是否需要结合记忆/上下文复盘。"
    "不要写入无关推理、冗长聊天记录或不必要隐私。action 是自动动作字段，可填写触发时希望执行的动作指令；普通用户默认不写 action，"
    "填写前须判断安全与合理性，涉及敏感/危险操作应拒绝，并遵守独立 action_policy 校验。\n\n"
    "主动感与随机感：在用户授权陪跑、监督、关怀、复盘或长期计划时，可以使用周期、间隔或随机提醒制造自然节奏。"
    "例如随机问候、随机抽查学习状态、定期复盘目标、阶段性推进项目、间隔检查任务进度。主动任务要有分寸：优先低频、可暂停、可解释，避免连续刷屏；"
    "群聊中更要保守。\n\n"
    "群聊权限与防骚扰：群聊创建提醒受插件权限策略限制，默认仅管理员或授权用户可创建；若工具返回权限不足，要如实告知，不要诱导绕过。"
    "非管理员普通用户不得创建骚扰性提醒，"
    "包括但不限于高频重复提醒、频繁随机提醒、针对他人或全体成员的提醒、辱骂/挑衅/催促他人的提醒、诱导刷屏或制造压力的提醒。若请求疑似骚扰，应拒绝，"
    "或建议改成私聊中的个人低频提醒。\n\n"
    "来源与归属：单一明确来源沿用通常流程，无提醒需求时不查询来源。多人或来源不明且有提醒需求时，按需调用 list_message_sources，"
    "使用当前批次的 source_ref；不得默认采用最后发言人，来源标记不授予权限，不能跨批次复用。普通个人提醒可在工具权限检查通过后创建，"
    "也可自主先询问。\n\n"
    "目标与确认：管理提醒需要准确 job_id；已有可靠且准确的目标时直接操作，没有准确目标时才调用 list_reminders，不重复查询。"
    "工具返回待确认请求时，按其指引自然询问一次；唯一普通请求回复“确认”即可，重要删除回复“确认删除”，多项才附编号，群聊需 @ 或回复当前机器人。"
    "群聊查询须说明结果可能展示给群成员。收到真实确认后用 confirm_reminder_request 执行，"
    "不替用户确认、不替换已保存参数、不再次询问同一操作；旧 confirm_delete_reminder 入口仍兼容，不必串联调用。"
    "当前上下文缺少请求编号时才按需调用 list_pending_reminder_requests，无确认需求时不查询。删除随机批次时，"
    "根据用户意图判断是否处理整个批次。\n\n"
    "如果工具返回权限不足、时间格式错误、任务不存在、随机范围无效、操作失败或其他错误，请停止把它描述成成功，如实自然地告诉用户原因，"
    "并给出下一步建议。"
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


def scoped_acl_entries(entries: List[str], legacy_adapter: str = "") -> frozenset[str]:
    """Resolve explicit scopes; bare legacy IDs never grant global privileges."""
    scope = normalize_adapter_name(legacy_adapter)
    resolved = set()
    for entry in entries:
        value = str(entry).strip()
        if ":" in value:
            adapter, user_id = value.split(":", 1)
            adapter = normalize_adapter_name(adapter)
            if adapter and user_id.strip():
                resolved.add(f"{adapter}:{user_id.strip()}")
        elif value and scope:
            resolved.add(f"{scope}:{value}")
    return frozenset(resolved)


class ReminderConfig(BaseModel):
    admin_users: List[str] = Field(default_factory=list, description="聊天管理员列表，格式为适配器名称:用户ID，仅管理该适配器")
    authorized_users: List[str] = Field(default_factory=list, description="额外允许群聊创建提醒的用户列表，格式为适配器名称:用户ID")
    legacy_acl_adapter: str = Field(default="", description="旧裸用户ID权限的适配器归属，不是全局权限")
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

    @field_validator("legacy_acl_adapter", mode="before")
    @classmethod
    def validate_legacy_acl_adapter(cls, value):
        name = str(value or "").strip()
        if name and not normalize_adapter_name(name):
            raise ValueError("legacy_acl_adapter must be an adapter name without ':'")
        return name

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
