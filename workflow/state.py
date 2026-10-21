"""
共享状态：流程的唯一事实来源（single source of truth）。

两种数据在这里汇合：

1. **领域数据**（``data``）：需求、代码目录、每轮审查结论、每轮修复结果、门禁结论。
   这些是"业务状态"，报告与最终判断都从这里取。
2. **执行状态**（``completed`` / ``cursors``）：哪些节点已经跑完、循环跑到第几轮。
   这些是"控制状态"，``resume`` 完全依赖它。

为什么把两者放在同一个存档里：断点续跑要求"业务状态与执行进度必须一致"。分成两个文件
就会出现"进度说跑完了、业务数据还没落盘"的不一致，恢复后行为不可预测。

存档格式刻意用**扁平的 JSON**（不用 pickle）：可读、可 diff、可手工修改、跨版本安全。
pickle 存档在生产系统里是反模式 —— 它把"读一个数据文件"变成了"执行任意代码"。
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, MutableMapping

from workflow.errors import CheckpointError

#: 存档格式版本。破坏性变更时递增，加载时会校验。
CHECKPOINT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Checkpoint:
    """一份可持久化的运行存档。"""

    run_id: str
    version: int = CHECKPOINT_VERSION
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    data: dict[str, Any] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "data": self.data,
            "completed": self.completed,
            "cursors": self.cursors,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Checkpoint":
        version = int(payload.get("version") or 0)
        if version != CHECKPOINT_VERSION:
            raise CheckpointError(
                f"存档版本不兼容：文件为 v{version}，当前支持 v{CHECKPOINT_VERSION}。"
                "请重新开始一次运行（--no-resume）。"
            )
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            raise CheckpointError("存档缺少 run_id 字段，文件可能已损坏")
        return cls(
            run_id=run_id,
            version=version,
            created_at=str(payload.get("created_at") or _now()),
            updated_at=str(payload.get("updated_at") or _now()),
            data=dict(payload.get("data") or {}),
            completed=[str(x) for x in payload.get("completed") or []],
            cursors={str(k): int(v) for k, v in (payload.get("cursors") or {}).items()},
            meta=dict(payload.get("meta") or {}),
        )


class SharedState(MutableMapping[str, Any]):
    """
    带存档能力的共享状态。

    实现 ``MutableMapping`` 是为了让它用起来就像 dict（``state["gate"]`` 直接读写），
    但额外提供：

    - **点号路径访问**：``state.get_path("gate.passed")``，报告里少写一堆 ``.get().get()``；
    - **节点完成标记**：``mark_done(qid)`` / ``is_done(qid)``，撑起 resume；
    - **存档**：``save()`` / ``load()``。
    """

    def __init__(
        self,
        run_id: str,
        *,
        data: dict[str, Any] | None = None,
        completed: list[str] | None = None,
        cursors: dict[str, int] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self._data: dict[str, Any] = dict(data or {})
        self._completed: set[str] = set(completed or [])
        self._cursors: dict[str, int] = dict(cursors or {})
        self._meta: dict[str, Any] = dict(meta or {})
        self._path: Path | None = None
        #: 自上次存档以来是否发生过写入（避免无意义的写盘）
        self._dirty = False
        #: 存档写互斥（并行节点会同时触发存档）
        self._lock = threading.RLock()
        #: 临时文件名序号
        self._temp_counter = itertools.count(1)

    # MutableMapping 接口
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._dirty = True

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        self._dirty = True

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    # 便捷访问
    def get_path(self, path: str, default: Any = None) -> Any:
        """按点号路径读取，例如 ``state.get_path("gate.passed", False)``。"""
        node: Any = self._data
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return default
        return node

    def set_path(self, path: str, value: Any) -> None:
        """按点号路径写入（中间层不存在时自动建字典）。"""
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
        self._dirty = True

    def update_data(self, values: dict[str, Any]) -> None:
        self._data.update(values)
        self._dirty = True

    # 执行进度
    def mark_done(self, qid: str) -> None:
        if qid not in self._completed:
            self._completed.add(qid)
            self._dirty = True

    def is_done(self, qid: str) -> bool:
        return qid in self._completed

    @property
    def completed(self) -> list[str]:
        """已完成的节点（按完成顺序）。"""
        return sorted(self._completed)

    def count_done(self) -> int:
        return len(self._completed)

    # 循环游标
    def set_cursor(self, path: str, value: int) -> None:
        self._cursors[path] = value
        self._dirty = True

    def get_cursor(self, path: str) -> int:
        return self._cursors.get(path, 0)

    # 元信息
    @property
    def meta(self) -> dict[str, Any]:
        return self._meta

    def note(self, key: str, value: Any) -> None:
        self._meta[key] = value

    # 存档
    @property
    def checkpoint_path(self) -> Path | None:
        return self._path

    def bind(self, path: str | Path) -> Path:
        """绑定存档路径（之后 ``save()`` 默认写到这里）。"""
        self._path = Path(path)
        return self._path

    def to_checkpoint(self) -> Checkpoint:
        return Checkpoint(
            run_id=self.run_id,
            data=self._data,
            completed=sorted(self._completed),
            cursors=dict(self._cursors),
            meta=self._meta,
            updated_at=_now(),
        )

    def save(self, path: str | Path | None = None, *, force: bool = False) -> Path | None:
        """
        写存档。返回写入路径；无路径且未绑定则返回 None。

        **必须可并发调用**：并行节点里每个分片都会在完成时触发一次存档，多个线程可能
        同时进来。两处保护缺一不可：

        - ``_lock``：同一进程内的写互斥；
        - 临时文件名带线程/进程标识：不同线程不会抢同一个 ``.tmp``。曾经用固定
          ``state.json.tmp``，在 Windows 上直接 ``Permission denied`` —— 一个只在并行
          路径下才复现的 bug。
        """
        target = Path(path) if path else self._path
        if target is None:
            return None
        with self._lock:
            if not self._dirty and not force and target.exists():
                return target
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                text = json.dumps(self.to_checkpoint().to_dict(), ensure_ascii=False, indent=2)
                # 先写临时文件再替换：避免中途崩溃留下半截 JSON，导致无法 resume
                tag = f"{os.getpid()}.{threading.get_ident()}.{next(self._temp_counter)}"
                temp = target.with_name(f"{target.name}.{tag}.tmp")
                temp.write_text(text, encoding="utf-8", newline="\n")
                temp.replace(target)
            except OSError as exc:
                raise CheckpointError(f"写入存档失败：{target}（{exc}）") from exc
            self._dirty = False
            return target

    @classmethod
    def load(cls, path: str | Path) -> "SharedState":
        file_path = Path(path)
        if not file_path.is_file():
            raise CheckpointError(f"存档不存在：{file_path}")
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"读取存档失败：{file_path}（{exc}）") from exc
        if not isinstance(payload, dict):
            raise CheckpointError(f"存档格式错误（顶层应为对象）：{file_path}")
        checkpoint = Checkpoint.from_dict(payload)
        state = cls(
            checkpoint.run_id,
            data=checkpoint.data,
            completed=checkpoint.completed,
            cursors=checkpoint.cursors,
            meta=checkpoint.meta,
        )
        state.bind(file_path)
        state._dirty = False
        return state

    # 快照
    def snapshot(self) -> dict[str, Any]:
        """只读快照（交给 Agent 当 ``shared`` 上下文用）。

        做深拷贝：Agent 拿到的是"当时的样子"，它后续怎么改都不会影响编排器。
        """
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    def summary(self) -> dict[str, Any]:
        """执行进度摘要（用于日志与报告）。"""
        return {
            "run_id": self.run_id,
            "keys": sorted(self._data),
            "completed": len(self._completed),
            "cursors": dict(self._cursors),
        }


__all__ = ["CHECKPOINT_VERSION", "Checkpoint", "SharedState"]
