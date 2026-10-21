"""真实三件套的协同测试（端到端）。

这是唯一依赖外部仓库的测试文件。设计上**允许跳过**：找不到任何一个 Agent 时，
相关用例直接返回并打印提示，而不是让整套测试变红 —— 因为"隔壁仓库没 clone 下来"
不是本仓库的缺陷。但一旦三个仓库都在，这条链路就必须真的跑通。

判据不是"退出码为 0"，而是三条硬结论：

1. 初始草案确实带着阻断级问题（否则流程没意义）；
2. 修复后阻断级问题归零、门禁通过；
3. 总线上留下了编码 → 审查 → 门禁 → 修复 → 复审的完整消息序列。
"""

from __future__ import annotations

from pathlib import Path

from workflow.registry import AgentRegistry
from workflow.config import WorkflowConfig
from workflow.runner import Orchestrator

from tests.support import tempdir

SPEC = """# 用户存储服务

## 模块
`user_store`

## 功能

- [R1] 创建用户：`create_user(cursor, name, email, age)`
- [R2] 按主键查询用户：`get_user(cursor, user_id)`
- [R3] 按名称关键词搜索用户：`find_users(cursor, keyword, limit_collector)`
- [R4] 加载服务配置：`load_config(path)`

## 数据模型
`users(id, name, email, age)`
"""


def _agents_available() -> bool:
    registry = AgentRegistry.discover()
    ok = all(registry.has(name) for name in ("coding", "review", "fix"))
    if not ok:
        print("（跳过：未找齐三个 Agent，请把四个仓库放在同一目录下）")
        print(f"  缺失：{registry.missing}")
    return ok


def _config(tmp, **overrides) -> WorkflowConfig:
    from tests.support import write

    spec = write(tmp / "spec.md", SPEC)
    config = WorkflowConfig(spec_path=str(spec), output_root=str(tmp / "runs"), max_rounds=3)
    config.coding.options.update({"engine": "offline", "profile": "realistic"})
    config.review.options.update({"mode": "static"})
    config.fix.options.update({"engine": "rules"})
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_full_loop_closes() -> None:
    if not _agents_available():
        return
    with tempdir() as tmp:
        config = _config(tmp)
        orchestrator = Orchestrator(config)
        outcome = orchestrator.run()

    assert outcome.total_rounds >= 2, f"应当至少跑两轮才收敛：{[r.round for r in outcome.rounds]}"
    assert outcome.rounds[0].blocking > 0, "第一轮必须检出阻断级问题"
    assert outcome.rounds[-1].blocking == 0, "最后一轮必须没有阻断级问题"
    assert outcome.outcome == "passed", (outcome.outcome, outcome.error)
    assert outcome.total_fixed >= 5, outcome.total_fixed


def test_bus_records_full_collaboration() -> None:
    if not _agents_available():
        return
    with tempdir() as tmp:
        orchestrator = Orchestrator(_config(tmp))
        orchestrator.run()
        topics = [message["topic"] for message in orchestrator.bus.transcript()]

    for required in ("code.ready", "review.completed", "gate.evaluated", "fix.applied", "pipeline.done"):
        assert required in topics, f"缺少消息主题 {required}：{topics}"
    assert topics.count("review.completed") >= 2, "复审应当再发一次审查完成消息"


def test_clean_profile_short_circuits() -> None:
    """干净草案应当第一轮就通过门禁，修复 Agent 根本不该被调用。"""
    if not _agents_available():
        return
    with tempdir() as tmp:
        config = _config(tmp)
        config.coding.options["profile"] = "clean"
        orchestrator = Orchestrator(config)
        outcome = orchestrator.run()
        fix_stage_events = [
            event for event in orchestrator.events.filter(kind="agent.call") if event.data.get("agent") == "fix"
        ]

    assert outcome.outcome == "passed"
    assert outcome.total_rounds == 1
    assert fix_stage_events == [], "干净草案不该触发修复 Agent"
    # 干净草案里仍有 info 级（缺少类型注解），因此不算阻断
    assert outcome.rounds[0].blocking == 0


def test_artifacts_and_reports_exist() -> None:
    if not _agents_available():
        return
    with tempdir() as tmp:
        orchestrator = Orchestrator(_config(tmp))
        outcome = orchestrator.run()
        run_dir = orchestrator.run_dir
        for name in ("report.md", "report.html", "run.json", "events.jsonl", "state.json"):
            assert (run_dir / name).is_file(), name
        assert list((run_dir / "reports").iterdir()), "各轮报告目录不应为空"
        assert (Path(run_dir) / "worktree" / "user_store.py").is_file(), "最终代码应当留在工作副本里"
    assert outcome.residual is not None
