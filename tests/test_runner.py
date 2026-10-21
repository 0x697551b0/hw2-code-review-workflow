"""编排器（runner.py）的测试 —— 用伪 Agent 驱动，不依赖那三个真实仓库。

为什么必须这么做：如果编排测试直接依赖真实 Agent，那么"编排逻辑对不对"这个问题
就会被"隔壁仓库今天改了什么"污染。测试应当能精确定位失败原因，而不是一片红。

真实三件套的协同在 ``test_e2e.py`` 里单独测，并在缺失时明确跳过。
"""

from __future__ import annotations

from pathlib import Path

from workflow.config import WorkflowConfig
from workflow.errors import AgentNotFound, CheckpointError
from workflow.protocol import AgentResponse
from workflow.runner import Orchestrator, run_workflow

from tests.support import expect_raises, finding, scripted_registry, tempdir, write

SPEC = "# 演示\n\n## 模块\n`demo`\n\n## 功能\n- [R1] 做点什么：`do_thing(cursor, user_id)`\n"


def _config(tmp: Path, **overrides) -> WorkflowConfig:
    spec_path = write(tmp / "spec.md", SPEC)
    config = WorkflowConfig(
        spec_path=str(spec_path),
        output_root=str(tmp / "runs"),
        max_rounds=2,
        trace=False,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_happy_path_converges() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        registry = scripted_registry(
            findings_per_round=[[finding("CG106", "high", 10)], []]
        )
        orchestrator = Orchestrator(config, registry=registry)
        outcome = orchestrator.run()
    assert outcome.outcome == "passed", (outcome.outcome, outcome.error)
    assert outcome.total_rounds == 2, [r.round for r in outcome.rounds]
    assert outcome.rounds[0].blocking == 1
    assert outcome.rounds[1].blocking == 0
    assert outcome.total_fixed == 3


def test_reports_are_written() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        orchestrator = Orchestrator(config, registry=scripted_registry())
        orchestrator.run()
        run_dir = orchestrator.run_dir
        for name in ("report.md", "report.html", "run.json", "events.jsonl", "state.json"):
            assert (run_dir / name).is_file(), name


def test_branch_skips_fix_when_gate_passes() -> None:
    """第二轮门禁已通过时，修复阶段应被跳过（条件分支生效）。"""
    with tempdir() as tmp:
        config = _config(tmp, max_rounds=3)
        registry = scripted_registry(findings_per_round=[[], []])
        orchestrator = Orchestrator(config, registry=registry)
        outcome = orchestrator.run()
    fix_calls = [r for r in orchestrator.registry.handles["fix"].requests]
    assert fix_calls == [], "门禁通过时不应调用修复 Agent"
    assert outcome.outcome == "passed"


def test_loop_stops_when_nothing_fixable() -> None:
    """阻断级问题全都不可自动修时，不该无谓地再跑一轮。"""
    with tempdir() as tmp:
        config = _config(tmp, max_rounds=5)
        registry = scripted_registry(
            findings_per_round=[[finding("CG301", "high", 10, fixable=False)]]
        )
        orchestrator = Orchestrator(config, registry=registry)
        outcome = orchestrator.run()
    assert outcome.outcome == "needs_human"
    assert outcome.total_rounds == 1, [r.round for r in outcome.rounds]


def test_max_rounds_is_respected() -> None:
    with tempdir() as tmp:
        config = _config(tmp, max_rounds=3)
        registry = scripted_registry(
            findings_per_round=[[finding("CG106", "high", 10)]]  # 永远不收敛
        )
        orchestrator = Orchestrator(config, registry=registry)
        outcome = orchestrator.run()
    assert outcome.total_rounds == 3, [r.round for r in outcome.rounds]
    assert outcome.outcome == "needs_human"


def test_missing_agent_fails_fast() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        registry = scripted_registry()
        del registry._inner.handles["fix"]
        registry._inner.missing["fix"] = "测试里故意删掉"
        orchestrator = Orchestrator(config, registry=registry)
        outcome = orchestrator.run()
    assert outcome.outcome == "failed"
    assert "fix" in outcome.error


def test_agent_failure_is_retried_then_reported() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        registry = scripted_registry()
        registry._inner.handles["coding"].settings.max_attempts = 2

        def always_fail(request, call):
            return AgentResponse.failure("coding", "模型超时")

        registry._inner.handles["coding"]._responses = always_fail
        orchestrator = Orchestrator(config, registry=registry)
        outcome = orchestrator.run()
    assert outcome.outcome == "failed"
    assert "模型超时" in outcome.error
    assert len(registry._inner.handles["coding"].requests) == 2, "应当重试一次"


def test_bus_transcript_shows_collaboration() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        orchestrator = Orchestrator(config, registry=scripted_registry())
        orchestrator.run()
        topics = [message["topic"] for message in orchestrator.bus.transcript()]
    for topic in ("code.ready", "review.completed", "gate.evaluated", "pipeline.done"):
        assert topic in topics, topics


def test_checkpoint_supports_resume() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        orchestrator = Orchestrator(config, registry=scripted_registry())
        orchestrator.run()
        run_dir = orchestrator.run_dir
        checkpoint = run_dir / "state.json"
        assert checkpoint.is_file()

        from workflow.state import SharedState

        restored = SharedState.load(checkpoint)
        assert restored.count_done() > 0
        assert restored.run_id == orchestrator.run_id

        # 用同一份存档再起一个编排器，验证 resume 分支能跑通且不重复产出
        config2 = _config(tmp)
        orchestrator2 = Orchestrator(config2, registry=scripted_registry(), run_id=orchestrator.run_id)
        assert orchestrator2.resume(checkpoint) is True
        assert orchestrator2.run_id == orchestrator.run_id
        assert str(orchestrator2.run_dir) == str(run_dir), "运行目录必须以存档所在目录为准"
        outcome2 = orchestrator2.run(resumable=True)
        assert outcome2.outcome in {"passed", "needs_human"}


def test_resume_missing_checkpoint_raises() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        orchestrator = Orchestrator(config, registry=scripted_registry())
        expect_raises(CheckpointError, orchestrator.resume, tmp / "nope.json")


def test_resume_preserves_observability() -> None:
    """
    回归测试：续跑必须保留**事件日志、消息历史、阶段记录**，且不新建运行目录。

    这三条曾经同时是坏的：`EventLog` 在 `__init__` 里就按新 run_id 建好了文件和目录，
    resume 只改了 `run_dir` 却没重绑它 —— 结果是"续跑的事件写进另一个空目录"，
    而总线消息与阶段记录压根不在存档里，续跑后报告的两节直接变空。对一个以"执行日志
    与状态追踪"为卖点的项目来说，这几乎等于把卖点弄没了。
    """
    with tempdir() as tmp:
        first = Orchestrator(_config(tmp), registry=scripted_registry())
        first.run()
        run_dir = first.run_dir
        events_before = (run_dir / "events.jsonl").read_text(encoding="utf-8").count("\n")
        stages_before = len(first.outcome.stages)
        messages_before = len(first.bus)
        assert events_before > 0 and stages_before > 0 and messages_before > 0

        second = Orchestrator(_config(tmp), registry=scripted_registry())
        assert second.resume(run_dir / "state.json") is True
        assert second.run_id == first.run_id
        assert len(second.stages) == stages_before, "阶段记录应当从存档恢复"
        assert len(second.bus) == messages_before, "消息历史应当从存档恢复"
        assert str(second.run_dir) == str(run_dir)

        second.run(resumable=True)

        run_dirs = [p for p in (tmp / "runs").iterdir() if p.is_dir()]
        assert len(run_dirs) == 1, f"续跑不该新建运行目录：{[p.name for p in run_dirs]}"
        events_after = (run_dir / "events.jsonl").read_text(encoding="utf-8").count("\n")
        assert events_after > events_before, "续跑的事件应当追加，而不是覆盖第一次的记录"

        report = (run_dir / "report.md").read_text(encoding="utf-8")
        assert "M0001" in report, "续跑后的报告必须仍包含消息表"
        assert "## 6. 阶段耗时" in report


def test_message_digest_counts_total_from_counts() -> None:
    """
    回归测试：消息摘要里的"命中 N 条"不能恒为 0。

    早期实现直接取 `body["total"]`，而 `review.completed` 的消息体里没有这个字段，
    于是报告第 5 节永远显示"命中 0 条"——分布和阻断数都是对的，只有这个总数错，
    这种"部分正确"的错误最容易被漏看。
    """
    from workflow.reporting import _message_digest

    with_total = _message_digest(
        "review.completed", {"counts": {"high": 2, "low": 1}, "total": 3, "blocking": 2}
    )
    assert "命中 3 条" in with_total

    without_total = _message_digest("review.completed", {"counts": {"high": 2}, "blocking": 2})
    assert "命中 2 条" in without_total, "缺少 total 时应由 counts 求和兜底"


def test_missing_spec_aborts() -> None:
    with tempdir() as tmp:
        config = WorkflowConfig(spec_path=str(tmp / "nope.md"), output_root=str(tmp / "runs"))
        orchestrator = Orchestrator(config, registry=scripted_registry())
        outcome = orchestrator.run()
    assert outcome.outcome == "aborted"
    assert "需求文件不存在" in outcome.error


def test_events_file_is_jsonl() -> None:
    import json

    with tempdir() as tmp:
        config = _config(tmp)
        orchestrator = Orchestrator(config, registry=scripted_registry())
        orchestrator.run()
        lines = (orchestrator.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines, "应当有事件记录"
    for line in lines[:20]:
        payload = json.loads(line)
        assert "kind" in payload and "seq" in payload


def test_run_workflow_helper() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        outcome = run_workflow(config)
    assert outcome.run_id
    assert outcome.to_dict()["run_id"] == outcome.run_id


def test_open_questions_propagate() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        orchestrator = Orchestrator(config, registry=scripted_registry())
        outcome = orchestrator.run()
    assert outcome.open_questions, "编码 Agent 提出的开放问题应汇总到最终结论"


def test_health_shape() -> None:
    with tempdir() as tmp:
        config = _config(tmp)
        orchestrator = Orchestrator(config, registry=scripted_registry())
        health = orchestrator.health()
    assert health["run_id"]
    assert isinstance(health["agents"], list)
