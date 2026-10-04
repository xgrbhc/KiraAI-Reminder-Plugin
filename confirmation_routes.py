"""Route selected user operations through shared services and bounded approvals."""

from __future__ import annotations

from .confirmation import PendingRequests, CheckedReminderStorage, reminder_targets
from .identity import PrincipalKind, event_messages
from .message_sources import CONFIRMATION_REQUIRED, requires_source_selection, inspect_user_message
from .permissions import ReminderOperation

OPERATIONS = {
    "edit_reminder": ReminderOperation.EDIT,
    "pause_reminder": ReminderOperation.PAUSE,
    "resume_reminder": ReminderOperation.RESUME,
    "delete_reminder": ReminderOperation.DELETE,
    "mark_reminder_important": ReminderOperation.MARK_IMPORTANT,
    "unmark_reminder_important": ReminderOperation.MARK_IMPORTANT,
}


class ConfirmationRoutes:
    def __init__(self, plugin):
        self.plugin = plugin
        self.pending = PendingRequests()

    @staticmethod
    def describe(item):
        reply = "确认删除" if item.delete_confirmation else "确认"
        visibility = ("确认后将在本群查询，内容可能展示给群成员。"
                      if item.context.is_group_message()
                      and item.operation in {"list_reminders", "list_delivery_issues"} else "")
        target = str(item.params.get("job_id") or item.params.get("delivery_id") or "")[:160]
        return (f"⏳ 待确认请求 {item.request_id}：{item.operation}。"
                f"{'目标编号：' + target + '。' if target else ''}{visibility}"
                f"请自然询问对应用户；唯一请求回复“{reply}”即可，多项时附编号。"
                "群聊需 @ 或回复当前机器人；5 分钟内有效。"
                "收到确认后调用 confirm_reminder_request；不需要再次询问。")

    async def route(self, event, operation, params, source_ref=None, *, needs_confirmation=False):
        plugin = self.plugin
        mixed = requires_source_selection(event)
        if mixed or source_ref:
            if not source_ref:
                return CONFIRMATION_REQUIRED
            try:
                event, _ = plugin._source_resolver().select(event, source_ref)
            except ValueError as error:
                return f"❌ {error}"
        if not mixed and not needs_confirmation and operation not in {
            "delete_reminder", "unmark_reminder_important",
        }:
            return await self.execute(event, operation, params)
        principal = plugin._get_principal(event)
        if principal.kind is not PrincipalKind.USER:
            if mixed:
                return "❌ 此来源不是可确认的用户"
            return await self.execute(event, operation, params)
        try:
            sid = plugin._get_sid(event)
            targets = None
            important_delete = False
            critical = False
            if operation in OPERATIONS:
                targets = reminder_targets(await plugin._storage.load(), sid, params)
                if any(not plugin._check_permission(event, record, OPERATIONS[operation])
                       for record in targets["records"]):
                    return "❌ 权限拒绝：当前用户无权操作目标提醒"
                critical = operation in {"delete_reminder", "unmark_reminder_important"} and any(
                    record.get("important") for record in targets["records"])
                important_delete = critical and operation == "delete_reminder"
                if operation == "edit_reminder":
                    allowed, reason = plugin._check_action_permission(event, params.get("action"))
                    if not allowed:
                        return reason
            elif operation == "set_reminder":
                for allowed, reason in (plugin._check_create_permission(event),
                                        plugin._check_action_permission(event, params.get("action"))):
                    if not allowed:
                        return reason
            elif operation == "review_delivery_issue":
                ledger = await plugin._delivery_storage.load()
                entry = next((item for item in ledger.get(sid, [])
                              if item.get("delivery_id") == params["delivery_id"]), None)
                if entry is None or not plugin._delivery.may_review(
                    principal, entry.get("reminder") or {}, sid, plugin._admin_acl(),
                    plugin._allowed_autonomy_sessions(),
                ):
                    return "❌ 投递记录不可访问，请重新查询"
                if params["decision"] not in {"retry", "dismiss", "defer"}:
                    return "无效处理方式"
                current = reminder_targets(await plugin._storage.load(), sid, {"job_id": entry["job_id"]})
                targets = {"kind": "delivery", "entry": entry, "reminder": current}
            if mixed or needs_confirmation or critical:
                messages = event_messages(event)
                context = inspect_user_message(event, messages[-1]) if messages else None
                if context is None:
                    return "❌ 缺少可核验的原始用户消息，请重新提出需求"
                item = await self.pending.create(context, operation, params, targets,
                                                 delete_confirmation=important_delete)
                return self.describe(item)
            return await self.execute(event, operation, params, targets)
        except (ValueError, OSError, RuntimeError) as error:
            return f"❌ {error}"

    async def execute(self, event, operation, params, targets=None, *, approved=False):
        plugin = self.plugin
        if operation == "review_delivery_issue":
            sid = plugin._get_sid(event)
            message, entry = await plugin._delivery.resolve(
                sid, params["delivery_id"], plugin._get_principal(event), plugin._admin_acl(),
                plugin._allowed_autonomy_sessions(), params["decision"],
                expected=targets,
            )
            if entry and params["decision"] == "retry":
                await plugin._fire_reminder(sid, entry["reminder"], delivery_id=entry["retry_delivery_id"])
            return message
        if operation == "list_delivery_issues":
            return await plugin._list_delivery_issues(event)
        storage = CheckedReminderStorage(plugin._storage, targets) if targets else plugin._storage
        service = plugin._reminder_service(storage=storage, confirmed_delete=approved)
        result = await getattr(service, operation)(event, **params)
        if operation == "list_reminders":
            result = await plugin._append_delivery_status(event, result)
        return result

    async def confirm(self, event, request_id):
        try:
            item = await self.pending.consume(event, request_id)
            return await self.execute(item.context, item.operation, item.params, item.targets, approved=True)
        except Exception as error:
            return f"❌ 确认未完成：{error}。请核对结果后重新提出需求，勿重复提交。"
