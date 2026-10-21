"""流程控制节点。

Sequence / Branch / Loop / Parallel 四种，可以嵌套成一棵树。
断点续跑、失败处理这些公共逻辑统一在 Node.execute 里，
子类只管实现自己的 run()。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from workflow.context import RunContext
from workflow.errors import AbortWorkflow, NodeFailed
from workflow.events import EVENT_NODE_END, EVENT_NODE_SKIP

#: 节点结果状态
RESULT_OK = "ok"
RESULT_SKIPPED = "skipped"
RESULT_FAILED = "failed"


@dataclass
class NodeResult:
    """节点执行的返回值。"""

    status: str = RESULT_OK
    outputs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == RESULT_OK

    @property
    def skipped(self) -> bool:
        return self.status == RESULT_SKIPPED

    @classmethod
    def ok_(cls, summary: str = "", **outputs: Any) -> "NodeResult":
        return cls(status=RESULT_OK, summary=summary, outputs=outputs)

    @classmethod
    def skip(cls, reason: str = "") -> "NodeResult":
        return cls(status=RESULT_SKIPPED, summary=reason)

    @classmethod
    def fail(cls, error: str) -> "NodeResult":
        return cls(status=RESULT_FAILED, error=error)


#: 分支条件：接收上下文，返回布尔
Condition = Callable[[RunContext], bool]


class Node(ABC):
    """所有节点/阶段的基类。"""

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def run(self, ctx: RunContext) -> NodeResult:
        """执行节点。实现内部不要关心 resume，交给 :meth:`execute`。"""

    # 模板方法
    def execute(self, ctx: RunContext) -> NodeResult:
        """执行外壳：跳过判定 → 执行 → 记完成 → 存档。

        搁在基类是因为 resume 全靠它，漏掉一处标记就会出现"跳过了但状态没写"的错误。
        """
        qid = ctx.path or self.name
        if ctx.resumable and ctx.state.is_done(qid):
            ctx.events.emit(
                EVENT_NODE_SKIP,
                f"已完成，跳过（resume）：{_label(ctx, self)}",
                node=ctx.path,
                round=ctx.round,
                resumed=True,
            )
            return NodeResult.skip("resume：该节点已完成")

        result = self.run(ctx)
        if result.ok or result.skipped:
            ctx.state.mark_done(qid)
            ctx.checkpoint()
        ctx.events.emit(
            EVENT_NODE_END,
            f"{_label(ctx, self)} → {result.status}"
            + (f"：{result.summary}" if result.summary else ""),
            node=ctx.path,
            round=ctx.round,
            status=result.status,
        )
        return result

    def describe(self) -> dict[str, Any]:
        """流程图的节点描述（报告里的可视化用）。"""
        return {"type": type(self).__name__, "name": self.name}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name!r}>"


def _label(ctx: RunContext, node: "Node") -> str:
    """节点的展示名：取限定名最后一段；根节点的路径为空，退回节点自己的名字。"""
    return ctx.path.rsplit("/", 1)[-1] or node.name


class SequenceNode(Node):
    """顺序执行若干子节点。"""

    def __init__(self, name: str, children: Sequence[Node], *, on_error: str = "abort") -> None:
        super().__init__(name)
        if not children:
            raise ValueError("SequenceNode 至少需要一个子节点")
        if on_error not in {"abort", "continue"}:
            raise ValueError(f"on_error 只能取 abort/continue，当前为 {on_error!r}")
        self.children = list(children)
        self.on_error = on_error

    def run(self, ctx: RunContext) -> NodeResult:
        outputs: dict[str, Any] = {}
        for index, child in enumerate(self.children):
            child_ctx = ctx.child(f"{index}:{child.name}")
            try:
                result = child.execute(child_ctx)
            except NodeFailed as exc:
                if self.on_error == "abort" or index == len(self.children) - 1:
                    raise
                ctx.error(
                    "node.failed",
                    f"子节点 {child.name} 失败，按 on_error=continue 继续：{exc}",
                    child=child.name,
                )
                continue
            outputs[child.name] = result.outputs
        return NodeResult.ok_(f"{len(self.children)} 个子节点完成", **outputs)

    def describe(self) -> dict[str, Any]:
        return {
            "type": "sequence",
            "name": self.name,
            "children": [child.describe() for child in self.children],
        }


class BranchNode(Node):
    """条件分支：``condition(ctx)`` 为真走 ``then``，否则走 ``else_``。

    条件函数只能读共享状态。要是它两次调用给出不同结果，resume 重放分支就会走到另一边去。
    """

    def __init__(
        self,
        name: str,
        condition: Condition,
        then_node: Node,
        else_node: Node | None = None,
        *,
        description: str = "",
    ) -> None:
        super().__init__(name)
        self.condition = condition
        self.then_node = then_node
        self.else_node = else_node
        self.branch_description = description

    def run(self, ctx: RunContext) -> NodeResult:
        decided = bool(self.condition(ctx))
        chosen = self.then_node if decided else self.else_node
        ctx.debug(
            "node.branch",
            f"分支判定 {'then' if decided else 'else'}：{self.branch_description or self.name}",
            decided=decided,
        )
        if chosen is None:
            return NodeResult.skip("条件不满足且没有 else 分支")
        result = chosen.execute(ctx.child(f"{'then' if decided else 'else'}:{chosen.name}"))
        result.outputs.setdefault("branch_taken", "then" if decided else "else")
        return result

    def describe(self) -> dict[str, Any]:
        return {
            "type": "branch",
            "name": self.name,
            "condition": self.branch_description or "condition",
            "then": self.then_node.describe(),
            "else": self.else_node.describe() if self.else_node else None,
        }


class LoopNode(Node):
    """有上限的循环，直到 ``until(ctx)`` 为真或者跑满 ``max_rounds``。

    跑满上限不算异常，真实项目里也常有"能自动修的都修了，剩下的得人来判断"。所以退出
    原因会写进返回值，报告照实写。
    """

    def __init__(
        self,
        name: str,
        body: Node,
        *,
        max_rounds: int = 3,
        until: Condition | None = None,
        round_observer: Callable[[RunContext, int], None] | None = None,
    ) -> None:
        super().__init__(name)
        if max_rounds < 1:
            raise ValueError("max_rounds 至少为 1")
        self.body = body
        self.max_rounds = max_rounds
        self.until = until or (lambda ctx: False)
        self.round_observer = round_observer

    def run(self, ctx: RunContext) -> NodeResult:
        start = ctx.state.get_cursor(ctx.path) + 1
        if start > 1:
            ctx.debug("loop.resume", f"从第 {start} 轮继续")
        reason = "max_rounds"
        iterations = 0
        for iteration in range(start, self.max_rounds + 1):
            ctx.state.set_cursor(ctx.path, iteration - 1)
            ctx.round = iteration
            # 轮次也写进共享状态：循环之外的节点（收尾复审）同样要知道现在第几轮。
            # 光靠上下文传不行，循环结束回到父上下文时 round 又变回 0。
            ctx.state["current_round"] = iteration
            ctx.outcome.diagnostics["round"] = iteration
            body_ctx = ctx.child(f"#{iteration}")
            body_ctx.round = iteration
            result = self.body.execute(body_ctx)
            iterations = iteration - start + 1
            ctx.state.set_cursor(ctx.path, iteration)
            ctx.checkpoint(force=True)
            if self.round_observer is not None:
                self.round_observer(ctx, iteration)
            if self.until(ctx):
                reason = "converged"
                break
            del result  # 仅用于推进流程，逐轮结论由各阶段写进共享状态
        else:
            reason = "max_rounds"
        return NodeResult.ok_(
            f"循环 {iterations} 轮（{reason}）", iterations=iterations, stop_reason=reason
        )

    def describe(self) -> dict[str, Any]:
        return {
            "type": "loop",
            "name": self.name,
            "max_rounds": self.max_rounds,
            "body": self.body.describe(),
        }


class ParallelNode(Node):
    """并行执行无副作用的子节点，然后按定义顺序合并结果。

    并发完成的先后是不确定的，要是按完成顺序合并，同样的输入会产出不一样的报告，测试和
    diff 就没法比了。所以合并顺序跟子节点的定义顺序走。
    """

    def __init__(self, name: str, children: Sequence[Node], *, max_workers: int = 4) -> None:
        super().__init__(name)
        if not children:
            raise ValueError("ParallelNode 至少需要一个子节点")
        self.children = list(children)
        self.max_workers = max(1, max_workers)

    def run(self, ctx: RunContext) -> NodeResult:
        results: list[NodeResult | None] = [None] * len(self.children)

        def worker(index: int, child: Node) -> None:
            child_ctx = ctx.child(f"{index}:{child.name}")
            try:
                results[index] = child.execute(child_ctx)
            except NodeFailed as exc:
                results[index] = NodeResult.fail(str(exc))

        if len(self.children) == 1:
            worker(0, self.children[0])
        else:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(self.children))) as pool:
                futures = [
                    pool.submit(worker, index, child)
                    for index, child in enumerate(self.children)
                ]
                for future in futures:
                    future.result()

        failures = [r for r in results if r is not None and not r.ok and not r.skipped]
        merged: list[dict[str, Any]] = []
        for child, result in zip(self.children, results):
            if result is not None:
                merged.append({"name": child.name, "status": result.status, **result.outputs})
        if failures:
            return NodeResult.fail(
                f"{len(failures)}/{len(self.children)} 个并行分片失败：" + "；".join(f.error for f in failures)
            )
        return NodeResult.ok_(f"{len(self.children)} 个分片完成", shards=merged)

    def describe(self) -> dict[str, Any]:
        return {
            "type": "parallel",
            "name": self.name,
            "children": [child.describe() for child in self.children],
        }


class FunctionalNode(Node):
    """用函数实现的叶子节点，小步骤就不必单独建类了。

    ``action`` 可以改共享状态，返回 :class:`NodeResult` 或者字符串。
    """

    def __init__(self, name: str, action: Callable[[RunContext], NodeResult | str], *, summary: str = "") -> None:
        super().__init__(name)
        self.action = action
        self.summary = summary

    def run(self, ctx: RunContext) -> NodeResult:
        outcome = self.action(ctx)
        if isinstance(outcome, NodeResult):
            return outcome
        return NodeResult.ok_(str(outcome) or self.summary)

    def describe(self) -> dict[str, Any]:
        return {"type": "functional", "name": self.name}


class AbortNode(Node):
    """主动中止流程（例如前置条件不满足且无法降级）。"""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(name)
        self.message = message

    def run(self, ctx: RunContext) -> NodeResult:
        raise AbortWorkflow(self.message)


def collect_nodes(node: Node) -> list[Node]:
    """深度优先列出流程树中的所有节点（报告里画图用）。"""
    found: list[Node] = [node]
    for attr in ("children",):
        for child in getattr(node, attr, []) or []:
            found.extend(collect_nodes(child))
    for attr in ("body", "then_node", "else_node"):
        child = getattr(node, attr, None)
        if isinstance(child, Node):
            found.extend(collect_nodes(child))
    return found


__all__ = [
    "AbortNode",
    "BranchNode",
    "Condition",
    "FunctionalNode",
    "LoopNode",
    "Node",
    "NodeResult",
    "ParallelNode",
    "RESULT_FAILED",
    "RESULT_OK",
    "RESULT_SKIPPED",
    "SequenceNode",
    "collect_nodes",
]
