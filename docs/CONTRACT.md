# Agent 契约（CONTRACT）

> 这份文档是四个仓库之间唯一的接口约定。改这里之前先想清楚四个仓库要怎么一起升级。

## 为什么要有这份文档

四个 Agent 各自是独立仓库，能单独 clone、单独跑测试。所以编排器不能 `import` 任何 Agent 的
内部类型，Agent 也不能假设编排器一定存在。双方只剩一个共同点：JSON 信封。

信封的形状写在这份文档里，四个仓库各放一份内容相同的 `protocol.py` 副本，用复制换零依赖。
副本之间的一致性由编排器仓库的 `tests/test_contract.py` 用哈希比对守住，四份文件必须逐字节
相同。这是整套设计里最"笨"的一处，但也是让四个仓库能各自独立演进的前提。

## 信封长什么样

### 请求：编排器 → Agent

```json
{
  "contract_version": "1.0",
  "agent": "review",
  "task_id": "review-r1",
  "run_id": "run-20261022-052057-ee24",
  "round": 1,
  "objective": "对编码 Agent 的产出做静态审查并给出质量门禁结论",
  "payload": {
    "target": "<run-dir>/worktree",
    "report_dir": "<run-dir>/reports/review-r1",
    "blocking": ["critical", "high"]
  },
  "shared": {
    "paths": { "workdir": "<run-dir>/worktree", "reports": "<run-dir>/reports" },
    "code": { "files": [ { "path": "user_store.py", "lines": 37 } ] },
    "current_round": 1
  },
  "metadata": { "node": "2:review-fix-loop/#1/0:review", "attempt": 1, "transport": "inproc" }
}
```

`shared` 这里是摘出来的片段，真实快照里还有需求文本、需求路径、代码清单、开放问题等字段；
交给 Agent 的是深拷贝，Agent 改了也不会影响编排器。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `contract_version` | string | 必填 | 主版本不一致时 Agent 拒收，返回 `failed` |
| `agent` | string | 必填 | `coding` / `review` / `fix`，和接收方不符就拒收 |
| `task_id` | string | 必填 | 任务标识，写进日志便于对齐 |
| `run_id` | string | 可选 | 整次运行的 ID |
| `round` | int | 可选 | 第几轮，编码阶段是 0 或 1 |
| `objective` | string | 可选 | 本次任务的自然语言目标 |
| `payload` | object | 必填 | Agent 私有参数，编排器不解释 |
| `shared` | object | 可选 | 共享状态的只读快照 |
| `metadata` | object | 可选 | 节点路径、重试次数、传输方式，方便排障 |

### 响应：Agent → 编排器

下面这份是演示运行里 review-r1 的真实响应，`findings` 数组只留了一行注释，其余字段原样：

```json
{
  "agent": "review",
  "status": "ok",
  "summary": "审查 1 个文件，命中 15 条（1 critical、2 high、3 medium、5 low、4 info）；❌ 未通过门禁（3 条阻断级）；健康分 38",
  "payload": {
    "mode": "static",
    "blocking_severities": ["critical", "high"],
    "findings": [ /* FindingContract 数组，15 条 */ ],
    "counts": { "critical": 1, "high": 2, "medium": 3, "low": 5, "info": 4 },
    "total": 15,
    "blocking": 3,
    "passed": false,
    "fixable": 7,
    "needs_human": 8,
    "score": 38,
    "overall_risk": "critical",
    "files_reviewed": ["user_store.py"],
    "summary": "确定性规则引擎审查 1 个文件，命中 15 条问题；结果完全可复现，不含语义推断"
  },
  "artifacts": [
    { "kind": "review_report", "path": "review.json", "media_type": "application/json", "description": "15 条发现" },
    { "kind": "review_markdown", "path": "review.md", "media_type": "text/markdown", "description": "人类可读审查报告" }
  ],
  "metrics": { "elapsed_seconds": 0.006, "findings": 15, "blocking": 3 },
  "diagnostics": {
    "engine": "codeguard.static_analysis", "parallel": 4, "mode": "static",
    "codeguard_version": "1.0.0", "files": 1, "patterns": ["*.py"], "mock": false
  },
  "error": ""
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | `ok` / `failed` / `skipped` |
| `summary` | string | 一句结论，编排器直接写进日志和报告 |
| `payload` | object | Agent 私有产出 |
| `artifacts` | array | 可寻址产物，`path` 相对于 `payload.report_dir` |
| `metrics` | object | 耗时、条数这类数值指标 |
| `diagnostics` | object | 引擎、版本等排障信息 |
| `error` | string | `status != ok` 时必须非空 |

`status` 是 `failed` 而 `error` 为空，这算非法响应。编排器会当失败处理，但日志里只会出现
"未提供错误信息"，等于丢了一半排障线索。

### 状态码与编排器行为

| Agent 响应 | 编排器行为 |
| --- | --- |
| `ok` | 继续下一个节点 |
| `failed` | 按 `max_attempts` 重试；用尽后按 `on_error` 决定中止还是继续 |
| `skipped` | 算成功但没产出，比如"没有需要修复的问题" |
| 超时 / 子进程无响应 | 算可重试失败 |

## FindingContract：三个 Agent 共用的"发现"结构

审查 Agent 生产它，修复 Agent 消费它并更新 `status`，编排器拿它算门禁。演示运行里 CG106 那条
长这样：

```json
{
  "finding_id": "FF737229D",
  "rule_id": "CG106",
  "severity": "high",
  "category": "security",
  "file": "user_store.py",
  "line": 13,
  "title": "SQL 语句由拼接/格式化构造（在 create_user 中）",
  "evidence": "cursor.execute(f\"INSERT INTO users (name, email, age) VALUES ('{name}', '{email}', {age})\")",
  "explanation": "把外部输入直接拼进 SQL 会造成 SQL 注入，输入可完全控制查询语义。",
  "suggestion": "使用参数化查询：`cursor.execute('SELECT * FROM t WHERE id = ?', (user_id,))`。",
  "source": "static",
  "fixable": true,
  "confidence": null,
  "status": "open",
  "note": ""
}
```

`finding_id` 由规则、文件和归一化后的标题一起哈希出来，不含行号。原因是第 1 轮修复会插入删除代码，
第 2 轮复审时行号整体位移；ID 要是由行号构造，"这条到底修好了没有"就永远对不上，而整条循环要回答
的就是这个问题。同一文件里同规则同标题的重复命中靠序号后缀区分，形如 `FF737229D-2`。

`severity` 取 `critical` / `high` / `medium` / `low` / `info`，和 codeguard 保持一致。哪些级别
算不合格由编排器配置决定（默认 `critical` + `high`），并通过 `payload.blocking` 下发给审查
Agent，门禁口径只有这一处权威来源。

## 可修复性词汇表

审查 Agent 的 `fixability.py` 和修复 Agent 的 `patchers.py` 必须一一对应。两边不一致是这个项目
最危险的静默失败：审查说能自动修，修复却没有策略，流程每轮都报"可自动修 N 条"，实际一条都
修不动。

| 规则 | 策略名 | 做什么 |
| --- | --- | --- |
| `CG105` | `move_secret_to_env` | 字面量密钥 → `os.environ[...]` |
| `CG106` | `parameterize_sql` | f-string / `%` 拼接 SQL → 占位符加参数元组 |
| `CG202` | `log_broad_except` | 给宽泛 except 补 `logger.exception`，保留原来的降级行为 |
| `CG203` | `sentinel_default` | 可变默认参数 → `None` 哨兵 |
| `CG204` | `identity_comparison` | `== None` → `is None` |
| `CG206` | `context_manager` | `open()` → `with` |
| `CG207` | `explicit_validation` | `assert` → `if not ...: raise ValueError` |
| `CG402` | `strip_trailing_whitespace` | 删行尾空白 |
| `CG403` | `drop_unused_import` | 删未使用的导入 |

判"可修"要求改写是局部的、确定的、能自动验证的。其余规则一律判不可修，并给出
"为什么要人来看"。两边是否对齐由 `code-review-workflow/tests/test_contract.py` 断言。

## 进程内与子进程

同一个信封既能走函数调用，也能走进程边界：

```bash
# 进程内（默认）：编排器直接 AgentClass(config).handle(request)
python -m workflow run --spec demo/spec_user_store.md

# 子进程：python -m <pkg>.worker --request-file req.json --response-file resp.json
python -m workflow run --spec demo/spec_user_store.md --transport subprocess
```

子进程这条路上有两条纪律。stdout 只允许出现那一个 JSON 对象，日志一律写 stderr。结果以响应
文件的内容为准，退出码只是参考（0 是 ok，1 是 failed）：进程可能在写响应之前就崩了，这时候
退出码本身不可信。

## 演进规则

| 变更类型 | 做法 |
| --- | --- |
| 新增可选字段 | 直接加，默认值保证旧 Agent 能跑；`CONTRACT_VERSION` 次版本加一 |
| 新增必填字段 | 不做，改成"编排器下发、Agent 有默认值" |
| 改已有字段的语义 | `CONTRACT_VERSION` 主版本加一，四个仓库同步升级 |
| 删字段 | 主版本加一 |

改动任何一处 `protocol.py`，都要把同样的改动复制到另外三份，再跑一遍 `test_contract.py`。四份
文件逐字节相同这条约束没有例外。
