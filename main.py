"""KiraAI Reminder Plugin v2.2.0.

The plugin provides scheduled reminders, trusted principal authorization,
identity-aware reminder ownership, and an opt-in autonomous intent loop.
"""

import json
import os
import tempfile
import time
import traceback
import uuid
import asyncio
import datetime
import random
import secrets
import shutil
from pathlib import Path
from typing import Optional, Dict, List, Any, AsyncGenerator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.plugin import BasePlugin, logger, register_tool, on, Priority, register, PluginPage, PageMenu
from core.prompt_manager import Prompt
from core.provider import LLMRequest
from core.chat.message_utils import KiraMessageBatchEvent, KiraMessageEvent, KiraIMMessage, MessageChain
from core.chat.session import User, Group, Session
from core.chat.message_elements import Notice, Text
from core.utils.path_utils import get_data_path
from core.adapter.adapter_utils import IMAdapter
from pydantic import BaseModel, Field, field_validator
from urllib.parse import unquote

from .identity import (
    EventOrigin,
    IdentityResolver,
    PrincipalContext,
    PrincipalKind,
    build_bot_principal_id,
    migrate_reminder_identity,
)
from .permissions import (
    ReminderOperation,
    can_create_reminder,
    can_manage_reminder,
    can_set_action,
    can_view_reminder,
    is_admin,
    is_authorized,
)

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

# ========== 全局常量 ==========
_CONFIRM_TTL = 300  # 确认令牌有效期（秒）
_MAX_PENDING_TOKENS = 100  # 待确认令牌最大缓存数
_FIRE_MAX_RETRIES = 3  # 提醒触发最大重试次数
_FIRE_RETRY_DELAY = 5  # 提醒触发重试间隔（秒）
_HEALTH_CHECK_INTERVAL = 60  # 健康检查间隔（秒）


# ========== 工具函数（纯函数，无副作用）==========

def get_local_now() -> datetime.datetime:
    return datetime.datetime.now()


def parse_time_string(time_str: str) -> datetime.datetime:
    return datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")


def generate_multiple_random_times(
    start_time: datetime.datetime, end_time: datetime.datetime, count: int
) -> List[datetime.datetime]:
    if count <= 0:
        return []
    time_diff = int((end_time - start_time).total_seconds())
    if time_diff <= 0:
        return [start_time] * count
    min_interval = 60
    max_possible = time_diff // min_interval
    actual = min(count, max(1, max_possible))
    if actual < count:
        logger.warning(f"[Reminder] 时间范围不足以容纳 {count} 个提醒，已调整为 {actual} 个")
    seg = time_diff // actual
    times = []
    for i in range(actual):
        seg_start = start_time + datetime.timedelta(seconds=i * seg)
        offset = random.randint(0, max(seg - 1, 0))  # 防止 seg=0 时 randint 报错
        times.append(seg_start + datetime.timedelta(seconds=offset))
    times.sort()
    return times


def determine_random_count(
    random_count=None, random_count_min=None, random_count_max=None
) -> int:
    if random_count and random_count > 0:
        return random_count
    if random_count_min is not None and random_count_max is not None:
        if random_count_min > random_count_max:
            random_count_min, random_count_max = random_count_max, random_count_min
        return random.randint(random_count_min, random_count_max)
    return 1


# ========== 存储层 ==========

class ReminderStorage:
    """带并发锁和原子写入的提醒数据存储"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _unsafe_load(self) -> Dict[str, List[Dict]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[Reminder] 加载数据失败: {e}")
            return {}

    def _unsafe_save(self, data: Dict[str, List[Dict]]):
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
                    temp_file.write(content)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(tmp_path, self.path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error(f"[Reminder] 保存数据失败: {e}")
            raise

    async def load(self) -> Dict[str, List[Dict]]:
        async with self._lock:
            return self._unsafe_load()

    async def save(self, data: Dict[str, List[Dict]]):
        async with self._lock:
            self._unsafe_save(data)

    @asynccontextmanager
    async def modify(self) -> AsyncGenerator[Dict[str, List[Dict]], None]:
        """提供原子读-写事务"""
        async with self._lock:
            data = self._unsafe_load()
            yield data
            self._unsafe_save(data)


# ========== 插件主类 ==========

class ReminderPlugin(BasePlugin):
    """Reminder service with identity-aware ACLs and autonomous follow-up."""

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        cfg = self._migrate_advanced_config(cfg)
        self.plugin_cfg = cfg
        self.config = ReminderConfig(**self._flatten_config(cfg))
        self._default_usage_prompt = self._load_default_usage_prompt()
        data_dir = get_data_path() / "plugin_data" / "reminder_plugin"
        self._storage = ReminderStorage(data_dir / "reminders.json")
        self._autonomy_storage = ReminderStorage(data_dir / "autonomous_state.json")
        self._identity = IdentityResolver(secrets.token_urlsafe(32))
        self._scheduler: Optional[AsyncIOScheduler] = None
        # 待确认删除缓存: token -> {session_id, job_ids, content, expires_at}
        self._pending: Dict[str, Any] = {}
        self._health_task: Optional[asyncio.Task] = None
        self._fire_semaphore = asyncio.Semaphore(3)  # 最多同时 3 个提醒触发写入并发

    @staticmethod
    def _flatten_config(cfg: dict) -> dict:
        if not isinstance(cfg, dict):
            return {}
        merged = dict(cfg)
        advanced = cfg.get(ADVANCED_CONFIG_KEY)
        if isinstance(advanced, dict):
            for key, value in advanced.items():
                merged[key] = value
        return merged

    @staticmethod
    def _migrate_advanced_config(cfg: dict) -> dict:
        if not isinstance(cfg, dict):
            return {}
        advanced = cfg.get(ADVANCED_CONFIG_KEY)
        if not isinstance(advanced, dict):
            return cfg

        changed = False
        migrated = dict(cfg)
        migrated_advanced = dict(advanced)
        for key, default_value in ADVANCED_CONFIG_DEFAULTS.items():
            if key in migrated:
                if migrated_advanced.get(key) == default_value:
                    migrated_advanced[key] = migrated[key]
                migrated.pop(key, None)
                changed = True
        migrated[ADVANCED_CONFIG_KEY] = migrated_advanced

        if changed:
            try:
                config_path = get_data_path() / "config" / "plugins" / "reminder_plugin.json"
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(
                    json.dumps(migrated, ensure_ascii=False, indent=4),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning(f"[Reminder] Failed to migrate advanced config: {e}")
        return migrated

    async def initialize(self):
        await self._migrate_identity_schema_v2()
        
        self._scheduler = AsyncIOScheduler()
        self._scheduler.start()
        await self._restore_jobs()
        await self._start_autonomous_jobs()
        # 启动健康检查后台协程
        self._health_task = asyncio.get_event_loop().create_task(self._health_check_loop())
        logger.info("[Reminder] 插件初始化完成，调度器与健康检查已启动")

    async def terminate(self):
        # 取消健康检查
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            self._health_task = None
        # 关闭调度器
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        # 清空待确认缓存
        self._pending.clear()

        logger.info("[Reminder] 插件已终止")

    # ──────── 内部调度辅助 ────────

    @on.llm_request(priority=Priority.HIGH)
    async def inject_usage_prompt(self, _event: KiraMessageBatchEvent, req: LLMRequest, *_):
        usage_prompt = self._get_usage_prompt()
        if not usage_prompt:
            return

        prompt = Prompt(
            usage_prompt,
            name="reminder_usage_prompt",
            source="reminder_plugin",
        )
        self._insert_prompt_after(req.system_prompt, prompt, after_name="output")

    @on.llm_request(priority=Priority.LOW)
    async def enforce_autonomy_tool_policy(
        self,
        event: KiraMessageBatchEvent,
        req: LLMRequest,
        *_,
    ):
        self._filter_internal_event_tools(event, req)

    @staticmethod
    def _insert_prompt_after(prompts: list[Prompt], prompt: Prompt, after_name: str):
        for idx, item in enumerate(prompts):
            if isinstance(item, Prompt) and item.name == after_name:
                prompts.insert(idx + 1, prompt)
                return
        prompts.append(prompt)

    def _get_usage_prompt(self) -> str:
        cfg = self.plugin_cfg if isinstance(self.plugin_cfg, dict) else {}
        flat_cfg = self._flatten_config(cfg)
        if "usage_prompt" in flat_cfg:
            return str(flat_cfg.get("usage_prompt") or "").strip()
        return str(getattr(self, "_default_usage_prompt", DEFAULT_USAGE_PROMPT) or "").strip()

    @staticmethod
    def _load_default_usage_prompt() -> str:
        schema_path = Path(__file__).with_name("schema.json")
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            usage_prompt = schema.get("usage_prompt", {}).get("default")
            if not usage_prompt:
                usage_prompt = (
                    schema.get(ADVANCED_CONFIG_KEY, {})
                    .get("fields", {})
                    .get("usage_prompt", {})
                    .get("default")
                )
        except Exception as e:
            logger.warning(f"[Reminder] 读取默认 LLM 使用提示词失败: {e}")
            usage_prompt = None
        return str(usage_prompt or DEFAULT_USAGE_PROMPT).strip()

    def _get_autonomous_usage_prompt(self) -> str:
        cfg = self.plugin_cfg if isinstance(self.plugin_cfg, dict) else {}
        flat_cfg = self._flatten_config(cfg)
        prompt = str(flat_cfg.get("autonomous_usage_prompt") or "").strip()
        return prompt or DEFAULT_AUTONOMOUS_USAGE_PROMPT

    def _filter_internal_event_tools(self, event: KiraMessageBatchEvent, req: LLMRequest):
        principal = self._get_principal(event)
        tool_set = getattr(req, "tool_set", None)
        tools = getattr(tool_set, "tools", None)
        if not isinstance(tools, list):
            return
        if principal.trusted and principal.kind is PrincipalKind.SYSTEM:
            tool_set.remove(*(tool.name for tool in tools if getattr(tool, "name", "")))
            return
        if not (principal.trusted and principal.kind is PrincipalKind.BOT and principal.is_autonomy_event):
            return
        allowed = set(self.config.autonomy_allowed_tools)
        if self.config.autonomy_mode != "trusted_admin":
            allowed &= set(DEFAULT_AUTONOMY_ALLOWED_TOOLS)
        if self.config.autonomy_mode == "observe":
            allowed &= {"list_reminders", "list_autonomous_intents"}
        disabled = [tool.name for tool in tools if getattr(tool, "name", "") not in allowed]
        if disabled:
            tool_set.remove(*disabled)

    def _autonomy_enabled(self) -> bool:
        return bool(self.config.autonomy_enabled and self.config.autonomy_mode != "off")

    def _allowed_autonomy_sessions(self) -> List[str]:
        if not self._autonomy_enabled():
            return []
        return [str(s).strip() for s in self.config.allowed_sessions if str(s).strip()]

    def _is_autonomy_allowed_for_sid(self, sid: str) -> bool:
        return sid in self._allowed_autonomy_sessions()

    @staticmethod
    def _now_str() -> str:
        return get_local_now().strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _ensure_autonomy_root(state: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(state.get("sessions"), dict):
            state["sessions"] = {}
        return state

    @staticmethod
    def _ensure_autonomy_session(state: Dict[str, Any], sid: str) -> Dict[str, Any]:
        ReminderPlugin._ensure_autonomy_root(state)
        sessions = state["sessions"]
        session_state = sessions.setdefault(sid, {})
        session_state.setdefault("enabled", True)
        session_state.setdefault("last_cycle_at", "")
        session_state.setdefault("cooldown_until", "")
        session_state.setdefault("random_check_plan_date", "")
        session_state.setdefault("random_check_window", "")
        session_state.setdefault("random_check_times", [])
        session_state.setdefault("intents", [])
        if not isinstance(session_state["random_check_times"], list):
            session_state["random_check_times"] = []
        if not isinstance(session_state["intents"], list):
            session_state["intents"] = []
        return session_state

    @staticmethod
    def _find_intent(session_state: Dict[str, Any], intent_id: str) -> Optional[Dict[str, Any]]:
        for intent in session_state.get("intents", []):
            if str(intent.get("id")) == str(intent_id):
                return intent
        return None

    @staticmethod
    def _is_autonomous_reminder(reminder: Dict[str, Any]) -> bool:
        return (
            reminder.get("source") == AUTONOMOUS_SOURCE
            or reminder.get("managed_by") == AUTONOMOUS_MANAGER
        )

    async def _load_autonomy_state(self) -> Dict[str, Any]:
        state = await self._autonomy_storage.load()
        if not isinstance(state, dict):
            state = {}
        return self._ensure_autonomy_root(state)

    def _check_autonomy_tool_access(self, event, operation: str = "write") -> tuple[bool, str, str]:
        sid = self._get_sid(event)
        if not self._autonomy_enabled():
            return False, "❌ 自主意图循环未启用。", sid
        if not self._is_autonomy_allowed_for_sid(sid):
            return False, "❌ 当前会话不在 autonomous allowed_sessions 白名单中。", sid
        principal = self._get_principal(event)
        if self.config.autonomy_mode == "observe" and operation != "read":
            return False, "❌ observe 模式只允许读取自主意图状态。", sid
        if self._is_admin_user(event):
            return True, "", sid
        if not (
            principal.trusted
            and principal.kind is PrincipalKind.BOT
            and principal.is_autonomy_event
            and "intent.manage" in principal.capabilities
        ):
            return False, "❌ 权限拒绝：自主意图工具仅限可信机器人主体或管理员。", sid
        return True, "", sid

    async def _start_autonomous_jobs(self):
        if not self._scheduler or not self._autonomy_enabled():
            return

        allowed_sessions = self._allowed_autonomy_sessions()
        if not allowed_sessions:
            logger.info("[Reminder][Autonomous] 未配置 allowed_sessions，跳过自主循环调度")
            return

        if self.config.daily_reflection_enabled:
            self._scheduler.add_job(
                self._autonomous_daily_cycle_job,
                trigger=CronTrigger(hour=self.config.daily_reflection_hour, minute=0),
                id=AUTONOMOUS_DAILY_JOB_ID,
                replace_existing=True,
                misfire_grace_time=600,
            )
            logger.info(
                f"[Reminder][Autonomous] 每日自检已设置: {self.config.daily_reflection_hour}:00"
            )

        if self.config.followup_due_enabled and self.config.autonomy_mode != "observe":
            self._scheduler.add_job(
                self._autonomous_followup_due_job,
                trigger=IntervalTrigger(minutes=AUTONOMOUS_CHECK_INTERVAL_MINUTES),
                id=AUTONOMOUS_FOLLOWUP_JOB_ID,
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info("[Reminder][Autonomous] 到期跟进兜底检查已启动")

        if self.config.random_check_enabled:
            self._scheduler.add_job(
                self._autonomous_random_daily_plan_job,
                trigger=CronTrigger(hour=0, minute=5),
                id=AUTONOMOUS_RANDOM_PLAN_JOB_ID,
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info(
                "[Reminder][Autonomous] 随机自检已启动: "
                f"每日 {self.config.random_check_daily_count} 次，"
                f"{self.config.random_check_start_hour}:00-"
                f"{self.config.random_check_end_hour}:00"
            )
            await self._schedule_autonomous_random_checks(force_new=False)

    def _build_autonomous_notice_text(
        self,
        sid: str,
        trigger_type: str,
        intent: Optional[Dict[str, Any]] = None,
        content: str = "",
    ) -> str:
        lines = [
            "[系统事件: autonomous_intent_loop]",
            f"会话: {sid}",
            f"触发类型: {trigger_type}",
            f"自主模式: {self.config.autonomy_mode}",
            f"可见输出策略: {self.config.visible_output_policy}",
            f"当前时间: {self._now_str()}",
            "",
            "请进行低频自主意图检查。你可以保持沉默；只有失败、需要确认或有明确高价值跟进时才发短消息。",
            "如需安排下一次自主跟进，请优先调用 schedule_intent_followup，而不是普通 set_reminder。",
        ]
        if intent:
            lines.extend([
                "",
                "当前意图:",
                f"- id: {intent.get('id', '')}",
                f"- title: {intent.get('title', '')}",
                f"- status: {intent.get('status', '')}",
                f"- notes: {intent.get('notes', '')}",
            ])
        if content:
            lines.extend(["", f"跟进内容: {content}"])
        lines.extend(["", self._get_autonomous_usage_prompt()])
        return "\n".join(lines)

    async def _publish_autonomous_notice(
        self,
        sid: str,
        trigger_type: str,
        intent: Optional[Dict[str, Any]] = None,
        content: str = "",
    ):
        text = self._build_autonomous_notice_text(
            sid=sid,
            trigger_type=trigger_type,
            intent=intent,
            content=content,
        )
        origin_by_trigger = {
            "daily_reflection": EventOrigin.AUTONOMY_DAILY_REFLECTION,
            "followup_due": EventOrigin.AUTONOMY_FOLLOWUP_DUE,
            "random_check": EventOrigin.AUTONOMY_RANDOM_CHECK,
        }
        capabilities = {"intent.manage", "reminder.create", "reminder.action"}
        if self.config.autonomy_mode == "trusted_admin":
            capabilities.add("reminder.manage_all")
        await self._publish_immediate_notice(
            session=sid,
            chain=MessageChain([Text(text)]),
            origin=origin_by_trigger.get(trigger_type, EventOrigin.AUTONOMY_FOLLOWUP_DUE),
            principal_kind=PrincipalKind.BOT,
            capabilities=capabilities,
        )

    async def _autonomous_daily_cycle_job(self):
        if not (self._autonomy_enabled() and self.config.daily_reflection_enabled):
            return

        state = await self._load_autonomy_state()
        changed = False
        for sid in self._allowed_autonomy_sessions():
            session_state = self._ensure_autonomy_session(state, sid)
            if not session_state.get("enabled", True):
                continue
            try:
                await self._publish_autonomous_notice(sid, "daily_reflection")
                session_state["last_cycle_at"] = self._now_str()
                changed = True
            except Exception as e:
                logger.warning(f"[Reminder][Autonomous] 每日自检触发失败 sid={sid}: {e}")

        if changed:
            await self._autonomy_storage.save(state)

    @staticmethod
    def _parse_optional_time(value: str) -> Optional[datetime.datetime]:
        if not value:
            return None
        try:
            return parse_time_string(value)
        except ValueError:
            return None

    @staticmethod
    def _autonomous_reminder_exists(
        reminders_data: Dict[str, List[Dict]],
        sid: str,
        job_id: str,
    ) -> bool:
        if not job_id:
            return False
        for reminder in reminders_data.get(sid, []):
            if reminder.get("job_id") == job_id and ReminderPlugin._is_autonomous_reminder(reminder):
                return True
        return False

    async def _autonomous_followup_due_job(self):
        if not (
            self._autonomy_enabled()
            and self.config.followup_due_enabled
            and self.config.autonomy_mode != "observe"
        ):
            return

        state = await self._load_autonomy_state()
        reminders_data = await self._storage.load()
        now = get_local_now()
        changed = False

        for sid in self._allowed_autonomy_sessions():
            session_state = self._ensure_autonomy_session(state, sid)
            if not session_state.get("enabled", True):
                continue
            for intent in session_state.get("intents", []):
                if intent.get("status", "active") not in ("active", "waiting_confirmation"):
                    continue
                next_check_at = self._parse_optional_time(str(intent.get("next_check_at", "")))
                if not next_check_at or next_check_at > now:
                    continue
                job_id = str(intent.get("next_check_job_id", ""))
                if self._autonomous_reminder_exists(reminders_data, sid, job_id):
                    continue
                try:
                    await self._publish_autonomous_notice(
                        sid,
                        "followup_due",
                        intent=intent,
                        content=str(intent.get("followup_content", "")),
                    )
                    intent["last_followup_at"] = self._now_str()
                    intent["last_followup_source"] = "fallback_due_job"
                    intent["next_check_at"] = ""
                    intent["next_check_job_id"] = ""
                    intent["updated_at"] = self._now_str()
                    changed = True
                except Exception as e:
                    logger.warning(
                        f"[Reminder][Autonomous] 到期跟进触发失败 sid={sid} intent={intent.get('id')}: {e}"
                    )

        if changed:
            await self._autonomy_storage.save(state)

    def _random_check_window(self) -> tuple[int, int]:
        start_hour = self.config.random_check_start_hour
        end_hour = self.config.random_check_end_hour
        if end_hour <= start_hour:
            return AUTONOMOUS_RANDOM_START_HOUR, AUTONOMOUS_RANDOM_END_HOUR
        return start_hour, end_hour

    def _generate_random_check_times(self, now: datetime.datetime) -> List[str]:
        count = self.config.random_check_daily_count
        if count <= 0:
            return []
        start_hour, end_hour = self._random_check_window()
        day_start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        day_end = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        if now > day_start:
            day_start = (now + datetime.timedelta(minutes=1)).replace(second=0, microsecond=0)
        total_minutes = int((day_end - day_start).total_seconds() // 60)
        if total_minutes <= 0:
            return []
        final_count = min(count, total_minutes)
        offsets = sorted(random.sample(range(total_minutes), final_count))
        return [
            (day_start + datetime.timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M")
            for offset in offsets
        ]

    def _random_job_id(self, sid: str, time_str: str) -> str:
        safe_sid = sid.replace(":", "_")
        safe_time = time_str.replace("-", "").replace(" ", "_").replace(":", "")
        return f"{AUTONOMOUS_RANDOM_JOB_ID}_{safe_sid}_{safe_time}"

    async def _schedule_autonomous_random_checks(self, force_new: bool = False):
        if not (self._scheduler and self._autonomy_enabled() and self.config.random_check_enabled):
            return
        if self.config.random_check_daily_count <= 0:
            return

        state = await self._load_autonomy_state()
        now = get_local_now()
        today = now.strftime("%Y-%m-%d")
        start_hour, end_hour = self._random_check_window()
        window_key = f"{start_hour}-{end_hour}"
        changed = False

        for sid in self._allowed_autonomy_sessions():
            session_state = self._ensure_autonomy_session(state, sid)
            if not session_state.get("enabled", True):
                continue

            current_times = session_state.get("random_check_times", [])
            should_generate = (
                force_new
                or session_state.get("random_check_plan_date") != today
                or session_state.get("random_check_window") != window_key
                or len(current_times) != self.config.random_check_daily_count
            )
            if should_generate:
                current_times = self._generate_random_check_times(now)
                session_state["random_check_plan_date"] = today
                session_state["random_check_window"] = window_key
                session_state["random_check_times"] = current_times
                changed = True

            registered = 0
            for time_str in current_times:
                run_at = self._parse_optional_time(str(time_str))
                if not run_at or run_at <= now:
                    continue
                self._scheduler.add_job(
                    self._autonomous_random_check_job,
                    trigger=DateTrigger(run_date=run_at),
                    id=self._random_job_id(sid, time_str),
                    kwargs={"sid": sid, "scheduled_time": time_str},
                    replace_existing=True,
                    misfire_grace_time=600,
                )
                registered += 1
            logger.info(
                f"[Reminder][Autonomous] 随机自检计划 sid={sid}, "
                f"date={today}, times={current_times}, registered={registered}"
            )

        if changed:
            await self._autonomy_storage.save(state)

    async def _autonomous_random_daily_plan_job(self):
        await self._schedule_autonomous_random_checks(force_new=True)

    async def _autonomous_random_check_job(self, sid: str, scheduled_time: str = ""):
        if not (self._autonomy_enabled() and self.config.random_check_enabled):
            return

        state = await self._load_autonomy_state()
        changed = False

        if not self._is_autonomy_allowed_for_sid(sid):
            return
        session_state = self._ensure_autonomy_session(state, sid)
        if not session_state.get("enabled", True):
            return
        try:
            await self._publish_autonomous_notice(sid, "random_check")
            session_state["last_random_check_at"] = self._now_str()
            session_state["last_random_check_scheduled_at"] = str(scheduled_time or "")
            changed = True
        except Exception as e:
            logger.warning(f"[Reminder][Autonomous] 随机自检触发失败 sid={sid}: {e}")

        if changed:
            await self._autonomy_storage.save(state)

    async def _mark_autonomous_followup_fired(self, sid: str, reminder: Dict[str, Any]):
        intent_id = str(reminder.get("intent_id") or "")
        if not intent_id:
            return
        async with self._autonomy_storage.modify() as state:
            session_state = self._ensure_autonomy_session(state, sid)
            intent = self._find_intent(session_state, intent_id)
            if not intent:
                return
            intent["last_followup_at"] = self._now_str()
            intent["last_followup_job_id"] = reminder.get("job_id", "")
            if intent.get("next_check_job_id") == reminder.get("job_id"):
                intent["next_check_at"] = ""
                intent["next_check_job_id"] = ""
            intent["updated_at"] = self._now_str()

    async def _remove_autonomous_reminders(
        self,
        sid: str,
        intent_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> int:
        removed = 0
        async with self._storage.modify() as data:
            reminders = data.get(sid, [])
            kept = []
            for reminder in reminders:
                matches_intent = intent_id and reminder.get("intent_id") == intent_id
                matches_job = job_id and reminder.get("job_id") == job_id
                if self._is_autonomous_reminder(reminder) and (matches_intent or matches_job):
                    removed += 1
                    try:
                        if self._scheduler and reminder.get("job_id"):
                            self._scheduler.remove_job(reminder["job_id"])
                    except Exception:
                        pass
                    continue
                kept.append(reminder)
            data[sid] = kept
        return removed

    @register.page(
        "/dashboard",
        menu=PageMenu(
            label={"zh": "提醒", "en": "Reminders"},
            icon="Document",
            order=20,
        ),
    )
    def dashboard(self):
        return PluginPage.from_folder("./web")

    @register.api("GET", "/sessions")
    async def api_get_sessions(self):
        try:
            data = await self._storage.load()
            sessions = []
            for k, v in data.items():
                if len(v) > 0:
                    creators = {}
                    for r in v:
                        uid = r.get("creator_id", "unknown")
                        uname = r.get("creator_name", "未知")
                        if uid not in creators:
                            creators[uid] = uname
                        elif creators[uid] in ("未知", "legacy_user") and uname not in ("未知", "legacy_user"):
                            creators[uid] = uname

                    users_list = [{"id": uid, "name": uname} for uid, uname in creators.items()]
                    sessions.append({"id": k, "count": len(v), "users": users_list})

            return {"status": "ok", "data": sessions}
        except Exception as e:
            logger.error(f"[Reminder] WebUI 获取会话列表失败: {e}")
            return {"status": "error", "msg": str(e)}

    @register.api("GET", "/reminders/{session_id}")
    async def api_get_reminders(self, session_id: str):
        try:
            sid = unquote(session_id)
            data = await self._storage.load()
            reminders = data.get(sid, [])
            return {"status": "ok", "data": reminders}
        except Exception as e:
            logger.error(f"[Reminder] WebUI 获取提醒列表失败: {e}")
            return {"status": "error", "msg": str(e)}

    @register.api("POST", "/reminders/confirm-delete")
    async def api_confirm_delete_reminder(self, payload: dict):
        confirm_token = str(payload.get("confirm_token") or "").strip()
        if not confirm_token:
            return {"status": "error", "msg": "缺少确认令牌"}

        sid = payload.get("session_id") or "webui:dm:web_admin_superuser"
        fake_event = self._build_web_event(str(sid))
        res = await self.confirm_delete_reminder(fake_event, confirm_token=confirm_token)
        return {"status": "ok" if self._is_web_action_success(res) else "error", "msg": res}

    @register.api("POST", "/reminders/{action}")
    async def api_action_reminders(self, action: str, payload: dict):
        sid = payload.get("session_id")
        job_id = payload.get("job_id")
        force = payload.get("force", True)

        if not sid or not job_id:
            return {"status": "error", "msg": "缺少必要参数"}

        fake_event = self._build_web_event(str(sid))

        if action == "delete":
            res = await self.delete_reminder(fake_event, job_id=job_id, force=force)
        elif action == "pause":
            res = await self.pause_reminder(fake_event, job_id=job_id)
        elif action == "resume":
            res = await self.resume_reminder(fake_event, job_id=job_id)
        else:
            return {"status": "error", "msg": "无效动作"}

        return {"status": "ok" if self._is_web_action_success(res) else "error", "msg": res}

    def _build_web_event(self, sid: str):
        class DummyWebEvent:
            class DummySender:
                def __init__(self):
                    self.user_id = "web_admin"
                    self.nickname = "Web UI 用户"

            class DummyMsg:
                def __init__(self, extra):
                    self.sender = DummyWebEvent.DummySender()
                    self.extra = extra
                    self.self_id = "webui"

            def __init__(self, _sid, extra):
                self.sid = _sid
                self.message = DummyWebEvent.DummyMsg(extra)

        envelope = self._identity.build_envelope(
            origin=EventOrigin.WEB_ADMIN,
            principal_kind=PrincipalKind.WEB,
            principal_id="web:authenticated-admin",
            capabilities={"reminder.manage", "reminder.action", "intent.manage"},
        )
        return DummyWebEvent(sid, envelope)

    @staticmethod
    def _is_web_action_success(result: str) -> bool:
        error_markers = (
            "❌", "拒绝", "出错", "错误", "找不到", "无效", "缺少",
            "无法", "不支持", "请确认删除令牌", "重要提醒",
        )
        return not any(marker in result for marker in error_markers)

    async def _migrate_identity_schema_v2(self):
        """Upgrade persisted reminder ownership with a one-time recoverable backup."""
        data = await self._storage.load()
        if not isinstance(data, dict) or not data:
            return
        pending = sum(
            1
            for reminders in data.values()
            if isinstance(reminders, list)
            for reminder in reminders
            if isinstance(reminder, dict) and reminder.get("identity_schema") != 2
        )
        if not pending:
            return

        backup_path = self._storage.path.with_name("reminders.pre-v2.2.backup.json")
        if self._storage.path.exists() and not backup_path.exists():
            shutil.copy2(self._storage.path, backup_path)

        migrated = 0
        for sid, reminders in data.items():
            if not isinstance(reminders, list):
                continue
            for reminder in reminders:
                if isinstance(reminder, dict) and migrate_reminder_identity(str(sid), reminder):
                    migrated += 1
        if migrated:
            await self._storage.save(data)
            logger.info(f"[Reminder] Migrated {migrated} reminders to identity schema v2")

    def _get_principal(self, event) -> PrincipalContext:
        return self._identity.resolve(event)

    def _get_creator_info(self, event) -> Dict[str, str]:
        principal = self._get_principal(event)
        return {
            "creator_id": principal.principal_id or "unknown",
            "creator_name": principal.display_name or (
                "自主意图循环" if principal.kind is PrincipalKind.BOT else "未知"
            ),
        }

    def _identity_fields_for_event(self, event) -> Dict[str, Any]:
        principal = self._get_principal(event)
        if principal.kind is PrincipalKind.BOT and principal.trusted:
            owner_type = PrincipalKind.BOT.value
            owner_id = principal.principal_id
            owner_name = "自主意图循环"
            visibility = "session_readonly"
            managed_by = AUTONOMOUS_MANAGER
        elif principal.kind is PrincipalKind.USER:
            owner_type = PrincipalKind.USER.value
            owner_id = principal.principal_id
            owner_name = principal.display_name or "未知"
            visibility = "owner"
            managed_by = "reminder_plugin"
        elif principal.kind is PrincipalKind.WEB and principal.trusted:
            owner_type = PrincipalKind.WEB.value
            owner_id = principal.principal_id
            owner_name = "Web UI 管理员"
            visibility = "admin_only"
            managed_by = "reminder_plugin"
        else:
            owner_type = PrincipalKind.LEGACY.value
            owner_id = "legacy"
            owner_name = "未知"
            visibility = "admin_only"
            managed_by = "reminder_plugin"
        return {
            "identity_schema": 2,
            "owner_type": owner_type,
            "owner_id": owner_id,
            "owner_name": owner_name,
            "created_by_type": principal.kind.value,
            "created_by_id": principal.principal_id,
            "origin": principal.origin.value,
            "visibility": visibility,
            "managed_by": managed_by,
        }

    def _check_permission(
        self,
        event,
        target_reminder: Dict,
        operation: ReminderOperation = ReminderOperation.EDIT,
    ) -> bool:
        return can_manage_reminder(
            self._get_principal(event),
            target_reminder,
            operation=operation,
            sid=self._get_sid(event),
            admin_users=self.config.admin_users,
        )

    def _is_admin_user(self, event) -> bool:
        return is_admin(self._get_principal(event), self.config.admin_users)

    def _is_group_event(self, event) -> bool:
        try:
            is_group_message = getattr(event, "is_group_message", None)
            if callable(is_group_message):
                return bool(is_group_message())
        except Exception:
            pass

        sid = getattr(event, "sid", "") or getattr(getattr(event, "session", None), "sid", "")
        if sid:
            return ":gm:" in sid

        try:
            if hasattr(event, "message") and getattr(event.message, "group", None) is not None:
                return True
            messages = getattr(event, "messages", None)
            if messages:
                return getattr(messages[-1], "group", None) is not None
        except Exception:
            pass
        return False

    def _is_event_mentioned(self, event) -> bool:
        mentioned = getattr(event, "is_mentioned", None)
        if mentioned is not None:
            return bool(mentioned)

        try:
            messages = getattr(event, "messages", None)
            if messages:
                return bool(getattr(messages[-1], "is_mentioned", False))
        except Exception:
            pass
        return False

    def _is_authorized_user(self, event) -> bool:
        return is_authorized(self._get_principal(event), self.config.authorized_users)

    def _check_create_permission(self, event) -> tuple[bool, str]:
        if can_create_reminder(
            self._get_principal(event),
            is_group=self._is_group_event(event),
            is_mentioned=self._is_event_mentioned(event),
            group_policy=self.config.group_create_policy,
            admin_users=self.config.admin_users,
            authorized_users=self.config.authorized_users,
            autonomy_mode=self.config.autonomy_mode,
        ):
            return True, ""
        return False, "❌ 权限拒绝：当前主体或群聊策略不允许创建提醒。"

    def _check_action_permission(self, event, action: Optional[str]) -> tuple[bool, str]:
        if not action:
            return True, ""
        if can_set_action(
            self._get_principal(event),
            action_policy=self.config.action_policy,
            admin_users=self.config.admin_users,
            autonomy_mode=self.config.autonomy_mode,
        ):
            return True, ""
        return False, "❌ 权限拒绝：当前 action_policy 不允许该主体设置自动动作。"

    async def _health_check_loop(self):
        """定期检查调度器状态，异常时自动重启"""
        try:
            while True:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
                if self._scheduler and not self._scheduler.running:
                    logger.error("[Reminder] 调度器已停止，尝试重启...")
                    try:
                        self._scheduler.start()
                        await self._restore_jobs()
                        logger.info("[Reminder] 调度器重启成功")
                    except Exception as e:
                        logger.error(f"[Reminder] 调度器重启失败: {e}")
        except asyncio.CancelledError:
            logger.debug("[Reminder] 健康检查任务已取消")
        except Exception as e:
            logger.error(f"[Reminder] 健康检查异常: {e}")

    async def _restore_jobs(self):
        """启动时从持久化存储恢复提醒任务，同时清理过期的一次性提醒"""
        async with self._storage.modify() as data:
            now = get_local_now()
            restored = 0
            expired = 0
            for sid, reminders in list(data.items()):
                kept = []
                for r in reminders:
                    try:
                        trigger_time = parse_time_string(r["time"])
                        repeat = r.get("repeat", "none")
                        if repeat != "none":
                            # 重复提醒始终恢复
                            if not r.get("paused"):
                                self._add_job(sid, r)
                            kept.append(r)
                            restored += 1
                        elif trigger_time > now:
                            # 未过期的一次性提醒
                            if not r.get("paused"):
                                self._add_job(sid, r)
                            kept.append(r)
                            restored += 1
                        else:
                            # 过期的一次性提醒 → 丢弃
                            expired += 1
                    except Exception as e:
                        logger.warning(f"[Reminder] 恢复任务失败: {e}")
                        kept.append(r)  # 解析失败的保留，不丢数据
                data[sid] = kept
            
            if expired:
                logger.info(f"[Reminder] 清理了 {expired} 条过期一次性提醒")
            if restored:
                logger.info(f"[Reminder] 已恢复 {restored} 个提醒任务")

    def _add_job(self, sid: str, r: Dict):
        """向调度器添加一个任务"""
        if not self._scheduler:
            return
            
        trigger_time = parse_time_string(r["time"])
        repeat = r.get("repeat", "none")
        job_id = r.get("job_id", "")

        if repeat == "none":
            trigger = DateTrigger(run_date=trigger_time)
        elif repeat == "daily":
            trigger = CronTrigger(hour=trigger_time.hour, minute=trigger_time.minute)
        elif repeat == "weekly":
            trigger = CronTrigger(day_of_week=trigger_time.weekday(),
                                  hour=trigger_time.hour, minute=trigger_time.minute)
        elif repeat == "monthly":
            trigger = CronTrigger(day=trigger_time.day,
                                  hour=trigger_time.hour, minute=trigger_time.minute)
        elif repeat == "yearly":
            trigger = CronTrigger(month=trigger_time.month, day=trigger_time.day,
                                  hour=trigger_time.hour, minute=trigger_time.minute)
        elif repeat == "interval":
            interval_minutes = r.get("interval_minutes", 30)
            # 加固：若基准时间已过，将 start_date 调整为当前时间+1个间隔，避免意外立即触发
            now = get_local_now()
            safe_start = trigger_time if trigger_time > now else now + datetime.timedelta(minutes=interval_minutes)
            trigger = IntervalTrigger(minutes=interval_minutes, start_date=safe_start)
        else:
            trigger = DateTrigger(run_date=trigger_time)

        try:
            self._scheduler.add_job(
                self._fire_reminder,
                trigger=trigger,
                id=job_id,
                args=[sid, r],
                replace_existing=True,
                misfire_grace_time=300,
            )
        except Exception as e:
            logger.warning(f"[Reminder] 添加调度任务失败 job_id={job_id}: {e}")

    async def _publish_immediate_notice(
        self,
        session: str,
        chain: MessageChain,
        *,
        origin: EventOrigin = EventOrigin.REMINDER_FIRE,
        principal_kind: PrincipalKind = PrincipalKind.SYSTEM,
        principal_id: str = "",
        delegated_owner_id: str = "",
        capabilities: Optional[set[str]] = None,
    ):
        """Publish a reminder notice and force the current session buffer to flush."""
        cur_time = int(time.time())
        parts = session.split(":")
        if len(parts) != 3:
            raise ValueError(f"Failed to parse session string: {session}")

        adapter_name, session_type, target_id = parts
        adapter = self.ctx.adapter_mgr.get_adapter(adapter_name)
        if not adapter:
            raise ValueError(f"Failed to get adapter: {adapter_name}")

        group = Group(group_id=target_id) if session_type == "gm" else None
        adapter_config = getattr(adapter, "config", {}) or {}
        self_id = str(
            adapter_config.get("self_id")
            or adapter_config.get("bot_pid")
            or getattr(adapter, "self_id", "")
            or "unknown"
        )
        if principal_kind is PrincipalKind.BOT:
            resolved_principal_id = principal_id or build_bot_principal_id(adapter_name, self_id)
            sender_id = self_id
            sender_name = "Kira"
        elif principal_kind is PrincipalKind.USER:
            resolved_principal_id = principal_id
            sender_id = principal_id
            sender_name = "提醒任务所有者"
        else:
            resolved_principal_id = principal_id or "system:reminder_plugin"
            sender_id = "system:reminder_plugin"
            sender_name = "system"
        envelope = self._identity.build_envelope(
            origin=origin,
            principal_kind=principal_kind,
            principal_id=resolved_principal_id,
            delegated_owner_id=delegated_owner_id,
            capabilities=capabilities or set(),
        )
        event = KiraMessageEvent(
            adapter=adapter.info,
            message_types=adapter.message_types,
            message=KiraIMMessage(
                timestamp=cur_time,
                sender=User(
                    user_id=sender_id,
                    nickname=sender_name,
                ),
                group=group,
                message_id="system_message",
                self_id=self_id,
                is_notice=True,
                is_mentioned=True,
                chain=chain,
                extra=envelope,
            ),
            timestamp=cur_time,
        )
        if session_type == "dm":
            event.session = Session(
                adapter_name=adapter.info.name,
                session_type="dm",
                session_id=target_id,
                session_title=sender_name,
            )
        event.flush(force=True)
        await self.ctx.event_bus.publish(event)

    async def _fire_reminder(self, sid: str, r: Dict):
        """提醒触发时发送消息到目标会话，带重试机制与并发写入保护"""
        async with self._fire_semaphore:  # 最多同时 3 个提醒执行，避免竞争写入
            content = r.get("content", "")
            job_id = r.get("job_id", "")
            repeat = r.get("repeat", "none")
            category = r.get("category", "")
            action = r.get("action", "")
            logger.info(f"[Reminder] 触发提醒 sid={sid} content={content}")
            from core.chat.message_elements import Text
            
            if self._is_autonomous_reminder(r):
                msg = self._build_autonomous_notice_text(
                    sid=sid,
                    trigger_type="followup_due",
                    intent={
                        "id": r.get("intent_id", ""),
                        "title": r.get("intent_title", ""),
                        "status": "active",
                        "notes": r.get("intent_notes", ""),
                    },
                    content=content,
                )
            else:
                cat_str = f"[{category}] " if category else ""
                msg = f"\u23f0 {cat_str}提醒：{content}"
                if action:
                    msg += f"\n\U0001f449 自动动作指令：{action}\n(请优先执行上述动作建议并回复结果)"
                
            chain = MessageChain([Text(msg)])
            sent = False
            for attempt in range(1, _FIRE_MAX_RETRIES + 1):
                try:
                    owner_type = str(r.get("owner_type") or "legacy")
                    if owner_type == PrincipalKind.BOT.value:
                        principal_kind = PrincipalKind.BOT
                        principal_id = ""
                        capabilities = {"intent.manage", "reminder.create", "reminder.action"}
                        if self.config.autonomy_mode == "trusted_admin":
                            capabilities.add("reminder.manage_all")
                        origin = EventOrigin.AUTONOMY_FOLLOWUP_DUE
                    elif owner_type == PrincipalKind.USER.value:
                        principal_kind = PrincipalKind.USER
                        principal_id = str(r.get("owner_id") or "")
                        capabilities = {"reminder.create", "reminder.manage_own"}
                        origin = EventOrigin.REMINDER_FIRE
                    else:
                        principal_kind = PrincipalKind.SYSTEM
                        principal_id = "system:reminder_plugin"
                        capabilities = set()
                        origin = EventOrigin.REMINDER_FIRE
                    await self._publish_immediate_notice(
                        session=sid,
                        chain=chain,
                        origin=origin,
                        principal_kind=principal_kind,
                        principal_id=principal_id,
                        delegated_owner_id=str(r.get("owner_id") or ""),
                        capabilities=capabilities,
                    )
                    sent = True
                    break
                except Exception as e:
                    logger.warning(
                        f"[Reminder] 发送提醒失败 (尝试 {attempt}/{_FIRE_MAX_RETRIES}): {e}"
                    )
                    if attempt < _FIRE_MAX_RETRIES:
                        await asyncio.sleep(_FIRE_RETRY_DELAY)
            if not sent:
                logger.error(f"[Reminder] 提醒发送彻底失败 sid={sid} job_id={job_id}")
                try:
                    async with self._storage.modify() as data:
                        for rv in data.get(sid, []):
                            if rv.get("job_id") == job_id:
                                rv["failed_at"] = get_local_now().strftime("%Y-%m-%d %H:%M")
                                break
                except Exception:
                    pass
            elif self._is_autonomous_reminder(r):
                await self._mark_autonomous_followup_fired(sid, r)
            # 一次性提醒触发后清理存储记录
            if sent and repeat == "none":
                try:
                    async with self._storage.modify() as data:
                        if sid in data:
                            data[sid] = [rv for rv in data[sid] if rv.get("job_id") != job_id]
                except Exception as e:
                    logger.warning(f"[Reminder] 清理已触发提醒失败: {e}")

    @on.im_message(priority=Priority.HIGH)
    async def handle_quick_command(self, event: KiraMessageEvent):
        """绕过 LLM 的极速指令响应拦截器"""
        if not event.message.chain or event.is_stopped:
            return

        # 提取纯文本
        text = " ".join([m.text.strip() for m in event.message.chain if isinstance(m, Text)]).strip()
        if not text:
            return
        if self._is_group_event(event) and not self._is_event_mentioned(event):
            return

        sid = self._get_sid(event)
        
        # --- 读取当前会话已有提醒列表用于下发的卡片绘制或序号映射 ---
        try:
            data = await self._storage.load()
            reminders = data.get(sid, [])
        except Exception:
            reminders = []

        # 0. 极简帮助菜单
        if text in ["/r help", "/remind help", "-r help", "/待办 帮助", "/提醒 帮助", "/r h"]:
            help_str = (
                "[ 待办控制台 ]\n"
                "/r : 查看列表\n"
                "/r add 时间 内容\n"
                "/r rm <序号> : 删除\n"
                "/r pause <序号> : 暂停\n"
                "/r resume <序号> : 恢复\n"
                "/r view <序号> : 详情\n"
                "*(别名: add为a/+, rm为-, view为v/i)*\n\n"
                "[ 全景视图（超管） ]\n"
                "/r all : 跨人员查看所有事项\n"
                "/r view @xxx @某人 : 聚焦特定成员"
            )
            await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(help_str)]))
            event.discard(force=True)
            event.stop()
            return

        # 1. 独立极简卡片格式化 (Mac/CLI 高级质感)
        list_triggers = ["/r", "/remind", "/提", "/待办", "-r list"]
        if text in list_triggers or text in ["/r all", "/待办全部"]:
            is_global_view = text in ["/r all", "/待办全部"]
            
            if is_global_view and not self._is_admin_user(event):
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("❌ 拒绝执行：您没有全局查看权限。")]))
                event.discard(force=True)
                event.stop()
                return

            # 数据过滤隔离
            filtered_reminders = []
            for r in reminders:
                if is_global_view or can_view_reminder(
                    self._get_principal(event),
                    r,
                    sid=sid,
                    admin_users=self.config.admin_users,
                ):
                    filtered_reminders.append(r)

            if not filtered_reminders:
                view_type = "全系群落" if is_global_view else "您的私有"
                result_str = f"[ 待办列表 ({view_type}) ]\n空空如也"
            else:
                title = "[ 全景穿梭视图 (上帝模式) ]" if is_global_view else "[ 可访问待办列表 ]"
                lines = [title]
                
                # 如果是上帝模式，最好按人物聚类
                if is_global_view:
                    from itertools import groupby
                    sorted_rems = sorted(filtered_reminders, key=lambda x: x.get('creator_name', '未知'))
                    for creator, group in groupby(sorted_rems, key=lambda x: x.get('creator_name', '未知')):
                        lines.append(f"\n👥 {creator}")
                        for r in group:
                            idx = reminders.index(r) + 1
                            lines.append(f"  {idx}. {r.get('content', '')[:20]}.. ({r.get('time', '')})")
                else:
                    for r in filtered_reminders:
                        i = reminders.index(r) + 1  # 保持原有索引用于极短删除映射
                        status = " (已暂停)" if r.get("paused") else ""
                        cate_str = f"[{r['category']}] " if r.get("category") else ""
                        
                        rep_map = {"none": "单次", "daily": "每天", "weekly": "每周", "monthly": "每月", "yearly": "每年", "interval": f"每{r.get('interval_minutes', 30)}分"}
                        rep_str = f" · {rep_map.get(r.get('repeat', 'none'), '单次')}"
                        imp_str = " ⭐" if r.get("important") else ""
                        
                        lines.append("")
                        lines.append(f"{i}. {r.get('time', '未知')}{rep_str}")
                        
                        raw_content = r.get('content', '')
                        disp_content = raw_content[:40] + "..." if len(raw_content) > 40 else raw_content
                        lines.append(f"   {cate_str}{disp_content}{imp_str}{status}")
                        
                result_str = "\n".join(lines)
            
            await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
            event.discard(force=True)
            event.stop()
            return

        # 1.5 查阅特定人员清单
        if text.startswith("/待办查 ") or text.startswith("/r view @"):
            if not self._is_admin_user(event):
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("❌ 拒绝执行：这是管理员专属探查工具。")]))
                event.discard(force=True)
                event.stop()
                return
                
            match_name = text.split(" ", 1)[1].strip().lstrip("@")
            targets = [r for r in reminders if match_name.lower() in r.get("creator_name", "").lower()]
            if not targets:
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(f"未找到与 [{match_name}] 相关的日程。")]))
                event.discard(force=True)
                event.stop()
                return
                
            lines = [f"🔍 [ 聚焦透视: {match_name} ]"]
            for r in targets:
                i = reminders.index(r) + 1
                lines.append(f"\n{i}. {r.get('time', '')} - {r.get('content', '')[:30]}...")
            
            await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("\n".join(lines))]))
            event.discard(force=True)
            event.stop()
            return

        # 2. 规范化指令前缀，解析子命令与参数
        parts = text.split()
        if len(parts) >= 2:
            action_cmd = parts[0]
            action_arg = parts[1]
            
            # 若第一段是体系主词 (如 /r, /待办)，则将第二段视作子命令
            if action_cmd in list_triggers and len(parts) >= 3:
                # 兼容中文及英文短拼
                sub_cmd_map = {
                    "删除": "/rm", "rm": "/rm", "d": "/rm", "-": "/rm",
                    "暂停": "/pause", "pause": "/pause", "p": "/pause",
                    "恢复": "/resume", "resume": "/resume", "re": "/resume",
                    "查看": "/view", "view": "/view", "v": "/view", "i": "/view", "info": "/view"
                }
                if parts[1] in sub_cmd_map:
                    action_cmd = sub_cmd_map[parts[1]]
                    action_arg = parts[2]

            # 3. 解析操作目标：支持用极短序号代替长 job_id
            job_id_target = action_arg
            if action_arg.isdigit():
                idx = int(action_arg) - 1
                if 0 <= idx < len(reminders):
                    job_id_target = reminders[idx].get("job_id")
                else:
                    msg = f"找不到序号: {action_arg}"
                    if action_cmd in ["/rm", "-rm", "/删除", "/pause", "-pause", "/暂停", "/resume", "-resume", "/恢复", "/view", "-view", "/查看", "/v", "/i"]:
                        await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(msg)]))
                        event.discard(force=True)
                        event.stop()
                        return

            # 执行删除指令
            if action_cmd in ["/rm", "-rm", "/删除"]:
                result_str = await self.delete_reminder(event, job_id=job_id_target)
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
                event.discard(force=True)
                event.stop()
                return

            # 执行暂停指令
            if action_cmd in ["/pause", "-pause", "/暂停"]:
                result_str = await self.pause_reminder(event, job_id=job_id_target)
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
                event.discard(force=True)
                event.stop()
                return
                
            # 执行恢复指令
            if action_cmd in ["/resume", "-resume", "/恢复"]:
                result_str = await self.resume_reminder(event, job_id=job_id_target)
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
                event.discard(force=True)
                event.stop()
                return

            # 执行查看详情指令
            if action_cmd in ["/view", "-view", "/查看", "/v", "-v", "/i"]:
                target_r = next((r for r in reminders if r.get("job_id") == job_id_target), None)
                if not target_r:
                    result_str = f"找不到任务: {job_id_target}"
                elif not self._check_permission(event, target_r, ReminderOperation.VIEW):
                    result_str = "❌ 权限拒绝：您无权查看该任务。"
                else:
                    rep_map = {"none": "单次", "daily": "每天", "weekly": "每周", "monthly": "每月", "yearly": "每年", "interval": f"每{target_r.get('interval_minutes', 30)}分"}
                    rep_str = rep_map.get(target_r.get('repeat', 'none'), '单次')
                    status = "已暂停" if target_r.get("paused") else "活动中"
                    cate = target_r.get('category', '无分类')
                    imp = "是" if target_r.get('important') else "否"
                    full_content = target_r.get('content', '')
                    result_str = (
                        f"[ 任务详情 ]\n"
                        f"时间: {target_r.get('time', '未知')}\n"
                        f"频率: {rep_str}\n"
                        f"状态: {status}\n"
                        f"分类: {cate}\n"
                        f"重要: {imp}\n"
                        f"创建: {target_r.get('created_at', '未知')}\n"
                        f"创建人: {target_r.get('creator_name', '未知')}\n"
                        f"内容:\n{full_content}"
                    )
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(result_str)]))
                event.discard(force=True)
                event.stop()
                return

        # 4. 处理快捷创建指令 (/r add, /添加待办, 等)
        cmd_is_add = False
        payload = ""
        if text.startswith("/r add "):
            cmd_is_add = True
            payload = text[len("/r add "):].strip()
        elif text.startswith("/r a "):
            cmd_is_add = True
            payload = text[len("/r a "):].strip()
        elif text.startswith("/r + "):
            cmd_is_add = True
            payload = text[len("/r + "):].strip()
        elif text.startswith("/添加待办 ") or text.startswith("/待办 添加 "):
            cmd_is_add = True
            prefix = "/添加待办 " if text.startswith("/添加待办 ") else "/待办 添加 "
            payload = text[len(prefix):].strip()

        if cmd_is_add:
            parts = payload.split(" ", 2)
            if len(parts) >= 3:
                time_str = f"{parts[0]} {parts[1]}"
                content = parts[2]
                try:
                    if "-" in time_str and ":" in time_str:
                        res = await self.set_reminder(event, content=content, time=time_str)
                        await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(res)]))
                    else:
                        await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("时间格式需为 YYYY-MM-DD HH:MM")]))
                except Exception as e:
                    await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text(f"出错了: {e}")]))
            else:
                await self.ctx.message_processor.send_message_chain(session=sid, chain=MessageChain([Text("格式: /r add 2026-03-20 08:30 内容")]))
            
            event.discard(force=True)
            event.stop()
            return

    def _get_sid(self, event: KiraMessageBatchEvent) -> str:
        """获取会话 ID 并校验格式（adapter:type:id）"""
        sid = getattr(getattr(event, "session", None), "sid", None) or getattr(event, "sid", "default")
        if sid and sid != "default" and len(sid.split(":")) < 3:
            logger.warning(f"[Reminder] session_id 格式异常: {sid}")
        return sid

    def _cleanup_tokens(self):
        """清理过期令牌并限制最大缓存数量"""
        now = time.time()
        expired = [k for k, v in self._pending.items() if v["expires_at"] < now]
        for k in expired:
            del self._pending[k]
        # 超出上限时按过期时间淘汰最旧的
        if len(self._pending) > _MAX_PENDING_TOKENS:
            sorted_keys = sorted(
                self._pending, key=lambda k: self._pending[k]["expires_at"]
            )
            for k in sorted_keys[: len(self._pending) - _MAX_PENDING_TOKENS]:
                del self._pending[k]

    def _create_token(self, event, sid: str, job_ids: List[str], content: str) -> str:
        token = uuid.uuid4().hex[:12]
        self._pending[token] = {"session_id": sid, "job_ids": job_ids,
                                "content": content,
                                "actor_key": self._get_principal(event).actor_key,
                                "expires_at": time.time() + _CONFIRM_TTL}
        return token

    # ──────── 工具方法 ────────

    @register_tool(
        name="set_reminder",
        description=(
            "为当前发言人设置提醒。支持一次性、重复、间隔和随机时间提醒。"
            "time 必须为 'YYYY-MM-DD HH:MM' 格式。群聊创建会按插件权限策略校验，权限不足时会拒绝。"
            "不要替其他群成员创建提醒，除非当前发言人是管理员。action 会按独立 action_policy 校验。"
        ),
        params={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "提醒内容"},
                "time": {"type": "string", "description": "提醒时间，格式 YYYY-MM-DD HH:MM"},
                "repeat": {"type": "string",
                           "enum": ["none", "daily", "weekly", "monthly", "yearly", "interval"],
                           "description": "重复类型，默认 none"},
                "interval_minutes": {"type": "integer",
                                     "description": "间隔提醒的分钟数（repeat=interval 时必填）"},
                "category": {"type": "string", "description": "提醒分类（如工作/学习等）"},
                "action": {"type": "string", "description": "触发时期望执行的动作指令；高风险字段，受 action_policy 独立控制"},
                "time_range_end": {"type": "string",
                                   "description": "随机提醒结束时间，设置后在 time~time_range_end 内随机触发"},
                "random_count": {"type": "integer", "description": "随机提醒次数（固定值）"},
                "random_count_min": {"type": "integer", "description": "随机次数最小值"},
                "random_count_max": {"type": "integer", "description": "随机次数最大值"},
            },
            "required": ["content", "time"],
        }
    )
    async def set_reminder(self, event: KiraMessageBatchEvent, content: str, time: str,
                           repeat: str = "none", interval_minutes: Optional[int] = None,
                           category: Optional[str] = None, action: Optional[str] = None,
                           time_range_end: Optional[str] = None,
                           random_count: Optional[int] = None,
                           random_count_min: Optional[int] = None,
                           random_count_max: Optional[int] = None, **kwargs) -> str:
        try:
            allowed, reason = self._check_create_permission(event)
            if not allowed:
                return reason
            allowed, reason = self._check_action_permission(event, action)
            if not allowed:
                return reason
            if repeat not in ("none", "daily", "weekly", "monthly", "yearly", "interval"):
                return "❌ repeat 参数无效"
            if repeat == "interval":
                if not interval_minutes:
                    return "❌ 间隔提醒需要指定 interval_minutes"
                if not (1 <= interval_minutes <= 1440):
                    return "❌ 间隔时间必须在 1~1440 分钟之间"
            try:
                start_time = parse_time_string(time)
            except ValueError:
                return "❌ 时间格式错误，请使用 YYYY-MM-DD HH:MM 格式"
            sid = self._get_sid(event)
            batch_ts = get_local_now().strftime("%Y%m%d%H%M%S%f")
            creator_info = self._get_creator_info(event)
            identity_fields = self._identity_fields_for_event(event)
            sess_type = "gm" if ":gm:" in sid else "dm"
            
            # --- 时间前置校验（避免不必要的锁占用） ---
            if time_range_end:
                try:
                    end_time = parse_time_string(time_range_end)
                except ValueError:
                    return "❌ 结束时间格式错误"
                if end_time <= start_time:
                    return "❌ 结束时间必须晚于开始时间"
                final_count = determine_random_count(random_count, random_count_min, random_count_max)
                trigger_times = generate_multiple_random_times(start_time, end_time, final_count)
                trigger_times = [t for t in trigger_times if t > get_local_now()]
                if not trigger_times:
                    return "❌ 所有随机时间点都已过去"
            elif repeat == "none" and start_time < get_local_now():
                return "❌ 不能设置过去的时间"

            async with self._storage.modify() as data:
                data.setdefault(sid, [])
                
                if time_range_end:
                    for i, t in enumerate(trigger_times):
                        job_id = f"reminder_{sid}_{batch_ts}_{i}"
                        r: Dict[str, Any] = {
                            "content": content, "time": t.strftime("%Y-%m-%d %H:%M"),
                            "repeat": "none", "job_id": job_id,
                            "created_at": get_local_now().strftime("%Y-%m-%d %H:%M"),
                            "is_random": True, "random_batch_id": batch_ts,
                            "random_index": i + 1, "random_total": len(trigger_times),
                            "time_range": {"start": time, "end": time_range_end},
                            "creator_id": creator_info["creator_id"],
                            "creator_name": creator_info["creator_name"],
                            "session_type": sess_type
                        }
                        if category: r["category"] = category
                        if action: r["action"] = action
                        r.update(identity_fields)
                        data[sid].append(r)
                        self._add_job(sid, r)
                    times_str = "\n".join(f"  {i+1}. {t.strftime('%Y-%m-%d %H:%M')}"
                                           for i, t in enumerate(trigger_times))
                    return (f"已添加随机待办: {content} (共{len(trigger_times)}次)\n"
                            f"时间列表:\n{times_str}")
                            
                # --- 非随机多签分支 ---
                job_id = f"reminder_{sid}_{batch_ts}"
                r = {"content": content, "time": start_time.strftime("%Y-%m-%d %H:%M"),
                     "repeat": repeat, "job_id": job_id,
                     "created_at": get_local_now().strftime("%Y-%m-%d %H:%M"),
                     "creator_id": creator_info["creator_id"],
                     "creator_name": creator_info["creator_name"],
                     "session_type": sess_type}
                if repeat == "interval":
                    r["interval_minutes"] = interval_minutes
                if category: r["category"] = category
                if action: r["action"] = action
                r.update(identity_fields)
                data[sid].append(r)
                self._add_job(sid, r)
                
            repeat_text = {"none": "", "daily": " (每天)", "weekly": " (每周)",
                           "monthly": " (每月)", "yearly": " (每年)",
                           "interval": f" (每{interval_minutes}分钟)"}.get(repeat, "")
            return (f"已添加: {content}\n"
                    f"[{start_time.strftime('%Y-%m-%d %H:%M')}{repeat_text}]")
        except Exception as e:
            logger.error(f"[Reminder] 设置提醒失败: {e}")
            return f"设置出错: {e}"

    @register_tool(
        name="list_reminders",
        description="列出当前会话的所有提醒，包括 job_id、重要标记、重复类型等。删除/标记前必须先调用此工具。",
        params={"type": "object", "properties": {}, "required": []}
    )
    async def list_reminders(self, event: KiraMessageBatchEvent, **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            data = await self._storage.load()
            reminders = data.get(sid, [])
            is_admin_user = self._is_admin_user(event)
            principal = self._get_principal(event)
            filtered = [
                r for r in reminders
                if can_view_reminder(
                    principal,
                    r,
                    sid=sid,
                    admin_users=self.config.admin_users,
                )
            ]

            if not filtered:
                return "当前没有任何待办"
            lines = ["📋 可访问待办列表 (超管可透视)：\n"] if is_admin_user else ["📋 可访问待办列表：\n"]
            for r in filtered:
                i = reminders.index(r) + 1
                imp = " ⭐重要" if r.get("important") else ""
                paused = " ⏸️已暂停" if r.get("paused") else ""
                cat = f" [{r['category']}]" if r.get("category") else ""
                action = f"\n   🤖动作:{r['action']}" if r.get("action") else ""
                rep = {"none": "", "daily": " [每天]", "weekly": " [每周]",
                       "monthly": " [每月]", "yearly": " [每年]",
                       "interval": f" [每{r.get('interval_minutes',30)}分钟]"}.get(r.get("repeat","none"), "")
                rand = ""
                if r.get("is_random"):
                    tr = r.get("time_range", {})
                    idx, total = r.get("random_index"), r.get("random_total")
                    rand = (f"\n   范围: {tr.get('start','?')} ~ {tr.get('end','?')}"
                            f" [随机 {idx}/{total}]" if total else "")
                lines.append(f"{i}. {r['content']}{cat}{imp}{paused}\n   时间: {r['time']}{rep}{rand}{action}"
                             f"\n   job_id: {r.get('job_id','unknown')}"
                             f"\n   创建人: {r.get('creator_name','未知')}")
            self._cleanup_tokens()
            pending = sum(1 for v in self._pending.values() if v.get("session_id") == sid)
            if pending:
                lines.append(f"\n⏳ 有 {pending} 个重要提醒的删除请求等待确认")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 列出提醒失败: {e}"

    @register_tool(
        name="list_autonomous_intents",
        description="列出当前会话的自主意图状态。只读工具，用于查看 intent、状态、下次检查时间和关联提醒。",
        params={
            "type": "object",
            "properties": {
                "include_closed": {"type": "boolean", "description": "是否包含已关闭 intent，默认 false"},
            },
            "required": [],
        }
    )
    async def list_autonomous_intents(self, event: KiraMessageBatchEvent, include_closed: bool = False, **kwargs) -> str:
        allowed, reason, sid = self._check_autonomy_tool_access(event, operation="read")
        if not allowed:
            return reason
        state = await self._load_autonomy_state()
        session_state = self._ensure_autonomy_session(state, sid)
        intents = session_state.get("intents", [])
        if not include_closed:
            intents = [i for i in intents if i.get("status") != "closed"]
        if not intents:
            return "当前会话没有自主意图"
        lines = ["[自主意图列表]"]
        for i, intent in enumerate(intents, start=1):
            lines.append(
                "\n".join([
                    f"{i}. {intent.get('title', '未命名')}",
                    f"   id: {intent.get('id', '')}",
                    f"   status: {intent.get('status', 'active')}",
                    f"   priority: {intent.get('priority', 0.5)}",
                    f"   next_check_at: {intent.get('next_check_at', '') or '未安排'}",
                    f"   notes: {intent.get('notes', '') or '无'}",
                ])
            )
        return "\n".join(lines)

    @register_tool(
        name="create_autonomous_intent",
        description="为当前会话创建一个自主意图。只保存最小状态，不会自动设置提醒；如需后续检查，继续调用 schedule_intent_followup。",
        params={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "意图标题，简短描述要跟进的目标"},
                "notes": {"type": "string", "description": "简短依据或背景，不要写入长推理链"},
                "priority": {"type": "number", "description": "优先级 0~1，默认 0.5"},
            },
            "required": ["title"],
        }
    )
    async def create_autonomous_intent(
        self,
        event: KiraMessageBatchEvent,
        title: str,
        notes: str = "",
        priority: float = 0.5,
        **kwargs,
    ) -> str:
        allowed, reason, sid = self._check_autonomy_tool_access(event)
        if not allowed:
            return reason
        title = str(title or "").strip()
        if not title:
            return "❌ 意图标题不能为空"
        try:
            priority_value = max(0.0, min(1.0, float(priority)))
        except (TypeError, ValueError):
            priority_value = 0.5
        intent_id = f"intent_{get_local_now().strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:8]}"
        now = self._now_str()
        async with self._autonomy_storage.modify() as state:
            session_state = self._ensure_autonomy_session(state, sid)
            session_state["intents"].append({
                "id": intent_id,
                "title": title,
                "status": "active",
                "priority": priority_value,
                "source": "llm",
                "created_at": now,
                "updated_at": now,
                "next_check_at": "",
                "next_check_job_id": "",
                "notes": str(notes or "").strip(),
            })
        return f"已创建自主意图: {title}\nid: {intent_id}"

    @register_tool(
        name="update_autonomous_intent",
        description="更新当前会话的自主意图最小状态。不会自动改 reminder；需要改后续检查时间时调用 schedule_intent_followup。",
        params={
            "type": "object",
            "properties": {
                "intent_id": {"type": "string", "description": "要更新的 intent id"},
                "title": {"type": "string", "description": "新标题，可选"},
                "notes": {"type": "string", "description": "新简短依据，可选"},
                "status": {"type": "string", "enum": ["active", "paused", "waiting_confirmation", "closed"], "description": "新状态，可选"},
                "priority": {"type": "number", "description": "新优先级 0~1，可选"},
            },
            "required": ["intent_id"],
        }
    )
    async def update_autonomous_intent(
        self,
        event: KiraMessageBatchEvent,
        intent_id: str,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[float] = None,
        **kwargs,
    ) -> str:
        allowed, reason, sid = self._check_autonomy_tool_access(event)
        if not allowed:
            return reason
        async with self._autonomy_storage.modify() as state:
            session_state = self._ensure_autonomy_session(state, sid)
            intent = self._find_intent(session_state, intent_id)
            if not intent:
                return f"找不到自主意图: {intent_id}"
            if title is not None and str(title).strip():
                intent["title"] = str(title).strip()
            if notes is not None:
                intent["notes"] = str(notes or "").strip()
            if status is not None:
                status_value = str(status or "").strip()
                if status_value not in ("active", "paused", "waiting_confirmation", "closed"):
                    return "❌ status 参数无效"
                intent["status"] = status_value
            if priority is not None:
                try:
                    intent["priority"] = max(0.0, min(1.0, float(priority)))
                except (TypeError, ValueError):
                    return "❌ priority 需要是 0~1 的数字"
            intent["updated_at"] = self._now_str()
            return f"已更新自主意图: {intent.get('title', intent_id)}"

    @register_tool(
        name="close_autonomous_intent",
        description="关闭当前会话的自主意图，并可取消该 intent 关联的自主 reminder。只会处理 autonomous 来源的提醒。",
        params={
            "type": "object",
            "properties": {
                "intent_id": {"type": "string", "description": "要关闭的 intent id"},
                "cancel_followup": {"type": "boolean", "description": "是否取消关联的后续检查提醒，默认 true"},
            },
            "required": ["intent_id"],
        }
    )
    async def close_autonomous_intent(
        self,
        event: KiraMessageBatchEvent,
        intent_id: str,
        cancel_followup: bool = True,
        **kwargs,
    ) -> str:
        allowed, reason, sid = self._check_autonomy_tool_access(event)
        if not allowed:
            return reason
        async with self._autonomy_storage.modify() as state:
            session_state = self._ensure_autonomy_session(state, sid)
            intent = self._find_intent(session_state, intent_id)
            if not intent:
                return f"找不到自主意图: {intent_id}"
            intent["status"] = "closed"
            intent["closed_at"] = self._now_str()
            intent["updated_at"] = self._now_str()
            intent["next_check_at"] = ""
            intent["next_check_job_id"] = ""
        removed = 0
        if cancel_followup:
            removed = await self._remove_autonomous_reminders(sid, intent_id=intent_id)
        suffix = f"，已取消 {removed} 个后续检查提醒" if removed else ""
        return f"已关闭自主意图: {intent_id}{suffix}"

    @register_tool(
        name="schedule_intent_followup",
        description=(
            "为当前会话的自主意图安排下一次后续检查。该工具会复用 reminder 调度，"
            "并自动写入 source=autonomous_intent_loop、intent_id、managed_by，避免误改用户普通提醒。"
        ),
        params={
            "type": "object",
            "properties": {
                "intent_id": {"type": "string", "description": "要安排跟进的 intent id"},
                "time": {"type": "string", "description": "跟进时间，格式 YYYY-MM-DD HH:MM"},
                "content": {"type": "string", "description": "到点给自主循环看的简短跟进内容，可选"},
                "replace_existing": {"type": "boolean", "description": "是否替换同 intent 的旧后续检查，默认 true"},
            },
            "required": ["intent_id", "time"],
        }
    )
    async def schedule_intent_followup(
        self,
        event: KiraMessageBatchEvent,
        intent_id: str,
        time: str,
        content: str = "",
        replace_existing: bool = True,
        **kwargs,
    ) -> str:
        allowed, reason, sid = self._check_autonomy_tool_access(event)
        if not allowed:
            return reason
        try:
            trigger_time = parse_time_string(str(time or "").strip())
        except ValueError:
            return "❌ 时间格式错误，请使用 YYYY-MM-DD HH:MM"
        if trigger_time <= get_local_now():
            return "❌ 不能设置过去的跟进时间"

        state = await self._load_autonomy_state()
        session_state = self._ensure_autonomy_session(state, sid)
        intent = self._find_intent(session_state, intent_id)
        if not intent:
            return f"找不到自主意图: {intent_id}"
        if intent.get("status") == "closed":
            return "❌ 不能为已关闭意图安排跟进"

        if replace_existing:
            await self._remove_autonomous_reminders(sid, intent_id=intent_id)

        batch_ts = get_local_now().strftime("%Y%m%d%H%M%S%f")
        job_id = f"autonomous_{sid}_{intent_id}_{batch_ts}"
        followup_content = str(content or "").strip() or f"检查自主意图进展: {intent.get('title', intent_id)}"
        principal = self._get_principal(event)
        adapter_name = sid.split(":", 1)[0] if ":" in sid else "unknown"
        creator_id = build_bot_principal_id(adapter_name, principal.bot_id or "unknown")
        reminder = {
            "content": followup_content,
            "time": trigger_time.strftime("%Y-%m-%d %H:%M"),
            "repeat": "none",
            "job_id": job_id,
            "created_at": self._now_str(),
            "creator_id": creator_id,
            "creator_name": "自主意图循环",
            "session_type": "gm" if ":gm:" in sid else "dm",
            "category": "自主规划",
            "source": AUTONOMOUS_SOURCE,
            "intent_id": intent_id,
            "intent_title": intent.get("title", ""),
            "intent_notes": intent.get("notes", ""),
            "identity_schema": 2,
            "owner_type": PrincipalKind.BOT.value,
            "owner_id": creator_id,
            "owner_name": "自主意图循环",
            "created_by_type": principal.kind.value,
            "created_by_id": principal.principal_id,
            "origin": principal.origin.value,
            "managed_by": AUTONOMOUS_MANAGER,
            "visibility": "session_readonly",
            "visible_output_policy": self.config.visible_output_policy,
        }

        async with self._storage.modify() as data:
            data.setdefault(sid, [])
            data[sid].append(reminder)
            self._add_job(sid, reminder)

        async with self._autonomy_storage.modify() as state_to_save:
            session_state = self._ensure_autonomy_session(state_to_save, sid)
            intent_to_save = self._find_intent(session_state, intent_id)
            if intent_to_save:
                intent_to_save["next_check_at"] = reminder["time"]
                intent_to_save["next_check_job_id"] = job_id
                intent_to_save["followup_content"] = followup_content
                intent_to_save["updated_at"] = self._now_str()

        return (
            f"已安排自主跟进: {intent.get('title', intent_id)}\n"
            f"时间: {reminder['time']}\njob_id: {job_id}"
        )

    @register_tool(
        name="delete_reminder",
        description="根据 job_id 删除提醒。重要提醒需二次确认。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "要删除的提醒 ID"},
                "delete_batch": {"type": "boolean", "description": "是否删除整个随机批次，默认 false"},
            },
            "required": ["job_id"],
        }
    )
    async def delete_reminder(self, event: KiraMessageBatchEvent, job_id: str = "",
                              delete_batch: bool = False, **kwargs) -> str:
        try:
            self._cleanup_tokens()
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                if not reminders:
                    return "当前没有待办"
                reminder = next((r for r in reminders if r.get("job_id") == job_id), None)
                if reminder is None:
                    return f"找不到任务: {job_id}"
                    
                if not self._check_permission(event, reminder, ReminderOperation.DELETE):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {reminder.get('creator_name', '未知')})"
                    
                batch_id = reminder.get("random_batch_id")
                targets = ([r for r in reminders if r.get("random_batch_id") == batch_id]
                           if delete_batch and batch_id else [reminder])
                if any(
                    not self._check_permission(event, target, ReminderOperation.DELETE)
                    for target in targets
                ):
                    return "❌ 权限拒绝：批次中包含当前主体无权删除的任务。"
                if any(r.get("important") for r in targets):
                    job_ids = [r["job_id"] for r in targets if "job_id" in r]
                    token = self._create_token(event, sid, job_ids, reminder["content"])
                    return (f"注意: 「{reminder['content']}」是重要提醒\n"
                            f"请确认删除令牌: {token}")
                            
                # 直接删除
                for r in targets:
                    if "job_id" in r:
                        try:
                            if self._scheduler: self._scheduler.remove_job(r["job_id"])
                        except Exception:
                            pass
                            
                if delete_batch and batch_id:
                    data[sid] = [r for r in reminders if r.get("random_batch_id") != batch_id]
                    return f"已批量删除: {reminder['content']} (共{len(targets)}项)"
                
                data[sid] = [r for r in reminders if r.get("job_id") != job_id]
                return f"已删除: {reminder['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="confirm_delete_reminder",
        description="用户明确同意后，使用 delete_reminder 返回的 confirm_token 完成重要提醒删除。",
        params={
            "type": "object",
            "properties": {
                "confirm_token": {"type": "string", "description": "delete_reminder 返回的确认令牌"},
            },
            "required": ["confirm_token"],
        }
    )
    async def confirm_delete_reminder(self, event: KiraMessageBatchEvent,
                                      confirm_token: str = "", **kwargs) -> str:
        try:
            self._cleanup_tokens()
            pending = self._pending.get(confirm_token)
            if not pending:
                return "令牌无效或已过期"
            principal = self._get_principal(event)
            if pending.get("actor_key") != principal.actor_key and not self._is_admin_user(event):
                return "❌ 权限拒绝：确认令牌不属于当前主体。"
            sid, job_ids, content = pending["session_id"], pending["job_ids"], pending["content"]
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                targets = [r for r in reminders if r.get("job_id") in job_ids]
                if any(
                    not can_manage_reminder(
                        principal,
                        reminder,
                        operation=ReminderOperation.DELETE,
                        sid=sid,
                        admin_users=self.config.admin_users,
                    )
                    for reminder in targets
                ):
                    return "❌ 权限拒绝：当前主体无权删除目标提醒。"
                before = len(reminders)
                data[sid] = [r for r in reminders if r.get("job_id") not in job_ids]
                deleted = before - len(data[sid])
                for jid in job_ids:
                    try:
                        if self._scheduler: self._scheduler.remove_job(jid)
                    except Exception:
                        pass
                del self._pending[confirm_token]
                suffix = f" (共{deleted}项)" if deleted > 1 else ""
                return f"已删除: {content}{suffix}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="mark_reminder_important",
        description="将指定 job_id 的提醒标记为重要，删除时需二次确认。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "提醒 ID"}},
            "required": ["job_id"],
        }
    )
    async def mark_reminder_important(self, event: KiraMessageBatchEvent,
                                      job_id: str = "", **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r, ReminderOperation.MARK_IMPORTANT):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if r.get("important"):
                    return f"已设为重要: {r['content']}"
                r["important"] = True
                return f"设为重要: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="unmark_reminder_important",
        description="取消指定 job_id 提醒的重要标记。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "提醒 ID"}},
            "required": ["job_id"],
        }
    )
    async def unmark_reminder_important(self, event: KiraMessageBatchEvent,
                                        job_id: str = "", **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r, ReminderOperation.MARK_IMPORTANT):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if not r.get("important"):
                    return f"并非重要提醒: {r['content']}"
                r["important"] = False
                return f"取消重要标记: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="pause_reminder",
        description="暂停提醒，暂停后的提醒不会触发，但可以随时恢复。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "要暂停的提醒 ID"}},
            "required": ["job_id"],
        }
    )
    async def pause_reminder(self, event: KiraMessageBatchEvent, job_id: str = "", **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r, ReminderOperation.PAUSE):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if r.get("paused"):
                    return f"已经暂停: {r['content']}"
                r["paused"] = True
                try:
                    if self._scheduler: self._scheduler.remove_job(job_id)
                except Exception:
                    pass
                return f"已暂停: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="resume_reminder",
        description="恢复被暂停的提醒。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "要恢复的提醒 ID"}},
            "required": ["job_id"],
        }
    )
    async def resume_reminder(self, event: KiraMessageBatchEvent, job_id: str = "", **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r, ReminderOperation.RESUME):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if not r.get("paused"):
                    return f"已经处于活动状态: {r['content']}"
                
                # 检查是否尝试恢复已经过期的一次性任务
                if r.get("repeat", "none") == "none":
                    trigger_time = parse_time_string(r["time"])
                    if trigger_time <= get_local_now():
                        return f"已过期，无法恢复: {r['content']}"
                
                r["paused"] = False
                self._add_job(sid, r)
                return f"已恢复: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    @register_tool(
        name="edit_reminder",
        description="修改已设置的提醒。仅需提供想修改的字段，未提供的字段保持原样。必须先用 list_reminders 获取 job_id。",
        params={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "要修改的提醒 ID"},
                "content": {"type": "string", "description": "新提醒内容（可选）"},
                "time": {"type": "string", "description": "新提醒时间，格式 YYYY-MM-DD HH:MM（可选）"},
                "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "monthly", "yearly", "interval"], "description": "新重复类型（可选）"},
                "interval_minutes": {"type": "integer", "description": "新间隔分钟数（可选）"},
                "category": {"type": "string", "description": "新提醒分类（可选）"},
                "action": {"type": "string", "description": "新自动动作指令（可选）；高风险字段，受 action_policy 独立控制"},
            },
            "required": ["job_id"],
        }
    )
    async def edit_reminder(self, event: KiraMessageBatchEvent, job_id: str = "",
                            content: Optional[str] = None, time: Optional[str] = None,
                            repeat: Optional[str] = None, interval_minutes: Optional[int] = None,
                            category: Optional[str] = None, action: Optional[str] = None, **kwargs) -> str:
        try:
            sid = self._get_sid(event)
            
            # 提前校验时间以免持有锁时做无用功
            new_time_str = time
            new_start_time = None
            if new_time_str is not None:
                try:
                    new_start_time = parse_time_string(new_time_str)
                except ValueError:
                    return "时间格式需为 YYYY-MM-DD HH:MM"

            async with self._storage.modify() as data:
                reminders = data.get(sid, [])
                r_index = next((i for i, rv in enumerate(reminders) if rv.get("job_id") == job_id), -1)
                if r_index == -1:
                    return f"找不到任务: {job_id}"
                
                r = reminders[r_index]
                if not self._check_permission(event, r, ReminderOperation.EDIT):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                allowed, reason = self._check_action_permission(event, action)
                if not allowed:
                    return reason
                    
                if r.get("is_random"):
                    return "不支持修改随机批次提醒"
                    
                new_repeat = repeat if repeat is not None else r.get("repeat", "none")
                if new_repeat not in ("none", "daily", "weekly", "monthly", "yearly", "interval"):
                    return "重复参数无效"
                
                if new_repeat == "interval":
                    new_interval = interval_minutes if interval_minutes is not None else r.get("interval_minutes")
                    if not new_interval:
                        return "缺少间隔时间参数"
                    if not (1 <= new_interval <= 1440):
                        return "间隔时间应在 1~1440 分钟"
                    r["interval_minutes"] = new_interval
                
                if new_time_str is None:
                    new_time_str = r.get("time")
                    new_start_time = parse_time_string(new_time_str)
                    
                if new_repeat == "none" and new_start_time < get_local_now() and time is not None:
                    return "无法设置过去的时间"

                if content is not None: r["content"] = content
                if time is not None: r["time"] = new_time_str
                if repeat is not None: r["repeat"] = new_repeat
                if category is not None: r["category"] = category
                if action is not None: r["action"] = action
                
                # 先移除旧任务
                try:
                    if self._scheduler: self._scheduler.remove_job(job_id)
                except Exception:
                    pass
                    
                # 非暂停状态则重新加入调度器
                if not r.get("paused"):
                    self._add_job(sid, r)
                    
                return f"已更新: {r['content']}\n[{r['time']}]"
        except Exception as e:
            logger.error(f"[Reminder] 编辑提醒失败: {e}")
            return f"修改出错: {e}"
