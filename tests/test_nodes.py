"""流程控制节点（nodes.py）的测试。

顺序、条件分支、循环、并行四种都覆盖了。另外专门测 resume：跳过已完成的节点是断点续跑
唯一的行为约定，必须由基类的 ``execute`` 统一实现，不能指望每个节点自觉。
"""

from __future__ import annotations

from workflow.bus import MessageBus
from workflow.config import WorkflowConfig
from workflow.context import RunContext
from workflow.errors import AbortWorkflow, NodeFailed
from workflow.events import EventLog, ListenerBuffer
from workflow.models import RunOutcome
from workflow.nodes import (
    AbortNode,
    BranchNode,
    FunctionalNode,
    LoopNode,
    NodeResult,
    ParallelNode,
    SequenceNode,
    collect_nodes,
)
from workflow.state import SharedState

from tests.support import FakeRegistry, tempdir


def _ctx(tmp, *, resumable: bool = False, handles=None) -> RunContext:
    config = WorkflowConfig(spec_path="x.md", output_root=str(tmp))
    state = SharedState("r1")
    state.bind(tmp / "state.json")
    events = EventLog()
    events.add_listener(ListenerBuffer())
    return RunContext(
        config=config,
        state=state,
        bus=MessageBus(events=events, run_id="r1"),
        events=events,
        registry=FakeRegistry(handles or {}),
        outcome=RunOutcome(run_id="r1"),
        run_id="r1",
        resumable=resumable,
    )


def test_sequence_runs_in_order() -> None:
    order: list[str] = []
    node = SequenceNode(
        "seq",
        [
            FunctionalNode("a", lambda ctx: order.append("a") or NodeResult.ok_()),
            FunctionalNode("b", lambda ctx: order.append("b") or NodeResult.ok_()),
        ],
    )
    with tempdir() as tmp:
        result = node.execute(_ctx(tmp))
    assert result.ok
    assert order == ["a", "b"]


def test_sequence_aborts_on_failure() -> None:
    def boom(ctx):
        raise NodeFailed("炸了", node="bad")

    node = SequenceNode("seq", [FunctionalNode("bad", boom), FunctionalNode("after", lambda ctx: NodeResult.ok_())])
    with tempdir() as tmp:
        try:
            node.execute(_ctx(tmp))
        except NodeFailed as exc:
            assert "炸了" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("应当向上抛出 NodeFailed")


def test_sequence_continue_policy() -> None:
    def boom(ctx):
        raise NodeFailed("炸了", node="bad")

    node = SequenceNode(
        "seq",
        [FunctionalNode("bad", boom), FunctionalNode("after", lambda ctx: NodeResult.ok_("ok"))],
        on_error="continue",
    )
    with tempdir() as tmp:
        result = node.execute(_ctx(tmp))
    assert result.ok


def test_branch_takes_then_and_else() -> None:
    def make(decided: bool) -> BranchNode:
        return BranchNode(
            "branch",
            condition=lambda ctx: decided,
            then_node=FunctionalNode("then", lambda ctx: NodeResult.ok_("Then")),
            else_node=FunctionalNode("else", lambda ctx: NodeResult.ok_("Else")),
        )

    with tempdir() as tmp:
        ctx = _ctx(tmp)
        assert make(True).execute(ctx.child("1")).outputs["branch_taken"] == "then"
        assert make(False).execute(ctx.child("2")).outputs["branch_taken"] == "else"


def test_branch_without_else_is_skipped() -> None:
    branch = BranchNode("branch", condition=lambda ctx: False, then_node=FunctionalNode("t", lambda ctx: NodeResult.ok_()))
    with tempdir() as tmp:
        result = branch.execute(_ctx(tmp))
    assert result.skipped


def test_branch_condition_is_read_only_repeatable() -> None:
    """条件函数必须只读：重复求值结果一致，否则 resume 不可靠。"""
    calls: list[bool] = []

    def condition(ctx) -> bool:
        value = bool(ctx.state.get_path("gate.passed", False))
        calls.append(value)
        return value

    branch = BranchNode("b", condition=condition, then_node=FunctionalNode("t", lambda ctx: NodeResult.ok_()))
    with tempdir() as tmp:
        ctx = _ctx(tmp)
        ctx.state.set_path("gate.passed", True)
        branch.execute(ctx.child("1"))
        branch.execute(ctx.child("2"))
    assert calls == [True, True]


def test_loop_until_converged() -> None:
    def tick(ctx) -> NodeResult:
        counter = ctx.state.get("counter", 0) + 1
        ctx.state["counter"] = counter
        return NodeResult.ok_(f"第 {counter} 次")

    loop = LoopNode("loop", FunctionalNode("tick", tick), max_rounds=5, until=lambda ctx: ctx.state.get("counter", 0) >= 2)
    with tempdir() as tmp:
        result = loop.execute(_ctx(tmp))
    assert result.outputs["stop_reason"] == "converged"
    assert result.outputs["iterations"] == 2


def test_loop_stops_at_max_rounds() -> None:
    loop = LoopNode("loop", FunctionalNode("tick", lambda ctx: NodeResult.ok_()), max_rounds=3)
    with tempdir() as tmp:
        result = loop.execute(_ctx(tmp))
    assert result.outputs["stop_reason"] == "max_rounds"
    assert result.outputs["iterations"] == 3


def test_loop_resumes_from_cursor() -> None:
    """断点续跑时，循环应该从存档里的游标往后走，不是从头再来。

    跑到第 2 轮崩了，恢复之后不该把第 1 轮（已经改过文件的那一轮）再重跑一遍，这才是
    阶段存档的用处。
    """
    ran: list[int] = []

    def body_action(ctx) -> NodeResult:
        ran.append(ctx.round)
        return NodeResult.ok_(f"第 {ctx.round} 轮")

    loop = LoopNode(
        "loop",
        FunctionalNode("body", body_action),
        max_rounds=4,
        until=lambda ctx: len(ran) >= 2,
    )
    with tempdir() as tmp:
        ctx = _ctx(tmp).child("loop")
        ctx.state.set_cursor("loop", 2)  # 假装前两轮已完成
        ctx.state["current_round"] = 2
        loop.run(ctx)
    assert ran == [3, 4], f"应当从第 3 轮继续，实际执行了 {ran}"


def test_loop_rejects_bad_max_rounds() -> None:
    from tests.support import expect_raises

    expect_raises(ValueError, LoopNode, "loop", FunctionalNode("x", lambda ctx: NodeResult.ok_()), max_rounds=0)


def test_parallel_merges_in_definition_order() -> None:
    node = ParallelNode(
        "fan",
        [
            FunctionalNode("a", lambda ctx: NodeResult.ok_("A", value="a")),
            FunctionalNode("b", lambda ctx: NodeResult.ok_("B", value="b")),
            FunctionalNode("c", lambda ctx: NodeResult.ok_("C", value="c")),
        ],
        max_workers=3,
    )
    with tempdir() as tmp:
        result = node.execute(_ctx(tmp))
    names = [shard["name"] for shard in result.outputs["shards"]]
    assert names == ["a", "b", "c"], "并行结果必须按定义顺序合并"


def test_parallel_reports_failures() -> None:
    def boom(ctx):
        raise NodeFailed("分片炸了", node="b")

    node = ParallelNode("fan", [FunctionalNode("ok", lambda ctx: NodeResult.ok_()), FunctionalNode("bad", boom)])
    with tempdir() as tmp:
        result = node.execute(_ctx(tmp))
    assert not result.ok
    assert "分片" in result.error


def test_abort_node_raises() -> None:
    with tempdir() as tmp:
        try:
            AbortNode("stop", "没有需求文件").execute(_ctx(tmp))
        except AbortWorkflow as exc:
            assert "没有需求文件" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("应当抛 AbortWorkflow")


def test_resume_skips_completed_nodes() -> None:
    """核心行为：已完成节点在 resume 模式下必须被跳过。"""
    ran: list[str] = []
    node = SequenceNode(
        "seq",
        [
            FunctionalNode("prepare", lambda ctx: ran.append("prepare") or NodeResult.ok_()),
            FunctionalNode("coding", lambda ctx: ran.append("coding") or NodeResult.ok_()),
        ],
    )
    with tempdir() as tmp:
        node.execute(_ctx(tmp))
        assert ran == ["prepare", "coding"]

        # 模拟"进程重启 + 从存档恢复"：同一个 run 的状态里已经有两个子节点的完成标记
        ctx2 = _ctx(tmp, resumable=True)
        ctx2.state.mark_done("0:prepare")
        ctx2.state.mark_done("1:coding")
        result = node.execute(ctx2)
    assert ran == ["prepare", "coding"], "resume 模式下不应重复执行已完成的子节点"
    assert result.ok


def test_resume_skip_mechanism_directly() -> None:
    """更直接的 resume 断言：把限定名预先标成完成，节点就不该再执行。"""
    calls: list[int] = []

    def action(ctx) -> NodeResult:
        calls.append(1)
        return NodeResult.ok_()

    node = FunctionalNode("work", action)
    with tempdir() as tmp:
        ctx = _ctx(tmp, resumable=True).child("0:work")
        ctx.state.mark_done(ctx.path)
        result = node.execute(ctx)
    assert result.skipped
    assert calls == []


def test_completed_marked_after_success() -> None:
    node = FunctionalNode("work", lambda ctx: NodeResult.ok_())
    with tempdir() as tmp:
        ctx = _ctx(tmp).child("0:work")
        node.execute(ctx)
        assert ctx.state.is_done("0:work")


def test_failure_is_not_marked_completed() -> None:
    def boom(ctx):
        raise NodeFailed("炸了", node="work")

    node = FunctionalNode("work", boom)
    with tempdir() as tmp:
        ctx = _ctx(tmp).child("0:work")
        try:
            node.execute(ctx)
        except NodeFailed:
            pass
    assert not ctx.state.is_done("0:work"), "失败的节点不能标记为完成，否则 resume 会跳过它"


def test_collect_nodes_walks_tree() -> None:
    tree = SequenceNode(
        "root",
        [
            BranchNode("b", condition=lambda ctx: True, then_node=FunctionalNode("t", lambda ctx: NodeResult.ok_())),
            LoopNode("l", FunctionalNode("body", lambda ctx: NodeResult.ok_()), max_rounds=2),
        ],
    )
    names = {node.name for node in collect_nodes(tree)}
    assert {"root", "b", "t", "l", "body"} <= names, names


def test_describe_shape() -> None:
    tree = SequenceNode("seq", [FunctionalNode("a", lambda ctx: NodeResult.ok_())])
    desc = tree.describe()
    assert desc["type"] == "sequence"
    assert desc["children"][0]["name"] == "a"
