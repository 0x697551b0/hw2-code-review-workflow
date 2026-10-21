"""
命令行入口。

命令设计的取舍：

- ``run`` 是唯一会改文件的命令（编码 + 修复都会写工作副本），因此它默认输出**摘要**，
  完整过程看 ``report.md`` / ``--trace``；
- ``plan`` / ``agents`` / ``graph`` 都是**只读**命令，用来在真正动手前确认环境与流程；
- 退出码严格区分"流程没通过"（1）与"流程跑挂了"（2），CI 里才能据此做不同处置。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Sequence

from workflow import __version__
from workflow.config import WorkflowConfig
from workflow.errors import WorkflowError
from workflow.events import read_events, summarize_events
from workflow.protocol import dumps
from workflow.registry import AGENT_LABELS, AgentRegistry
from workflow.reporting import render_ascii_tree, render_mermaid
from workflow.runner import Orchestrator
from workflow.stages import build_pipeline

BANNER = r"""
 __        ___  __        __ _                 _
 \ \      / / |/ _|  __  / _| | _____      __ | | _____
  \ \ /\ / /| |_  |  |/ /| |_| |/ _ \ \ /\ / / | |/ _ \
   \ V  V / |  _| |    < |  _| | (_) \ V  V /| | | (_) |
    \_/\_/  |_|   |_|\_\|_| |_|\___/ \_/\_/ |_|_|\___/    WF4SE · Homework 2
""".strip("\n")

#: 退出码
EXIT_OK = 0
EXIT_NOT_PASSED = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review-workflow",
        description="多 Agent 代码审查工作流：编码 → 审查 → 修复（含分支、循环、并行与断点续跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m workflow run --spec demo/spec_user_store.md\n"
            "  python -m workflow run --spec demo/spec_user_store.md --trace\n"
            "  python -m workflow run --spec demo/spec_user_store.md --max-rounds 3\n"
            "  python -m workflow run --spec demo/spec_user_store.md --resume runs/<run-id>/state.json\n"
            "  python -m workflow plan\n"
            "  python -m workflow agents\n"
            "  python -m workflow replay runs/<run-id>/events.jsonl\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"code-review-workflow {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # run
    run_cmd = sub.add_parser("run", help="执行完整流程")
    run_cmd.add_argument("--spec", required=True, help="需求文件（Markdown）")
    run_cmd.add_argument("--output-root", help="运行产物根目录（默认 ./runs）")
    run_cmd.add_argument("--agents-home", help="三个 Agent 仓库所在目录（默认自动搜索）")
    run_cmd.add_argument("--codeguard-path", help="codeguard 源码根目录（可选）")
    run_cmd.add_argument("--blocking", action="append", help="阻断级别，可重复；默认 critical + high")
    run_cmd.add_argument("--max-rounds", type=int, help="最多跑几轮审查→修复（默认 3）")
    run_cmd.add_argument("--pattern", action="append", dest="patterns", help="审查文件 glob，可重复")
    run_cmd.add_argument("--coding-engine", choices=("offline", "llm"), help="编码 Agent 引擎")
    run_cmd.add_argument("--coding-profile", choices=("realistic", "clean"), help="编码草案档位")
    run_cmd.add_argument("--review-mode", choices=("static", "llm"), help="审查 Agent 模式")
    run_cmd.add_argument("--fix-engine", choices=("rules", "llm"), help="修复 Agent 引擎")
    run_cmd.add_argument("--transport", choices=("inproc", "subprocess"), help="Agent 调用方式（三个一起设置）")
    run_cmd.add_argument("--resume", metavar="STATE_JSON", help="从存档继续")
    run_cmd.add_argument("--no-checkpoint", action="store_true", help="关闭阶段存档（也就无法 resume）")
    run_cmd.add_argument("--trace", action="store_true", help="实时打印执行轨迹")
    run_cmd.add_argument("--json", action="store_true", help="以 JSON 输出运行结论")
    run_cmd.add_argument("--quiet", action="store_true", help="只输出一行结论")
    run_cmd.add_argument("--verbose", action="store_true")

    # plan
    plan_cmd = sub.add_parser("plan", help="打印流程结构（不执行）")
    plan_cmd.add_argument("--max-rounds", type=int, default=3)
    plan_cmd.add_argument("--json", action="store_true", help="输出流程树的 JSON 描述")
    plan_cmd.add_argument("--mermaid", action="store_true", help="输出 Mermaid 代码")

    # agents
    agents_cmd = sub.add_parser("agents", help="检查三个 Agent 是否可用")
    agents_cmd.add_argument("--agents-home", help="三个 Agent 仓库所在目录")
    agents_cmd.add_argument("--json", action="store_true")

    # replay
    replay_cmd = sub.add_parser("replay", help="回放某次运行的事件日志")
    replay_cmd.add_argument("events", help="events.jsonl 路径")
    replay_cmd.add_argument("--filter", help="只看某个事件类型")
    replay_cmd.add_argument("--json", action="store_true")

    sub.add_parser("info", help="打印版本与环境")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args)

    if not args.command or args.command == "info":
        print(BANNER)
        print(f"\n版本：{__version__}    环境：Python {sys.version.split()[0]}")
        print("\n可用命令：run / plan / agents / replay / info")
        return EXIT_OK

    try:
        if args.command == "plan":
            return _cmd_plan(args)
        if args.command == "agents":
            return _cmd_agents(args)
        if args.command == "replay":
            return _cmd_replay(args)
        return _cmd_run(args)
    except WorkflowError as exc:
        print(f"❌ {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"❌ 找不到文件：{exc}", file=sys.stderr)
        return EXIT_ERROR


def _cmd_run(args: argparse.Namespace) -> int:
    config = _build_config(args)
    orchestrator = Orchestrator(config)

    resumable = False
    if args.resume:
        resumable = orchestrator.resume(args.resume)
    else:
        # 同一次运行目录被复用（极少见）：确保从干净状态开始，避免误跳过节点
        orchestrator.fresh_state()

    outcome = orchestrator.run(resumable=resumable)

    if args.json:
        payload = outcome.to_dict()
        payload["messages"] = orchestrator.bus.transcript()
        print(dumps(payload))
    elif args.quiet:
        print(outcome.outcome)
    else:
        print(_render_outcome(orchestrator, outcome))

    if outcome.outcome == "failed" or outcome.outcome == "aborted":
        return EXIT_ERROR
    if outcome.outcome == "needs_human":
        return EXIT_NOT_PASSED
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace) -> int:
    desc = build_pipeline(max_rounds=args.max_rounds).describe()
    if args.json:
        print(dumps(desc))
    elif args.mermaid:
        print(render_mermaid(desc))
    else:
        print(f"流程结构（max_rounds={args.max_rounds}）")
        print("═" * 68)
        print(render_ascii_tree(desc))
        print("═" * 68)
        print("图例：◉ 组合节点　◇ 条件分支　▭ 业务阶段")
    return EXIT_OK


def _cmd_agents(args: argparse.Namespace) -> int:
    registry = AgentRegistry.discover(agents_home=args.agents_home)
    rows = registry.health()
    if args.json:
        print(dumps({"agents": rows, "search": registry.describe()}))
        return EXIT_OK if all(row.get("ok") for row in rows) else EXIT_NOT_PASSED

    print("Agent 可用性检查")
    print("═" * 68)
    for row in rows:
        label = row.get("label") or AGENT_LABELS.get(row.get("name", ""), row.get("name"))
        if row.get("ok"):
            print(f"  ✅ {row['name']:<7} {label:<10} v{row.get('version', '?'):<8} {row.get('home', '')}")
        else:
            print(f"  ❌ {row['name']:<7} {label:<10} {row.get('error', '不可用')}")
    print("═" * 68)
    found = len(registry.handles)
    print(f"已发现 {found}/3 个 Agent。")
    if found < 3:
        print("提示：把四个仓库放在同一目录下，或用 --agents-home 指定该目录。")
        print(f"已搜索前 8 个候选根目录：")
        for root in registry.describe()["search_roots"][:8]:
            print(f"  - {root}")
        return EXIT_NOT_PASSED
    return EXIT_OK


def _cmd_replay(args: argparse.Namespace) -> int:
    events = read_events(args.events)
    if args.filter:
        events = [event for event in events if event.get("kind") == args.filter]
    if args.json:
        print(dumps(events))
        return EXIT_OK
    for event in events:
        node = event.get("node") or ""
        round_no = event.get("round") or ""
        print(
            f"{event.get('seq'):>4} {event.get('ts', '')[11:23]} "
            f"[{event.get('kind')}] {node} R{round_no} {event.get('message', '')}"
        )
    print(f"\n共 {len(events)} 条事件。")
    summary = summarize_events(events)
    print("类型分布：" + "、".join(f"{k}×{v}" for k, v in sorted(summary["counts"].items())))
    return EXIT_OK


def _render_outcome(orchestrator: Orchestrator, outcome) -> str:
    from workflow.reporting import render_ascii_tree as _tree  # 局部导入避免循环

    lines = [
        BANNER.splitlines()[0],
        "═" * 72,
        f"运行 {outcome.run_id}　结论：{_label(outcome.outcome)}　耗时 {outcome.elapsed_seconds:.2f}s",
        "═" * 72,
    ]
    if outcome.error:
        lines += [f"❗ {outcome.error}", ""]

    if outcome.rounds:
        lines.append("收敛曲线")
        lines.append("  轮次  阻断级  健康分  门禁  修复  转人工")
        for round_summary in outcome.rounds:
            lines.append(
                f"  {round_summary.round:^4}  {round_summary.blocking:^6}  {round_summary.score:^6}  "
                f"{'✅' if round_summary.passed else '❌':^4}  {round_summary.fixed:^4}  {round_summary.rejected:^6}"
            )
        lines.append("")

    lines.append("Agent 消息传递")
    for message in orchestrator.bus.transcript():
        body = message.get("body") or {}
        lines.append(
            f"  {message.get('msg_id')} {message.get('sender'):<12} → {str(message.get('recipient')):<12} "
            f"R{message.get('round') or '-'} {message.get('topic')}"
        )
    lines.append("")

    residual = outcome.residual or []
    if residual:
        lines.append(f"残余非阻断问题 {len(residual)} 条：")
        for finding in residual[:8]:
            lines.append(
                f"  · {finding.get('severity'):<8} {finding.get('rule_id') or '-':<7} "
                f"{finding.get('file')}:{finding.get('line')} {finding.get('title')}"
            )
        if len(residual) > 8:
            lines.append(f"  … 另有 {len(residual) - 8} 条，见报告")
        lines.append("")

    lines += [
        "产物",
        f"  report.md    {orchestrator.run_dir / 'report.md'}",
        f"  report.html  {orchestrator.run_dir / 'report.html'}",
        f"  run.json     {orchestrator.run_dir / 'run.json'}",
        f"  events.jsonl {orchestrator.run_dir / 'events.jsonl'}",
        f"  state.json   {orchestrator.run_dir / 'state.json'}",
        f"  工作副本     {outcome.workdir}",
        "═" * 72,
    ]
    return "\n".join(lines)


def _label(outcome: str) -> str:
    return {
        "passed": "✅ 通过",
        "needs_human": "⚠️ 需人工介入",
        "failed": "❌ 失败",
        "aborted": "⛔ 已中止",
    }.get(outcome, outcome)


def _build_config(args: argparse.Namespace) -> WorkflowConfig:
    overrides: dict[str, object] = {
        "spec_path": args.spec,
        "output_root": args.output_root,
        "agents_home": args.agents_home,
        "codeguard_path": args.codeguard_path,
        "max_rounds": args.max_rounds,
        "trace": args.trace,
        "verbose": args.verbose,
    }
    if args.blocking:
        overrides["blocking"] = tuple(args.blocking)
    if args.patterns:
        overrides["patterns"] = tuple(args.patterns)
    if args.no_checkpoint:
        overrides["checkpoint"] = False

    config = WorkflowConfig.from_env(**{k: v for k, v in overrides.items() if v is not None})

    if args.coding_engine:
        config.coding.options["engine"] = args.coding_engine
    if args.coding_profile:
        config.coding.options["profile"] = args.coding_profile
    if args.review_mode:
        config.review.options["mode"] = args.review_mode
    if args.fix_engine:
        config.fix.options["engine"] = args.fix_engine
    if args.transport:
        for name in ("coding", "review", "fix"):
            getattr(config, name).transport = args.transport
    if config.codeguard_path:
        # 显式指定的 codeguard 路径要同时传给审查与修复两个 Agent
        config.review.options["codeguard_path"] = config.codeguard_path
        config.fix.options["codeguard_path"] = config.codeguard_path

    config.validate()
    return config


def _setup_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
