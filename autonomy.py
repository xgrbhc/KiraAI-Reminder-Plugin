"""Pure state and scheduling helpers for autonomous intent checks."""

from __future__ import annotations

import datetime
import random
import uuid
from collections.abc import Callable
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.chat.message_elements import Text
from core.chat.message_utils import MessageChain
from core.plugin import logger

from .config import (
    AUTONOMOUS_CHECK_INTERVAL_MINUTES,
    AUTONOMOUS_DAILY_JOB_ID,
    AUTONOMOUS_FOLLOWUP_JOB_ID,
    AUTONOMOUS_MANAGER,
    AUTONOMOUS_RANDOM_END_HOUR,
    AUTONOMOUS_RANDOM_JOB_ID,
    AUTONOMOUS_RANDOM_PLAN_JOB_ID,
    AUTONOMOUS_RANDOM_START_HOUR,
    AUTONOMOUS_SOURCE,
)
from .identity import (
    IDENTITY_SCHEMA_VERSION, EventOrigin, PrincipalContext, PrincipalKind,
    adapter_from_session_id, build_bot_principal_id,
)
from .job_sync import reminder_job_commit
from .storage import ReminderStorage
from .time_utils import get_local_now, parse_time_string


def now_str() -> str:
    return get_local_now().strftime("%Y-%m-%d %H:%M")


def ensure_autonomy_root(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state.get("sessions"), dict):
        state["sessions"] = {}
    return state


def ensure_autonomy_session(state: dict[str, Any], sid: str) -> dict[str, Any]:
    ensure_autonomy_root(state)
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


def find_intent(session_state: dict[str, Any], intent_id: str) -> dict[str, Any] | None:
    for intent in session_state.get("intents", []):
        if str(intent.get("id")) == str(intent_id):
            return intent
    return None


def is_autonomous_reminder(reminder: dict[str, Any]) -> bool:
    return (
        reminder.get("source") == AUTONOMOUS_SOURCE
        or reminder.get("managed_by") == AUTONOMOUS_MANAGER
    )


def parse_optional_time(value: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return parse_time_string(value)
    except ValueError:
        return None


def autonomous_reminder_exists(
    reminders_data: dict[str, list[dict]], sid: str, job_id: str
) -> bool:
    if not job_id:
        return False
    for reminder in reminders_data.get(sid, []):
        if reminder.get("job_id") == job_id and is_autonomous_reminder(reminder):
            return True
    return False


def random_check_window(config: Any) -> tuple[int, int]:
    start_hour = config.random_check_start_hour
    end_hour = config.random_check_end_hour
    if end_hour <= start_hour:
        return AUTONOMOUS_RANDOM_START_HOUR, AUTONOMOUS_RANDOM_END_HOUR
    return start_hour, end_hour


def generate_random_check_times(config: Any, now: datetime.datetime) -> list[str]:
    count = config.random_check_daily_count
    if count <= 0:
        return []
    start_hour, end_hour = random_check_window(config)
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


def random_job_id(sid: str, time_str: str) -> str:
    safe_sid = sid.replace(":", "_")
    safe_time = time_str.replace("-", "").replace(" ", "_").replace(":", "")
    return f"{AUTONOMOUS_RANDOM_JOB_ID}_{safe_sid}_{safe_time}"


class AutonomyCoordinator:
    """Own persisted intent state and schedule autonomous check jobs."""

    def __init__(
        self,
        *,
        config: Any,
        storage: ReminderStorage,
        autonomy_storage: ReminderStorage,
        get_scheduler: Callable[[], Any],
        publish_immediate_notice: Callable[..., Any],
        publish_autonomous_notice: Callable[..., Any],
        get_autonomous_usage_prompt: Callable[[], str],
        daily_cycle_callback: Callable[..., Any],
        followup_due_callback: Callable[..., Any],
        random_daily_plan_callback: Callable[..., Any],
        random_check_callback: Callable[..., Any],
        add_reminder_job: Callable[..., Any],
    ) -> None:
        self.config = config
        self._storage = storage
        self._autonomy_storage = autonomy_storage
        self._get_scheduler = get_scheduler
        self._publish_immediate_notice = publish_immediate_notice
        self._publish_autonomous_notice = publish_autonomous_notice
        self._get_autonomous_usage_prompt = get_autonomous_usage_prompt
        self._daily_cycle_callback = daily_cycle_callback
        self._followup_due_callback = followup_due_callback
        self._random_daily_plan_callback = random_daily_plan_callback
        self._random_check_callback = random_check_callback
        self._add_reminder_job = add_reminder_job

    def enabled(self) -> bool:
        return bool(self.config.autonomy_enabled and self.config.autonomy_mode != "off")

    def allowed_sessions(self) -> list[str]:
        if not self.enabled():
            return []
        return [str(s).strip() for s in self.config.allowed_sessions if str(s).strip()]

    async def load_state(self) -> dict[str, Any]:
        state = await self._autonomy_storage.load()
        if not isinstance(state, dict):
            state = {}
        return ensure_autonomy_root(state)

    async def start_jobs(self) -> None:
        scheduler = self._get_scheduler()
        if not scheduler or not self.enabled():
            return

        allowed_sessions = self.allowed_sessions()
        if not allowed_sessions:
            logger.info("[Reminder][Autonomous] 未配置 allowed_sessions，跳过自主循环调度")
            return

        if self.config.daily_reflection_enabled:
            scheduler.add_job(
                self._daily_cycle_callback,
                trigger=CronTrigger(hour=self.config.daily_reflection_hour, minute=0),
                id=AUTONOMOUS_DAILY_JOB_ID,
                replace_existing=True,
                misfire_grace_time=600,
            )
            logger.info(
                f"[Reminder][Autonomous] 每日自检已设置: {self.config.daily_reflection_hour}:00"
            )

        if self.config.followup_due_enabled and self.config.autonomy_mode != "observe":
            scheduler.add_job(
                self._followup_due_callback,
                trigger=IntervalTrigger(minutes=AUTONOMOUS_CHECK_INTERVAL_MINUTES),
                id=AUTONOMOUS_FOLLOWUP_JOB_ID,
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info("[Reminder][Autonomous] 到期跟进兜底检查已启动")

        if self.config.random_check_enabled:
            scheduler.add_job(
                self._random_daily_plan_callback,
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
            await self.schedule_random_checks(force_new=False)

    def build_notice_text(
        self,
        sid: str,
        trigger_type: str,
        intent: dict[str, Any] | None = None,
        content: str = "",
    ) -> str:
        lines = [
            "[系统事件: autonomous_intent_loop]",
            f"会话: {sid}",
            f"触发类型: {trigger_type}",
            f"自主模式: {self.config.autonomy_mode}",
            f"可见输出策略: {self.config.visible_output_policy}",
            f"当前时间: {now_str()}",
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

    async def publish_notice(
        self,
        sid: str,
        trigger_type: str,
        intent: dict[str, Any] | None = None,
        content: str = "",
    ) -> None:
        text = self.build_notice_text(sid, trigger_type, intent, content)
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

    async def daily_cycle_job(self) -> None:
        if not (self.enabled() and self.config.daily_reflection_enabled):
            return

        initial_state = await self.load_state()
        for sid in self.allowed_sessions():
            state = await self.load_state()
            allow_create = sid not in initial_state["sessions"] and sid not in state["sessions"]
            session_state = self._enabled_session(state, sid, allow_create=allow_create)
            if session_state is None:
                continue
            try:
                await self._publish_autonomous_notice(sid, "daily_reflection")
            except Exception as e:
                logger.warning(f"[Reminder][Autonomous] 每日自检触发失败 sid={sid}: {e}")
                continue
            await self._record_session_check(sid, {"last_cycle_at": now_str()}, allow_create=allow_create)

    @staticmethod
    def _enabled_session(
        state: dict[str, Any], sid: str, *, allow_create: bool = False,
    ) -> dict[str, Any] | None:
        """Keep removed or disabled sessions out of a late result commit."""
        sessions = ensure_autonomy_root(state)["sessions"]
        if sid not in sessions and not allow_create:
            return None
        session_state = ensure_autonomy_session(state, sid)
        return session_state if session_state.get("enabled", True) else None

    async def _record_session_check(
        self, sid: str, fields: dict[str, Any], *, allow_create: bool,
    ) -> None:
        """Merge successful check metadata without awaiting publication under a lock."""
        async with self._autonomy_storage.modify() as state:
            session_state = self._enabled_session(state, sid, allow_create=allow_create)
            if session_state is not None:
                session_state.update(fields)

    @staticmethod
    def _matching_followup(
        session_state: dict[str, Any] | None, intent_id: str, schedule: tuple[Any, Any],
    ) -> dict[str, Any] | None:
        """Match the pending occurrence, not just its stable intent ID."""
        if session_state is None:
            return None
        intent = find_intent(session_state, intent_id)
        if intent is None or intent.get("status", "active") not in ("active", "waiting_confirmation"):
            return None
        current_schedule = (intent.get("next_check_at", ""), intent.get("next_check_job_id", ""))
        return intent if current_schedule == schedule else None

    async def followup_due_job(self) -> None:
        if not (
            self.enabled()
            and self.config.followup_due_enabled
            and self.config.autonomy_mode != "observe"
        ):
            return

        state = await self.load_state()
        now = get_local_now()

        for sid in self.allowed_sessions():
            session_state = ensure_autonomy_session(state, sid)
            if not session_state.get("enabled", True):
                continue
            for intent in session_state.get("intents", []):
                if intent.get("status", "active") not in ("active", "waiting_confirmation"):
                    continue
                next_check_at = parse_optional_time(str(intent.get("next_check_at", "")))
                if not next_check_at or next_check_at > now:
                    continue
                intent_id = str(intent.get("id", ""))
                schedule = (intent.get("next_check_at", ""), intent.get("next_check_job_id", ""))
                reminders_data = await self._storage.load()
                current_state = await self.load_state()
                current_intent = self._matching_followup(
                    self._enabled_session(current_state, sid), intent_id, schedule,
                )
                if current_intent is None:
                    continue
                job_id = str(current_intent.get("next_check_job_id", ""))
                if autonomous_reminder_exists(reminders_data, sid, job_id):
                    continue
                try:
                    await self._publish_autonomous_notice(
                        sid,
                        "followup_due",
                        intent=current_intent,
                        content=str(current_intent.get("followup_content", "")),
                    )
                except Exception as e:
                    logger.warning(
                        f"[Reminder][Autonomous] 到期跟进触发失败 sid={sid} intent={intent.get('id')}: {e}"
                    )
                    continue
                async with self._autonomy_storage.modify() as current_state:
                    current_intent = self._matching_followup(
                        self._enabled_session(current_state, sid), intent_id, schedule,
                    )
                    if current_intent is not None:
                        completed_at = now_str()
                        current_intent.update({
                            "last_followup_at": completed_at,
                            "last_followup_source": "fallback_due_job",
                            "next_check_at": "",
                            "next_check_job_id": "",
                            "updated_at": completed_at,
                        })

    async def schedule_random_checks(self, force_new: bool = False) -> None:
        scheduler = self._get_scheduler()
        if not (scheduler and self.enabled() and self.config.random_check_enabled):
            return
        if self.config.random_check_daily_count <= 0:
            return

        async with self._autonomy_storage.modify() as state:
            now = get_local_now()
            today = now.strftime("%Y-%m-%d")
            start_hour, end_hour = random_check_window(self.config)
            window_key = f"{start_hour}-{end_hour}"

            for sid in self.allowed_sessions():
                session_state = ensure_autonomy_session(state, sid)
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
                    current_times = generate_random_check_times(self.config, now)
                    session_state["random_check_plan_date"] = today
                    session_state["random_check_window"] = window_key
                    session_state["random_check_times"] = current_times

                registered = 0
                for time_str in current_times:
                    run_at = parse_optional_time(str(time_str))
                    if not run_at or run_at <= now:
                        continue
                    scheduler.add_job(
                        self._random_check_callback,
                        trigger=DateTrigger(run_date=run_at),
                        id=random_job_id(sid, time_str),
                        kwargs={"sid": sid, "scheduled_time": time_str},
                        replace_existing=True,
                        misfire_grace_time=600,
                    )
                    registered += 1
                logger.info(
                    f"[Reminder][Autonomous] 随机自检计划 sid={sid}, "
                    f"date={today}, times={current_times}, registered={registered}"
                )

    async def random_daily_plan_job(self) -> None:
        await self.schedule_random_checks(force_new=True)

    async def random_check_job(self, sid: str, scheduled_time: str = "") -> None:
        if not (self.enabled() and self.config.random_check_enabled):
            return

        if sid not in self.allowed_sessions():
            return
        state = await self.load_state()
        allow_create = sid not in state["sessions"]
        session_state = self._enabled_session(state, sid, allow_create=allow_create)
        if session_state is None:
            return
        try:
            await self._publish_autonomous_notice(sid, "random_check")
        except Exception as e:
            logger.warning(f"[Reminder][Autonomous] 随机自检触发失败 sid={sid}: {e}")
            return
        await self._record_session_check(sid, {
            "last_random_check_at": now_str(),
            "last_random_check_scheduled_at": str(scheduled_time or ""),
        }, allow_create=allow_create)

    async def mark_followup_fired(self, sid: str, reminder: dict[str, Any]) -> None:
        intent_id = str(reminder.get("intent_id") or "")
        if not intent_id:
            return
        async with self._autonomy_storage.modify() as state:
            session_state = ensure_autonomy_session(state, sid)
            intent = find_intent(session_state, intent_id)
            if not intent:
                return
            intent["last_followup_at"] = now_str()
            intent["last_followup_job_id"] = reminder.get("job_id", "")
            if intent.get("next_check_job_id") == reminder.get("job_id"):
                intent["next_check_at"] = ""
                intent["next_check_job_id"] = ""
            intent["updated_at"] = now_str()

    async def remove_autonomous_reminders(
        self,
        sid: str,
        intent_id: str | None = None,
        job_id: str | None = None,
    ) -> int:
        removed = 0
        async with self._storage.modify(after_save=self._job_commit(sid)) as data:
            reminders = data.get(sid, [])
            if any(is_autonomous_reminder(reminder) and reminder.get("important")
                   and ((intent_id and reminder.get("intent_id") == intent_id)
                        or (job_id and reminder.get("job_id") == job_id)) for reminder in reminders):
                raise ValueError("关联提醒已标记重要，请先确认删除或取消重要标记")
            kept = []
            for reminder in reminders:
                matches_intent = intent_id and reminder.get("intent_id") == intent_id
                matches_job = job_id and reminder.get("job_id") == job_id
                if is_autonomous_reminder(reminder) and (matches_intent or matches_job):
                    removed += 1
                    continue
                kept.append(reminder)
            data[sid] = kept
        return removed

    def _job_commit(self, sid: str) -> Callable[[dict, dict], None]:
        def remove_job(job_id: str) -> None:
            scheduler = self._get_scheduler()
            if scheduler is not None:
                scheduler.remove_job(job_id)

        return reminder_job_commit(
            sid, add_job=self._add_reminder_job, remove_job=remove_job,
            get_scheduler=self._get_scheduler,
        )

    async def list_intents(self, sid: str, include_closed: bool = False) -> str:
        state = await self.load_state()
        session_state = ensure_autonomy_session(state, sid)
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

    async def create_intent(
        self, sid: str, title: str, notes: str = "", priority: float = 0.5
    ) -> str:
        title = str(title or "").strip()
        if not title:
            return "❌ 意图标题不能为空"
        try:
            priority_value = max(0.0, min(1.0, float(priority)))
        except (TypeError, ValueError):
            priority_value = 0.5
        intent_id = f"intent_{get_local_now().strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:8]}"
        now = now_str()
        async with self._autonomy_storage.modify() as state:
            session_state = ensure_autonomy_session(state, sid)
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

    async def update_intent(
        self,
        sid: str,
        intent_id: str,
        title: str | None = None,
        notes: str | None = None,
        status: str | None = None,
        priority: float | None = None,
    ) -> str:
        changes = {}
        if title is not None and str(title).strip():
            changes["title"] = str(title).strip()
        if notes is not None:
            changes["notes"] = str(notes or "").strip()
        if status is not None:
            status_value = str(status or "").strip()
            if status_value not in ("active", "paused", "waiting_confirmation", "closed"):
                return "❌ status 参数无效"
            changes["status"] = status_value
        if priority is not None:
            try:
                changes["priority"] = max(0.0, min(1.0, float(priority)))
            except (TypeError, ValueError):
                return "❌ priority 需要是 0~1 的数字"

        async with self._autonomy_storage.modify() as state:
            session_state = ensure_autonomy_session(state, sid)
            intent = find_intent(session_state, intent_id)
            if not intent:
                return f"找不到自主意图: {intent_id}"
            changes["updated_at"] = now_str()
            intent.update(changes)
            return f"已更新自主意图: {intent.get('title', intent_id)}"

    async def close_intent(
        self, sid: str, intent_id: str, cancel_followup: bool = True
    ) -> str:
        removed = 0
        async with self._autonomy_storage.modify() as state:
            session_state = ensure_autonomy_session(state, sid)
            intent = find_intent(session_state, intent_id)
            if not intent:
                return f"找不到自主意图: {intent_id}"
            if cancel_followup:
                try:
                    removed = await self.remove_autonomous_reminders(sid, intent_id=intent_id)
                except ValueError as error:
                    return f"❌ {error}"
            intent["status"] = "closed"
            intent["closed_at"] = now_str()
            intent["updated_at"] = now_str()
            intent["next_check_at"] = ""
            intent["next_check_job_id"] = ""
        suffix = f"，已取消 {removed} 个后续检查提醒" if removed else ""
        return f"已关闭自主意图: {intent_id}{suffix}"

    async def schedule_intent_followup(
        self,
        sid: str,
        principal: PrincipalContext,
        intent_id: str,
        time: str,
        content: str = "",
        replace_existing: bool = True,
    ) -> str:
        try:
            trigger_time = parse_time_string(str(time or "").strip())
        except ValueError:
            return "❌ 时间格式错误，请使用 YYYY-MM-DD HH:MM"
        if trigger_time <= get_local_now():
            return "❌ 不能设置过去的跟进时间"

        state = await self.load_state()
        session_state = ensure_autonomy_session(state, sid)
        intent = find_intent(session_state, intent_id)
        if not intent:
            return f"找不到自主意图: {intent_id}"
        if intent.get("status") == "closed":
            return "❌ 不能为已关闭意图安排跟进"

        batch_ts = get_local_now().strftime("%Y%m%d%H%M%S%f")
        job_id = f"autonomous_{sid}_{intent_id}_{batch_ts}"
        followup_content = str(content or "").strip() or f"检查自主意图进展: {intent.get('title', intent_id)}"
        adapter_name = adapter_from_session_id(sid)
        creator_id = build_bot_principal_id(adapter_name, principal.bot_id or "unknown")
        reminder = {
            "content": followup_content,
            "time": trigger_time.strftime("%Y-%m-%d %H:%M"),
            "repeat": "none",
            "job_id": job_id,
            "created_at": now_str(),
            "creator_id": creator_id,
            "creator_name": "自主意图循环",
            "session_type": "gm" if ":gm:" in sid else "dm",
            "category": "自主规划",
            "source": AUTONOMOUS_SOURCE,
            "intent_id": intent_id,
            "intent_title": intent.get("title", ""),
            "intent_notes": intent.get("notes", ""),
            "identity_schema": IDENTITY_SCHEMA_VERSION,
            "owner_type": PrincipalKind.BOT.value,
            "owner_id": creator_id,
            "owner_adapter_name": adapter_name,
            "owner_name": "自主意图循环",
            "created_by_type": principal.kind.value,
            "created_by_id": principal.principal_id,
            "created_by_adapter_name": principal.adapter_scope,
            "origin": principal.origin.value,
            "managed_by": AUTONOMOUS_MANAGER,
            "visibility": "session_readonly",
            "visible_output_policy": self.config.visible_output_policy,
        }

        try:
            async with self._storage.modify(after_save=self._job_commit(sid)) as data:
                reminders = data.setdefault(sid, [])
                targets = [r for r in reminders if replace_existing
                           and is_autonomous_reminder(r) and r.get("intent_id") == intent_id]
                if any(r.get("important") for r in targets):
                    return "❌ 关联提醒已标记重要，请先确认删除或取消重要标记"
                data[sid] = [r for r in reminders if r not in targets] + [reminder]
        except Exception as error:
            logger.error(f"[Reminder] Follow-up scheduling failed: {error}")
            return f"❌ 安排自主跟进失败: {error}"

        async with self._autonomy_storage.modify() as state_to_save:
            session_state = ensure_autonomy_session(state_to_save, sid)
            intent_to_save = find_intent(session_state, intent_id)
            if intent_to_save:
                intent_to_save["next_check_at"] = reminder["time"]
                intent_to_save["next_check_job_id"] = job_id
                intent_to_save["followup_content"] = followup_content
                intent_to_save["updated_at"] = now_str()

        return (
            f"已安排自主跟进: {intent.get('title', intent_id)}\n"
            f"时间: {reminder['time']}\njob_id: {job_id}"
        )
