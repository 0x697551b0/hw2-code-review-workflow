"""共享状态与存档（state.py）的测试。

断点续跑的可靠性完全取决于这一层，因此重点打三个地方：

1. 点号路径读写（报告与阶段大量使用）；
2. 存档写入的**原子性**（不能留下半截 JSON，否则 resume 直接失败）；
3. 版本不兼容时给出可执行的提示，而不是让 JSON 解析错误冒出来。
"""

from __future__ import annotations

import json
from pathlib import Path

from workflow.errors import CheckpointError
from workflow.state import CHECKPOINT_VERSION, SharedState

from tests.support import expect_raises, read, tempdir, write


def test_mapping_interface() -> None:
    state = SharedState("r1")
    state["gate"] = {"passed": True}
    assert state["gate"]["passed"] is True
    assert "gate" in state
    assert len(state) == 1
    del state["gate"]
    assert len(state) == 0


def test_dotted_path_read_write() -> None:
    state = SharedState("r1")
    state.set_path("gate.passed", True)
    state.set_path("gate.counts.high", 3)
    assert state.get_path("gate.passed") is True
    assert state.get_path("gate.counts.high") == 3
    assert state.get_path("gate.missing", "fallback") == "fallback"
    assert state.get_path("nope.deep.path", None) is None


def test_completed_markers() -> None:
    state = SharedState("r1")
    state.mark_done("pipeline/1:review")
    state.mark_done("pipeline/1:review")  # 幂等
    assert state.is_done("pipeline/1:review")
    assert not state.is_done("pipeline/2:review")
    assert state.count_done() == 1
    assert state.completed == ["pipeline/1:review"]


def test_cursors() -> None:
    state = SharedState("r1")
    assert state.get_cursor("loop") == 0
    state.set_cursor("loop", 3)
    assert state.get_cursor("loop") == 3


def test_snapshot_is_deep_copy() -> None:
    state = SharedState("r1", data={"code": {"files": [{"path": "a.py"}]}})
    snapshot = state.snapshot()
    snapshot["code"]["files"][0]["path"] = "hacked.py"
    assert state["code"]["files"][0]["path"] == "a.py", "快照必须是深拷贝"


def test_save_and_load_roundtrip() -> None:
    with tempdir() as tmp:
        path = tmp / "state.json"
        state = SharedState("r1")
        state.bind(path)
        state["spec"] = {"module": "demo"}
        state.mark_done("pipeline/0:prepare")
        state.set_cursor("loop", 2)
        state.note("started_at", "2026-01-01T00:00:00Z")
        state.save()
        assert path.is_file()

        restored = SharedState.load(path)
        assert restored.run_id == "r1"
        assert restored["spec"]["module"] == "demo"
        assert restored.is_done("pipeline/0:prepare")
        assert restored.get_cursor("loop") == 2
        assert restored.meta["started_at"] == "2026-01-01T00:00:00Z"


def test_save_is_atomic_no_tmp_left() -> None:
    with tempdir() as tmp:
        path = tmp / "state.json"
        state = SharedState("r1")
        state.bind(path)
        state["x"] = 1
        state.save()
        leftovers = [p.name for p in tmp.iterdir() if p.suffix == ".tmp"]
        assert not leftovers, leftovers
        json.loads(read(path))  # 必须是完整 JSON


def test_skip_write_when_clean() -> None:
    with tempdir() as tmp:
        path = tmp / "state.json"
        state = SharedState("r1")
        state.bind(path)
        state["x"] = 1
        state.save()
        mtime = path.stat().st_mtime_ns
        state.save()  # 无改动
        assert path.stat().st_mtime_ns == mtime, "无改动时不应重复写盘"


def test_load_missing_file_raises() -> None:
    expect_raises(CheckpointError, SharedState.load, "/definitely/not/here.json")


def test_load_corrupt_file_raises() -> None:
    with tempdir() as tmp:
        path = write(tmp / "state.json", "{ not json ")
        expect_raises(CheckpointError, SharedState.load, path)


def test_version_mismatch_raises_with_hint() -> None:
    with tempdir() as tmp:
        payload = {"run_id": "r1", "version": CHECKPOINT_VERSION + 99, "data": {}}
        path = write(tmp / "state.json", json.dumps(payload))
        error = expect_raises(CheckpointError, SharedState.load, path)
        assert "版本" in str(error)


def test_missing_run_id_rejected() -> None:
    with tempdir() as tmp:
        payload = {"version": CHECKPOINT_VERSION, "data": {}}
        path = write(tmp / "state.json", json.dumps(payload))
        expect_raises(CheckpointError, SharedState.load, path)


def test_checkpoint_payload_is_plain_json() -> None:
    """存档必须是纯 JSON（不用 pickle）：可读、可 diff、跨版本安全。"""
    state = SharedState("r1", data={"a": 1})
    payload = state.to_checkpoint().to_dict()
    assert json.loads(json.dumps(payload))["run_id"] == "r1"
    assert set(payload) >= {"run_id", "version", "data", "completed", "cursors", "meta"}


def test_summary_is_informative() -> None:
    state = SharedState("r1")
    state["gate"] = {}
    state.mark_done("a")
    summary = state.summary()
    assert summary["run_id"] == "r1"
    assert "gate" in summary["keys"]
    assert summary["completed"] == 1
