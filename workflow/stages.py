"""具体流程：编码 → 审查 → 修复，中间有条件分支、循环和并行 fan-out。

整条流程就是一棵节点树：

    pipeline
    ├── 0:prepare            准备目录、读取需求
    ├── 1:coding             编码 Agent  → code.ready
    ├── 2:review-fix-loop    循环（最多 max_rounds 轮）
    │   └── #N
    │       ├── 0:review        审查 Agent（多文件时按文件并行 fan-out）→ review.completed
    │       ├── 1:gate          质量门禁判定 → gate.evaluated
    │       ├── 2:fix-if-needed 条件分支：有可修的阻断级问题才调用修复 Agent
    │       └── 3:round-close   记录本轮轨迹
    ├── 3:final-recheck      条件分支：最后一次动作是修复则补一次复审
    └── 4:wrap-up            汇总开放问题、写运行结论

两种协作机制都用了。消息（:mod:`workflow.bus`）走 ``code.ready`` /
``review.completed`` / ``gate.evaluated`` / ``fix.applied``，修复阶段是订阅
``review.completed`` 拿到发现清单的，不去读别人的中间变量。状态（:mod:`workflow.state`）
放需求、代码清单和每轮门禁结论。消息说"发生了什么"，状态说"现在是什么样"，resume
只要恢复状态就够了。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

from workflow.bus import (
    TOPIC_CODE_READY,
    TOPIC_FIX_APPLIED,
    TOPIC_GATE_EVALUATED,
    TOPIC_HUMAN_REQUIRED,
    TOPIC_PIPELINE_DONE,
    TOPIC_REVIEW_COMPLETED,
    Inbox,
)
from workflow.context import RunContext
from workflow.errors import AbortWorkflow, NodeFailed
from workflow.events import EVENT_GATE
from workflow.models import RoundSummary, StageReport
from workflow.nodes import BranchNode, FunctionalNode, LoopNode, Node, NodeResult, SequenceNode
from workflow.protocol import FindingContract, count_blocking, count_by_severity, dumps, is_blocking

#: 严重级别从重到轻，合并分片时按它排序、取最严重的一个
_RISK_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "info")


# 准备阶段
class PrepareStage(Node):
    """解析需求、建立工作副本与报告目录。"""

    def __init__(self, name: str = "prepare") -> None:
        super().__init__(name)

    def run(self, ctx: RunContext) -> NodeResult:
        started = time.perf_counter()
        config = ctx.config
        if not config.spec_path:
            raise AbortWorkflow("未指定需求文件（--spec），流程无法开始")
        spec_path = Path(config.spec_path).expanduser()
        if not spec_path.is_absolute():
            spec_path = Path.cwd() / spec_path
        spec_path = spec_path.resolve()
        if not spec_path.is_file():
            raise AbortWorkflow(f"需求文件不存在：{spec_path}")

        output_root = config.resolve_output_root()
        run_dir = output_root / ctx.run_id
        workdir = run_dir / config.worktree_name
        reports = run_dir / config.report_dir_name
        for directory in (workdir, reports):
            directory.mkdir(parents=True, exist_ok=True)

        spec_text = spec_path.read_text(encoding="utf-8-sig")
        ctx.state.set_path("spec.path", str(spec_path))
        ctx.state.set_path("spec.text", spec_text)
        ctx.state.set_path("paths.run_dir", str(run_dir))
        ctx.state.set_path("paths.workdir", str(workdir))
        ctx.state.set_path("paths.reports", str(reports))
        ctx.state.set_path("paths.spec", str(spec_path))
        ctx.outcome.spec_path = str(spec_path)
        ctx.outcome.workdir = str(workdir)
        ctx.outcome.report_dir = str(reports)

        ctx.record_stage(
            StageReport(
                name=self.name,
                node=ctx.path,
                summary=f"工作目录 {workdir.name}，需求 {spec_path.name}",
                elapsed_seconds=round(time.perf_counter() - started, 4),
                metrics={"spec_bytes": len(spec_text)},
            )
        )
        return NodeResult.ok_(f"准备完成：{spec_path.name} → {run_dir}")


# 编码阶段
class CodingStage(Node):
    """调用编码 Agent 把需求变成代码草案。"""

    def __init__(self, name: str = "coding") -> None:
        super().__init__(name)

    def run(self, ctx: RunContext) -> NodeResult:
        started = time.perf_counter()
        workdir = ctx.state.get_path("paths.workdir", "")
        report_dir = Path(ctx.state.get_path("paths.reports", ".")) / "coding"
        payload = {
            "spec_path": ctx.state.get_path("spec.path", ""),
            "out_dir": workdir,
            "overwrite": True,
        }
        response = ctx.call_agent(
            "coding",
            task_id=f"coding-r{ctx.round or 1}",
            objective="把 Markdown 需求实现成可运行的代码草案",
            payload=payload,
        )

        code = response.payload
        ctx.state["code"] = code
        ctx.state["last_action"] = "coding"
        write_reports(report_dir, {"response.json": dumps(response.to_dict())})
        if code.get("files"):
            first = code["files"][0].get("path", "draft.py")
            source = Path(workdir) / first
            if source.is_file():
                write_reports(report_dir, {f"draft_{Path(first).name}": source.read_text(encoding="utf-8")})

        ctx.bus.publish(
            TOPIC_CODE_READY,
            sender="coding",
            body={
                "round": ctx.round,
                "files": [f.get("path") for f in code.get("files", [])],
                "module": code.get("module", ""),
                "total_lines": code.get("total_lines", 0),
            },
        )
        questions = list(ctx.state.get_path("open_questions", [])) + list(
            code.get("open_questions") or []
        )
        ctx.state["open_questions"] = _dedupe(questions)

        ctx.record_stage(
            StageReport(
                name=self.name,
                node=ctx.path,
                agent="coding",
                summary=response.summary,
                elapsed_seconds=round(time.perf_counter() - started, 4),
                metrics=response.metrics,
                artifacts=[a.to_dict() for a in response.artifacts],
            )
        )
        return NodeResult.ok_(response.summary or "代码生成完成", files=code.get("files", []))


# 审查阶段（含并行 fan-out）
class ReviewStage(Node):
    """调用审查 Agent。目标文件超过一个时按文件并行 fan-out，再确定性合并。

    fan-out 放在编排器而不是审查 Agent 内部的线程池，是为了拿到每个文件各自的报告、
    失败时能指出是哪个文件审崩了，也让并行成为工作流层的能力（以后加个文档 Agent 也
    一样）。前提是并行子任务没有副作用，审查只读代码，正好成立。

    代价是合并得自己写。规则在 :func:`merge_reviews` 里，是纯函数，可以单测。
    """

    def __init__(self, name: str = "review", *, label: str = "review") -> None:
        super().__init__(name)
        #: 报告目录前缀。收尾复审用 ``recheck``，避免覆盖本轮正式的审查报告
        self.label = label

    def run(self, ctx: RunContext) -> NodeResult:
        started = time.perf_counter()
        ctx.round = _current_round(ctx)
        workdir = Path(ctx.state.get_path("paths.workdir", "."))
        reports = Path(ctx.state.get_path("paths.reports", ".")) / f"{self.label}-r{ctx.round}"
        reports.mkdir(parents=True, exist_ok=True)

        targets = _collect_targets(workdir, ctx.config.patterns)
        if not targets:
            raise AbortWorkflow(
                f"工作目录 {workdir} 里没有匹配 {list(ctx.config.patterns)} 的源码，审查无从开始"
            )

        blocking = list(ctx.config.blocking)
        if len(targets) == 1:
            response = self._review_one(ctx, targets[0], reports, blocking)
            payload = response.payload
            merged = dict(payload)
            merged.setdefault("shards", 1)
        else:
            merged = self._review_many(ctx, targets, reports, blocking)

        findings = merged.get("findings") or []
        ctx.state["review"] = merged
        ctx.state["findings"] = findings
        ctx.state["last_action"] = "review"
        ctx.state["needs_recheck"] = False

        write_reports(
            reports,
            {
                "review.json": dumps(merged),
                "summary.md": _render_review_markdown(ctx.round, merged),
            },
        )

        ctx.bus.publish(
            TOPIC_REVIEW_COMPLETED,
            sender="review",
            round=ctx.round,
            body={
                "round": ctx.round,
                "findings": findings,
                "counts": merged.get("counts", {}),
                "total": merged.get("total", len(findings)),
                "blocking": merged.get("blocking", 0),
                "passed": bool(merged.get("passed")),
                "fixable": merged.get("fixable", 0),
                "files_reviewed": merged.get("files_reviewed", []),
            },
        )

        ctx.record_stage(
            StageReport(
                name=self.name,
                node=ctx.path,
                round=ctx.round,
                agent="review",
                summary=merged.get("summary", ""),
                elapsed_seconds=round(time.perf_counter() - started, 4),
                metrics={
                    "total": merged.get("total", len(findings)),
                    "blocking": merged.get("blocking", 0),
                    "shards": merged.get("shards", 1),
                    "score": merged.get("score", 100),
                },
            )
        )
        return NodeResult.ok_(merged.get("summary", "审查完成"), findings=findings)

    # 单个目标
    def _review_one(
        self, ctx: RunContext, target: Path, reports: Path, blocking: list[str]
    ) -> Any:
        return ctx.call_agent(
            "review",
            task_id=f"review-r{ctx.round}",
            objective="对编码 Agent 的产出做静态审查并给出质量门禁结论",
            payload={
                "target": str(target),
                "report_dir": str(reports),
                "blocking": blocking,
            },
        )

    # 多目标并行
    def _review_many(
        self,
        ctx: RunContext,
        targets: Sequence[Path],
        reports: Path,
        blocking: list[str],
    ) -> dict[str, Any]:
        # XXX: 4 是照审查 Agent 的默认并行度抄的，两边其实没联动
        workers = min(4, len(targets))
        ctx.debug(
            "review.fanout",
            f"按文件并行审查 {len(targets)} 个目标（workers={workers}）",
        )

        def shard(index: int, target: Path) -> dict[str, Any]:
            child = ctx.child(f"{index}:review-{target.name}")
            child.round = ctx.round
            response = child.call_agent(
                "review",
                task_id=f"review-r{ctx.round}-{index}",
                objective=f"审查单个文件 {target.name}",
                payload={
                    "target": str(target),
                    "report_dir": str(reports / "shards" / target.stem),
                    "blocking": blocking,
                },
            )
            return response.payload

        payloads: list[dict[str, Any] | None] = [None] * len(targets)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(shard, index, target): index for index, target in enumerate(targets)}
            errors: list[str] = []
            for future, index in futures.items():
                try:
                    payloads[index] = future.result()
                except (NodeFailed, AbortWorkflow) as exc:
                    errors.append(f"{targets[index].name}: {exc}")
            if errors:
                raise NodeFailed("并行审查失败：" + "；".join(errors), node="review", retryable=False)

        shards = [payload for payload in payloads if payload]
        return merge_reviews(shards, blocking=ctx.config.blocking)


# 门禁阶段
class GateStage(Node):
    """把审查结论翻译成门禁判定，供条件分支使用。"""

    def __init__(self, name: str = "gate") -> None:
        super().__init__(name)

    def run(self, ctx: RunContext) -> NodeResult:
        started = time.perf_counter()
        ctx.round = _current_round(ctx)
        review = ctx.state.get("review") or {}
        findings = review.get("findings") or []
        blocking = tuple(ctx.config.blocking)
        blocking_count = count_blocking(
            [FindingContract.from_dict(f) for f in findings], blocking
        )
        fixable_blocking = sum(
            1
            for f in findings
            if is_blocking(f.get("severity", ""), blocking) and f.get("fixable")
        )
        gate = {
            "passed": blocking_count == 0,
            "blocking": list(blocking),
            "blocking_count": blocking_count,
            "total": len(findings),
            "counts": review.get("counts") or count_by_severity(
                [FindingContract.from_dict(f) for f in findings]
            ),
            "fixable": review.get("fixable", 0),
            "needs_human": review.get("needs_human", 0),
            "score": review.get("score", 100),
            "overall_risk": review.get("overall_risk", "clean"),
            "can_improve": fixable_blocking > 0,
            "round": ctx.round,
            "reason": (
                "没有达到阻断级别的问题"
                if blocking_count == 0
                else (
                    f"存在 {blocking_count} 条阻断级问题，其中 {fixable_blocking} 条可自动修复"
                    if fixable_blocking
                    else f"存在 {blocking_count} 条阻断级问题，且都没有自动修复策略"
                )
            ),
        }
        ctx.state["gate"] = gate
        ctx.events.emit(
            EVENT_GATE,
            f"门禁：{'通过' if gate['passed'] else '未通过'}（阻断 {blocking_count} 条，可修 {fixable_blocking} 条）",
            node=ctx.path,
            round=ctx.round,
            **{"passed": gate["passed"], "blocking": blocking_count, "fixable_blocking": fixable_blocking},
        )
        ctx.bus.publish(
            TOPIC_GATE_EVALUATED,
            sender="orchestrator",
            round=ctx.round,
            body={"round": ctx.round, **gate},
        )
        return NodeResult.ok_(gate["reason"], gate=gate)


# 修复阶段
class FixStage(Node):
    """调用修复 Agent。发现清单是从消息总线订阅来的，不去读别人的中间变量。

    修复 Agent 不是被"传入"参数，而是订阅 ``review.completed``，编排器只管把消息发出来。
    以后再加一个归档修复结果的 Agent，它订阅 ``fix.applied`` 就行，这里不用动。
    """

    def __init__(self, name: str = "fix", *, inbox: Inbox | None = None) -> None:
        super().__init__(name)
        self.inbox = inbox

    def run(self, ctx: RunContext) -> NodeResult:
        inbox = self.inbox
        if inbox is None:
            # 允许在构造时未注入：这里现场声明订阅，语义更直白
            inbox = Inbox(ctx.bus, recipient="fix").listen(TOPIC_REVIEW_COMPLETED, TOPIC_GATE_EVALUATED)
        message = inbox.latest(TOPIC_REVIEW_COMPLETED)
        if message is None:
            raise NodeFailed("修复阶段收不到 review.completed 消息，无法确定要修什么", node="fix")

        findings = message.body.get("findings") or []
        blocking = tuple(ctx.config.blocking)
        to_fix = [
            f
            for f in findings
            if is_blocking(f.get("severity", ""), blocking) or f.get("fixable")
        ]
        if not to_fix:
            return NodeResult.skip("没有需要修复的阻断级问题")

        started = time.perf_counter()
        ctx.round = _current_round(ctx)
        reports = Path(ctx.state.get_path("paths.reports", ".")) / f"fix-r{ctx.round}"
        reports.mkdir(parents=True, exist_ok=True)
        response = ctx.call_agent(
            "fix",
            task_id=f"fix-r{ctx.round}",
            objective="按审查发现逐条改写代码，并在每处改写后自检",
            payload={
                "target": ctx.state.get_path("paths.workdir", "."),
                "findings": to_fix,
                "report_dir": str(reports),
                "dry_run": False,
            },
        )

        payload = response.payload
        ctx.state["fix"] = payload
        ctx.state["last_action"] = "fix"
        changed = payload.get("files_changed") or []
        ctx.state["needs_recheck"] = bool(changed)
        ctx.state.set_path(f"fixes.round{ctx.round}", payload)

        write_reports(
            reports,
            {
                "fix.json": dumps(payload),
                "fix.diff": payload.get("diff", ""),
                "summary.md": _render_fix_markdown(ctx.round, payload),
            },
        )
        ctx.bus.publish(
            TOPIC_FIX_APPLIED,
            sender="fix",
            round=ctx.round,
            body={
                "round": ctx.round,
                "applied": len(payload.get("applied") or []),
                "rejected": len(payload.get("rejected") or []),
                "files_changed": changed,
            },
        )

        ctx.record_stage(
            StageReport(
                name=self.name,
                node=ctx.path,
                round=ctx.round,
                agent="fix",
                summary=payload.get("summary", response.summary),
                elapsed_seconds=round(time.perf_counter() - started, 4),
                metrics={
                    "applied": len(payload.get("applied") or []),
                    "rejected": len(payload.get("rejected") or []),
                    "deferred": len(payload.get("deferred") or []),
                    "files_changed": len(changed),
                },
                artifacts=[a.to_dict() for a in response.artifacts],
            )
        )
        return NodeResult.ok_(payload.get("summary", "修复完成"), changed=changed)


# 轮次收尾
class RoundCloseStage(Node):
    """把本轮轨迹写进 ``outcome.rounds``，形成收敛曲线。"""

    def __init__(self, name: str = "round-close") -> None:
        super().__init__(name)

    def run(self, ctx: RunContext) -> NodeResult:
        started = time.perf_counter()
        ctx.round = _current_round(ctx)
        review = ctx.state.get("review") or {}
        gate = ctx.state.get("gate") or {}
        fix = ctx.state.get("fix") or {}
        applied = fix.get("applied") or []
        in_this_round = [a for a in applied if a.get("round") in (None, ctx.round)]
        summary = RoundSummary(
            round=ctx.round,
            review_summary=review.get("summary", ""),
            counts=gate.get("counts", {}),
            blocking=gate.get("blocking_count", 0),
            score=gate.get("score", 100),
            passed=bool(gate.get("passed")),
            fixed=len(in_this_round) if fix else 0,
            rejected=len(fix.get("rejected") or []) if fix else 0,
            deferred=len(fix.get("deferred") or []) if fix else 0,
            files_changed=list(fix.get("files_changed") or []),
        )
        existing = {r.round for r in ctx.outcome.rounds}
        if ctx.round in existing:
            ctx.outcome.rounds = [summary if r.round == ctx.round else r for r in ctx.outcome.rounds]
        else:
            ctx.outcome.rounds.append(summary)
        ctx.outcome.rounds.sort(key=lambda r: r.round)
        ctx.state["rounds"] = [r.to_dict() for r in ctx.outcome.rounds]

        ctx.record_stage(
            StageReport(
                name=self.name,
                node=ctx.path,
                round=ctx.round,
                summary=(
                    f"第 {ctx.round} 轮：阻断 {summary.blocking} 条、健康分 {summary.score}、"
                    f"修复 {summary.fixed} 条"
                ),
                elapsed_seconds=round(time.perf_counter() - started, 4),
                metrics={
                    "blocking": summary.blocking,
                    "score": summary.score,
                    "fixed": summary.fixed,
                },
            )
        )
        # 每轮结束清掉上一轮的修复结果，避免下一轮误用
        ctx.state.pop("fix", None)
        return NodeResult.ok_(f"第 {ctx.round} 轮结束")


# 收尾
class WrapUpStage(Node):
    """汇总剩余风险与开放问题，给出运行结论。"""

    def __init__(self, name: str = "wrap-up") -> None:
        super().__init__(name)

    def run(self, ctx: RunContext) -> NodeResult:
        started = time.perf_counter()
        review = ctx.state.get("review") or {}
        findings = review.get("findings") or []
        gate = ctx.state.get("gate") or {}
        blocking = tuple(ctx.config.blocking)

        residual = [f for f in findings if not is_blocking(f.get("severity", ""), blocking)]
        needs_human = [f for f in findings if is_blocking(f.get("severity", ""), blocking)]

        ctx.outcome.residual = residual
        ctx.outcome.open_questions = list(ctx.state.get_path("open_questions", []))
        before = ctx.outcome.rounds[0].blocking if ctx.outcome.rounds else 0
        after = len(needs_human)
        summary = (
            f"阻断级问题 {before} → {after}；剩余非阻断问题 {len(residual)} 条需后续处理"
            if ctx.outcome.rounds
            else f"剩余非阻断问题 {len(residual)} 条"
        )
        ctx.state["residual"] = residual
        ctx.state["needs_human"] = needs_human

        if needs_human:
            ctx.bus.publish(
                TOPIC_HUMAN_REQUIRED,
                sender="orchestrator",
                round=ctx.round,
                body={"count": len(needs_human), "findings": needs_human[:20]},
            )
        ctx.bus.publish(
            TOPIC_PIPELINE_DONE,
            sender="orchestrator",
            round=ctx.round,
            body={"blocking": after, "residual": len(residual), "gate_passed": bool(gate.get("passed"))},
        )

        ctx.record_stage(
            StageReport(
                name=self.name,
                node=ctx.path,
                round=ctx.round,
                summary=summary,
                elapsed_seconds=round(time.perf_counter() - started, 4),
            )
        )
        return NodeResult.ok_(summary, residual=len(residual), needs_human=len(needs_human))


# 组装
def build_pipeline(*, max_rounds: int, review_inbox: Inbox | None = None) -> Node:
    """构造完整的流程树。单独成函数，方便测试直接拿到结构做断言。"""

    loop_body = SequenceNode(
        "round",
        [
            ReviewStage("review"),
            GateStage("gate"),
            BranchNode(
                "fix-if-needed",
                condition=lambda ctx: not (ctx.state.get_path("gate.passed", False))
                and bool(ctx.state.get_path("gate.can_improve", False)),
                then_node=FixStage("fix", inbox=review_inbox),
                else_node=FunctionalNode(
                    "skip-fix",
                    lambda ctx: NodeResult.skip(
                        "门禁已通过" if ctx.state.get_path("gate.passed", False) else "没有可自动修复的阻断级问题"
                    ),
                ),
                description="门禁未通过且存在可自动修复的问题",
            ),
            RoundCloseStage("round-close"),
        ],
    )

    loop = LoopNode(
        "review-fix-loop",
        loop_body,
        max_rounds=max_rounds,
        until=lambda ctx: bool(ctx.state.get_path("gate.passed", False))
        or not bool(ctx.state.get_path("gate.can_improve", False)),
    )

    recheck = BranchNode(
        "final-recheck",
        condition=lambda ctx: bool(ctx.state.get("needs_recheck", False)),
        then_node=SequenceNode(
            "recheck",
            [
                ReviewStage("review", label="recheck"),
                GateStage("gate"),
                RoundCloseStage("round-close"),
            ],
        ),
        else_node=None,
        description="最后一次动作是修复，需要补一次复审确认",
    )

    return SequenceNode(
        "pipeline",
        [
            PrepareStage("prepare"),
            CodingStage("coding"),
            loop,
            recheck,
            WrapUpStage("wrap-up"),
        ],
    )


def _current_round(ctx: RunContext) -> int:
    """从共享状态取"现在是第几轮"。

    不用 ``ctx.round`` 是因为循环结束回到父上下文时它就变回 0 了，而收尾复审还得知道刚
    才跑到第几轮。放进共享状态才能既跨节点可见、又跟着 resume 一起恢复。
    """
    try:
        return int(ctx.state.get("current_round", ctx.round) or ctx.round or 0)
    except (TypeError, ValueError):  # pragma: no cover
        return ctx.round or 0


# 合并与工具
def merge_reviews(
    payloads: Sequence[dict[str, Any]], *, blocking: Sequence[str] = ("critical", "high")
) -> dict[str, Any]:
    """合并多个分片的审查结果。纯函数，可以直接单测。

    findings 按 ``finding_id`` 去重再按严重级别排序；counts / blocking / total / fixable /
    needs_human 都从合并后的清单重算，不取各分片之和，因为分片在边界上可能重复报同一处；
    score 取最小值（各分片都是按全量权重算的分，相加没意义）；overall_risk 取最严重的那
    个；有一条阻断就算不通过。
    """
    if not payloads:
        return {
            "findings": [],
            "counts": {sev: 0 for sev in _RISK_ORDER},
            "total": 0,
            "blocking": 0,
            "passed": True,
            "fixable": 0,
            "needs_human": 0,
            "score": 100,
            "overall_risk": "clean",
            "files_reviewed": [],
            "shards": 0,
            "summary": "没有可合并的审查结果",
        }

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    files: list[str] = []
    for payload in payloads:
        for file_name in payload.get("files_reviewed") or []:
            if file_name not in files:
                files.append(file_name)
        for finding in payload.get("findings") or []:
            key = str(finding.get("finding_id") or "")
            if key and key in seen:
                continue
            seen.add(key)
            merged.append(dict(finding))

    contracts = [FindingContract.from_dict(f) for f in merged]
    order = {sev: index for index, sev in enumerate(_RISK_ORDER)}
    merged.sort(key=lambda f: (order.get(f.get("severity", "medium"), 99), f.get("file", ""), f.get("line") or 0))
    counts = count_by_severity(contracts)
    blocking_count = count_blocking(contracts, blocking)
    scores = [int(p.get("score", 100)) for p in payloads if p.get("score") is not None]
    risks = [str(p.get("overall_risk") or "clean") for p in payloads]
    worst_risk = min(risks, key=lambda r: order.get(r, 99)) if risks else "clean"

    return {
        "findings": merged,
        "counts": counts,
        "total": len(merged),
        "blocking": blocking_count,
        "blocking_severities": list(blocking),
        "passed": blocking_count == 0,
        "fixable": sum(1 for f in contracts if f.fixable),
        "needs_human": sum(1 for f in contracts if not f.fixable),
        "score": min(scores) if scores else 100,
        "overall_risk": worst_risk,
        "files_reviewed": files,
        "shards": len(payloads),
        "summary": (
            f"并行审查 {len(payloads)} 个分片，合并后命中 {len(merged)} 条"
            f"（阻断 {blocking_count} 条）"
        ),
    }


def _collect_targets(root: Path, patterns: Sequence[str]) -> list[Path]:
    """按 glob 收集待审查文件，排除缓存与 VCS 目录。"""
    excluded = {
        ".git", "__pycache__", ".venv", "venv", "node_modules",
        ".mypy_cache", ".pytest_cache", "build", "dist", ".idea",
    }
    found: list[Path] = []
    for pattern in patterns:
        for path in sorted(root.rglob(pattern)):
            if not path.is_file():
                continue
            if any(part in excluded for part in path.parts):
                continue
            if path not in found:
                found.append(path)
    return found


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def write_reports(directory: Path, files: dict[str, str]) -> None:
    """把报告文件写到磁盘。空内容跳过，避免生成 0 字节的 diff。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            if content is None:
                continue
            (directory / name).write_text(str(content), encoding="utf-8", newline="\n")
    except OSError:  # pragma: no cover - 报告写不进去不该中断流程
        pass


def _render_review_markdown(round_no: int, payload: dict[str, Any]) -> str:
    counts = payload.get("counts") or {}
    lines = [
        f"# 第 {round_no} 轮审查",
        "",
        f"- 结论：{payload.get('summary', '')}",
        f"- 门禁：{'✅ 通过' if payload.get('passed') else '❌ 未通过'}",
        "- 分布：" + " / ".join(f"{k} {v}" for k, v in counts.items()),
        "",
        "| 级别 | 规则 | 位置 | 问题 | 可自动修 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for finding in payload.get("findings") or []:
        auto = "✅" if finding.get("fixable") else "—"
        location = finding.get("file", "")
        if finding.get("line"):
            location = f"{location}:{finding['line']}"
        lines.append(
            f"| {finding.get('severity')} | {finding.get('rule_id') or '-'} | `{location}` | "
            f"{finding.get('title')} | {auto} |"
        )
    return "\n".join(lines) + "\n"


def _render_fix_markdown(round_no: int, payload: dict[str, Any]) -> str:
    lines = [
        f"# 第 {round_no} 轮修复",
        "",
        f"- 结论：{payload.get('summary', '')}",
        f"- 已修复 {len(payload.get('applied') or [])} 条 / "
        f"转人工 {len(payload.get('rejected') or [])} 条 / 暂缓 {len(payload.get('deferred') or [])} 条",
        "",
    ]
    applied = payload.get("applied") or []
    if applied:
        lines += ["## 已修复", ""]
        for record in applied:
            lines.append(
                f"- `{record.get('file')}:{record.get('line')}` {record.get('rule_id')} — "
                f"{record.get('note', '')}"
            )
    rejected = payload.get("rejected") or []
    if rejected:
        lines += ["", "## 转人工", ""]
        for record in rejected:
            lines.append(
                f"- `{record.get('file')}:{record.get('line')}` {record.get('rule_id')} — "
                f"{record.get('note', '')}"
            )
    diff = payload.get("diff") or ""
    if diff:
        lines += ["", "## 补丁", "", "```diff", diff.rstrip("\n"), "```"]
    return "\n".join(lines) + "\n"


__all__ = [
    "CodingStage",
    "FixStage",
    "GateStage",
    "PrepareStage",
    "ReviewStage",
    "RoundCloseStage",
    "WrapUpStage",
    "build_pipeline",
    "merge_reviews",
    "write_reports",
]
