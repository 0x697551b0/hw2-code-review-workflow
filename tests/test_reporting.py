"""报告渲染（reporting.py）的测试。

报告是改坏了也不容易看出来的产物，所以这里守住几条：流程图必须由流程树生成，不能是写死
的字符串；Markdown 该有的小节都在；HTML 不引外部资源。
"""

from __future__ import annotations

from workflow.reporting import render_ascii_tree, render_mermaid
from workflow.stages import build_pipeline

from tests.support import finding


def _desc():
    return build_pipeline(max_rounds=3).describe()


def test_mermaid_contains_all_stage_names() -> None:
    mermaid = render_mermaid(_desc())
    assert mermaid.startswith("flowchart TD")
    for name in ("prepare", "coding", "review", "gate", "fix", "wrap-up"):
        assert name in mermaid, name


def test_mermaid_labels_branch_edges() -> None:
    mermaid = render_mermaid(_desc())
    assert "|成立|" in mermaid
    assert "|不成立|" in mermaid


def test_mermaid_has_no_duplicate_parent_edges() -> None:
    """分支节点的边只应出现一次（带标签），否则图上会重影。"""
    mermaid = render_mermaid(_desc())
    fix_node = next(line for line in mermaid.splitlines() if '["▭ fix"]' in line).split("[")[0].strip()
    plain_edges = [line for line in mermaid.splitlines() if line.strip().endswith(f"--> {fix_node}")]
    assert not plain_edges, plain_edges


def test_ascii_tree_is_indented() -> None:
    tree = render_ascii_tree(_desc())
    lines = tree.splitlines()
    assert lines[0].startswith("◉ pipeline")
    assert any(line.startswith("├──") for line in lines)
    assert any("max_rounds=3" in line for line in lines)


def test_ascii_tree_shows_branch_condition() -> None:
    assert "门禁未通过且存在可自动修复的问题" in render_ascii_tree(_desc())


def test_markdown_and_html_render(tmp_path=None) -> None:
    """用假编排器渲染完整报告，验证关键小节都在。"""
    from workflow.reporting import render_html, render_markdown

    orchestrator, outcome = _fake_orchestrator()
    markdown = render_markdown(orchestrator, outcome)
    for section in ("## 1. 概要", "## 2. 质量门禁", "## 3. 流程图", "## 4. 收敛曲线", "## 5. 消息传递记录"):
        assert section in markdown, section
    assert "```mermaid" in markdown
    assert "CG304" in markdown, "残余问题应出现在报告里"

    page = render_html(orchestrator, outcome)
    assert page.startswith("<!doctype html>")
    assert "http://" not in page.split("<style>")[0], "不应引用外部资源"
    assert "流程图" in page
    assert "</html>" in page


def _fake_orchestrator():
    from workflow.bus import MessageBus, TOPIC_CODE_READY
    from workflow.config import WorkflowConfig
    from workflow.models import GateResult, RoundSummary, RunOutcome
    from workflow.state import SharedState
    from workflow.events import EventLog

    class FakeOrchestrator:
        pass

    fork = FakeOrchestrator()
    fork.config = WorkflowConfig(spec_path="spec.md", blocking=("critical", "high"), max_rounds=3)
    fork.run_dir = __import__("pathlib").Path(".testtmp") / "fake-run"
    fork.run_dir.mkdir(parents=True, exist_ok=True)
    fork.state = SharedState("r1")
    fork.events = EventLog()
    fork.bus = MessageBus(run_id="r1")
    fork.bus.publish(TOPIC_CODE_READY, sender="coding", body={"files": ["mod.py"], "total_lines": 30})
    gate = GateResult.from_findings([finding("CG106", "high")])
    fork.state["gate"] = gate.to_dict()

    outcome = RunOutcome(
        run_id="r1",
        outcome="needs_human",
        rounds=[RoundSummary(round=1, blocking=1, score=90, fixed=1)],
        gate=gate,
        spec_path="spec.md",
        workdir="",
        residual=[finding("CG304", "low")],
    )
    return fork, outcome
