"""
事件日志：把"流程里发生了什么"写成可回放的 JSON Lines。

为什么是 JSONL 而不是普通日志文本：

- 每条事件是一个独立 JSON 对象，一行一条，**不需要解析器**就能被 ``jq`` / pandas
  直接读，也不需要整文件加载（日志可能很大）；
- 写入即持久：中途崩溃时已经落盘的行依然可读，这让"出错后复盘"成为可能；
- 同一份事件流既能喂给人看（终端 trace），也能喂给报告生成器画时间线。

设计上把"记录"与"渲染"分开：:class:`EventLog` 只负责写，:class:`ConsoleListener`
只负责把事件变成一行可读文字。这样想加一个 HTML 时间线、或者想接 OpenTelemetry，
都不需要动记录逻辑。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

#: 事件级别
LEVEL_DEBUG = "debug"
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

#: 常用事件类型（字符串常量，便于检索；未列出的自定义类型同样允许）
EVENT_RUN_START = "run.start"
EVENT_RUN_END = "run.end"
EVENT_NODE_START = "node.start"
EVENT_NODE_END = "node.end"
EVENT_NODE_SKIP = "node.skip"
EVENT_AGENT_CALL = "agent.call"
EVENT_AGENT_RESULT = "agent.result"
EVENT_AGENT_RETRY = "agent.retry"
EVENT_MESSAGE = "bus.message"
EVENT_GATE = "gate.evaluated"
EVENT_CHECKPOINT = "state.checkpoint"


@dataclass
class Event:
    """一条事件。"""

    seq: int
    kind: str
    message: str = ""
    level: str = LEVEL_INFO
    node: str = ""
    round: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.timestamp,
            "kind": self.kind,
            "level": self.level,
            "node": self.node,
            "round": self.round,
            "message": self.message,
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class EventLog:
    """
    事件记录器。

    ``path`` 为 None 时只保留在内存里（测试与单元用法）；给出路径时一边追加写盘。
    """

    def __init__(self, path: str | Path | None = None, *, append: bool = False) -> None:
        self.path = Path(path) if path else None
        self.events: list[Event] = []
        self._seq = 0
        self._listeners: list["EventListener"] = []
        #: 并行节点会同时写事件（见 ParallelNode / 审查 fan-out），
        #: 没有锁时序号可能重复、JSONL 行可能交错 —— 一个只在并行路径下才复现的 bug
        self._lock = threading.Lock()
        #: 首次写入前先清空文件（新运行）；续跑时改为追加并回读已有序号
        self._needs_truncate = self.path is not None and not append
        # 关键：这里**不创建目录**。编排器先按新 run_id 建好事件日志，随后 resume 会把
        # 运行目录改绑到存档所在目录；若此处就 mkdir，就会凭空多出一个空运行目录。
        if self.path is not None and append:
            self._load_existing()

    # 订阅
    def add_listener(self, listener: "EventListener") -> None:
        self._listeners.append(listener)

    def _load_existing(self) -> None:
        """把磁盘上已有的事件读回内存，并把序号接到最大值之后。"""
        if self.path is None or not self.path.is_file():
            return
        try:
            raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:  # pragma: no cover
            return
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            self._seq = max(self._seq, int(payload.get("seq") or 0))

    # 记录
    def emit(
        self,
        kind: str,
        message: str = "",
        *,
        level: str = LEVEL_INFO,
        node: str = "",
        round: int = 0,
        **data: Any,
    ) -> Event:
        # 序号递增也放进锁里：并行节点会同时写事件，不加锁时序号可能重复，
        # 而事件序号是 replay 与测试断言的基础
        with self._lock:
            self._seq += 1
            seq = self._seq
        event = Event(
            seq=seq,
            kind=kind,
            message=message,
            level=level,
            node=node,
            round=round,
            data=data,
        )
        self.events.append(event)
        if self.path is not None:
            try:
                with self._lock:
                    self._prepare_first_write()
                    with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(event.to_json() + "\n")
            except OSError:  # pragma: no cover - 日志写不进去不该中断流程
                pass
        for listener in self._listeners:
            try:
                listener.on_event(event)
            except Exception:  # noqa: BLE001 - 监听器异常不能影响主流程
                pass
        return event

    def _prepare_first_write(self) -> None:
        """首次落盘：建目录并按需清空文件（新运行）；之后都是追加。"""
        if not self._needs_truncate:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        self.path.write_text("", encoding="utf-8")  # type: ignore[union-attr]
        self._needs_truncate = False

    def debug(self, kind: str, message: str = "", **data: Any) -> Event:
        return self.emit(kind, message, level=LEVEL_DEBUG, **data)

    def warn(self, kind: str, message: str = "", **data: Any) -> Event:
        return self.emit(kind, message, level=LEVEL_WARN, **data)

    def error(self, kind: str, message: str = "", **data: Any) -> Event:
        return self.emit(kind, message, level=LEVEL_ERROR, **data)

    # 查询
    def filter(self, *, kind: str | None = None, level: str | None = None, node: str | None = None) -> list[Event]:
        return [
            event
            for event in self.events
            if (kind is None or event.kind == kind)
            and (level is None or event.level == level)
            and (node is None or event.node == node)
        ]

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)


class EventListener:
    """事件监听器接口。"""

    def on_event(self, event: Event) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class ConsoleListener(EventListener):
    """把事件实时打印到终端（``--trace`` 打开的视图）。"""

    #: 缩进层级：按 node 路径里的层级数缩进，让嵌套结构一眼可读
    _ICONS: dict[str, str] = {
        EVENT_RUN_START: "▶",
        EVENT_RUN_END: "■",
        EVENT_NODE_START: "┌",
        EVENT_NODE_END: "└",
        EVENT_NODE_SKIP: "⤼",
        EVENT_AGENT_CALL: "→",
        EVENT_AGENT_RESULT: "←",
        EVENT_AGENT_RETRY: "↻",
        EVENT_MESSAGE: "✉",
        EVENT_GATE: "⚖",
        EVENT_CHECKPOINT: "💾",
    }

    def __init__(self, *, stream: Any = None, show_debug: bool = False, clock: Any = None) -> None:
        self.stream = stream or sys.stderr
        self.show_debug = show_debug
        self.clock = clock or time.perf_counter
        self._start = self.clock()

    def on_event(self, event: Event) -> None:
        if event.level == LEVEL_DEBUG and not self.show_debug:
            return
        icon = self._ICONS.get(event.kind, "·")
        depth = max(0, event.node.count("/") - 1) if event.node else 0
        indent = "  " * min(depth, 6)
        stamp = f"{self.clock() - self._start:6.2f}s"
        rnd = f"R{event.round}" if event.round else "  "
        line = f"{stamp} {rnd} {indent}{icon} {event.message or event.kind}"
        try:
            print(line, file=self.stream, flush=True)
        except (ValueError, OSError):  # pragma: no cover - 终端已关闭
            pass


class ListenerBuffer(EventListener):
    """把事件收进列表，便于测试断言"流程确实按顺序发生了这些事"。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def on_event(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def messages(self) -> list[str]:
        return [event.message for event in self.events]


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """读回一个 JSONL 事件文件（用于复盘与测试）。"""
    events: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """给报告用的轻量统计：各类事件多少条、发生在哪些节点。"""
    counts: dict[str, int] = {}
    nodes: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        node = str(event.get("node") or "")
        if node and node not in nodes:
            nodes.append(node)
    return {"counts": counts, "nodes": nodes, "total": sum(counts.values())}


__all__ = [
    "ConsoleListener",
    "EVENT_AGENT_CALL",
    "EVENT_AGENT_RESULT",
    "EVENT_AGENT_RETRY",
    "EVENT_CHECKPOINT",
    "EVENT_GATE",
    "EVENT_MESSAGE",
    "EVENT_NODE_END",
    "EVENT_NODE_SKIP",
    "EVENT_NODE_START",
    "EVENT_RUN_END",
    "EVENT_RUN_START",
    "Event",
    "EventListener",
    "EventLog",
    "LEVEL_DEBUG",
    "LEVEL_ERROR",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "ListenerBuffer",
    "read_events",
    "summarize_events",
]
