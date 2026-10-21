"""
Agent 注册表：发现、装配、调用三个 Agent。

三个仓库被 clone 到同一目录是最常见的交付形态，因此发现逻辑的第一优先级就是
"在附近找"：

1. ``--agents-home`` 显式指定；
2. ``HW2_AGENTS_HOME`` 环境变量；
3. 当前目录、本仓库、以及它们的上级目录 / 直接子目录里，含 ``<pkg>/__init__.py`` 的那一层。

找到之后只把该层插进 ``sys.path``，**不安装、不改环境**，退出即失效。

调用有两种方式：

- ``inproc``：进程内直接 ``AgentClass(config).handle(request)``。默认。
- ``subprocess``：起一个 ``python -m <pkg>.worker``，用 stdin/stdout 做单次 JSON 往返。
  隔离性更好（Agent 崩了不会带走编排器），也演示了"契约是 JSON 而不是 Python 对象"
  这个设计的好处 —— 同一条流水线，换个传输方式就能跑在别的容器里。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from workflow.config import AgentSettings
from workflow.errors import AgentNotFound, NodeFailed, NodeTimeout
from workflow.protocol import AgentRequest, AgentResponse

#: 编排器里的 Agent 名 → (包名, Agent 类名, 配置类名)
AGENT_SPECS: dict[str, tuple[str, str, str]] = {
    "coding": ("coding_agent", "CodingAgent", "CodingConfig"),
    "review": ("review_agent", "ReviewAgent", "ReviewConfig"),
    "fix": ("fix_agent", "FixAgent", "FixConfig"),
}

#: 人类可读的名字
AGENT_LABELS: dict[str, str] = {
    "coding": "编码 Agent",
    "review": "审查 Agent",
    "fix": "修复 Agent",
}

#: 环境变量：额外的包搜索根目录
ENV_AGENTS_HOME = "HW2_AGENTS_HOME"

#: 向上搜索的最大层数
_MAX_ANCESTORS = 5


@dataclass
class AgentHandle:
    """一个已装配好的 Agent 的调用句柄。"""

    name: str
    package: str
    module: ModuleType
    settings: AgentSettings
    #: 包所在目录（subprocess 传输时作为工作目录）
    home: Path
    agent_cls: type = field(default=None)  # type: ignore[assignment]
    config_cls: type = field(default=None)  # type: ignore[assignment]

    @property
    def label(self) -> str:
        return AGENT_LABELS.get(self.name, self.name)

    # 构造
    def build_options(self) -> dict[str, Any]:
        """把编排器的选项翻译成 Agent 自己配置类的关键字参数。"""
        options = self.settings.merged_options(self.name)
        if self.name == "review" and "blocking" not in options:
            # 门禁口径必须与编排器一致，否则会出现"审查说不合格、编排器说合格"
            options.setdefault("blocking", None)
        return {k: v for k, v in options.items() if v is not None}

    def create(self) -> Any:
        """实例化 Agent。配置类与 Agent 类都由包自己导出。"""
        options = self.build_options()
        options = {k: v for k, v in options.items() if v is not None}
        try:
            config = self.config_cls(**options)  # type: ignore[misc]
        except TypeError:
            # 配置类不接受这些键：交给它的 from_env 走默认值，并提示一声
            config = self.config_cls.from_env(**options)  # type: ignore[attr-defined]
        return self.agent_cls(config)  # type: ignore[misc]

    # 调用
    def send(self, request: AgentRequest, *, timeout: float | None = None) -> AgentResponse:
        """按配置的传输方式调用 Agent。"""
        started = time.perf_counter()
        if self.settings.transport == "subprocess":
            response = self._send_subprocess(request, timeout=timeout or self.settings.timeout)
        else:
            response = self._send_inproc(request)
        elapsed = time.perf_counter() - started
        response.metrics.setdefault("elapsed_seconds", round(elapsed, 3))
        return response

    def _send_inproc(self, request: AgentRequest) -> AgentResponse:
        try:
            agent = self.create()
        except Exception as exc:  # noqa: BLE001 - 装配失败要变成契约内的失败响应
            return AgentResponse.failure(
                self.name, f"装配 {self.label} 失败：{type(exc).__name__}: {exc}"
            )
        try:
            return agent.handle(request)
        finally:
            close = getattr(agent, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                    pass

    def _send_subprocess(self, request: AgentRequest, *, timeout: float) -> AgentResponse:
        with tempfile.TemporaryDirectory(prefix="wf_agent_") as tmp:
            request_file = Path(tmp) / "request.json"
            response_file = Path(tmp) / "response.json"
            request_file.write_text(
                json.dumps(request.to_dict(), ensure_ascii=False), encoding="utf-8", newline="\n"
            )
            command = [
                sys.executable,
                "-m",
                f"{self.package}.worker",
                "--request-file",
                str(request_file),
                "--response-file",
                str(response_file),
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(self.home),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise NodeTimeout(
                    f"{self.label} 子进程超时（>{timeout:.0f}s）", node=self.name, retryable=True
                ) from exc
            except OSError as exc:
                raise NodeFailed(
                    f"无法启动 {self.label} 子进程：{exc}", node=self.name
                ) from exc

            if not response_file.is_file():
                detail = (completed.stderr or completed.stdout or "").strip()[-800:]
                raise NodeFailed(
                    f"{self.label} 子进程未产出响应（退出码 {completed.returncode}）：{detail}",
                    node=self.name,
                )
            try:
                payload = json.loads(response_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise NodeFailed(f"{self.label} 响应不是合法 JSON：{exc}", node=self.name) from exc
            return AgentResponse.from_dict(payload)

    # 健康检查
    def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "package": self.package,
            "transport": self.settings.transport,
            "home": str(self.home),
            "version": getattr(self.module, "__version__", "unknown"),
        }
        try:
            self.create()
            info["ok"] = True
        except Exception as exc:  # noqa: BLE001 - 健康检查不该抛
            info["ok"] = False
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info


class AgentRegistry:
    """三个 Agent 的集合。未发现的 Agent 记在 ``missing`` 里，而不是直接崩。"""

    def __init__(self) -> None:
        self.handles: dict[str, AgentHandle] = {}
        self.missing: dict[str, str] = {}
        self.search_roots: list[Path] = []

    # 发现
    @classmethod
    def discover(
        cls,
        *,
        agents_home: str | Path | None = None,
        settings: dict[str, AgentSettings] | None = None,
    ) -> "AgentRegistry":
        registry = cls()
        registry.search_roots = _candidate_roots(agents_home)
        settings = settings or {}
        for name, (package, agent_cls_name, config_cls_name) in AGENT_SPECS.items():
            agent_settings = settings.get(name) or AgentSettings()
            if not agent_settings.enabled:
                registry.missing[name] = "已在配置中禁用"
                continue
            module = _ensure_package(package, registry.search_roots)
            if module is None:
                registry.missing[name] = f"找不到包 {package}（搜索了 {len(registry.search_roots)} 个候选根目录）"
                continue
            agent_cls = getattr(module, agent_cls_name, None)
            config_cls = getattr(module, config_cls_name, None)
            if agent_cls is None or config_cls is None:
                registry.missing[name] = (
                    f"{package} 里没有导出 {agent_cls_name}/{config_cls_name}"
                )
                continue
            home = Path(getattr(module, "__file__", "")).resolve().parents[1]
            registry.handles[name] = AgentHandle(
                name=name,
                package=package,
                module=module,
                settings=agent_settings,
                home=home,
                agent_cls=agent_cls,
                config_cls=config_cls,
            )
        return registry

    # 查询
    def get(self, name: str) -> AgentHandle:
        handle = self.handles.get(name)
        if handle is None:
            reason = self.missing.get(name, "未发现")
            raise AgentNotFound(
                f"找不到 {AGENT_LABELS.get(name, name)}（{name}）：{reason}\n"
                "请把四个仓库放在同一个目录下，或用 --agents-home 指定该目录。"
            )
        return handle

    def has(self, name: str) -> bool:
        return name in self.handles

    def names(self) -> list[str]:
        return sorted(self.handles)

    def require(self, *names: str) -> None:
        """要求这些 Agent 都在；缺任何一个就抛错（启动时快速失败）。"""
        missing = [n for n in names if n not in self.handles]
        if missing:
            details = "；".join(f"{n}（{self.missing.get(n, '未发现')}）" for n in missing)
            raise AgentNotFound(
                f"缺少必要的 Agent：{details}\n"
                f"已搜索 {len(self.search_roots)} 个候选根目录；可用 --agents-home 显式指定。"
            )

    def health(self) -> list[dict[str, Any]]:
        rows = [handle.health() for handle in self.handles.values()]
        for name, reason in self.missing.items():
            rows.append({"name": name, "label": AGENT_LABELS.get(name, name), "ok": False, "error": reason})
        return sorted(rows, key=lambda row: row["name"])

    def describe(self) -> dict[str, Any]:
        return {
            "found": {name: str(handle.home) for name, handle in self.handles.items()},
            "missing": dict(self.missing),
            "search_roots": [str(root) for root in self.search_roots[:12]],
        }


# 包发现
def _ensure_package(package: str, roots: list[Path]) -> ModuleType | None:
    """在候选根目录里找 ``package/__init__.py``，找到就插进 sys.path 并导入。"""
    try:
        return importlib.import_module(package)
    except Exception:  # noqa: BLE001 - 导入失败当作"没装"
        pass
    for root in roots:
        try:
            if not (root / package / "__init__.py").is_file():
                continue
        except OSError:  # pragma: no cover
            continue
        entry = str(root)
        sys.modules.pop(package, None)
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)
        importlib.invalidate_caches()
        try:
            return importlib.import_module(package)
        except Exception:  # noqa: BLE001 - 这个根目录不合适，继续找下一个
            continue
    return None


def _candidate_roots(explicit: str | Path | None) -> list[Path]:
    roots: list[Path] = []

    def add(candidate: Path | str | None) -> None:
        if candidate is None:
            return
        try:
            resolved = Path(candidate).expanduser().resolve()
        except OSError:  # pragma: no cover
            return
        if resolved not in roots:
            roots.append(resolved)

    if explicit:
        add(Path(explicit))
        add(Path(explicit).parent)

    for entry in (os.environ.get(ENV_AGENTS_HOME) or "").split(os.pathsep):
        if entry.strip():
            add(entry.strip())

    _walk_up(Path.cwd(), add)
    _walk_up(Path(__file__).resolve().parents[1], add)
    return roots


def _walk_up(start: Path, add) -> None:
    node = start
    for _ in range(_MAX_ANCESTORS):
        add(node)
        try:
            for child in node.iterdir():
                if child.is_dir():
                    add(child)
        except OSError:  # pragma: no cover
            pass
        if node.parent == node:
            break
        node = node.parent


__all__ = [
    "AGENT_LABELS",
    "AGENT_SPECS",
    "AgentHandle",
    "AgentRegistry",
    "ENV_AGENTS_HOME",
]
