"""报告生成：Markdown（给人读、进版本库）和 HTML（给演示看）。

HTML 不引用任何 CDN，样式和图表全是内联 CSS，断网也能打开。结构化数据单独存
（``run.json`` / ``review-rN/review.json``），报告只是它的一个渲染视图，改样式不会影响
自动化断言。流程图从 :meth:`workflow.nodes.Node.describe` 递归生成，不手画，手画的图
过一周就跟代码脱节了。Mermaid 和 ASCII 各出一份。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from workflow.models import OUTCOME_PASSED, RunOutcome
from workflow.protocol import SEVERITY_ORDER

#: 级别 → 展示色
_SEVERITY_COLOR: dict[str, str] = {
    "critical": "#b3261e",
    "high": "#e8710a",
    "medium": "#f9ab00",
    "low": "#1e8e3e",
    "info": "#5f6368",
}

_TITLE = "多 Agent 代码审查工作流"


# 流程图
def render_mermaid(node_describe: dict[str, Any]) -> str:
    """把流程树描述转成 Mermaid flowchart 文本。"""
    lines = ["flowchart TD"]
    counter = {"n": 0}
    links: list[str] = []

    def new_id() -> str:
        counter["n"] += 1
        return f"n{counter['n']}"

    def wrap(label: str, width: int = 22) -> str:
        return label if len(label) <= width else label[: width - 1] + "…"

    def emit(desc: dict[str, Any], parent: str | None = None) -> str:
        kind = desc.get("type", "functional")
        node_id = new_id()
        name = str(desc.get("name", ""))
        if kind == "sequence":
            label = f"⟲ 顺序 · {wrap(name)}"
        elif kind == "loop":
            label = f"↻ 循环 · {wrap(name)}<br/>最多 {desc.get('max_rounds', '?')} 轮"
        elif kind == "branch":
            label = f"◇ 分支 · {wrap(name)}<br/>{wrap(str(desc.get('condition', '')), 28)}"
        else:
            label = f"▭ {wrap(name)}"
        lines.append(f'    {node_id}["{label}"]')
        if parent:
            links.append(f"    {parent} --> {node_id}")

        if kind == "branch":
            # 分支的子节点只走"带标签的边"，否则会同时出现无标签边与标签边，图上重影
            then_desc = desc.get("then") or {}
            else_desc = desc.get("else") or {}
            if then_desc:
                child = emit(then_desc, None)
                links.append(f"    {node_id} -->|成立| {child}")
            if else_desc:
                child = emit(else_desc, None)
                links.append(f"    {node_id} -->|不成立| {child}")
        else:
            for child in desc.get("children") or []:
                emit(child, node_id)
            body = desc.get("body")
            if isinstance(body, dict):
                emit(body, node_id)
        return node_id

    emit(node_describe)
    return "\n".join(lines + links)


def render_ascii_tree(node_describe: dict[str, Any]) -> str:
    """把流程树渲染成终端友好的缩进树。"""
    lines: list[str] = []

    def walk(desc: dict[str, Any], prefix: str, is_last: bool, is_root: bool) -> None:
        kind = desc.get("type", "node")
        name = str(desc.get("name", ""))
        marker = "◉" if kind in {"sequence", "loop"} else ("◇" if kind == "branch" else "▭")
        extra = ""
        if kind == "loop":
            extra = f"  (max_rounds={desc.get('max_rounds')})"
        elif kind == "branch":
            extra = f"  ({desc.get('condition')})"
        connector = "" if is_root else ("└── " if is_last else "├── ")
        lines.append(f"{prefix}{connector}{marker} {name}{extra}")

        child_prefix = prefix if is_root else prefix + ("    " if is_last else "│   ")
        if kind == "branch":
            children = [
                ("then", desc.get("then")),
                ("else", desc.get("else")),
            ]
            items = [(label, child) for label, child in children if isinstance(child, dict)]
        else:
            items = [
                (f"{index}", child)
                for index, child in enumerate(desc.get("children") or [])
                if isinstance(child, dict)
            ]
            if isinstance(desc.get("body"), dict):
                items.append(("body", desc["body"]))
        for index, (label, child) in enumerate(items):
            last = index == len(items) - 1
            walk(child, child_prefix, last, False)

    walk(node_describe, "", True, True)
    return "\n".join(lines)


# Markdown
def render_markdown(orchestrator: Any, outcome: RunOutcome) -> str:
    """生成人类可读的最终报告。"""
    state = orchestrator.state
    lines: list[str] = [
        f"# {_TITLE} · 运行报告",
        "",
        f"> 运行 ID `{outcome.run_id}`　|　结论 **{_outcome_label(outcome.outcome)}**　|　"
        f"耗时 {outcome.elapsed_seconds:.2f}s",
        "",
        "## 1. 概要",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 需求文件 | `{Path(outcome.spec_path).name if outcome.spec_path else '-'}` |",
        f"| 工作副本 | `{_rel(outcome.workdir, orchestrator.run_dir)}` |",
        f"| 阻断级别 | {', '.join(orchestrator.config.blocking)} |",
        f"| 最多轮次 | {orchestrator.config.max_rounds} |",
        f"| 实际轮次 | {outcome.total_rounds} |",
        f"| 自动修复 | {outcome.total_fixed} 条 |",
        f"| 总线消息 | {len(orchestrator.bus)} 条 |",
        f"| 完成节点 | {state.count_done()} 个 |",
        f"| 事件记录 | {len(orchestrator.events)} 条 |",
        "",
    ]
    if outcome.error:
        lines += ["```text", outcome.error, "```", ""]

    lines += ["## 2. 质量门禁", ""]
    gate = state.get_path("gate", {}) or {}
    if gate:
        lines += [
            f"**{'✅ 通过' if gate.get('passed') else '❌ 未通过'}** —— {gate.get('reason', '')}",
            "",
            "| 级别 | critical | high | medium | low | info |",
            "| --- | --- | --- | --- | --- | --- |",
            "| 数量 | "
            + " | ".join(str((gate.get("counts") or {}).get(sev, 0)) for sev in SEVERITY_ORDER)
            + " |",
            "",
            f"健康分：**{gate.get('score', '-')}**　可自动修 {gate.get('fixable', 0)} 条　"
            f"需人工 {gate.get('needs_human', 0)} 条",
            "",
        ]
    else:
        lines += ["（本轮没有产生门禁结论）", ""]

    lines += ["## 3. 流程图", "", "```mermaid"]
    desc = getattr(orchestrator, "_pipeline_desc", None) or _pipeline_description()
    lines += [render_mermaid(desc), "```", "", "```text", render_ascii_tree(desc), "```", ""]

    lines += ["## 4. 收敛曲线（审查 → 修复）", ""]
    if outcome.rounds:
        lines += [
            "| 轮次 | 阻断级 | 健康分 | 门禁 | 修复 | 转人工 | 改动文件 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for round_summary in outcome.rounds:
            files = "、".join(round_summary.files_changed) or "—"
            lines.append(
                f"| {round_summary.round} | {round_summary.blocking} | {round_summary.score} | "
                f"{'✅' if round_summary.passed else '❌'} | {round_summary.fixed} | "
                f"{round_summary.rejected} | {files} |"
            )
        lines += [""]
        first = outcome.rounds[0]
        last = outcome.rounds[-1]
        lines += [
            f"阻断级问题从 **{first.blocking}** 条降到 **{last.blocking}** 条"
            + (f"（共自动修复 {outcome.total_fixed} 条）" if outcome.total_fixed else ""),
            "",
        ]
    else:
        lines += ["（没有完成任何轮次）", ""]

    lines += ["## 5. 消息传递记录（Agent 协作证据）", ""]
    transcript = orchestrator.bus.transcript()
    if transcript:
        lines += ["| # | 主题 | 发送方 → 接收方 | 轮次 | 摘要 |", "| --- | --- | --- | --- | --- |"]
        for message in transcript:
            body = message.get("body") or {}
            digest = _message_digest(message.get("topic", ""), body)
            lines.append(
                f"| {message.get('msg_id')} | `{message.get('topic')}` | "
                f"{message.get('sender')} → {message.get('recipient')} | "
                f"{message.get('round') or '-'} | {digest} |"
            )
        lines += [""]
    else:
        lines += ["（没有消息）", ""]

    lines += ["## 6. 阶段耗时", ""]
    if outcome.stages:
        lines += ["| 阶段 | 轮次 | 状态 | 耗时 | 摘要 |", "| --- | --- | --- | --- | --- |"]
        for stage in outcome.stages:
            lines.append(
                f"| {stage.name} | {stage.round or '-'} | {stage.status} | "
                f"{stage.elapsed_seconds:.2f}s | {_short(stage.summary, 60)} |"
            )
        lines += [""]

    lines += ["## 7. 残余问题与开放问题", ""]
    residual = outcome.residual or []
    if residual:
        lines += ["以下问题**未达到阻断级别**，本轮不动它们（避免把流程拖进无意义的风格争论）：", ""]
        for finding in residual:
            location = finding.get("file", "")
            if finding.get("line"):
                location = f"{location}:{finding['line']}"
            lines.append(
                f"- `{location}` **{finding.get('severity')}** {finding.get('title')}"
                + (f" —— {_short(finding.get('note') or finding.get('suggestion') or '', 80)}")
            )
        lines += [""]
    else:
        lines += ["没有残余问题。", ""]

    needs_human = state.get_path("needs_human", []) or []
    if needs_human:
        lines += ["### 必须人工处理", ""]
        for finding in needs_human:
            lines.append(
                f"- `{finding.get('file')}:{finding.get('line')}` **{finding.get('rule_id') or '-'}** "
                f"{finding.get('title')}"
            )
        lines += [""]

    questions = outcome.open_questions or []
    if questions:
        lines += ["### 编码 Agent 提出的开放问题", ""]
        lines += [f"- {question}" for question in questions]
        lines += [""]

    lines += ["## 8. 最终代码", ""]
    files = _final_source_files(outcome)
    for path, content in files.items():
        language = "python" if path.endswith(".py") else "text"
        lines += [f"### `{path}`", "", f"```{language}", content.rstrip("\n"), "```", ""]
    if not files:
        lines += ["（没有找到最终代码文件）", ""]

    lines += ["## 9. 产物清单", ""]
    lines += ["```text"]
    lines += [line for line in _artifact_tree(orchestrator.run_dir)]
    lines += ["```", ""]

    return "\n".join(lines) + "\n"


# HTML
def render_html(orchestrator: Any, outcome: RunOutcome) -> str:
    """生成单文件 HTML 报告（无外部依赖，可直接双击打开）。"""
    state = orchestrator.state
    gate = state.get_path("gate", {}) or {}
    desc = getattr(orchestrator, "_pipeline_desc", None) or _pipeline_description()

    counts = gate.get("counts") or {}
    max_count = max([counts.get(sev, 0) for sev in SEVERITY_ORDER] + [1])

    bars = "".join(
        f'<div class="bar-row"><span class="bar-label">{sev}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:'
        f"{max(2, int(100 * counts.get(sev, 0) / max_count))}%;"
        f'background:{_SEVERITY_COLOR[sev]}"></span></span>'
        f'<span class="bar-value">{counts.get(sev, 0)}</span></div>'
        for sev in SEVERITY_ORDER
    )

    rounds_rows = "".join(
        f"<tr><td>{r.round}</td><td>{r.blocking}</td><td>{r.score}</td>"
        f"<td>{'✅' if r.passed else '❌'}</td><td>{r.fixed}</td><td>{r.rejected}</td>"
        f"<td>{html.escape('、'.join(r.files_changed) or '—')}</td></tr>"
        for r in outcome.rounds
    )

    message_rows = "".join(
        f"<tr><td><code>{html.escape(str(m.get('msg_id')))}</code></td>"
        f"<td><code>{html.escape(str(m.get('topic')))}</code></td>"
        f"<td>{html.escape(str(m.get('sender')))} → {html.escape(str(m.get('recipient')))}</td>"
        f"<td>{m.get('round') or '-'}</td>"
        f"<td>{html.escape(_message_digest(str(m.get('topic')), m.get('body') or {}))}</td></tr>"
        for m in orchestrator.bus.transcript()
    )

    stage_rows = "".join(
        f"<tr><td>{html.escape(s.name)}</td><td>{s.round or '-'}</td>"
        f"<td><span class='chip {s.status}'>{s.status}</span></td>"
        f"<td>{s.elapsed_seconds:.2f}s</td><td>{html.escape(_short(s.summary, 70))}</td></tr>"
        for s in outcome.stages
    )

    finding_rows = "".join(
        f"<tr><td><span class='sev' style=\"color:{_SEVERITY_COLOR.get(str(f.get('severity')), '#333')}\">"
        f"{html.escape(str(f.get('severity')))}</span></td>"
        f"<td><code>{html.escape(str(f.get('rule_id') or '-'))}</code></td>"
        f"<td><code>{html.escape(str(f.get('file', '')))}:{f.get('line') or '-'}</code></td>"
        f"<td>{html.escape(str(f.get('title', '')))}</td>"
        f"<td>{'✅' if f.get('fixable') else '—'}</td></tr>"
        for f in (outcome.residual or []) + (state.get_path("needs_human", []) or [])
    )

    flow_nodes = _flow_boxes(desc)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_TITLE} · {html.escape(outcome.run_id)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px 24px 64px; font: 14px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f6f7f9; color: #1f2328; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #16181d; color: #e6e8eb; }}
    .card {{ background: #1e2126 !important; box-shadow: none !important; border-color: #2c3036 !important; }}
    table th {{ background: #23272e !important; }}
    code {{ background: #23272e !important; }}
  }}
  h1 {{ font-size: 24px; margin: 0 0 4px; }}
  h2 {{ font-size: 17px; margin: 32px 0 12px; padding-left: 10px; border-left: 3px solid #4c8dff; }}
  .sub {{ color: #6b7280; font-size: 13px; margin-bottom: 24px; }}
  .card {{ background: #fff; border: 1px solid #e4e7ec; border-radius: 10px; padding: 18px 20px;
           box-shadow: 0 1px 2px rgba(16,24,40,.05); margin-bottom: 16px; }}
  .verdict {{ display: inline-block; padding: 3px 12px; border-radius: 999px; font-weight: 600; font-size: 13px; }}
  .verdict.pass {{ background: #e6f4ea; color: #146c2e; }}
  .verdict.fail {{ background: #fce8e6; color: #a50e0e; }}
  .kpis {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .kpi {{ flex: 1 1 140px; background: #fff; border: 1px solid #e4e7ec; border-radius: 10px; padding: 14px 16px; }}
  .kpi .v {{ font-size: 22px; font-weight: 700; }}
  .kpi .k {{ font-size: 12px; color: #6b7280; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #eceff3; }}
  th {{ background: #f8fafc; font-weight: 600; }}
  code {{ background: #f1f3f6; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin: 6px 0; }}
  .bar-label {{ width: 68px; font-size: 12px; color: #6b7280; }}
  .bar-track {{ flex: 1; height: 14px; background: #eceff3; border-radius: 7px; overflow: hidden; }}
  .bar-fill {{ display: block; height: 100%; border-radius: 7px; }}
  .bar-value {{ width: 32px; text-align: right; font-variant-numeric: tabular-nums; }}
  .flow {{ display: flex; flex-direction: column; gap: 6px; }}
  .fnode {{ border: 1px solid #d7dce4; border-radius: 8px; padding: 8px 12px; background: #fbfcfd; font-size: 13px; }}
  .fnode.agent {{ border-left: 4px solid #4c8dff; }}
  .fnode.ctrl {{ border-left: 4px solid #9aa4b2; background: #f4f6f8; }}
  .arrow {{ color: #9aa4b2; font-size: 12px; padding-left: 14px; }}
  .chip {{ padding: 1px 8px; border-radius: 999px; font-size: 12px; background: #eceff3; }}
  .chip.ok {{ background: #e6f4ea; color: #146c2e; }}
  .chip.failed {{ background: #fce8e6; color: #a50e0e; }}
  .chip.skipped {{ background: #fff4e5; color: #8a5100; }}
  .err {{ background: #fce8e6; border-color: #f5c2bd; }}
  pre {{ background: #f1f3f6; padding: 14px; border-radius: 8px; overflow-x: auto; font-size: 12.5px; }}
</style>
</head>
<body>
  <h1>{_TITLE}</h1>
  <div class="sub">
    运行 <code>{html.escape(outcome.run_id)}</code>　
    <span class="verdict {'pass' if outcome.outcome == OUTCOME_PASSED else 'fail'}">{_outcome_label(outcome.outcome)}</span>　
    耗时 {outcome.elapsed_seconds:.2f}s
  </div>

  <div class="kpis">
    <div class="kpi"><div class="v">{outcome.total_rounds}</div><div class="k">轮次</div></div>
    <div class="kpi"><div class="v">{outcome.total_fixed}</div><div class="k">自动修复</div></div>
    <div class="kpi"><div class="v">{gate.get('score', '-')}</div><div class="k">健康分</div></div>
    <div class="kpi"><div class="v">{len(orchestrator.bus)}</div><div class="k">总线消息</div></div>
    <div class="kpi"><div class="v">{state.count_done()}</div><div class="k">完成节点</div></div>
  </div>

  {f'<div class="card err"><strong>运行错误</strong><pre>{html.escape(outcome.error)}</pre></div>' if outcome.error else ''}

  <h2>问题分布</h2>
  <div class="card">
    {bars or '<p>没有发现问题。</p>'}
    <p style="margin:12px 0 0;color:#6b7280;font-size:13px">
      门禁：{html.escape(str(gate.get('reason', '')))}
    </p>
  </div>

  <h2>流程图（由流程树生成）</h2>
  <div class="card"><div class="flow">{flow_nodes}</div></div>

  <h2>收敛曲线</h2>
  <div class="card">
    <table>
      <thead><tr><th>轮次</th><th>阻断级</th><th>健康分</th><th>门禁</th><th>修复</th><th>转人工</th><th>改动文件</th></tr></thead>
      <tbody>{rounds_rows or '<tr><td colspan="7">没有完成任何轮次</td></tr>'}</tbody>
    </table>
  </div>

  <h2>Agent 消息传递</h2>
  <div class="card">
    <table>
      <thead><tr><th>#</th><th>主题</th><th>发送 → 接收</th><th>轮次</th><th>摘要</th></tr></thead>
      <tbody>{message_rows or '<tr><td colspan="5">没有消息</td></tr>'}</tbody>
    </table>
  </div>

  <h2>阶段耗时</h2>
  <div class="card">
    <table>
      <thead><tr><th>阶段</th><th>轮次</th><th>状态</th><th>耗时</th><th>摘要</th></tr></thead>
      <tbody>{stage_rows or '<tr><td colspan="5">没有阶段记录</td></tr>'}</tbody>
    </table>
  </div>

  <h2>残余问题</h2>
  <div class="card">
    <table>
      <thead><tr><th>级别</th><th>规则</th><th>位置</th><th>问题</th><th>可自动修</th></tr></thead>
      <tbody>{finding_rows or '<tr><td colspan="5">没有残余问题</td></tr>'}</tbody>
    </table>
  </div>

  <h2>产物目录</h2>
  <div class="card"><pre>{html.escape(chr(10).join(_artifact_tree(orchestrator.run_dir)))}</pre></div>
</body>
</html>
"""


# 写文件
def write_final_reports(orchestrator: Any, outcome: RunOutcome) -> dict[str, Path]:
    """把 report.md / report.html / run.json 写到运行目录。"""
    run_dir: Path = orchestrator.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    markdown = render_markdown(orchestrator, outcome)
    (run_dir / "report.md").write_text(markdown, encoding="utf-8", newline="\n")
    written["markdown"] = run_dir / "report.md"

    try:
        page = render_html(orchestrator, outcome)
        (run_dir / "report.html").write_text(page, encoding="utf-8", newline="\n")
        written["html"] = run_dir / "report.html"
    except (OSError, ValueError):  # pragma: no cover - HTML 渲染失败不该影响 Markdown
        pass

    payload = outcome.to_dict()
    payload["messages"] = orchestrator.bus.transcript()
    payload["pipeline"] = getattr(orchestrator, "_pipeline_desc", None) or _pipeline_description()
    (run_dir / "run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    written["json"] = run_dir / "run.json"
    return written


# 工具
def _pipeline_description() -> dict[str, Any]:
    """按默认配置生成流程树描述（只用于画图，不执行）。

    # XXX: 这里的 max_rounds 写死 3，跑起来才按配置算，两者不一样时图上的轮数对不上
    """
    from workflow.stages import build_pipeline

    return build_pipeline(max_rounds=3).describe()


def _outcome_label(outcome: str) -> str:
    return {
        "passed": "通过",
        "needs_human": "需人工介入",
        "failed": "失败",
        "aborted": "已中止",
    }.get(outcome, outcome)


def _short(text: Any, limit: int) -> str:
    value = str(text or "").replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _rel(path: str, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _message_digest(topic: str, body: dict[str, Any]) -> str:
    """给每条消息生成一行摘要，便于在报告里快速扫读。"""
    if topic == "code.ready":
        return f"{len(body.get('files') or [])} 个文件，{body.get('total_lines', 0)} 行"
    if topic == "review.completed":
        counts = body.get("counts") or {}
        pieces = "、".join(f"{k} {v}" for k, v in counts.items() if v)
        total = body.get("total")
        if total is None:
            total = sum(int(v) for v in counts.values())
        return f"命中 {total} 条（{pieces or '无'}），阻断 {body.get('blocking', 0)}"
    if topic == "gate.evaluated":
        return f"{'通过' if body.get('passed') else '未通过'}：{_short(body.get('reason', ''), 40)}"
    if topic == "fix.applied":
        return f"修复 {body.get('applied', 0)} 条，改动 {len(body.get('files_changed') or [])} 个文件"
    if topic == "human.required":
        return f"{body.get('count', 0)} 条问题需要人工处理"
    if topic == "pipeline.done":
        return f"阻断 {body.get('blocking', 0)}，残余 {body.get('residual', 0)}"
    return _short(json.dumps(body, ensure_ascii=False), 48)


def _flow_boxes(desc: dict[str, Any], depth: int = 0) -> str:
    """把流程树画成一列带箭头的盒子（纯 CSS，不依赖任何前端库）。"""
    parts: list[str] = []
    kind = desc.get("type", "")
    name = str(desc.get("name", ""))
    css = "ctrl" if kind in {"sequence", "loop", "branch"} else "agent"

    if depth:
        parts.append('<div class="arrow">↓</div>')

    if kind == "branch":
        parts.append(
            f'<div class="fnode {css}">◇ {html.escape(name)}　'
            f'<span style="color:#6b7280">条件：{html.escape(str(desc.get("condition", "")))}</span></div>'
        )
        for label, key in (("成立", "then"), ("不成立", "else")):
            child = desc.get(key)
            if isinstance(child, dict):
                parts.append(f'<div class="arrow">{label} ↓</div>')
                parts.append(_flow_boxes(child, depth + 1))
        return "".join(parts)

    if kind == "loop":
        parts.append(
            f'<div class="fnode {css}">↻ {html.escape(name)}　'
            f'<span style="color:#6b7280">最多 {desc.get("max_rounds")} 轮，直到门禁通过</span></div>'
        )
    elif kind == "sequence":
        parts.append(f'<div class="fnode {css}">⟲ {html.escape(name)}</div>')
    else:
        parts.append(f'<div class="fnode {css}">▭ {html.escape(name)}</div>')

    children = desc.get("children") or []
    body = desc.get("body")
    if isinstance(body, dict):
        children = [body]
    for child in children:
        if isinstance(child, dict):
            parts.append(_flow_boxes(child, depth + 1))
    return "".join(parts)


def _final_source_files(outcome: RunOutcome) -> dict[str, str]:
    """读取工作副本里的最终源码（报告里展示用）。"""
    files: dict[str, str] = {}
    workdir = Path(outcome.workdir) if outcome.workdir else None
    if workdir is None or not workdir.is_dir():
        return files
    for path in sorted(workdir.rglob("*.py")):
        if any(part in {"__pycache__", ".git"} for part in path.parts):
            continue
        try:
            files[path.relative_to(workdir).as_posix()] = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover
            continue
    return files


def _artifact_tree(root: Path, *, limit: int = 60) -> list[str]:
    """运行目录的文件树（截断到 ``limit`` 条，避免报告被产物清单淹没）。"""
    lines: list[str] = []
    if not root.is_dir():
        return [f"(目录不存在：{root})"]
    entries = sorted(root.rglob("*"))
    for index, path in enumerate(entries):
        if index >= limit:
            lines.append("… (更多文件已省略)")
            break
        if any(part in {"__pycache__"} for part in path.parts):
            continue
        relative = path.relative_to(root)
        prefix = "└── " if path.is_file() else "├── "
        size = f"  ({path.stat().st_size} B)" if path.is_file() else ""
        lines.append(f"{prefix}{relative.as_posix()}{size}")
    return lines


__all__ = [
    "render_ascii_tree",
    "render_html",
    "render_markdown",
    "render_mermaid",
    "write_final_reports",
]
