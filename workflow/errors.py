"""编排器异常体系。

按"谁来负责"分，不按"哪一层抛的"分：:class:`WorkflowConfigError` 和
:class:`AgentNotFound` 是环境问题，用户得装或得配；:class:`NodeFailed` 是单节点失败，
编排器按错误策略重试或降级；:class:`CheckpointError` 影响 resume；由
:class:`AbortWorkflow` 主动叫停。环境问题重试一万次也没用，所以要和运行失败分开。
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
