"""消息总线（bus.py）的测试。

其中 ``test_directed_subscriber_receives_broadcast`` 是回归测试：这里曾经把
"广播"和"定向"的判断写反过，结果是定向订阅者永远收不到广播消息 ——
一个"订阅上了但收不到"的静默错误。
"""

from __future__ import annotations

from workflow.bus import (
    BROADCAST,
    TOPIC_CODE_READY,
    TOPIC_REVIEW_COMPLETED,
    Inbox,
    Message,
    MessageBus,
)
from workflow.events import ListenerBuffer, EventLog


def test_publish_and_latest() -> None:
    bus = MessageBus()
    bus.publish(TOPIC_CODE_READY, sender="coding", body={"files": ["a.py"]})
    message = bus.latest(TOPIC_CODE_READY)
    assert isinstance(message, Message)
    assert message.sender == "coding"
    assert message.body["files"] == ["a.py"]


def test_subscriber_receives_message() -> None:
    bus = MessageBus()
    received: list[Message] = []
    bus.subscribe(TOPIC_CODE_READY, received.append)
    bus.publish(TOPIC_CODE_READY, sender="coding")
    assert len(received) == 1
    assert received[0].topic == TOPIC_CODE_READY


def test_directed_subscriber_receives_broadcast() -> None:
    """定向订阅（recipient="fix"）也必须收到广播消息。"""
    bus = MessageBus()
    inbox: list[Message] = []
    bus.subscribe(TOPIC_REVIEW_COMPLETED, inbox.append, recipient="fix")
    bus.publish(TOPIC_REVIEW_COMPLETED, sender="review", body={"ok": True})
    assert len(inbox) == 1, "定向订阅者漏收了广播消息"


def test_directed_message_not_delivered_to_others() -> None:
    bus = MessageBus()
    for_fix: list[Message] = []
    for_docs: list[Message] = []
    bus.subscribe(TOPIC_REVIEW_COMPLETED, for_fix.append, recipient="fix")
    bus.subscribe(TOPIC_REVIEW_COMPLETED, for_docs.append, recipient="docs")
    bus.publish(TOPIC_REVIEW_COMPLETED, sender="review", recipient="fix")
    assert len(for_fix) == 1
    assert for_docs == []


def test_broadcast_subscriber_receives_everything() -> None:
    bus = MessageBus()
    all_messages: list[Message] = []
    bus.subscribe(TOPIC_REVIEW_COMPLETED, all_messages.append, recipient=BROADCAST)
    bus.publish(TOPIC_REVIEW_COMPLETED, sender="review", recipient="fix")
    bus.publish(TOPIC_REVIEW_COMPLETED, sender="review", recipient="docs")
    assert len(all_messages) == 2


def test_unsubscribe() -> None:
    bus = MessageBus()
    received: list[Message] = []
    bus.subscribe(TOPIC_CODE_READY, received.append)
    assert bus.unsubscribe(TOPIC_CODE_READY, received.append) is False, "不同函数对象应取消失败"
    assert len(received) == 0
    assert bus.subscriber_count(TOPIC_CODE_READY) == 1


def test_history_and_filter() -> None:
    bus = MessageBus()
    bus.publish(TOPIC_CODE_READY, sender="coding")
    bus.publish(TOPIC_REVIEW_COMPLETED, sender="review")
    assert len(bus) == 2
    assert [m.topic for m in bus.filter(sender="review")] == [TOPIC_REVIEW_COMPLETED]
    assert set(bus.topics()) == {TOPIC_CODE_READY, TOPIC_REVIEW_COMPLETED}


def test_message_ids_are_unique() -> None:
    bus = MessageBus()
    for _ in range(5):
        bus.publish(TOPIC_CODE_READY, sender="coding")
    ids = [m.msg_id for m in bus.history]
    assert len(set(ids)) == 5, ids


def test_subscriber_exception_does_not_break_publish() -> None:
    """订阅者抛异常不能拖垮发布方 —— 否则一个坏扩展点就能让整条流水线停摆。"""
    bus = MessageBus()
    received: list[Message] = []

    def boom(message: Message) -> None:
        raise RuntimeError("订阅者炸了")

    bus.subscribe(TOPIC_CODE_READY, boom)
    bus.subscribe(TOPIC_CODE_READY, received.append)
    message = bus.publish(TOPIC_CODE_READY, sender="coding")
    assert len(received) == 1
    assert message.body["_delivered"] == 1


def test_transcript_captures_body() -> None:
    bus = MessageBus()
    bus.publish(TOPIC_REVIEW_COMPLETED, sender="review", round=2, body={"findings": [1, 2]})
    transcript = bus.transcript()
    assert transcript[0]["round"] == 2
    assert transcript[0]["body"]["findings"] == [1, 2]


def test_inbox_collects_and_queries() -> None:
    bus = MessageBus()
    inbox = Inbox(bus, recipient="fix").listen(TOPIC_REVIEW_COMPLETED)
    bus.publish(TOPIC_REVIEW_COMPLETED, sender="review", body={"round": 1})
    bus.publish(TOPIC_REVIEW_COMPLETED, sender="review", body={"round": 2})
    assert len(inbox) == 2
    assert inbox.latest(TOPIC_REVIEW_COMPLETED).body["round"] == 2
    inbox.clear()
    assert len(inbox) == 0


def test_events_are_recorded_on_bus() -> None:
    log = EventLog()
    buffer = ListenerBuffer()
    log.add_listener(buffer)
    bus = MessageBus(events=log, run_id="r1")
    bus.publish(TOPIC_CODE_READY, sender="coding")
    assert "bus.message" in buffer.kinds()


def test_correlation_id_defaults_to_run_id() -> None:
    bus = MessageBus(run_id="run-x")
    message = bus.publish(TOPIC_CODE_READY, sender="coding")
    assert message.correlation_id == "run-x"
