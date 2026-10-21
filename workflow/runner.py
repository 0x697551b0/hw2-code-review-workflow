"""
编排器（Orchestrator）：把节点树跑起来，并负责所有"横切"的事。

它只做四件事，但每件都必须做对：

1. **装配**——发现 Agent、准备状态/总线/事件、恢复或新建存档；
2. **执行**——调用流程树，捕获异常；
3. **收尾**——无论成功失败都写报告、返回结构化结论；
4. **不吞错**——失败时给出可执行的原因，而不是一句 "something went wrong"。

``resume`` 的语义说明：它跳过 ``state.completed`` 里已有的节点，因此能从中断处继续。
但它**不会重放已跳过节点的副作用**（例如修复已经写进磁盘）。这一点必须写进文档，
否则用户会期望"resume 等于 rewind"，而那不是它做的事。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from workflow.bus import MessageBus, TOPIC_REVIEW_COMPLETED, Inbox
from workflow.config import WorkflowConfig
from workflow.context import RunContext, new_run_id, utc_now
from workflow.errors import (
    AbortWorkflow,
    AgentNotFound,
    CheckpointError,
    NodeFailed,
    WorkflowConfigError,
    WorkflowError,
)
from workflow.events import (
    EVENT_RUN_END,
    EVENT_RUN_START,
    ConsoleListener,
    EventLog,
)
from workflow.models import (
    OUTCOME_ABORTED,
    OUTCOME_FAILED,
    OUTCOME_NEEDS_HUMAN,
    OUTCOME_PASSED,
    RoundSummary,
    RunOutcome,
    StageReport,
)
from workflow.protocol import dumps
from workflow.registry import AgentRegistry
from workflow.state import SharedState
from workflow.stages import build_pipeline

#: 编排器必须拥有的 Agent
REQUIRED_AGENTS: tuple[str, ...] = ("coding", "review", "fix")


class Orchestrator:
    """一次运行的驱动者。"""

    def __init__(
        self,
        config: WorkflowConfig,
        *,
        registry: AgentRegistry | None = None,
        run_id: str = "",
    ) -> None:
        config.validate()
        self.config = config
        self.run_id = run_id or new_run_id()
        self.registry = registry or AgentRegistry.discover(
            agents_home=config.agents_home_path,
            settings={"coding": config.coding, "review": config.review, "fix": config.fix},
        )
        # 门禁口径强制对齐：审查 Agent 必须用编排器的阻断级别，否则两边判定会打架
        self._align_blocking()

        self.run_dir = config.resolve_output_root() / self.run_id
        self.events = EventLog(self.run_dir / "events.jsonl")
        if config.trace:
            self.events.add_listener(ConsoleListener(show_debug=config.verbose))
        self.bus = MessageBus(events=self.events, run_id=self.run_id)
        self.outcome = RunOutcome(run_id=self.run_id)
        self.stages: list[StageReport] = []
        self.state = SharedState(self.run_id)
        self.state.bind(self.run_dir / "state.json")
        self.state.note("started_at", utc_now())
        self.state.note("config", config.describe())

    # 门禁对齐
    def _align_blocking(self) -> None:
        if self.registry.has("review"):
            review = self.registry.handles["review"]
            review.settings.options["blocking"] = tuple(self.config.blocking)

    # 恢复
    def resume(self, checkpoint_path: str | Path) -> bool:
        """
        从存档恢复。返回 True 表示确实进入 resume 模式。

        这里有三个"看着不起眼、漏了就很难查"的细节：

        - 存档里的 ``run_id`` 才是权威的。用户可能只给了 ``state.json`` 的路径，
          这时运行目录必须改成**存档所在目录**，否则续跑会往一个新建的空目录里写报告。
        - **事件日志要重绑并改为追加**。``EventLog`` 是在 ``__init__`` 里按当时的
          ``run_dir`` 建好的；不重绑就会写到旧路径（或一个新建的空目录），把第一次运行
          留下的 ``events.jsonl`` 变成另一份互不相干的文件。
        - **阶段记录与总线消息要从存档里恢复**。这两样不在业务状态里，恢复后若为空，
          最终报告的"阶段耗时""Agent 消息传递"两节会一片空白 —— 而它们正是"执行日志
          与状态追踪"这项要求的直接证据。
        """
        path = Path(checkpoint_path)
        if not path.is_file():
            raise CheckpointError(f"存档不存在，无法 resume：{path}")
        restored = SharedState.load(path)
        if restored.run_id != self.run_id:
            self.run_id = restored.run_id
            self.outcome.run_id = restored.run_id
            self.state.run_id = restored.run_id
        self.state = restored
        self.state.bind(path)
        # 运行目录以存档所在目录为准
        self.run_dir = path.parent

        # 事件日志重绑到同一个 run 目录，并保留已有记录（续跑 ≠ 重来）
        self.events = EventLog(self.run_dir / "events.jsonl", append=True)
        if self.config.trace:
            self.events.add_listener(ConsoleListener(show_debug=self.config.verbose))
        self.bus.events = self.events

        # 恢复阶段记录与消息历史
        self.stages = [StageReport(**row) for row in self.state.meta.get("stages", []) or []]
        self.outcome.stages = list(self.stages)
        restored_messages = self.bus.restore(list(self.state.meta.get("messages", []) or []))

        self.outcome.workdir = str(self.state.get_path("paths.workdir", ""))
        self.outcome.report_dir = str(self.state.get_path("paths.reports", ""))
        self.outcome.spec_path = str(self.state.get_path("spec.path", ""))
        self.outcome.residual = list(self.state.get_path("residual", []) or [])
        self.outcome.open_questions = list(self.state.get_path("open_questions", []) or [])
        self.outcome.rounds = [RoundSummary(**row) for row in self.state.get_path("rounds", []) or []]
        self.events.emit(
            EVENT_RUN_START,
            f"从存档恢复：已完成 {self.state.count_done()} 个节点，"
            f"恢复 {restored_messages} 条消息、{len(self.stages)} 条阶段记录",
            resumed=True,
            checkpoint=str(path),
        )
        return True

    def fresh_state(self) -> None:
        """强制全新状态（``--no-resume``）。"""
        self.state = SharedState(self.run_id)
        self.state.bind(self.run_dir / "state.json")
        self.state.note("started_at", utc_now())
        self.state.note("config", self.config.describe())
        self.state.save(force=True)

    # 主入口
    def run(self, *, resumable: bool = False) -> RunOutcome:
        started = time.perf_counter()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events.emit(
            EVENT_RUN_START,
            f"开始运行 {self.run_id}（blocking={','.join(self.config.blocking)}，max_rounds={self.config.max_rounds}）",
            run_id=self.run_id,
            agents=self.registry.describe(),
        )
        self.state.save(force=True)

        inbox = Inbox(self.bus, recipient="fix").listen(TOPIC_REVIEW_COMPLETED)
        pipeline = build_pipeline(max_rounds=self.config.max_rounds, review_inbox=inbox)

        ctx = RunContext(
            config=self.config,
            state=self.state,
            bus=self.bus,
            events=self.events,
            registry=self.registry,
            outcome=self.outcome,
            run_id=self.run_id,
            path="",
            depth=0,
            round=0,
            resumable=resumable,
            stages=self.stages,
        )

        error = ""
        try:
            self.registry.require(*REQUIRED_AGENTS)
            pipeline.execute(ctx)
            self.outcome.outcome = (
                OUTCOME_PASSED if self._gate_passed() else OUTCOME_NEEDS_HUMAN
            )
        except AbortWorkflow as exc:
            error = f"流程被中止：{exc}"
            self.outcome.outcome = OUTCOME_ABORTED
        except AgentNotFound as exc:
            error = str(exc)
            self.outcome.outcome = OUTCOME_FAILED
        except NodeFailed as exc:
            error = str(exc)
            self.outcome.outcome = OUTCOME_FAILED
        except WorkflowError as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.outcome.outcome = OUTCOME_FAILED
        except Exception as exc:  # noqa: BLE001 - 顶层必须兜住，不能让 CLI 看到裸 traceback
            error = f"未预期错误 {type(exc).__name__}: {exc}"
            self.outcome.outcome = OUTCOME_FAILED
            if self.config.verbose:
                import traceback

                traceback.print_exc()

        elapsed = time.perf_counter() - started
        self.outcome.elapsed_seconds = elapsed
        self.outcome.error = error
        self.outcome.stages = list(self.stages)
        self.outcome.diagnostics.update(
            {
                "bus_messages": len(self.bus),
                "completed_nodes": self.state.count_done(),
                "events": len(self.events),
                "agents": self.registry.describe(),
                "config": self.config.describe(),
            }
        )
        if error:
            self.state.note("error", error)
        self.state.note("finished_at", utc_now())
        self.state.note("outcome", self.outcome.outcome)
        try:
            self.state.save(force=True)
        except CheckpointError:  # pragma: no cover - 存档失败不该掩盖主结果
            pass

        self.events.emit(
            EVENT_RUN_END,
            f"运行结束：{self.outcome.outcome}（{elapsed:.2f}s）"
            + (f" —— {error}" if error else ""),
            run_id=self.run_id,
            outcome=self.outcome.outcome,
            elapsed_seconds=round(elapsed, 3),
            error=error,
        )
        self._finalize_reports()
        return self.outcome

    # 收尾
    def _finalize_reports(self) -> None:
        """写最终的报告与运行汇总。**失败也要写**，否则排障缺材料。"""
        from workflow.reporting import write_final_reports

        try:
            write_final_reports(self, self.outcome)
        except OSError as exc:  # pragma: no cover
            self.events.error(EVENT_RUN_END, f"报告写入失败：{exc}")

    def _gate_passed(self) -> bool:
        return bool(self.state.get_path("gate.passed", False))

    # 诊断
    def health(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agents": self.registry.health(),
            "config": self.config.describe(),
            "run_dir": str(self.run_dir),
        }


def run_workflow(
    config: WorkflowConfig,
    *,
    resume_from: str | Path | None = None,
    run_id: str = "",
    verbose: bool = False,
) -> RunOutcome:
    """便捷函数：一条语句跑完整条流程（CLI 与测试共用）。"""
    config.verbose = config.verbose or verbose
    orchestrator = Orchestrator(config, run_id=run_id)
    resumable = False
    if resume_from:
        resumable = orchestrator.resume(resume_from)
    elif config.checkpoint and orchestrator.state.checkpoint_path and Path(
        orchestrator.state.checkpoint_path
    ).is_file():
        orchestrator.fresh_state()
    return orchestrator.run(resumable=resumable)


def write_run_summary(outcome: RunOutcome, path: str | Path) -> Path:
    """把运行结论写成 JSON（机器可读，便于 CI 断言）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(outcome.to_dict()), encoding="utf-8", newline="\n")
    return target


__all__ = ["Orchestrator", "REQUIRED_AGENTS", "run_workflow", "write_run_summary"]
