"""
Agent 契约：Workflow 编排器与各 Agent 之间**唯一**的通信边界。

为什么把契约定义成一个"信封"（envelope）：

1. **零耦合**：三个 Agent 各自是一个独立仓库、独立包，编排器不应该 import 它们的
   内部类型。双方只共享这一个 JSON 信封，一方换实现（甚至换成别的语言写的进程）
   另一方不用改。
2. **可序列化**：同一份信封既能走函数调用（进程内），也能走 stdin/stdout
   （跨进程），还能落盘成为运行存档（checkpoint）。用 dataclass 承载只是为了
   校验与可读性，真正的传输形态永远是 dict/JSON。
3. **可演进**：所有字段都有默认值，未知字段透传保留，因此新版 Agent 增加字段不会
   让旧编排器崩掉。

这个文件在三个 Agent 仓库与编排器仓库中各有一份**等价副本**。这是刻意的重复：
四个仓库必须能单独 clone、单独跑通，不能为了让四次 import 复用这 150 行而互相
依赖。契约的权威定义写在编排器仓库的 `docs/CONTRACT.md`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: 契约版本。破坏性变更时必须递增，编排器会校验主版本号一致。
CONTRACT_VERSION = "1.0"

#: 严重级别，从重到轻。与 codeguard 的 Severity 保持一致。
SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "info")

#: 默认阻断级别：出现这些级别的问题时，质量门禁判定为不通过。
DEFAULT_BLOCKING: tuple[str, ...] = ("critical", "high")

#: Agent 状态
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def severity_rank(severity: str) -> int:
    """把严重级别映射为排序权重（越小越严重）；未知级别排到最后。"""
    try:
        return SEVERITY_ORDER.index(str(severity).strip().lower())
    except ValueError:
        return len(SEVERITY_ORDER)


def is_blocking(severity: str, blocking: Iterable[str] = DEFAULT_BLOCKING) -> bool:
    """判断某级别是否属于阻断级别。"""
    return str(severity).strip().lower() in {str(b).strip().lower() for b in blocking}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


@dataclass
class Artifact:
    """Agent 产出的一份可寻址产物（文件、报告等）。"""

    kind: str
    path: str
    media_type: str = "text/plain"
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "media_type": self.media_type,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Artifact":
        return cls(
            kind=str(data.get("kind") or "file"),
            path=str(data.get("path") or ""),
            media_type=str(data.get("media_type") or "text/plain"),
            description=str(data.get("description") or ""),
        )


@dataclass
class AgentRequest:
    """
    编排器 → Agent 的一次任务请求。

    payload 是 Agent 私有的业务参数，编排器不解释它；shared 是**只读**的共享状态
    快照，让 Agent 了解上下文（例如修复 Agent 需要知道这是第几轮、上一轮改了什么）
    但不必自己维护全局状态。
    """

    agent: str
    task_id: str
    run_id: str = ""
    round: int = 1
    objective: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    shared: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION

    def get(self, key: str, default: Any = None) -> Any:
        """便捷读取 payload。"""
        return self.payload.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "agent": self.agent,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "round": self.round,
            "objective": self.objective,
            "payload": self.payload,
            "shared": self.shared,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentRequest":
        if not isinstance(data, Mapping):
            raise ValueError("AgentRequest 必须是 JSON 对象")
        return cls(
            agent=str(data.get("agent") or ""),
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            round=int(data.get("round") or 1),
            objective=str(data.get("objective") or ""),
            payload=dict(data.get("payload") or {}),
            shared=dict(data.get("shared") or {}),
            metadata=dict(data.get("metadata") or {}),
        )

    def validate(self, *, expected_agent: str = "") -> list[str]:
        """返回问题清单（空表示合法）。Agent 在入口处调用，尽早给出可读错误。"""
        problems: list[str] = []
        if not self.agent:
            problems.append("缺少 agent 字段")
        elif expected_agent and self.agent != expected_agent:
            problems.append(f"agent 字段应为 {expected_agent!r}，实际为 {self.agent!r}")
        if not self.task_id:
            problems.append("缺少 task_id 字段")
        if not isinstance(self.payload, dict):
            problems.append("payload 必须是 JSON 对象")
        major, _, _ = str(self.contract_version).partition(".")
        expected_major, _, _ = CONTRACT_VERSION.partition(".")
        if major != expected_major:
            problems.append(
                f"契约主版本不兼容：请求为 {self.contract_version}，本 Agent 为 {CONTRACT_VERSION}"
            )
        return problems


@dataclass
class AgentResponse:
    """Agent → 编排器的一次任务结果。status 为 failed 时 error 必须非空。"""

    agent: str
    status: str = STATUS_OK
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def add_artifact(self, kind: str, path: str, *, media_type: str = "text/plain", description: str = "") -> Artifact:
        artifact = Artifact(kind=kind, path=path, media_type=media_type, description=description)
        self.artifacts.append(artifact)
        return artifact

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status,
            "summary": self.summary,
            "payload": self.payload,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "metrics": self.metrics,
            "diagnostics": self.diagnostics,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentResponse":
        if not isinstance(data, Mapping):
            raise ValueError("AgentResponse 必须是 JSON 对象")
        artifacts = [Artifact.from_dict(a) for a in data.get("artifacts") or [] if isinstance(a, Mapping)]
        return cls(
            agent=str(data.get("agent") or ""),
            status=str(data.get("status") or STATUS_OK),
            summary=str(data.get("summary") or ""),
            payload=dict(data.get("payload") or {}),
            artifacts=artifacts,
            metrics=dict(data.get("metrics") or {}),
            diagnostics=dict(data.get("diagnostics") or {}),
            error=str(data.get("error") or ""),
        )

    @classmethod
    def failure(cls, agent: str, error: str, *, summary: str = "") -> "AgentResponse":
        """构造一个失败结果，保证 error 与 status 一致。"""
        return cls(
            agent=agent,
            status=STATUS_FAILED,
            summary=summary or "执行失败",
            error=error or "未提供错误信息",
        )


@dataclass
class FindingContract:
    """
    一条审查发现。**三个 Agent 共用**同一种发现结构：

    - 审查 Agent 生产它；
    - 修复 Agent 消费它，并把 ``status`` 更新为 fixed/deferred/rejected；
    - 编排器用它做质量门禁与报告。

    共享结构而不是让两边各自定义，是为了让"发现 → 修复 → 复验"可以按
    ``finding_id`` 精确闭环。
    """

    finding_id: str
    rule_id: str = ""
    severity: str = "medium"
    category: str = "general"
    file: str = ""
    line: int | None = None
    title: str = ""
    evidence: str = ""
    explanation: str = ""
    suggestion: str = ""
    source: str = "static"
    fixable: bool = False
    confidence: float | None = None
    status: str = "open"
    note: str = ""

    def __post_init__(self) -> None:
        self.severity = str(self.severity or "medium").strip().lower()
        if self.line is not None:
            try:
                value = int(self.line)
                self.line = value if value > 0 else None
            except (TypeError, ValueError):
                self.line = None

    @property
    def key(self) -> tuple[str, int | None, str]:
        """用于跨轮次匹配"同一个问题"的稳定键。"""
        return (self.file, self.line, self.rule_id or self.title)

    @property
    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or "(未知位置)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "title": self.title,
            "evidence": self.evidence,
            "explanation": self.explanation,
            "suggestion": self.suggestion,
            "source": self.source,
            "fixable": self.fixable,
            "confidence": self.confidence,
            "status": self.status,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FindingContract":
        return cls(
            finding_id=str(data.get("finding_id") or ""),
            rule_id=str(data.get("rule_id") or ""),
            severity=str(data.get("severity") or "medium"),
            category=str(data.get("category") or "general"),
            file=str(data.get("file") or ""),
            line=data.get("line"),
            title=str(data.get("title") or ""),
            evidence=str(data.get("evidence") or ""),
            explanation=str(data.get("explanation") or ""),
            suggestion=str(data.get("suggestion") or ""),
            source=str(data.get("source") or "static"),
            fixable=bool(data.get("fixable")),
            confidence=data.get("confidence"),
            status=str(data.get("status") or "open"),
            note=str(data.get("note") or ""),
        )


def count_by_severity(findings: Iterable[FindingContract]) -> dict[str, int]:
    """按级别统计，五个级别键始终存在（值为 0），便于前端直接渲染。"""
    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def count_blocking(findings: Iterable[FindingContract], blocking: Iterable[str] = DEFAULT_BLOCKING) -> int:
    """统计阻断级问题数量。"""
    return sum(1 for f in findings if is_blocking(f.severity, blocking))


def dumps(data: Any, *, indent: int = 2) -> str:
    """统一的 JSON 序列化：不转义中文，便于人读与 diff。"""
    return json.dumps(data, ensure_ascii=False, indent=indent)


__all__ = [
    "Artifact",
    "AgentRequest",
    "AgentResponse",
    "CONTRACT_VERSION",
    "DEFAULT_BLOCKING",
    "FindingContract",
    "SEVERITY_ORDER",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "count_blocking",
    "count_by_severity",
    "dumps",
    "is_blocking",
    "severity_rank",
]
