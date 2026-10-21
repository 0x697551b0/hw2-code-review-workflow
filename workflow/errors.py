"""编排器异常体系。

按"谁来负责"分类，而不是按"哪一层抛的"：

- :class:`WorkflowConfigError` / :class:`AgentNotFound` —— **环境问题**，用户要装/要配；
- :class:`NodeFailed` —— **单节点失败**，编排器按错误策略处理（重试 / 降级 / 中止）；
- :class:`CheckpointError` —— **存档问题**，影响 resume；
- :class:`AbortWorkflow` —— 由节点主动中止整个流程（例如编码阶段失败且无法降级）。

把"环境问题"和"运行失败"分开，是因为它们的处置完全不同：前者重试一万次也没用。
"""

from __future__ import annotations


class WorkflowError(Exception):
    """编排器所有异常的基类。"""


class WorkflowConfigError(WorkflowError):
    """配置不合法或缺少必要参数。"""


class AgentNotFound(WorkflowError):
    """找不到某个 Agent 的实现包。"""


class NodeFailed(WorkflowError):
    """节点执行失败。``node`` 与 ``retryable`` 决定编排器怎么处理。"""

    def __init__(self, message: str, *, node: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.node = node
        self.retryable = retryable


class NodeTimeout(NodeFailed):
    """节点超时。"""


class CheckpointError(WorkflowError):
    """存档读写失败或存档与当前流程不兼容。"""


class AbortWorkflow(WorkflowError):
    """节点主动要求中止整个流程（不可重试）。"""
