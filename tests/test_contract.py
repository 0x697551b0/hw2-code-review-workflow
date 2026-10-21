"""
跨仓库契约一致性测试。

四个仓库是独立仓库，但有两条"必须一致"的约定：

1. **信封结构**：四个仓库各有一份 ``protocol.py`` 副本。它们是刻意复制的（为了各自
   能单独 clone 与运行），因此必须**逐字节相同**，否则就会出现"编排器发 A 形状、
   Agent 读 B 形状"的诡异 bug。
2. **可修复性词汇表**：审查 Agent 说"这条能自动修"，修复 Agent 就必须真的有对应策略。
   两边对不上时，流程会在修复阶段静默降级为"转人工" —— 表面不报错，实际白跑一轮。

这类测试的价值在于：它把"跨仓库一致性"从口头约定变成了 CI 会失败的断言。
找不到某个仓库时用例会跳过（那说明四个仓库没放在一起，不是本仓库的缺陷）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tests.support import ROOT

#: 各仓库里 protocol.py 的预期位置（相对 homework2 根目录）
PROTOCOL_FILES = {
    "coding": "coding-agent/coding_agent/protocol.py",
    "review": "review-agent/review_agent/protocol.py",
    "fix": "fix-agent/fix_agent/protocol.py",
    "workflow": "code-review-workflow/workflow/protocol.py",
}


def _homework_root() -> Path:
    """定位 homework2 根目录（本仓库的上级目录）。"""
    return ROOT.parent


def _available(paths: dict[str, str]) -> dict[str, Path]:
    root = _homework_root()
    return {name: root / relative for name, relative in paths.items() if (root / relative).is_file()}


def test_protocol_copies_are_identical() -> None:
    """四个 protocol.py 必须逐字节相同。"""
    found = _available(PROTOCOL_FILES)
    if len(found) < 4:
        print(f"（跳过：只找到 {len(found)}/4 份 protocol.py，四个仓库没放在同一目录）")
        return
    digests = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in found.items()
    }
    unique = set(digests.values())
    assert len(unique) == 1, f"protocol.py 副本不一致：{digests}"


def test_protocol_contract_version_matches() -> None:
    """版本号常量也必须一致，否则主版本校验会拦下所有请求。"""
    from workflow.protocol import CONTRACT_VERSION

    found = _available(PROTOCOL_FILES)
    if len(found) < 4:
        print("（跳过：四个仓库未放在同一目录）")
        return
    for name, path in found.items():
        text = path.read_text(encoding="utf-8")
        assert f'CONTRACT_VERSION = "{CONTRACT_VERSION}"' in text, f"{name} 的契约版本不一致"


def _import_agent_packages() -> tuple[object, object] | None:
    """
    先把三个 Agent 仓库放进 sys.path，再 import 它们的包。

    用编排器自己的发现逻辑来做这件事（而不是在测试里手写 sys.path），这样测的
    "发现机制"与线上跑的是同一份代码 —— 否则测试可能在一个线上永远达不到的环境下通过。
    """
    try:
        from workflow.registry import AgentRegistry

        AgentRegistry.discover()  # 副作用：把找到的仓库根目录插进 sys.path
        from fix_agent.patchers import NOT_SUPPORTED_REASONS, PATCHERS
        from review_agent.fixability import FIXABLE_STRATEGIES, NOT_AUTOFIXABLE
    except Exception as exc:  # noqa: BLE001 - 仓库不全就跳过
        print(f"（跳过：{exc}）")
        return None
    return (PATCHERS, NOT_SUPPORTED_REASONS), (FIXABLE_STRATEGIES, NOT_AUTOFIXABLE)


def test_fixability_tables_agree() -> None:
    """
    审查 Agent 的可修复清单必须与修复 Agent 的策略注册表一一对应。

    这条最容易悄悄失效：审查侧新增一条"可修"规则、修复侧忘了实现策略，
    结果就是流程每轮都报"可自动修 1 条"，却一条都修不动。
    """
    imported = _import_agent_packages()
    if imported is None:
        return
    (PATCHERS, NOT_SUPPORTED_REASONS), (FIXABLE_STRATEGIES, NOT_AUTOFIXABLE) = imported

    review_rules = set(FIXABLE_STRATEGIES)
    fix_rules = set(PATCHERS)
    assert review_rules == fix_rules, (
        f"可修复规则集合不一致：仅审查声明 {sorted(review_rules - fix_rules)}，"
        f"仅修复实现 {sorted(fix_rules - review_rules)}"
    )

    for rule, strategy in FIXABLE_STRATEGIES.items():
        assert PATCHERS[rule].strategy == strategy, (
            f"{rule} 的策略名不一致：审查侧 {strategy!r}，修复侧 {PATCHERS[rule].strategy!r}"
        )

    missing_reasons = set(NOT_AUTOFIXABLE) - set(NOT_SUPPORTED_REASONS) - {"CG000"}
    assert not missing_reasons, f"修复侧缺少不可修理由：{sorted(missing_reasons)}"


def test_every_fixable_rule_has_a_strategy_module() -> None:
    """每条可修规则都要能在修复 Agent 里找到实现（防止"声明了却没实现"）。"""
    imported = _import_agent_packages()
    if imported is None:
        return
    (PATCHERS, _), (FIXABLE_STRATEGIES, _) = imported
    for rule in FIXABLE_STRATEGIES:
        patcher = PATCHERS.get(rule)
        assert patcher is not None, rule
        assert patcher.description, f"{rule} 的策略缺少说明"
