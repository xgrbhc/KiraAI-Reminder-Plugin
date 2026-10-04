"""Reminder use cases shared by tool, Web API, and quick-command entry points."""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Callable, Collection
from typing import Any

from core.plugin import logger

from .identity import PrincipalContext, PrincipalKind
from .job_sync import reminder_job_commit
from .permissions import ReminderOperation, can_manage_reminder
from .storage import ReminderStorage
from .time_utils import (
    determine_random_count,
    generate_multiple_random_times,
    get_local_now,
    parse_time_string,
)

_CONFIRM_TTL = 300
_MAX_PENDING_TOKENS = 100


class ReminderService:
    """Apply reminder mutations without depending on KiraAI entry-point types."""

    def __init__(
        self,
        *,
        storage: ReminderStorage,
        pending: dict[str, Any],
        get_sid: Callable[[Any], str],
        check_permission: Callable[[Any, dict, ReminderOperation], bool],
        check_create_permission: Callable[[Any], tuple[bool, str]],
        check_action_permission: Callable[[Any, str | None], tuple[bool, str]],
        get_creator_info: Callable[[Any], dict[str, str]],
        identity_fields_for_event: Callable[[Any], dict[str, Any]],
        get_principal: Callable[[Any], PrincipalContext],
        is_admin_user: Callable[[Any], bool],
        admin_users: Collection[str],
        remove_job: Callable[[str], None],
        add_job: Callable[[str, dict], None],
        get_scheduler: Callable[[], Any] | None = None,
        confirmed_delete: bool = False,
    ) -> None:
        self._storage = storage
        self._pending = pending
        self._get_sid = get_sid
        self._check_permission = check_permission
        self._check_create_permission = check_create_permission
        self._check_action_permission = check_action_permission
        self._get_creator_info = get_creator_info
        self._identity_fields_for_event = identity_fields_for_event
        self._get_principal = get_principal
        self._is_admin_user = is_admin_user
        self._admin_users = admin_users
        self._remove_job = remove_job
        self._add_job = add_job
        self._get_scheduler = get_scheduler
        self._confirmed_delete = confirmed_delete

    def _job_commit(self, sid: str) -> Callable[[dict, dict], None]:
        return reminder_job_commit(
            sid, add_job=self._add_job, remove_job=self._remove_job,
            get_scheduler=self._get_scheduler,
        )

    def _cleanup_tokens(self) -> None:
        now = time.time()
        expired = [k for k, v in self._pending.items() if v["expires_at"] < now]
        for k in expired:
            del self._pending[k]
        if len(self._pending) > _MAX_PENDING_TOKENS:
            sorted_keys = sorted(
                self._pending, key=lambda k: self._pending[k]["expires_at"]
            )
            for k in sorted_keys[: len(self._pending) - _MAX_PENDING_TOKENS]:
                del self._pending[k]

    def _create_token(self, event: Any, sid: str, job_ids: list[str], content: str) -> str:
        token = uuid.uuid4().hex[:12]
        self._pending[token] = {
            "session_id": sid,
            "job_ids": job_ids,
            "content": content,
            "actor_key": self._get_principal(event).actor_key,
            "expires_at": time.time() + _CONFIRM_TTL,
        }
        return token

    async def set_reminder(
        self,
        event: Any,
        content: str,
        time: str,
        repeat: str = "none",
        interval_minutes: int | None = None,
        category: str | None = None,
        action: str | None = None,
        time_range_end: str | None = None,
        random_count: int | None = None,
        random_count_min: int | None = None,
        random_count_max: int | None = None,
    ) -> str:
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

            if time_range_end:
                try:
                    end_time = parse_time_string(time_range_end)
                except ValueError:
                    return "❌ 结束时间格式错误"
                if end_time <= start_time:
                    return "❌ 结束时间必须晚于开始时间"
                try:
                    final_count = determine_random_count(random_count, random_count_min, random_count_max)
                except ValueError as error:
                    return f"❌ {error}"
                trigger_times = generate_multiple_random_times(start_time, end_time, final_count)
                trigger_times = [t for t in trigger_times if t > get_local_now()]
                if not trigger_times:
                    return "❌ 所有随机时间点都已过去"
            elif repeat == "none" and start_time < get_local_now():
                return "❌ 不能设置过去的时间"

            async with self._storage.modify(after_save=self._job_commit(sid)) as data:
                allowed, reason = self._check_create_permission(event)
                if not allowed:
                    return reason
                allowed, reason = self._check_action_permission(event, action)
                if not allowed:
                    return reason
                data.setdefault(sid, [])

                if time_range_end:
                    for i, t in enumerate(trigger_times):
                        job_id = f"reminder_{sid}_{batch_ts}_{i}"
                        r: dict[str, Any] = {
                            "content": content, "time": t.strftime("%Y-%m-%d %H:%M"),
                            "repeat": "none", "job_id": job_id,
                            "created_at": get_local_now().strftime("%Y-%m-%d %H:%M"),
                            "is_random": True, "random_batch_id": batch_ts,
                            "random_index": i + 1, "random_total": len(trigger_times),
                            "time_range": {"start": time, "end": time_range_end},
                            "creator_id": creator_info["creator_id"],
                            "creator_name": creator_info["creator_name"],
                            "session_type": sess_type,
                        }
                        if category: r["category"] = category
                        if action: r["action"] = action
                        r.update(identity_fields)
                        data[sid].append(r)
                    times_str = "\n".join(f"  {i+1}. {t.strftime('%Y-%m-%d %H:%M')}"
                                           for i, t in enumerate(trigger_times))
                    return (f"已添加随机待办: {content} (共{len(trigger_times)}次)\n"
                            f"时间列表:\n{times_str}")

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

            repeat_text = {"none": "", "daily": " (每天)", "weekly": " (每周)",
                           "monthly": " (每月)", "yearly": " (每年)",
                           "interval": f" (每{interval_minutes}分钟)"}.get(repeat, "")
            return (f"已添加: {content}\n"
                    f"[{start_time.strftime('%Y-%m-%d %H:%M')}{repeat_text}]")
        except Exception as e:
            logger.error(f"[Reminder] 设置提醒失败: {e}")
            return f"设置出错: {e}"

    async def list_reminders(self, event: Any) -> str:
        try:
            sid = self._get_sid(event)
            data = await self._storage.load()
            reminders = data.get(sid, [])
            is_admin_user = self._is_admin_user(event)
            filtered = [
                r for r in reminders
                if self._check_permission(event, r, ReminderOperation.VIEW)
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

    async def delete_reminder(
        self, event: Any, job_id: str = "", delete_batch: bool = False
    ) -> str:
        try:
            self._cleanup_tokens()
            sid = self._get_sid(event)
            async with self._storage.modify(after_save=self._job_commit(sid)) as data:
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
                if any(r.get("important") for r in targets) and not self._confirmed_delete:
                    job_ids = [r["job_id"] for r in targets if "job_id" in r]
                    token = self._create_token(event, sid, job_ids, reminder["content"])
                    self._pending[token]["targets"] = copy.deepcopy(targets)
                    self._pending[token]["batch_id"] = batch_id if delete_batch else None
                    return (f"注意: 「{reminder['content']}」是重要提醒\n"
                            f"请确认删除令牌: {token}")

                if delete_batch and batch_id:
                    data[sid] = [r for r in reminders if r.get("random_batch_id") != batch_id]
                    return f"已批量删除: {reminder['content']} (共{len(targets)}项)"

                data[sid] = [r for r in reminders if r.get("job_id") != job_id]
                return f"已删除: {reminder['content']}"
        except Exception as e:
            return f"出错: {e}"

    async def confirm_delete_reminder(self, event: Any, confirm_token: str = "") -> str:
        try:
            self._cleanup_tokens()
            pending = self._pending.get(confirm_token)
            if not pending:
                return "令牌无效或已过期"
            principal = self._get_principal(event)
            if not (principal.trusted and principal.kind is PrincipalKind.WEB) and not self._confirmed_delete:
                return "❌ 重要删除需要对应用户的真实确认"
            if pending.get("actor_key") != principal.actor_key and not self._is_admin_user(event):
                return "❌ 权限拒绝：确认令牌不属于当前主体。"
            sid, job_ids, content = pending["session_id"], pending["job_ids"], pending["content"]
            async with self._storage.modify(after_save=self._job_commit(sid)) as data:
                reminders = data.get(sid, [])
                targets = [r for r in reminders if r.get("job_id") in job_ids]
                batch_id = pending.get("batch_id")
                if (len(targets) != len(job_ids)
                        or ("targets" in pending and targets != pending["targets"])
                        or (batch_id and [r for r in reminders if r.get("random_batch_id") == batch_id] != targets)):
                    self._pending.pop(confirm_token, None)
                    return "❌ 目标提醒已变化或不存在，请重新查询并确认"
                if any(
                    not can_manage_reminder(
                        principal,
                        reminder,
                        operation=ReminderOperation.DELETE,
                        sid=sid,
                        admin_users=self._admin_users,
                    )
                    for reminder in targets
                ):
                    return "❌ 权限拒绝：当前主体无权删除目标提醒。"
                before = len(reminders)
                data[sid] = [r for r in reminders if r.get("job_id") not in job_ids]
                deleted = before - len(data[sid])
            del self._pending[confirm_token]
            suffix = f" (共{deleted}项)" if deleted > 1 else ""
            return f"已删除: {content}{suffix}"
        except Exception as e:
            return f"出错: {e}"

    async def mark_reminder_important(self, event: Any, job_id: str = "") -> str:
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

    async def unmark_reminder_important(self, event: Any, job_id: str = "") -> str:
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
                principal = self._get_principal(event)
                if not (principal.trusted and principal.kind is PrincipalKind.WEB) and not self._confirmed_delete:
                    return "❌ 取消重要标记需要对应用户的真实确认"
                r["important"] = False
                return f"取消重要标记: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    async def pause_reminder(self, event: Any, job_id: str = "") -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify(after_save=self._job_commit(sid)) as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r, ReminderOperation.PAUSE):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if r.get("paused"):
                    return f"已经暂停: {r['content']}"
                r["paused"] = True
                return f"已暂停: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    async def resume_reminder(self, event: Any, job_id: str = "") -> str:
        try:
            sid = self._get_sid(event)
            async with self._storage.modify(after_save=self._job_commit(sid)) as data:
                reminders = data.get(sid, [])
                r = next((r for r in reminders if r.get("job_id") == job_id), None)
                if r is None:
                    return f"找不到任务: {job_id}"
                if not self._check_permission(event, r, ReminderOperation.RESUME):
                    return f"❌ 权限拒绝：您无权操作该任务 (创建人: {r.get('creator_name', '未知')})"
                if not r.get("paused"):
                    return f"已经处于活动状态: {r['content']}"

                if r.get("repeat", "none") == "none":
                    trigger_time = parse_time_string(r["time"])
                    if trigger_time <= get_local_now():
                        return f"已过期，无法恢复: {r['content']}"

                r["paused"] = False
                return f"已恢复: {r['content']}"
        except Exception as e:
            return f"出错: {e}"

    async def edit_reminder(
        self,
        event: Any,
        job_id: str = "",
        content: str | None = None,
        time: str | None = None,
        repeat: str | None = None,
        interval_minutes: int | None = None,
        category: str | None = None,
        action: str | None = None,
    ) -> str:
        try:
            sid = self._get_sid(event)

            new_time_str = time
            new_start_time = None
            if new_time_str is not None:
                try:
                    new_start_time = parse_time_string(new_time_str)
                except ValueError:
                    return "时间格式需为 YYYY-MM-DD HH:MM"

            async with self._storage.modify(after_save=self._job_commit(sid)) as data:
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

                return f"已更新: {r['content']}\n[{r['time']}]"
        except Exception as e:
            logger.error(f"[Reminder] 编辑提醒失败: {e}")
            return f"修改出错: {e}"
