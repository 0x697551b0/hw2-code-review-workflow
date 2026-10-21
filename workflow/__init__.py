"""多 Agent 代码审查工作流：编码 → 审查 → 修复。

三个 Agent 各在独立仓库里，本仓库只负责编排：

- 编码 Agent：<https://github.com/0x697551b0/hw2-coding-agent>
- 审查 Agent：<https://github.com/0x697551b0/hw2-review-agent>（复用 Homework 1 的 codeguard）
- 修复 Agent：<https://github.com/0x697551b0/hw2-fix-agent>

    from workflow import run_workflow, WorkflowConfig

    outcome = run_workflow(WorkflowConfig(spec_path="demo/spec_user_store.md"))
    print(outcome.outcome, outcome.total_fixed)
"""

from workflow.bus import Message, MessageBus
from workflow.config import WorkflowConfig
from workflow.context import RunContext, new_run_id
from workflow.errors import (
    AbortWorkflow,
    AgentNotFound,
    CheckpointError,
    NodeFailed,
    WorkflowConfigError,
    WorkflowError,
)
from workflow.events import ConsoleListener, Event, EventLog, read_events
from workflow.models import GateResult, RoundSummary, RunOutcome, StageReport
from workflow.nodes import (
    BranchNode,
    FunctionalNode,
    LoopNode,
    Node,
    NodeResult,
    ParallelNode,
    SequenceNode,
)
from workflow.protocol import AgentRequest, AgentResponse, Artifact, FindingContract
from workflow.registry import AgentRegistry
from workflow.runner import Orchestrator, run_workflow
from workflow.state import SharedState
from workflow.stages import build_pipeline, merge_reviews

__version__ = "1.0.0"

__all__ = [
    "AbortWorkflow",
    "AgentNotFound",
    "AgentRegistry",
    "AgentRequest",
    "AgentResponse",
    "Artifact",
    "BranchNode",
    "CheckpointError",
    "ConsoleListener",
    "Event",
    "EventLog",
    "FindingContract",
    "FunctionalNode",
    "GateResult",
    "LoopNode",
    "Message",
    "MessageBus",
    "Node",
    "NodeFailed",
    "NodeResult",
    "Orchestrator",
    "ParallelNode",
    "RoundSummary",
    "RunContext",
    "RunOutcome",
    "SequenceNode",
    "SharedState",
    "StageReport",
    "WorkflowConfig",
    "WorkflowConfigError",
    "WorkflowError",
    "__version__",
    "build_pipeline",
    "merge_reviews",
    "new_run_id",
    "read_events",
    "run_workflow",
]
