"""
工作流自有的领域模型：阶段记录、门禁结论、运行汇总。

Agent 契约（``protocol.py``）只描述"一次请求/响应"，而工作流还需要描述"整条流程
跑到哪了、每段花了多久、门禁为什么这么判"。两者分开定义，是因为它们的读者不同：
契约是给 Agent 看的，这些是给人看的（报告、日志、CLI 输出）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from workflow.protocol import (
    DEFAULT_BLOCKING,
    FindingContract,
    count_blocking,
    count_by_severity,
    is_blocking,
)

#: 阶段状态
STAGE_OK = "ok"
STAGE_SKIPPED = "skipped"
STAGE_FAILED = "failed"
STAGE_RETRIED = "retried"

#: 运行结论
OUTCOME_PASSED = "passed"
OUTCOME_NEEDS_HUMAN = "needs_human"
OUTCOME_FAILED = "failed"
OUTCOME_ABORTED = "aborted"


@dataclass
class StageReport:
    """一个阶段的执行记录。这是"执行日志与状态追踪"要求落到数据结构上的样子。"""

    name: str
    node: str = ""
    round: int = 0
    status: str = STAGE_OK
    agent: str = ""
    summary: str = ""
    elapsed_seconds: float = 0.0
    attempts: int = 1
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "node": self.node,
            "round": self.round,
            "status": self.status,
            "agent": self.agent,
            "summary": self.summary,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "attempts": self.attempts,
            "error": self.error,
            "metrics": self.metrics,
            "artifacts": self.artifacts,
        }


@dataclass
class GateResult:
    """质量门禁结论。**每个字段都可解释**，不允许出现"pass/fail 但不知道为什么"。"""

    passed: bool = True
    blocking: tuple[str, ...] = DEFAULT_BLOCKING
    blocking_count: int = 0
    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    fixable: int = 0
    needs_human: int = 0
    score: int = 100
    overall_risk: str = "clean"
    reason: str = ""
    #: 本轮之后是否还值得再修一轮（没有可修的阻断问题时就不值得）
    can_improve: bool = False
    round: int = 0

    @classmethod
    def from_findings(
        cls,
        findings: list[FindingContract] | list[dict[str, Any]],
        *,
        blocking: tuple[str, ...] = DEFAULT_BLOCKING,
        round: int = 0,
        score: int = 100,
        overall_risk: str = "clean",
        can_improve: bool = True,
    ) -> "GateResult":
        contracts = [
            f if isinstance(f, FindingContract) else FindingContract.from_dict(f) for f in findings
        ]
        counts = count_by_severity(contracts)
        blocking_count = count_blocking(contracts, blocking)
        fixable_blocking = sum(
            1 for f in contracts if is_blocking(f.severity, blocking) and f.fixable
        )
        passed = blocking_count == 0
        if passed:
            reason = f"没有达到阻断级别（{'/'.join(blocking)}）的问题"
        elif fixable_blocking:
            reason = (
                f"存在 {blocking_count} 条阻断级问题，其中 {fixable_blocking} 条可自动修复"
            )
        else:
            reason = f"存在 {blocking_count} 条阻断级问题，且都没有可自动修复的策略，需人工介入"
        return cls(
            passed=passed,
            blocking=tuple(blocking),
            blocking_count=blocking_count,
            total=len(contracts),
            counts=counts,
            fixable=sum(1 for f in contracts if f.fixable),
            needs_human=sum(1 for f in contracts if not f.fixable),
            score=score,
            overall_risk=overall_risk,
            reason=reason,
            can_improve=fixable_blocking > 0,
            round=round,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocking": list(self.blocking),
            "blocking_count": self.blocking_count,
            "total": self.total,
            "counts": dict(self.counts),
            "fixable": self.fixable,
            "needs_human": self.needs_human,
            "score": self.score,
            "overall_risk": self.overall_risk,
            "reason": self.reason,
            "can_improve": self.can_improve,
            "round": self.round,
        }


@dataclass
class RoundSummary:
    """一轮"审查 → 修复"的轨迹，用来在报告里画收敛曲线。"""

    round: int
    review_summary: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    blocking: int = 0
    score: int = 100
    passed: bool = False
    fixed: int = 0
    rejected: int = 0
    deferred: int = 0
    files_changed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "review_summary": self.review_summary,
            "counts": dict(self.counts),
            "blocking": self.blocking,
            "score": self.score,
            "passed": self.passed,
            "fixed": self.fixed,
            "rejected": self.rejected,
            "deferred": self.deferred,
            "files_changed": list(self.files_changed),
        }


@dataclass
class RunOutcome:
    """一次完整运行的汇总。CLI、报告、退出码都从这里取。"""

    run_id: str = ""
    outcome: str = OUTCOME_FAILED
    rounds: list[RoundSummary] = field(default_factory=list)
    stages: list[StageReport] = field(default_factory=list)
    gate: GateResult | None = None
    spec_path: str = ""
    workdir: str = ""
    report_dir: str = ""
    elapsed_seconds: float = 0.0
    #: 最终代码里的残余发现（含非阻断级）
    residual: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def total_fixed(self) -> int:
        return sum(r.fixed for r in self.rounds)

    @property
    def total_rounds(self) -> int:
        return len(self.rounds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "rounds": [r.to_dict() for r in self.rounds],
            "stages": [s.to_dict() for s in self.stages],
            "gate": self.gate.to_dict() if self.gate else None,
            "spec_path": self.spec_path,
            "workdir": self.workdir,
            "report_dir": self.report_dir,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "total_fixed": self.total_fixed,
            "residual": self.residual,
            "open_questions": self.open_questions,
            "diagnostics": self.diagnostics,
            "error": self.error,
        }


__all__ = [
    "GateResult",
    "OUTCOME_ABORTED",
    "OUTCOME_FAILED",
    "OUTCOME_NEEDS_HUMAN",
    "OUTCOME_PASSED",
    "RoundSummary",
    "RunOutcome",
    "STAGE_FAILED",
    "STAGE_OK",
    "STAGE_RETRIED",
    "STAGE_SKIPPED",
    "StageReport",
]
