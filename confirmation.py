"""Bounded, one-shot approvals observed from raw user messages."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import islice
from typing import Any

from core.chat.message_elements import At, Reply, Text

from .identity import event_messages
from .message_sources import SourceEvent, inspect_user_message

TTL_SECONDS = 300
MAX_PENDING = 100
MAX_PER_ACTOR = 10
MAX_PARAMS = 4096


def actor_scope(context: SourceEvent) -> tuple[str, str, str, str]:
    return (context.session.adapter_name, context.sid,
            context.message.sender.user_id, context.message.self_id)


def message_proof(event: Any, message: Any) -> tuple | None:
    context = inspect_user_message(event, message)
    if context is None:
        return None
    elements = list(islice(getattr(message, "chain", ()) or (), 65))
    if len(elements) > 64 or any(not isinstance(e, (Text, At, Reply)) for e in elements):
        return None
    text = " ".join(e.text.strip() for e in elements if isinstance(e, Text)).strip()
    if len(text) > 200:
        return None
    return (actor_scope(context), id(message), str(getattr(message, "message_id", "")), text)


@dataclass
class PendingRequest:
    request_id: str
    context: SourceEvent
    operation: str
    params: dict
    targets: dict | None
    expires_at: float
    delete_confirmation: bool = False
    proof: tuple | None = None


class PendingRequests:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = asyncio.Lock()
        self._items: dict[str, PendingRequest] = {}
        self._seen: dict[tuple, float] = {}

    def reset(self):
        self._items.clear()
        self._seen.clear()

    def _prune(self):
        now = self._clock()
        self._items = {key: item for key, item in self._items.items() if item.expires_at > now}
        self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}

    async def create(self, context, operation, params, targets=None, *, delete_confirmation=False,
                     request_id=None):
        encoded = json.dumps(params, ensure_ascii=False, sort_keys=True)
        if len(encoded) > MAX_PARAMS:
            raise ValueError("本次操作参数过长，请缩短内容")
        async with self._lock:
            self._prune()
            scope = actor_scope(context)
            own = [item for item in self._items.values() if actor_scope(item.context) == scope]
            for item in own:
                if (item.operation == operation and item.params == params and item.targets == targets
                        and item.delete_confirmation == delete_confirmation):
                    return item
            if len(self._items) >= MAX_PENDING or len(own) >= MAX_PER_ACTOR:
                raise ValueError("待确认请求过多，请先处理或等待过期")
            key = request_id or secrets.token_hex(6)
            if key in self._items:
                raise ValueError("请求编号冲突，请重新申请")
            item = PendingRequest(key, context, operation, copy.deepcopy(params),
                                  copy.deepcopy(targets), self._clock() + TTL_SECONDS,
                                  delete_confirmation)
            self._items[key] = item
            return item

    async def observe(self, event):
        message = getattr(event, "message", None)
        if message is None:
            return
        context = inspect_user_message(event, message)
        if context is None or (context.is_group_message() and not context.is_mentioned):
            return
        proof = message_proof(event, message)
        if proof is None:
            return
        text = proof[-1].strip().rstrip("。！!.").strip()
        match = re.fullmatch(r"(确认删除|确认|同意|取消)(?:\s+([a-f0-9]{12}))?", text)
        if match is None:
            return
        verb, key = match.groups()
        async with self._lock:
            self._prune()
            replay_key = (proof[0], proof[2])
            if not proof[2] or replay_key in self._seen:
                return
            if len(self._seen) >= 1000:
                return
            self._seen[replay_key] = self._clock() + TTL_SECONDS
            own = [item for item in self._items.values()
                   if actor_scope(item.context) == actor_scope(context)]
            candidates = [item for item in own if item.request_id == key] if key else own
            if len(candidates) != 1:
                return
            item = candidates[0]
            if verb == "取消":
                del self._items[item.request_id]
            elif ((item.delete_confirmation and verb == "确认删除")
                  or (not item.delete_confirmation and verb in {"确认", "同意"})
                  or (item.operation == "delete_reminder" and verb == "确认删除")):
                item.proof = proof

    async def visible(self, event):
        scopes = {actor_scope(context) for message in event_messages(event)[:20]
                  if (context := inspect_user_message(event, message)) is not None}
        async with self._lock:
            self._prune()
            # Expose only minimal markers, never stored parameters or target contents.
            return [{"request_id": item.request_id, "operation": item.operation,
                     "nickname": item.context.message.sender.nickname[:40],
                     "confirmed": item.proof is not None,
                     "reply": "确认删除" if item.delete_confirmation else "确认"}
                    for item in self._items.values() if actor_scope(item.context) in scopes][:20]

    async def consume(self, event, request_id):
        proofs = {proof for message in event_messages(event)[:20]
                  if (proof := message_proof(event, message)) is not None}
        async with self._lock:
            self._prune()
            item = self._items.get(request_id)
            if item is None:
                raise ValueError("请求已过期、取消或处理，请重新提出需求")
            if item.proof is None or item.proof not in proofs:
                raise ValueError("尚未收到对应用户的本轮确认；不要替用户确认")
            del self._items[request_id]
            return item


def reminder_targets(data, sid, params):
    job_id = params.get("job_id")
    reminders = data.get(sid, [])
    target = next((r for r in reminders if r.get("job_id") == job_id), None)
    if target is None:
        raise ValueError("目标提醒不存在，请重新查询")
    batch_id = target.get("random_batch_id") if params.get("delete_batch") else None
    selected = [r for r in reminders if r.get("random_batch_id") == batch_id] if batch_id else [target]
    if len(selected) > 100:
        raise ValueError("本次目标过多，请分次处理")
    if len(json.dumps(selected, ensure_ascii=False)) > 65536:
        raise ValueError("目标数据过大，请分次处理")
    return {"kind": "reminder", "sid": sid, "params": copy.deepcopy(params),
            "records": copy.deepcopy(selected)}


class CheckedReminderStorage:
    """Validate a snapshot inside the existing storage transaction, not before it."""

    def __init__(self, storage, targets):
        self._storage, self._targets = storage, targets

    def _check(self, data):
        if self._targets is None:
            return
        current = reminder_targets(data, self._targets["sid"], self._targets["params"])
        if current != self._targets:
            raise ValueError("目标提醒已变化，请重新查询并确认")

    async def load(self):
        data = await self._storage.load()
        self._check(data)
        return data

    @asynccontextmanager
    async def modify(self):
        async with self._storage.modify() as data:
            self._check(data)
            yield data
