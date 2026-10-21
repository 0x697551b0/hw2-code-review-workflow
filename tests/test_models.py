"""门禁与运行模型（models.py）的测试。

门禁结论必须**每个字段都能解释**：为什么没过、有多少条可自动修、还值不值得再跑一轮。
这组用例守的就是"可解释性"。
"""

from __future__ import annotations

from workflow.models import (
    OUTCOME_PASSED,
    GateResult,
    RoundSummary,
    RunOutcome,
    StageReport,
)

from tests.support import finding


def test_gate_passes_without_blocking() -> None:
    gate = GateResult.from_findings([finding("CG304", "low")])
    assert gate.passed
    assert gate.blocking_count == 0
    assert "没有达到阻断级别" in gate.reason


def test_gate_fails_and_explains_fixability() -> None:
    gate = GateResult.from_findings(
        [finding("CG106", "high", fixable=True), finding("CG301", "high", fixable=False)]
    )
    assert not gate.passed
    assert gate.blocking_count == 2
    assert gate.fixable == 1
    assert gate.needs_human == 1
    assert "可自动修复" in gate.reason
    assert gate.can_improve, "还有可修的就值得再跑一轮"


def test_gate_stops_when_nothing_fixable() -> None:
    gate = GateResult.from_findings([finding("CG301", "high", fixable=False)])
    assert not gate.passed
    assert not gate.can_improve, "没有可修策略时不该再循环，应转人工"
    assert "需人工介入" in gate.reason


def test_gate_blocking_is_configurable() -> None:
    findings = [finding("CG202", "medium", fixable=True)]
    assert GateResult.from_findings(findings).passed
    strict = GateResult.from_findings(findings, blocking=("critical", "high", "medium"))
    assert not strict.passed


def test_gate_accepts_dicts_and_contracts() -> None:
    from workflow.protocol import FindingContract

    gate = GateResult.from_findings(
        [finding("CG106", "high"), FindingContract(finding_id="F2", severity="medium")]
    )
    assert gate.total == 2
    assert gate.counts["high"] == 1


def test_gate_serializes() -> None:
    payload = GateResult.from_findings([finding("CG106", "high")]).to_dict()
    assert set(payload) >= {"passed", "blocking", "blocking_count", "reason", "can_improve"}
    assert payload["blocking"] == ["critical", "high"]


def test_round_summary_serializes() -> None:
    row = RoundSummary(round=1, blocking=3, score=38, passed=False, fixed=7)
    payload = row.to_dict()
    assert payload["round"] == 1
    assert payload["fixed"] == 7


def test_stage_report_rounds_elapsed() -> None:
    stage = StageReport(name="review", round=2, elapsed_seconds=1.23456)
    assert stage.to_dict()["elapsed_seconds"] == 1.235


def test_run_outcome_aggregates() -> None:
    outcome = RunOutcome(
        run_id="r1",
        outcome=OUTCOME_PASSED,
        rounds=[RoundSummary(round=1, fixed=3), RoundSummary(round=2, fixed=0)],
    )
    assert outcome.total_fixed == 3
    assert outcome.total_rounds == 2
    assert outcome.to_dict()["total_fixed"] == 3
