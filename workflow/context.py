"""RunContext：一次运行里所有节点共享的执行环境。

配置、状态、总线、事件、注册表、阶段记录都塞在这一个对象里，节点直接
``ctx.call_agent("review", ...)``，不用自己拼装。

``child()`` 派生的子上下文共享这些基础设施，只改 ``path`` 和 ``depth``。path 编码了节点
在流程树里的位置（比如 ``pipeline/1/review-fix-loop#2/0:review``），事件日志定位、resume
的完成标记（``state.completed``）、循环游标都用它，一个标识三处用。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from workflow.bus import MessageBus
from workflow.config import WorkflowConfig
from workflow.errors import NodeFailed
from workflow.events import (
    EVENT_AGENT_CALL,
    EVENT_AGENT_RESULT,
    EVENT_AGENT_RETRY,
    EVENT_CHECKPOINT,
    EVENT_NODE_START,
    EventLog,
)
from workflow.models import RunOutcome, StageReport
from workflow.protocol import AgentRequest, AgentResponse
from workflow.registry import AgentHandle, AgentRegistry
from workflow.state import SharedState


def new_run_id(prefix: str = "run") -> str:
    """生成可读的运行 ID：``run-20261022-153000-a1b2``。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:4]}"


@dataclass
class RunContext:
    """节点执行上下文。"""

    config: WorkflowConfig
    state: SharedState
    bus: MessageBus
    events: EventLog
    registry: AgentRegistry
    outcome: RunOutcome
    run_id: str = ""
    #: 当前节点在流程树中的限定名，``""`` 表示根
    path: str = ""
    depth: int = 0
    #: 当前轮次（0 表示还没进入审查循环）
    round: int = 0
    #: 允许 resume（跳过已完成节点）
    resumable: bool = False
    stages: list[StageReport] = field(default_factory=list)

    # 派生
    def child(self, segment: str, *, round: int | None = None) -> "RunContext":
        """派生子上下文。``segment`` 会拼到路径后面。"""
        return RunContext(
            config=self.config,
            state=self.state,
            bus=self.bus,
            events=self.events,
            registry=self.registry,
            outcome=self.outcome,
            run_id=self.run_id,
            path=f"{self.path}/{segment}" if self.path else segment,
            depth=self.depth + 1,
            round=self.round if round is None else round,
            resumable=self.resumable,
            stages=self.stages,
        )

    def at(self, path: str) -> "RunContext":
        """换成绝对路径（用于并行节点的每个分片）。"""
        clone = self.child("")
        clone.path = path
        return clone

    # 日志
    def emit(self, kind: str, message: str, **data: Any) -> None:
        self.events.emit(kind, message, node=self.path, round=self.round, **data)

    def debug(self, kind: str, message: str, **data: Any) -> None:
        self.events.debug(kind, message, node=self.path, round=self.round, **data)

    def warn(self, kind: str, message: str, **data: Any) -> None:
        self.events.warn(kind, message, node=self.path, round=self.round, **data)

    def error(self, kind: str, message: str, **data: Any) -> None:
        self.events.error(kind, message, node=self.path, round=self.round, **data)

    # 阶段记录
    def record_stage(self, report: StageReport) -> StageReport:
        self.stages.append(report)
        return report

    # Agent 调用
    def call_agent(
        self,
        name: str,
        *,
        task_id: str,
        objective: str = "",
        payload: dict[str, Any] | None = None,
        settings_name: str = "",
    ) -> AgentResponse:
        """调用一个 Agent，顺带处理重试和事件记录。

        整个编排器只有这一个 Agent 出入口，事件、重试、超时、状态快照、失败策略都收在
        这一处，节点里不用各写一遍。
        """
        handle: AgentHandle = self.registry.get(name)
        settings = handle.settings
        attempts = max(1, settings.max_attempts)
        last_error = ""

        for attempt in range(1, attempts + 1):
            request = AgentRequest(
                agent=name,
                task_id=task_id,
                run_id=self.run_id,
                round=self.round,
                objective=objective,
                payload=dict(payload or {}),
                shared=self.state.snapshot(),
                metadata={
                    "node": self.path,
                    "attempt": attempt,
                    "transport": settings.transport,
                    "blocking": list(self.config.blocking),
                },
            )
            self.events.emit(
                EVENT_AGENT_CALL,
                f"{handle.label}  task={task_id}  attempt={attempt}/{attempts}",
                node=self.path,
                round=self.round,
                agent=name,
                task_id=task_id,
                attempt=attempt,
                objective=objective,
                transport=settings.transport,
            )
            started = time.perf_counter()
            try:
                response = handle.send(request, timeout=settings.timeout)
            except NodeFailed as exc:
                last_error = str(exc)
                self.warn(
                    EVENT_AGENT_RETRY,
                    f"{handle.label} 调用失败（{exc}）",
                    agent=name,
                    attempt=attempt,
                    retryable=getattr(exc, "retryable", False),
                )
                if attempt >= attempts or not getattr(exc, "retryable", False):
                    raise
                continue
            elapsed = time.perf_counter() - started

            if response.ok:
                self.events.emit(
                    EVENT_AGENT_RESULT,
                    f"{handle.label} 完成（{elapsed:.2f}s）：{response.summary}",
                    node=self.path,
                    round=self.round,
                    agent=name,
                    task_id=task_id,
                    status=response.status,
                    summary=response.summary,
                    elapsed_seconds=round(elapsed, 3),
                )
                return response

            last_error = response.error or "未提供错误信息"
            self.warn(
                EVENT_AGENT_RETRY,
                f"{handle.label} 返回失败：{last_error}",
                agent=name,
                attempt=attempt,
                status=response.status,
            )
            if attempt >= attempts:
                raise NodeFailed(
                    f"{handle.label} 执行失败：{last_error}", node=name, retryable=False
                )

        raise NodeFailed(f"{handle.label} 执行失败：{last_error}", node=name)  # pragma: no cover

    # 存档
    def checkpoint(self, *, force: bool = False) -> None:
        """写存档。阶段记录和总线消息也一起存，不然 resume 之后报告里那两节是空的。"""
        if not self.config.checkpoint:
            return
        path = self.state.checkpoint_path
        if path is None:
            return
        self.state.note("stages", [stage.to_dict() for stage in self.stages])
        self.state.note("messages", self.bus.transcript())
        written = self.state.save(force=force)
        if written is not None:
            self.debug(EVENT_CHECKPOINT, f"存档 → {written.name}", completed=self.state.count_done())

    def started(self, message: str) -> float:
        """节点开始：打日志并返回起始时间戳。"""
        self.events.emit(EVENT_NODE_START, message, node=self.path, round=self.round)
        return time.perf_counter()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["RunContext", "new_run_id", "utc_now"]
