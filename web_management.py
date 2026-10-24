"""Validated WebUI mutations using the existing transactional reminder service."""

import hashlib
import json

from core.plugin import logger

from .autonomy import is_autonomous_reminder
from .confirmation import CheckedReminderStorage, reminder_targets
from .time_utils import get_local_now, parse_time_string


def web_revision(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def web_editable(record):
    if is_autonomous_reminder(record):
        return False
    if record.get("is_random"):
        try:
            return parse_time_string(record.get("time", "")) > get_local_now()
        except (ValueError, TypeError):
            return False
    return True


def batch_records(records, record):
    batch_id = record.get("random_batch_id")
    return sorted((r for r in records if r.get("random_batch_id") == batch_id),
                  key=lambda r: r.get("job_id", "")) if batch_id else [record]


def web_metadata(records):
    batches = {}
    for record in records:
        if record.get("random_batch_id"):
            batches.setdefault(record["random_batch_id"], []).append(record)
    batch_info = {key: {
        "web_batch_revision": web_revision(sorted(items, key=lambda r: r.get("job_id", ""))),
        "web_batch_editable_count": sum(bool(r.get("is_random")) and web_editable(r) for r in items),
    } for key, items in batches.items()}
    return [dict(web_revision=web_revision(record), web_editable=web_editable(record),
                 **(batch_info.get(record.get("random_batch_id"), {}) if record.get("is_random") else {}))
            for record in records]


class WebCheckedStorage(CheckedReminderStorage):
    conflicted = False

    def _check(self, data):
        try:
            super()._check(data)
        except ValueError:
            self.conflicted = True
            raise


def error(code, message, field=None):
    return {"status": "error", "code": code, "msg": message,
            "field": field}


async def mutate(plugin, operation, payload):
    sid, job_id = payload.get("session_id"), payload.get("job_id")
    if not isinstance(sid, str) or not isinstance(job_id, str) or not sid or not job_id:
        return error("validation", "缺少会话或任务 ID")
    edit_batch = payload.get("edit_batch", False)
    if type(edit_batch) is not bool:
        return error("validation", "修改范围无效")
    try:
        data = await plugin._storage.load()
        record = next((r for r in data.get(sid, []) if r.get("job_id") == job_id), None)
        expected = batch_records(data.get(sid, []), record) if record and edit_batch else record
        if record is None or payload.get("revision") != web_revision(expected):
            return error("conflict", "任务已变化，请刷新并重新打开后修改")
        checked = WebCheckedStorage(plugin._storage, reminder_targets(data, sid, {"job_id": job_id, "edit_batch": edit_batch}))
        service = plugin._reminder_service(storage=checked)
        event = plugin._build_web_event(sid)
        if operation == "importance":
            if edit_batch:
                return error("validation", "星标仅支持单条操作")
            important = payload.get("important")
            if type(important) is not bool:
                return error("validation", "重要标记必须为布尔值")
            method = service.mark_reminder_important if important else service.unmark_reminder_important
            result = await method(event, job_id=job_id)
            success = result.startswith(("设为重要:", "已设为重要:", "取消重要标记:", "并非重要提醒:"))
        else:
            if not web_editable(record):
                return error("readonly", "自主跟进或已到期随机任务不支持表单编辑")
            if edit_batch and (not record.get("is_random") or not record.get("random_batch_id")):
                return error("validation", "仅支持有批次编号的随机任务进行批次修改")
            changes = payload.get("changes")
            allowed = ({"content", "category", "action"} if record.get("is_random") else
                       {"content", "category", "time", "repeat", "interval_minutes", "action"})
            if not isinstance(changes, dict) or not changes or set(changes) - allowed:
                return error("validation", "没有有效修改或包含只读字段")
            for key, value in changes.items():
                if key != "interval_minutes" and not isinstance(value, str):
                    return error("validation", "字段必须为文本", key)
            if "content" in changes and not changes["content"].strip():
                return error("contentRequired", "任务内容不能为空", "content")
            repeat = changes.get("repeat", record.get("repeat", "none"))
            if repeat not in ("none", "daily", "weekly", "monthly", "yearly", "interval"):
                return error("validation", "重复规则无效", "repeat")
            interval = changes.get("interval_minutes", record.get("interval_minutes"))
            if repeat == "interval" or "interval_minutes" in changes:
                if type(interval) is not int or not 1 <= interval <= 1440:
                    return error("intervalInvalid", "间隔须为 1–1440 分钟的整数", "interval_minutes")
            try:
                when = parse_time_string(changes.get("time", record.get("time", "")))
            except (ValueError, TypeError):
                return error("timeInvalid", "时间格式需为 YYYY-MM-DD HH:MM", "time")
            # Editing an expired date job would otherwise register it again.
            if repeat == "none" and when <= get_local_now():
                return error("futureRequired", "一次性任务请设置未来时间后保存", "time")
            result = await service.edit_reminder(event, job_id=job_id, edit_batch=edit_batch, **changes)
            success = result.startswith("已更新:")
        if checked.conflicted:
            return error("conflict", "任务已变化，请刷新并重新打开后修改")
        return {"status": "ok" if success else "error", "msg": result}
    except Exception as exc:
        logger.error(f"[Reminder] WebUI mutation failed: {exc}")
        return error("unavailable", "保存失败，请刷新检查任务状态后重试")
