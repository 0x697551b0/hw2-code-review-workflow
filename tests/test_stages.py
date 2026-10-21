"""阶段与合并逻辑（stages.py）的测试。

``merge_reviews`` 是并行审查的合并点，也是最容易写错的地方（去重、重算、取最差值）。
它是纯函数，所以可以直接单测 —— 这是刻意把它设计成纯函数的原因。
"""

from __future__ import annotations

from workflow.stages import build_pipeline, merge_reviews

from tests.support import finding


def _payload(findings: list[dict], *, score: int = 100, risk: str = "clean") -> dict:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for item in findings:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    return {
        "findings": findings,
        "counts": counts,
        "total": len(findings),
        "score": score,
        "overall_risk": risk,
        "files_reviewed": [],
        "summary": "分片",
    }


def test_merge_empty() -> None:
    merged = merge_reviews([])
    assert merged["findings"] == []
    assert merged["passed"] is True
    assert merged["score"] == 100


def test_merge_dedupes_by_finding_id() -> None:
    shared = finding("CG106", "high", 10)
    merged = merge_reviews([_payload([shared]), _payload([shared])])
    assert merged["total"] == 1, "同一 finding_id 只能算一次"


def test_merge_recomputes_counts_not_sum() -> None:
    """counts 必须由合并后的清单重算，而不是各分片相加。"""
    merged = merge_reviews(
        [
            _payload([finding("CG106", "high", 10)]),
            _payload([finding("CG203", "high", 20)]),
        ]
    )
    assert merged["counts"]["high"] == 2
    assert merged["blocking"] == 2
    assert merged["total"] == 2


def test_merge_takes_min_score_and_worst_risk() -> None:
    merged = merge_reviews(
        [
            _payload([finding("CG106", "high")], score=60, risk="high"),
            _payload([], score=100, risk="clean"),
        ]
    )
    assert merged["score"] == 60
    assert merged["overall_risk"] == "high"


def test_merge_sorts_by_severity() -> None:
    merged = merge_reviews(
        [_payload([finding("CG304", "low", 1), finding("CG105", "critical", 2)])]
    )
    assert [f["severity"] for f in merged["findings"]] == ["critical", "low"]


def test_merge_unions_files() -> None:
    left = _payload([finding("CG304", "low", file="a.py")])
    left["files_reviewed"] = ["a.py"]
    right = _payload([finding("CG304", "low", file="b.py")])
    right["files_reviewed"] = ["b.py"]
    merged = merge_reviews([left, right])
    assert merged["files_reviewed"] == ["a.py", "b.py"]


def test_merge_marks_shard_count() -> None:
    merged = merge_reviews([_payload([]), _payload([]), _payload([])])
    assert merged["shards"] == 3


def test_pipeline_structure() -> None:
    """流程结构本身也是契约：改名或删阶段应当被测出来。"""
    desc = build_pipeline(max_rounds=4).describe()
    assert desc["type"] == "sequence"
    names = [child["name"] for child in desc["children"]]
    assert names == ["prepare", "coding", "review-fix-loop", "final-recheck", "wrap-up"], names

    loop = desc["children"][2]
    assert loop["max_rounds"] == 4
    body_names = [child["name"] for child in loop["body"]["children"]]
    assert body_names == ["review", "gate", "fix-if-needed", "round-close"], body_names

    branch = loop["body"]["children"][2]
    assert branch["type"] == "branch"
    assert branch["then"] == {"type": "functional", "name": "fix"} or branch["then"]["name"] == "fix"


def test_pipeline_max_rounds_validation() -> None:
    from tests.support import expect_raises

    expect_raises(ValueError, build_pipeline, max_rounds=0)


# 并行 fan-out
def test_review_fanout_merges_shards() -> None:
    """
    多文件时审查阶段应**按文件并行 fan-out**，并把分片结果确定性合并。

    这条路径之前没有被覆盖：演示工程只有一个源文件，`_review_many` 从来没被走到。
    并行是作业的"推荐实现"之一，必须真的测到。
    """
    from workflow.config import WorkflowConfig
    from workflow.context import RunContext
    from workflow.models import RunOutcome
    from workflow.bus import MessageBus
    from workflow.events import EventLog
    from workflow.stages import PrepareStage, CodingStage, ReviewStage
    from workflow.state import SharedState

    from tests.support import scripted_registry, tempdir, write

    with tempdir() as tmp:
        spec = write(tmp / "spec.md", "# 演示\n\n## 模块\n`demo`\n\n## 功能\n- [R1] x：`do_thing(cursor, user_id)`\n")
        config = WorkflowConfig(spec_path=str(spec), output_root=str(tmp / "runs"), max_rounds=1)
        registry = scripted_registry(
            findings_per_round=[[finding("CG106", "high", 10)]],
            files=3,
        )
        state = SharedState("r1")
        state.bind(tmp / "state.json")
        events = EventLog()
        ctx = RunContext(
            config=config,
            state=state,
            bus=MessageBus(events=events, run_id="r1"),
            events=events,
            registry=registry,
            outcome=RunOutcome(run_id="r1"),
            run_id="r1",
            round=1,
        )
        PrepareStage("prepare").execute(ctx.child("0:prepare"))
        CodingStage("coding").execute(ctx.child("1:coding"))
        result = ReviewStage("review").execute(ctx.child("2:review"))

    assert result.ok, result.error
    merged = ctx.state.get("review")
    assert merged["shards"] == 3, merged.get("shards")
    assert merged["total"] == 3, f"3 个分片各 1 条，合并后应当 3 条，实际 {merged.get('total')}"
    assert merged["blocking"] == 3
    assert sorted(merged["files_reviewed"]) == ["mod.py", "mod1.py", "mod2.py"], merged["files_reviewed"]
