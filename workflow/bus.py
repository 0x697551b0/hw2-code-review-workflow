"""消息总线：Agent 之间的显式消息传递。

只有共享状态的时候，修复 Agent 读到什么取决于别人在什么时候写了什么，光读代码看不
出来。改成显式发布/订阅之后，"谁关心什么"是可以声明的东西，加一个 Agent 就是多一个
订阅者。

实现是同步、无队列的：MVP 阶段流程本身就顺序执行，上异步反而会带出"消息在被写之前就
被读"的时序问题。历史都留在 ``history`` 里，将来想换异步队列只要换投递实现。
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from workflow.events import EVENT_MESSAGE, EventLog

#: 标准消息主题。字符串常量便于检索，也允许自定义主题。
TOPIC_CODE_READY = "code.ready"
TOPIC_REVIEW_COMPLETED = "review.completed"
TOPIC_REVIEW_FAILED = "review.failed"
TOPIC_FIX_APPLIED = "fix.applied"
TOPIC_GATE_EVALUATED = "gate.evaluated"
TOPIC_PIPELINE_DONE = "pipeline.done"
TOPIC_HUMAN_REQUIRED = "human.required"

#: 广播收件人
BROADCAST = "*"

#: 订阅处理函数：接收一条消息，返回 None
Handler = Callable[["Message"], None]


@dataclass
class Message:
    """一条 Agent 间消息。"""

    msg_id: str
    topic: str
    sender: str
    recipient: str = BROADCAST
    round: int = 0
    body: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    in_reply_to: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "topic": self.topic,
            "sender": self.sender,
            "recipient": self.recipient,
            "round": self.round,
            "body": self.body,
            "correlation_id": self.correlation_id,
            "in_reply_to": self.in_reply_to,
            "timestamp": self.timestamp,
        }


class MessageBus:
    """同步的发布/订阅总线。"""

    def __init__(self, *, events: EventLog | None = None, run_id: str = "") -> None:
        self.events = events
        self.run_id = run_id
        self.history: list[Message] = []
        self._subscribers: dict[str, list[tuple[str, Handler]]] = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    # 订阅
    def subscribe(self, topic: str, handler: Handler, *, recipient: str = BROADCAST) -> None:
        """订阅某主题。``recipient`` 用于多接收者场景下"我只收发给我的"。"""
        self._subscribers.setdefault(topic, []).append((recipient, handler))

    def unsubscribe(self, topic: str, handler: Handler) -> bool:
        entries = self._subscribers.get(topic, [])
        for index, (_, existing) in enumerate(entries):
            if existing is handler:
                entries.pop(index)
                return True
        return False

    def subscriber_count(self, topic: str = "") -> int:
        if topic:
            return len(self._subscribers.get(topic, []))
        return sum(len(entries) for entries in self._subscribers.values())

    # 发布
    def publish(
        self,
        topic: str,
        *,
        sender: str,
        body: dict[str, Any] | None = None,
        recipient: str = BROADCAST,
        round: int = 0,
        correlation_id: str = "",
        in_reply_to: str = "",
    ) -> Message:
        """发布一条消息并同步投递给订阅者。"""
        message = Message(
            msg_id=f"M{next(self._counter):04d}",
            topic=topic,
            sender=sender,
            recipient=recipient,
            round=round,
            body=dict(body or {}),
            correlation_id=correlation_id or self.run_id,
            in_reply_to=in_reply_to,
        )

        with self._lock:
            self.history.append(message)
            handlers = list(self._subscribers.get(topic, []))

        if self.events is not None:
            self.events.emit(
                EVENT_MESSAGE,
                f"{sender} → {recipient}: {topic}",
                round=round,
                msg_id=message.msg_id,
                topic=topic,
                sender=sender,
                recipient=recipient,
                body_keys=sorted(message.body),
            )

        delivered = 0
        for subscribed_recipient, handler in handlers:
            # 广播人人都收；定向消息只送给订阅了广播或者就订阅了自己的接收者。
            # 这里以前写反过，定向订阅永远收不到广播消息，测试才抓出来。
            if recipient != BROADCAST and subscribed_recipient not in (BROADCAST, recipient):
                continue
            try:
                handler(message)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - 单个订阅者出错不能拖垮发布方
                if self.events is not None:
                    self.events.error(
                        EVENT_MESSAGE,
                        f"订阅者处理消息失败：{type(exc).__name__}: {exc}",
                        round=round,
                        topic=topic,
                    )
        # 投递数记在消息上，方便断言"消息确实有人收"
        message.body.setdefault("_delivered", delivered)
        return message

    # 查询
    def latest(self, topic: str) -> Message | None:
        for message in reversed(self.history):
            if message.topic == topic:
                return message
        return None

    def since(self, seq_or_count: int) -> list[Message]:
        """取最近 N 条之后的消息（按条数索引，便于增量消费）。"""
        return self.history[seq_or_count:]

    def filter(self, *, topic: str | None = None, sender: str | None = None) -> list[Message]:
        return [
            message
            for message in self.history
            if (topic is None or message.topic == topic)
            and (sender is None or message.sender == sender)
        ]

    def topics(self) -> Iterable[str]:
        return sorted({message.topic for message in self.history})

    def transcript(self) -> list[dict[str, Any]]:
        """完整消息记录（写进报告，证明"Agent 协作"确实发生了）。"""
        return [message.to_dict() for message in self.history]

    def restore(self, transcript: list[dict[str, Any]]) -> int:
        """从存档恢复消息历史，返回恢复的条数。

        总线历史不在业务状态里，resume 之后不重建的话，报告里"Agent 消息传递"那节就是空的。
        """
        restored = 0
        max_index = 0
        for payload in transcript:
            if not isinstance(payload, dict):
                continue
            msg_id = str(payload.get("msg_id") or "")
            topic = str(payload.get("topic") or "")
            if not topic:
                continue
            try:
                message = Message(
                    msg_id=msg_id,
                    topic=topic,
                    sender=str(payload.get("sender") or ""),
                    recipient=str(payload.get("recipient") or BROADCAST),
                    round=int(payload.get("round") or 0),
                    body=dict(payload.get("body") or {}),
                    correlation_id=str(payload.get("correlation_id") or ""),
                    in_reply_to=str(payload.get("in_reply_to") or ""),
                    timestamp=str(payload.get("timestamp") or ""),
                )
            except (TypeError, ValueError):  # pragma: no cover - 存档被手工改坏
                continue
            self.history.append(message)
            restored += 1
            digits = "".join(ch for ch in msg_id if ch.isdigit())
            max_index = max(max_index, int(digits) if digits else 0)
        # 让后续消息的编号接着往下排，避免与历史消息撞号
        self._counter = itertools.count(max_index + 1)
        return restored

    def __len__(self) -> int:
        return len(self.history)


class Inbox:
    """订阅者的收件箱。

    订阅方不用自己维护状态：声明 ``Inbox`` 之后 ``latest(topic)`` 总能拿到最新一条，
    在订阅回调里马上读、还是等 run 的时候再读，互不影响。
    """

    def __init__(self, bus: MessageBus, *, recipient: str) -> None:
        self.bus = bus
        self.recipient = recipient
        self._messages: list[Message] = []

    def listen(self, *topics: str) -> "Inbox":
        """订阅若干主题，消息进本收件箱。"""
        for topic in topics:
            self.bus.subscribe(topic, self._receive, recipient=self.recipient)
        return self

    def _receive(self, message: Message) -> None:
        self._messages.append(message)

    def latest(self, topic: str) -> Message | None:
        for message in reversed(self._messages):
            if message.topic == topic:
                return message
        return None

    def all(self, topic: str = "") -> list[Message]:
        if not topic:
            return list(self._messages)
        return [m for m in self._messages if m.topic == topic]

    def clear(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)


__all__ = [
    "BROADCAST",
    "Handler",
    "Inbox",
    "Message",
    "MessageBus",
    "TOPIC_CODE_READY",
    "TOPIC_FIX_APPLIED",
    "TOPIC_GATE_EVALUATED",
    "TOPIC_HUMAN_REQUIRED",
    "TOPIC_PIPELINE_DONE",
    "TOPIC_REVIEW_COMPLETED",
    "TOPIC_REVIEW_FAILED",
]
