"""编排器配置。环境变量前缀 ``WF_``。

Agent 的私有配置由 Agent 自己解释。编排器只管把 ``{"engine": "offline", ...}`` 这样的
键值对递过去，不关心键是什么意思，所以给 Agent 加参数时这边不用改。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from workflow.errors import WorkflowConfigError
from workflow.protocol import DEFAULT_BLOCKING, SEVERITY_ORDER

ENV_PREFIX = "WF_"

#: Agent 出错的处置策略
#: - ``abort``：立刻中止整个流程（默认。不知道结果就不假装成功）
#: - ``continue``：记录失败并继续（适合"审查失败但仍想拿到代码"这类场景）
ERROR_POLICIES: tuple[str, ...] = ("abort", "continue")

#: Agent 调用方式
#: - ``inproc``：进程内直接调用（默认，快、好调试）
#: - ``subprocess``：子进程 JSON 往返（隔离性好，可跨环境部署）
TRANSPORTS: tuple[str, ...] = ("inproc", "subprocess")

#: 三个 Agent 的默认配置。键是编排器内部的 Agent 名。
DEFAULT_AGENT_OPTIONS: dict[str, dict[str, Any]] = {
    "coding": {"engine": "offline", "profile": "realistic"},
    "review": {"mode": "static", "parallel": 4},
    "fix": {"engine": "rules"},
}


def load_dotenv(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """极简 ``.env`` 解析（四个仓库共用的同一实现）。"""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in file_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not key:
            continue
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


@dataclass
class AgentSettings:
    """单个 Agent 的接入配置。"""

    enabled: bool = True
    transport: str = "inproc"
    #: 单次调用超时（秒）
    timeout: float = 300.0
    #: 失败重试次数（不含首次）
    max_attempts: int = 2
    #: 失败后的处置：abort / continue
    on_error: str = "abort"
    #: 透传给 Agent 自己配置类的键值对
    options: dict[str, Any] = field(default_factory=dict)

    def merged_options(self, name: str) -> dict[str, Any]:
        base = dict(DEFAULT_AGENT_OPTIONS.get(name, {}))
        base.update(self.options)
        return base


@dataclass
class WorkflowConfig:
    """一次运行的完整配置。"""

    # ---- 输入输出 ----
    spec_path: str = ""
    #: 运行产物根目录（每轮的工作副本、报告都在这里）
    output_root: str = ""
    #: 工作副本目录名（放在 output_root 下）
    worktree_name: str = "worktree"
    report_dir_name: str = "reports"

    # ---- Agent 发现 ----
    #: 三个 Agent 仓库所在的目录；为空则自动搜索
    agents_home: str = ""
    #: 显式指定 codeguard 源码根目录（可选）
    codeguard_path: str = ""

    # ---- 质量门禁 ----
    blocking: tuple[str, ...] = DEFAULT_BLOCKING
    #: 最多跑几轮"审查 → 修复"
    max_rounds: int = 3
    #: 审查时匹配的文件
    patterns: tuple[str, ...] = ("*.py",)

    # ---- 执行 ----
    coding: AgentSettings = field(default_factory=lambda: AgentSettings(max_attempts=1))
    review: AgentSettings = field(default_factory=lambda: AgentSettings(max_attempts=2))
    fix: AgentSettings = field(default_factory=lambda: AgentSettings(max_attempts=1))

    # ---- 观测 ----
    trace: bool = False
    verbose: bool = False
    #: 保留工作副本（关掉便于对比"原始草案"与"最终代码"）
    keep_worktree: bool = True
    #: 是否在阶段间存档（关掉会失去 resume 能力）
    checkpoint: bool = True

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = ".env", **overrides: Any) -> "WorkflowConfig":
        if dotenv_path is not None:
            load_dotenv(dotenv_path)
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            env_name = f"{ENV_PREFIX}{f.name.upper()}"
            if env_name not in os.environ:
                continue
            raw = os.environ[env_name]
            if f.name in {"coding", "review", "fix"}:
                continue  # 这三个用专门的函数构造
            kwargs[f.name] = _coerce(raw, f.type, f.name)
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        config = cls(**kwargs)
        for name in ("coding", "review", "fix"):
            options_env = os.environ.get(f"{ENV_PREFIX}{name.upper()}_OPTIONS")
            if options_env:
                getattr(config, name).options.update(_parse_options(options_env))
            transport = os.environ.get(f"{ENV_PREFIX}{name.upper()}_TRANSPORT")
            if transport:
                getattr(config, name).transport = transport
        return config

    # 校验
    def validate(self) -> None:
        unknown = [s for s in self.blocking if s not in SEVERITY_ORDER]
        if unknown:
            raise WorkflowConfigError(f"blocking 含有未知级别 {unknown}")
        if not self.blocking:
            raise WorkflowConfigError("blocking 不能为空")
        if self.max_rounds < 1:
            raise WorkflowConfigError("max_rounds 至少为 1（至少跑一轮审查）")
        for name in ("coding", "review", "fix"):
            settings: AgentSettings = getattr(self, name)
            if settings.transport not in TRANSPORTS:
                raise WorkflowConfigError(
                    f"{name}.transport 只能取 {TRANSPORTS}，当前为 {settings.transport!r}"
                )
            if settings.on_error not in ERROR_POLICIES:
                raise WorkflowConfigError(
                    f"{name}.on_error 只能取 {ERROR_POLICIES}，当前为 {settings.on_error!r}"
                )
            if settings.timeout <= 0:
                raise WorkflowConfigError(f"{name}.timeout 必须为正数")

    # 派生路径
    @property
    def agents_home_path(self) -> Path | None:
        return Path(self.agents_home).expanduser().resolve() if self.agents_home else None

    def resolve_output_root(self) -> Path:
        root = Path(self.output_root).expanduser() if self.output_root else Path.cwd() / "runs"
        return root.resolve()

    def describe(self) -> str:
        parts = [
            f"blocking={','.join(self.blocking)}",
            f"max_rounds={self.max_rounds}",
            f"patterns={','.join(self.patterns)}",
            f"agents_home={self.agents_home or '(自动搜索)'}",
        ]
        for name in ("coding", "review", "fix"):
            settings: AgentSettings = getattr(self, name)
            options = ",".join(f"{k}={v}" for k, v in settings.options.items())
            parts.append(f"{name}[{settings.transport}{'/' + options if options else ''}]")
        return "  ".join(parts)


def _parse_options(raw: str) -> dict[str, str]:
    """把 ``CODE=a;PROFILE=b`` 这样的字符串解析成字典（环境变量用）。"""
    options: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            if key.strip():
                options[key.strip()] = value.strip()
    return options


def _coerce(raw: str, type_hint: Any, name: str) -> Any:
    hint = type_hint if isinstance(type_hint, str) else getattr(type_hint, "__name__", "str")
    text = raw.strip()
    if hint == "bool":
        return text.lower() in {"1", "true", "yes", "y", "on"}
    if hint == "int":
        try:
            return int(text)
        except ValueError as exc:
            raise WorkflowConfigError(f"环境变量 {ENV_PREFIX}{name.upper()} 应为整数") from exc
    if hint == "float":
        try:
            return float(text)
        except ValueError as exc:
            raise WorkflowConfigError(f"环境变量 {ENV_PREFIX}{name.upper()} 应为小数") from exc
    if hint.startswith("tuple"):
        return tuple(part.strip() for part in text.split(",") if part.strip())
    return text


__all__ = [
    "AgentSettings",
    "DEFAULT_AGENT_OPTIONS",
    "ENV_PREFIX",
    "ERROR_POLICIES",
    "TRANSPORTS",
    "WorkflowConfig",
    "load_dotenv",
]
