"""测试辅助：临时目录、伪 Agent、断言小工具。

编排器的单元测试不能依赖那三个真实仓库，不然"编排逻辑对不对"会被"隔壁仓库今天改没改"
带偏，测试也就没了定位能力。所以这里用 :class:`FakeHandle` 造一个可编排的假 Agent，只
实现契约要求的 ``send``。真实 Agent 只在 ``test_e2e.py`` 里出场，找不到就跳过。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]


def _candidates() -> list[Path]:
    out: list[Path] = []
    for key in ("TMP", "TEMP", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            out.append(Path(value))
    out.append(Path(tempfile.gettempdir()))
    out.append(ROOT / ".testtmp")
    return out


@contextmanager
def tempdir(prefix: str = "wftest_") -> Iterator[Path]:
    created: Path | None = None
    for base in _candidates():
        try:
            base.mkdir(parents=True, exist_ok=True)
            created = Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))
            break
        except OSError:
            continue
    if created is None:  # pragma: no cover
        raise RuntimeError("找不到可写的临时目录")
    try:
        yield created
    finally:
        shutil.rmtree(created, ignore_errors=True)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def expect_raises(exc_type: type, func, *args, **kwargs) -> BaseException:
    try:
        func(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"期望 {exc_type.__name__}，实际抛出 {type(exc).__name__}: {exc}") from exc
    raise AssertionError(f"期望抛出 {exc_type.__name__}，但没有任何异常")


def finding(rule: str, severity: str, line: int = 1, *, fixable: bool = True, file: str = "mod.py") -> dict:
    """构造一条契约形态的发现。"""
    return {
        "finding_id": f"F-{rule}-{line}",
        "rule_id": rule,
        "severity": severity,
        "category": "security" if severity in {"critical", "high"} else "style",
        "file": file,
        "line": line,
        "title": f"{rule} 问题",
        "evidence": "",
        "explanation": "",
        "suggestion": "",
        "source": "static",
        "fixable": fixable,
        "confidence": None,
        "status": "open",
        "note": "",
    }


class FakeHandle:
    """可编排的假 Agent 句柄。``responses`` 可以是一个固定 payload，也可以是函数。"""

    def __init__(
        self,
        name: str,
        responses: Any,
        *,
        transport: str = "inproc",
        max_attempts: int = 1,
    ) -> None:
        from workflow.config import AgentSettings
        from workflow.registry import AGENT_LABELS

        self.name = name
        self.package = f"{name}_agent"
        self.settings = AgentSettings(transport=transport, max_attempts=max_attempts)
        self._responses = responses
        self._calls = 0
        self.requests: list[Any] = []
        self.label = AGENT_LABELS.get(name, name)
        self.module = None
        self.home = Path(".")
        self.agent_cls = None
        self.config_cls = None

    def send(self, request: Any, *, timeout: float | None = None) -> Any:
        from workflow.protocol import AgentResponse

        self.requests.append(request)
        self._calls += 1
        if callable(self._responses):
            payload = self._responses(request, self._calls)
        elif isinstance(self._responses, list):
            index = min(self._calls - 1, len(self._responses) - 1)
            payload = self._responses[index]
        else:
            payload = self._responses
        if isinstance(payload, AgentResponse):
            return payload
        return AgentResponse(agent=self.name, summary=f"{self.name} 完成", payload=payload or {})

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "ok": True, "transport": self.settings.transport}


class FakeRegistry:
    """包装真实 AgentRegistry，只替换句柄。"""

    def __init__(self, handles: dict[str, FakeHandle]) -> None:
        from workflow.registry import AgentRegistry

        self._inner = AgentRegistry()
        self._inner.handles = dict(handles)  # type: ignore[assignment]
        self._inner.missing = {}
        self._inner.search_roots = [ROOT]

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


def scripted_registry(
    *,
    findings_per_round: list[list[dict]] | None = None,
    fix_applied: int = 3,
    files: int = 1,
) -> FakeRegistry:
    """造一套"审查结果可控"的假 Agent。

    ``findings_per_round``：第 N 次审查返回的发现清单，用它精确驱动"门禁没过 → 修复 →
    复审通过"这条路径。``files``：假编码 Agent 写出几个源文件，≥2 时会走审查阶段的并行
    fan-out，用来验证分片合并。
    """
    rounds = findings_per_round or [
        [finding("CG106", "high", 10), finding("CG203", "high", 20)],
        [],
    ]
    counter = {"review": 0}
    file_names = [f"mod{index}.py" if index else "mod.py" for index in range(max(1, files))]

    def review_response(request: Any, call_no: int) -> dict:
        index = min(counter["review"], len(rounds) - 1)
        counter["review"] += 1
        target = Path(str((request.payload or {}).get("target") or "")).name or "mod.py"
        # 分片的发现得各自归到目标文件上，不然合并时会按 finding_id 去重成一条。
        # 去重本身是对的，只是夹具得让它们真的不一样
        items = [
            {**item, "file": target, "finding_id": f"{item['finding_id']}@{target}"}
            for item in rounds[index]
        ]
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        for item in items:
            counts[item["severity"]] = counts.get(item["severity"], 0) + 1
        blocking = sum(1 for item in items if item["severity"] in {"critical", "high"})
        return {
            "mode": "static",
            "findings": items,
            "counts": counts,
            "total": len(items),
            "blocking": blocking,
            "passed": blocking == 0,
            "fixable": sum(1 for item in items if item["fixable"]),
            "needs_human": sum(1 for item in items if not item["fixable"]),
            "score": 100 - 10 * blocking,
            "overall_risk": "high" if blocking else "clean",
            "files_reviewed": [target],
            "summary": f"{target} 命中 {len(items)} 条",
        }

    def coding_response(request: Any, call_no: int) -> dict:
        """假的编码 Agent，会真的把源文件写出来。

        审查阶段要扫工作目录里的源码，假 Agent 只返回 JSON 不落盘的话，流程会卡在"没有
        可审查的文件"那儿中止，测到的就不是编排逻辑了。所以副作用也得模拟。
        """
        out_dir = Path(str((request.payload or {}).get("out_dir") or "."))
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in file_names:
            (out_dir / name).write_text(
                '"""假 Agent 生成的占位模块。"""\n\n\ndef do_thing(cursor, user_id):\n    return user_id\n',
                encoding="utf-8",
                newline="\n",
            )
        return {
            "module": "demo",
            "title": "演示模块",
            "files": [{"path": name, "language": "python", "lines": 5} for name in file_names],
            "open_questions": ["需要确认边界条件"],
            "total_lines": 5 * len(file_names),
        }

    fix = {
        "applied": [
            {"finding_id": f"F-{index}", "rule_id": "CG106", "file": "mod.py", "line": index}
            for index in range(fix_applied)
        ],
        "rejected": [],
        "deferred": [],
        "files_changed": ["mod.py"],
        "diff": "--- a/mod.py\n+++ b/mod.py\n",
        "summary": f"修复 {fix_applied} 条",
    }
    return FakeRegistry(
        {
            "coding": FakeHandle("coding", coding_response),
            "review": FakeHandle("review", review_response),
            "fix": FakeHandle("fix", fix),
        }
    )


def ensure_root_on_path() -> None:
    entry = str(ROOT)
    if entry not in sys.path:
        sys.path.insert(0, entry)
