"""Real Kira event shapes for isolated confirmation tests."""

import time
import uuid
from types import SimpleNamespace

from core.chat.message_elements import Text
from core.chat.message_utils import KiraIMMessage, KiraMessageBatchEvent, KiraMessageEvent, MessageChain
from core.chat.session import User, Group, Session


def message(user="alice", text="请处理提醒", *, mentioned=True, group="group-1", bot="bot-1", **kwargs):
    return KiraIMMessage(
        message_id=uuid.uuid4().hex, self_id=bot, timestamp=int(time.time()),
        sender=User(user, user.title()), group=Group(group, group) if group else None,
        chain=MessageChain([Text(text)]), is_mentioned=mentioned, **kwargs,
    )


def batch(*messages, adapter="QQ"):
    first = messages[0]
    return KiraMessageBatchEvent(
        message_types=[], timestamp=int(time.time()), adapter=SimpleNamespace(name=adapter),
        session=Session(adapter, "gm" if first.group else "dm",
                        first.group.group_id if first.group else first.sender.user_id),
        messages=list(messages),
    )


async def observe(plugin, msg, adapter="QQ"):
    raw = KiraMessageEvent([], int(time.time()), msg, adapter=SimpleNamespace(name=adapter))
    await plugin.observe_reminder_confirmation(raw)
    return batch(msg, adapter=adapter)
