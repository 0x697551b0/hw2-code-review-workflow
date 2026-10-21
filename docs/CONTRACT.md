# Agent 契约（CONTRACT）

> 本文是四个仓库之间**唯一**的接口约定。改这里之前，先想清楚四个仓库要怎么一起升级。

## 1. 为什么需要一份独立于代码的契约文档

四个仓库是**四个独立仓库**，各自能单独 clone、单独跑测试。这意味着：

- 编排器不能 `import` 任何 Agent 的内部类型；
- Agent 也不能假设编排器一定存在。

于是双方只剩一个共同点：**JSON 信封**。信封的形状写在这里，四个仓库各持一份
`protocol.py` 副本（刻意复制 150 行换取零耦合依赖），并由编排器仓库的
`tests/test_contract.py` 用**哈希比对**保证四份副本逐字节相同。

---

## 2. 信封格式

### 2.1 请求：编排器 → Agent

```json
{
  "contract_version": "1.0",
  "agent": "review",
  "task_id": "review-r1",
  "run_id": "run-20261022-153000-a1b2",
  "round": 1,
  "objective": "对编码 Agent 的产出做静态审查并给出质量门禁结论",
  "payload": {
    "target": "/path/to/worktree",
    "report_dir": "/path/to/runs/<run-id>/reports/review-r1",
    "blocking": ["critical", "high"]
  },
  "shared": { "spec": { "module": "user_store" }, "code": { "files": ["user_store.py"] } },
  "metadata": { "node": "pipeline/2:review-fix-loop/#1/0:review", "attempt": 1, "transport": "inproc" }
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `contract_version` | string | ✅ | 主版本不一致时 Agent 必须拒收（返回 failed） |
| `agent` | string | ✅ | `coding` / `review` / `fix`，与接收方不符时拒收 |
| `task_id` | string | ✅ | 任务标识，写进日志便于对齐 |
| `run_id` | string | | 整次运行的 ID |
| `round` | int | | 第几轮（编码阶段为 0 或 1） |
| `objective` | string | | 本次任务的自然语言目标 |
| `payload` | object | ✅ | **Agent 私有**参数，编排器不解释 |
| `shared` | object | | 共享状态的**只读快照**（深拷贝） |
| `metadata` | object | | 编排器附加信息（节点路径、重试次数、传输方式等） |

### 2.2 响应：Agent → 编排器

```json
{
  "agent": "review",
  "status": "ok",
  "summary": "审查 1 个文件，命中 15 条；❌ 未通过门禁（3 条阻断级）；健康分 38",
  "payload": {
    "findings": [ /* FindingContract 数组 */ ],
    "counts": { "critical": 1, "high": 2, "medium": 3, "low": 5, "info": 4 },
    "passed": false,
    "blocking": 3,
    "fixable": 7,
    "score": 38,
    "files_reviewed": ["user_store.py"]
  },
  "artifacts": [
    { "kind": "review_report", "path": "review.json", "media_type": "application/json", "description": "15 条发现" }
  ],
  "metrics": { "elapsed_seconds": 0.07, "findings": 15 },
  "diagnostics": { "mode": "static", "codeguard_version": "1.0.0" },
  "error": ""
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | `ok` / `failed` / `skipped` |
| `summary` | string | 一句话结论，编排器直接写进日志与报告 |
| `payload` | object | Agent 私有产出 |
| `artifacts` | array | 可寻址产物；`path` 相对于 `payload.report_dir` |
| `metrics` | object | 耗时、条数等数值指标 |
| `diagnostics` | object | 引擎、版本、模型等排障信息 |
| `error` | string | `status != ok` 时必须非空 |

> **硬规则**：`status == "failed"` 且 `error` 为空，是非法响应。编排器会把它当失败处理，
> 但日志里会出现"未提供错误信息"，相当于丢掉了一半排障线索。

### 2.3 状态码与编排器行为

| Agent 响应 | 编排器行为 |
| --- | --- |
| `ok` | 继续下一个节点 |
| `failed` | 按 `max_attempts` 重试；用尽后按 `on_error` 决定中止或继续 |
| `skipped` | 视为成功但无产出（例如"没有需要修复的问题"） |
| 超时 / 子进程无响应 | 视为可重试失败 |

---

## 3. FindingContract：三个 Agent 共用的"发现"结构

审查 Agent 生产它，修复 Agent 消费它并更新 `status`，编排器用它做门禁。

```json
{
  "finding_id": "F1A2B3C4",
  "rule_id": "CG106",
  "severity": "high",
  "category": "security",
  "file": "user_store.py",
  "line": 13,
  "title": "SQL 语句由拼接/格式化构造",
  "evidence": "cursor.execute(f\"INSERT INTO users ...\")",
  "explanation": "把外部输入直接拼进 SQL 会造成注入",
  "suggestion": "使用参数化查询",
  "source": "static",
  "fixable": true,
  "confidence": null,
  "status": "open",
  "note": ""
}
```

### 3.1 `finding_id` 必须跨轮次稳定

`finding_id` 由 **规则 + 文件 + 归一化标题** 哈希得到，**不含行号**。

原因：第 1 轮修复会插入/删除代码，第 2 轮复审时行号整体位移。若 ID 由行号构造，
那么"这条到底修好了没有"就永远对不上 —— 而这正是闭环的核心问题。

同一文件里同规则同标题的重复命中，用序号后缀区分（`F1A2B3C4`, `F1A2B3C4-2`）。

### 3.2 `severity` 取值

`critical` / `high` / `medium` / `low` / `info`，与 codeguard 保持一致。

"哪些级别算不合格"由**编排器配置**决定（默认 `critical, high`），并作为
`payload.blocking` 下发给审查 Agent —— 门禁口径只有一处权威来源。

---

## 4. 可修复性词汇表（审查 ↔ 修复）

审查 Agent 的 `fixability.py` 与修复 Agent 的 `patchers.py` 必须一一对应。
**两边不一致是本项目最危险的静默失败**：审查说"能自动修"，修复却没有策略，
流程每轮都报"可自动修 N 条"，实际一条都修不动。

| 规则 | 策略名 | 做什么 |
| --- | --- | --- |
| `CG105` | `move_secret_to_env` | 字面量密钥 → `os.environ[...]` |
| `CG106` | `parameterize_sql` | f-string / `%` 拼接 SQL → 占位符 + 参数元组 |
| `CG202` | `log_broad_except` | 给宽泛 except 补 `logger.exception`（保留原降级行为） |
| `CG203` | `sentinel_default` | 可变默认参数 → `None` 哨兵 |
| `CG204` | `identity_comparison` | `== None` → `is None` |
| `CG206` | `context_manager` | `open()` → `with` |
| `CG207` | `explicit_validation` | `assert` → `if not ...: raise ValueError` |
| `CG402` | `strip_trailing_whitespace` | 删行尾空白 |
| `CG403` | `drop_unused_import` | 删未使用的导入 |

判"可修"的三条标准（缺一不可）：改写是**局部的**、是**确定的**、是**可验证的**。
其余规则一律判为不可修，并给出"为什么要人来看"。

一致性由 `code-review-workflow/tests/test_contract.py` 断言。

---

## 5. 两种传输方式

同一个信封既能走函数调用，也能走进程边界：

```bash
# 进程内（默认）：编排器直接 AgentClass(config).handle(request)
python -m workflow run --spec demo/spec_user_store.md

# 子进程：python -m <pkg>.worker --request-file req.json --response-file resp.json
python -m workflow run --spec demo/spec_user_store.md --transport subprocess
```

子进程协议的两条纪律：

1. **stdout 只允许出现那一个 JSON 对象**，日志一律写 stderr；
2. **退出码表达结果**（0=ok，1=failed），但**判定以响应文件内容为准** ——
   因为进程可能在写响应前就崩了，此时退出码不可信。

---

## 6. 演进规则

| 变更类型 | 做法 |
| --- | --- |
| 新增**可选**字段 | 直接加，默认值保证旧 Agent 能跑；`CONTRACT_VERSION` 次版本 +1 |
| 新增**必填**字段 | 不允许。改为"编排器下发 + Agent 有默认值" |
| 改动已有字段语义 | `CONTRACT_VERSION` 主版本 +1，四个仓库同步升级 |
| 删除字段 | 主版本 +1 |

四个仓库的 `protocol.py` 必须**逐字节相同**，任何一处的改动都要同步复制到另外三处，
再跑一遍 `test_contract.py`。
