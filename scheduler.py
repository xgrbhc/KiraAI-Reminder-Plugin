"""APScheduler job registration and restoration for persisted reminders."""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Callable
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.plugin import logger
from core.chat.message_elements import Text
from core.chat.message_utils import MessageChain

from .identity import EventOrigin, PrincipalKind
from .delivery import DeliveryTracker
from .storage import ReminderStorage
from .time_utils import get_local_now, parse_time_string

_HEALTH_CHECK_INTERVAL = 60
_FIRE_MAX_RETRIES = 3
_FIRE_RETRY_DELAY = 5


def reminder_schedule_info(scheduler: Any, reminder: dict) -> dict:
    """Derive live schedule and overdue display flags without changing storage."""
    if reminder.get("repeat", "none") not in {"daily", "weekly", "monthly", "yearly", "interval"}:
        if reminder.get("repeat", "none") == "none":
            try:
                if parse_time_string(reminder["time"]) <= get_local_now():
                    return {"is_overdue_once": True}
            except (KeyError, TypeError, ValueError):
                pass
        return {}
    details = {"next_run_time": None, "schedule_status": "unknown"}
    if reminder.get("paused"):
        details["schedule_status"] = "paused"
        return details
    try:
        if (scheduler is None or not getattr(scheduler, "running", False)
                or not callable(getattr(scheduler, "get_job", None))):
            details["schedule_status"] = "unavailable"
            return details
        job = scheduler.get_job(str(reminder.get("job_id") or ""))
        if job is None:
            details["schedule_status"] = "missing"
        elif getattr(job, "pending", False):
            details["schedule_status"] = "pending"
        else:
            next_time = getattr(job, "next_run_time", None)
            if isinstance(next_time, datetime.datetime):
                details.update(
                    next_run_time=next_time.astimezone().strftime("%Y-%m-%d %H:%M"),
                    schedule_status="scheduled",
                )
    except Exception as error:
        logger.warning(f"[Reminder] 读取下次调度时间失败: {error}")
        details["schedule_status"] = "unavailable"
    return details


class ReminderScheduler:
    """Register reminder jobs while leaving lifecycle ownership to the plugin."""

    def __init__(
        self,
        *,
        storage: ReminderStorage,
        delivery: DeliveryTracker,
        get_scheduler: Callable[[], Any],
        fire_reminder: Callable[[str, dict], Any],
        get_fire_semaphore: Callable[[], Any],
        is_autonomous_reminder: Callable[[dict], bool],
        build_autonomous_notice_text: Callable[..., str],
        publish_immediate_notice: Callable[..., Any],
        get_autonomy_mode: Callable[[], str],
        mark_autonomous_followup_fired: Callable[[str, dict], Any],
    ) -> None:
        self._storage = storage
        self._delivery = delivery
        self._get_scheduler = get_scheduler
        self._fire_reminder = fire_reminder
        self._get_fire_semaphore = get_fire_semaphore
        self._is_autonomous_reminder = is_autonomous_reminder
        self._build_autonomous_notice_text = build_autonomous_notice_text
        self._publish_immediate_notice = publish_immediate_notice
        self._get_autonomy_mode = get_autonomy_mode
        self._mark_autonomous_followup_fired = mark_autonomous_followup_fired

    async def health_check_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
                scheduler = self._get_scheduler()
                if scheduler and not scheduler.running:
                    logger.error("[Reminder] 调度器已停止，尝试重启...")
                    try:
                        scheduler.start()
                        await self.restore_jobs()
                        logger.info("[Reminder] 调度器重启成功")
                    except Exception as e:
                        logger.error(f"[Reminder] 调度器重启失败: {e}")
        except asyncio.CancelledError:
            logger.debug("[Reminder] 健康检查任务已取消")
        except Exception as e:
            logger.error(f"[Reminder] 健康检查异常: {e}")

    async def restore_jobs(self) -> None:
        async with self._storage.modify() as data:
            now = get_local_now()
            restored = 0
            overdue = 0
            for sid, reminders in list(data.items()):
                kept = []
                for r in reminders:
                    try:
                        trigger_time = parse_time_string(r["time"])
                        repeat = r.get("repeat", "none")
                        if repeat != "none":
                            if not r.get("paused"):
                                self.add_job(sid, r)
                            kept.append(r)
                            restored += 1
                        elif trigger_time > now:
                            if not r.get("paused"):
                                self.add_job(sid, r)
                            kept.append(r)
                            restored += 1
                        else:
                            # Keep overdue one-time records; ignoring issues never deletes them.
                            kept.append(r)
                            overdue += 1
                    except Exception as e:
                        logger.warning(f"[Reminder] 恢复任务失败: {e}")
                        kept.append(r)
                data[sid] = kept

            if overdue:
                logger.info(f"[Reminder] 保留了 {overdue} 条过期一次性提醒待确认")
            if restored:
                logger.info(f"[Reminder] 已恢复 {restored} 个提醒任务")

    def add_job(self, sid: str, reminder: dict) -> None:
        scheduler = self._get_scheduler()
        if not scheduler:
            raise RuntimeError("调度器不可用，本次操作未完成")

        trigger_time = parse_time_string(reminder["time"])
        repeat = reminder.get("repeat", "none")
        job_id = reminder.get("job_id", "")

        if repeat == "none":
            trigger = DateTrigger(run_date=trigger_time)
        elif repeat == "daily":
            trigger = CronTrigger(hour=trigger_time.hour, minute=trigger_time.minute,
                                  start_date=trigger_time)
        elif repeat == "weekly":
            trigger = CronTrigger(day_of_week=trigger_time.weekday(),
                                  hour=trigger_time.hour, minute=trigger_time.minute,
                                  start_date=trigger_time)
        elif repeat == "monthly":
            trigger = CronTrigger(day=trigger_time.day,
                                  hour=trigger_time.hour, minute=trigger_time.minute,
                                  start_date=trigger_time)
        elif repeat == "yearly":
            trigger = CronTrigger(month=trigger_time.month, day=trigger_time.day,
                                  hour=trigger_time.hour, minute=trigger_time.minute,
                                  start_date=trigger_time)
        elif repeat == "interval":
            interval_minutes = reminder.get("interval_minutes", 30)
            now = get_local_now()
            safe_start = trigger_time if trigger_time > now else now + datetime.timedelta(minutes=interval_minutes)
            trigger = IntervalTrigger(minutes=interval_minutes, start_date=safe_start)
        else:
            trigger = DateTrigger(run_date=trigger_time)

        try:
            scheduler.add_job(
                self._fire_reminder,
                trigger=trigger,
                id=job_id,
                args=[sid, reminder],
                replace_existing=True,
                misfire_grace_time=300,
            )
        except Exception as e:
            logger.warning(f"[Reminder] 添加调度任务失败 job_id={job_id}: {e}")
            raise RuntimeError("调度任务登记失败，请检查日志后重试") from e

    async def fire_reminder(
        self, sid: str, reminder: dict, delivery_id: str | None = None
    ) -> None:
        async with self._get_fire_semaphore():
            job_id = str(reminder.get("job_id") or "")
            current_data = await self._storage.load()
            current = next(
                (item for item in current_data.get(sid, []) if item.get("job_id") == job_id),
                None,
            )
            if current is None or current.get("paused"):
                if delivery_id:
                    await self._delivery.mark(sid, delivery_id, "failed", "reminder deleted or paused")
                return
            if delivery_id and current != reminder:
                await self._delivery.mark(sid, delivery_id, "failed", "reminder changed after retry approval")
                return
            reminder = current
            try:
                if delivery_id is None:
                    delivery_id = await self._delivery.begin(sid, reminder)
            except Exception as e:
                logger.error(f"[Reminder] 创建投递记录失败 sid={sid}: {e}")
                return
            if not delivery_id:
                return
            content = reminder.get("content", "")
            job_id = reminder.get("job_id", "")
            repeat = reminder.get("repeat", "none")
            category = reminder.get("category", "")
            action = reminder.get("action", "")
            logger.info(f"[Reminder] 触发提醒 sid={sid} content={content}")

            if self._is_autonomous_reminder(reminder):
                msg = self._build_autonomous_notice_text(
                    sid=sid,
                    trigger_type="followup_due",
                    intent={
                        "id": reminder.get("intent_id", ""),
                        "title": reminder.get("intent_title", ""),
                        "status": "active",
                        "notes": reminder.get("intent_notes", ""),
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
                    owner_type = str(reminder.get("owner_type") or "legacy")
                    if owner_type == PrincipalKind.BOT.value:
                        principal_kind = PrincipalKind.BOT
                        principal_id = ""
                        capabilities = {"intent.manage", "reminder.create", "reminder.action"}
                        if self._get_autonomy_mode() == "trusted_admin":
                            capabilities.add("reminder.manage_all")
                        origin = EventOrigin.AUTONOMY_FOLLOWUP_DUE
                    elif owner_type == PrincipalKind.USER.value:
                        principal_kind = PrincipalKind.USER
                        principal_id = str(reminder.get("owner_id") or "")
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
                        delegated_owner_id=str(reminder.get("owner_id") or ""),
                        capabilities=capabilities,
                        delivery_id=delivery_id,
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
                    await self._delivery.mark(sid, delivery_id, "failed", "event publish failed")
                except Exception as e:
                    logger.error(f"[Reminder] 记录投递失败状态失败: {e}")
