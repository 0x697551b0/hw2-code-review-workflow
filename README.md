# code-review-workflow —— 多 Agent 代码审查工作流

> 编码 → 审查 → 修复，带条件分支、循环收敛、按文件并行与断点续跑 —— 软件工程实践 Homework 2
>
> **源码仓库**：<https://github.com/0x697551b0/hw2-code-review-workflow>　**作者**：刘浩天 / 2412190219

本仓库是 Homework 2「代码审查流程」方向的**编排器**：它把三个独立仓库里的 Agent（编码
`coding_agent`、审查 `review_agent`、修复 `fix_agent`）当成三个可调用节点，串成一条会**自己收敛**
的流水线 —— 生成代码草案 → 审查 → 门禁不通过就自动修复 → 复审，直到阻断级问题清零或到达轮次上限。

| 项 | 值 |
| --- | --- |
| 版本 / Python | `1.0.0` / `>=3.10`（开发与验证环境 3.14.7） |
| 第三方依赖 | **0 个**（状态、总线、事件、报告、流程图全部标准库） |
| 测试 | **96 个用例**（`python run_tests.py`），其中 4 个是真实三 Agent 的端到端用例、4 个是跨仓库契约一致性用例 |
| Agent 数量 / 典型耗时 | **3 个**（编码 / 审查 / 修复）· 一次运行约 **0.1–0.7 秒**（全离线，不调 LLM） |

## 1. 作业要求对照表

对照 PPT《Simple Workflow MVP（2 Weeks）》逐条打勾。

| 类别 | PPT 要求 | 本项目的实现 |
| --- | --- | --- |
| 必须 | ✅ 至少 **2 个 Agent** 协作完成完整流程 | **3 个**：编码 → 审查 → 修复，全链路跑通（`test_e2e.py` 用真实三件套验证） |
| 必须 | ✅ 实现 Agent 间的**消息传递或状态共享** | **两者都做了**：`MessageBus` 主题消息 + `SharedState` 共享状态（见 [§9](#9-agent-间通信两套机制)） |
| 必须 | ✅ 支持基本的**流程控制（顺序执行）** | `SequenceNode`，子节点按序号命名（`0:prepare`、`1:coding` …） |
| 必须 | ✅ MVP 可运行即可，**不需要复杂调度** | 单进程、单机、同步执行；没有分布式调度、没有任务队列 |
| 推荐 | ✅ **条件分支 / 循环 / 并行执行** | `BranchNode`：`fix-if-needed`（门禁未过且有可修问题才修）、`final-recheck`（最后一次动作是修复才补复审），条件函数必须只读否则 resume 不可靠；`LoopNode`：`review-fix-loop`，`max_rounds` 可配（默认 3），"达到上限但没收敛"是正常结果；`ParallelNode` + 审查按文件 fan-out，结果**按定义顺序确定性合并**（单文件 demo 不触发，由单测覆盖） |
| 推荐 | ⚠️ 可视化编排界面（可选） | **不做 GUI**。替代：`plan` 打印 ASCII 流程树 / Mermaid、`report.html` 单文件可视化报告 |
| 推荐 | ✅ **执行日志与状态追踪** | `events.jsonl`（JSONL 事件流，60 条/次）+ `StageReport` 阶段记录 + `--trace` + `replay` |
| 推荐 | ✅ **错误处理与恢复机制 / 复用项目 1** | 分类型异常 + 重试 + 降级 + 退出码约定 + `--resume` 断点续跑（见 §10、§18）；审查 Agent 直接复用 Homework 1 的 codeguard 27 条规则引擎，修复 Agent 也用它做权威自检 |

**评审标准自查**：流程完整性 40% —— 一条命令跑完整链路，收敛曲线 3 → 0 阻断，报告/事件/存档/工作副本全部
落盘；Agent 协作 30% —— 7 条总线消息留下协作证据，共享状态是单一事实来源；可扩展性 20% —— 加第 4 个 Agent
= 一个 `AGENT_SPECS` 条目 + 一个订阅者；文档 10% —— 本 README + `Design.md` + 自动生成的两份报告。

## 2. 目录

- [1. 作业要求对照表](#1-作业要求对照表)　- [2. 目录](#2-目录)　- [3. 快速开始](#3-快速开始)　- [4. 效果预览](#4-效果预览)　- [5. 命令行用法](#5-命令行用法)
- [6. 流程图](#6-流程图)　- [7. 项目结构](#7-项目结构)　- [8. 架构设计](#8-架构设计)　- [9. Agent 间通信：两套机制](#9-agent-间通信两套机制)　- [10. 状态管理与断点续跑](#10-状态管理与断点续跑)
- [11. 执行日志与状态追踪](#11-执行日志与状态追踪)　- [12. 并行执行与它的硬约束](#12-并行执行与它的硬约束)　- [13. 质量门禁](#13-质量门禁)　- [14. 报告产物说明](#14-报告产物说明)　- [15. 四个仓库的关系图](#15-四个仓库的关系图)
- [16. 测试与自检](#16-测试与自检)　- [17. 配置项](#17-配置项)　- [18. 设计取舍](#18-设计取舍)　- [19. 常见问题](#19-常见问题)　- [20. 许可](#20-许可)

## 3. 快速开始

**前提**：把 4 个仓库放在**同一个目录**下（编排器靠"在附近找 `包名/__init__.py`"发现 Agent）：

```
homework2/
├── coding-agent/  → coding_agent      ├── fix-agent/            → fix_agent
├── review-agent/  → review_agent      └── code-review-workflow/ → workflow（本仓库）
```

```bash
cd code-review-workflow
python -m workflow agents    # 先确认三个 Agent 都在（只读）；下面一条命令跑通全流程
python -m workflow run --spec demo/spec_user_store.md
```

不需要装依赖、不需要联网、不需要 API Key —— 三个 Agent 的主路径都是离线的确定性实现。产物
默认写到 `runs/run-<时间戳>-<4位>/`，终端最后会打印所有产物的绝对路径。

## 4. 效果预览

`python -m workflow run --spec demo/spec_user_store.md` 的真实输出（运行 ID 与耗时每次都不同）：

```
════════════════════════════════════════════════════════════════════════
运行 run-20261022-043349-52a0　结论：✅ 通过　耗时 0.14s
════════════════════════════════════════════════════════════════════════
收敛曲线
  轮次  阻断级  健康分  门禁  修复  转人工
   1      3       38     ❌     7      0
   2      0       96     ✅     0      0

Agent 消息传递
  M0001 coding       → *            R- code.ready
  M0002 review       → *            R1 review.completed
  M0003 orchestrator → *            R1 gate.evaluated
  M0004 fix          → *            R1 fix.applied
  M0005 review       → *            R2 review.completed
  M0006 orchestrator → *            R2 gate.evaluated
  M0007 orchestrator → *            R- pipeline.done

残余非阻断问题 8 条（4 条 CG304 缺文档字符串 + 4 条 CG307 缺返回值类型注解，完整清单见报告）
  · low      CG304   user_store.py:14 缺少文档字符串：create_user（在 create_user 中）

产物：report.md / report.html / run.json / events.jsonl / state.json / worktree
════════════════════════════════════════════════════════════════════════
```

这一次运行干了什么（数字都来自产物）：**第 1 轮**编码 Agent 产出 37 行草案 → 审查命中 15 条（1 critical、
2 high、3 medium、5 low、4 info），阻断 3 条、健康分 38、门禁 ❌ → 修复 Agent 改写 7 条，完整补丁在
`reports/fix-r1/fix.diff`；**第 2 轮**复审命中 8 条（4 low + 4 info），阻断 0 条、健康分 96、门禁 ✅ →
`fix-if-needed` 走 `else:skip-fix`，修复 Agent **没有被调用**；**收尾** `final-recheck` 因
`needs_recheck=False` 跳过，`wrap-up` 记下 8 条残余问题并发出 `pipeline.done`。

`report.html` 是**单文件、零外部依赖、可离线双击打开**的可视化报告，包含：结论与 KPI（轮次/自动修复/
健康分/总线消息/完成节点）、问题分布条形图、由流程树生成的流程图、收敛曲线、Agent 消息表、阶段耗时表、
残余问题表、产物目录树。

## 5. 命令行用法

| 子命令 | 作用 | 改文件 | 典型耗时 |
| --- | --- | --- | --- |
| `run` | 执行完整流程（唯一会写文件、会调用 Agent 的命令） | ✅ | 0.1–0.7s |
| `plan` / `agents` / `replay` / `info` | 打印流程结构 / 检查 Agent 可用性 / 回放事件流 / 版本与环境（均只读） | ❌ | 瞬时 |

**`run` 参数**

| 参数 | 说明 |
| --- | --- |
| `--spec <path>` | **必填**。需求文件（Markdown），例如 `demo/spec_user_store.md` |
| `--output-root <dir>` / `--agents-home <dir>` | 产物根目录（默认 `./runs`）/ Agent 仓库目录（**追加**到搜索候选，不是唯一来源） |
| `--codeguard-path <dir>` / `--pattern <glob>` | codeguard 源码根目录（同时传给审查与修复）/ 审查文件 glob（默认 `*.py`） |
| `--blocking <level>` / `--max-rounds <N>` | 阻断级别，可重复（默认 `critical`+`high`）/ 最多几轮审查→修复（默认 3） |
| `--coding-engine offline\|llm`、`--coding-profile realistic\|clean` | 编码 Agent 的引擎与草案档位 |
| `--review-mode static\|llm` / `--fix-engine rules\|llm` / `--transport inproc\|subprocess` | 审查模式（默认 `static`=codeguard）/ 修复引擎 / 三个 Agent 的调用方式（默认 `inproc`） |
| `--resume <state.json>` / `--no-checkpoint` | 从存档继续（语义见 §10，**不会重放副作用**）/ 关闭存档 |
| `--trace` / `--json` / `--quiet` / `--verbose` | 实时轨迹 / JSON 结论 / 只打一行 / 调试日志 |

`plan` 支持 `--max-rounds`、`--json`、`--mermaid`；`agents` 支持 `--agents-home`、`--json`；
`replay` 支持 `<events.jsonl>`、`--filter <kind>`、`--json`。

**退出码约定**

| 退出码 | 含义 | 何时出现 |
| --- | --- | --- |
| `0` | 流程跑完，**门禁通过** | `outcome = passed` |
| `1` | 流程跑完但**没通过门禁**（需人工介入） | `outcome = needs_human`，如 `--blocking low` 时剩余问题都不可自动修 |
| `2` | 流程**跑挂了**（配置/环境/运行错误） | `outcome = failed` 或 `aborted`，如 `--spec nope.md` |

`agents` 在"没找齐 3 个 Agent"时返回 `1`，可在 CI 里当环境自检。

```bash
python -m workflow run --spec demo/spec_user_store.md --trace                 # 看实时轨迹
python -m workflow run --spec demo/spec_user_store.md --coding-profile clean  # 干净草案：第 1 轮就过
python -m workflow run --spec demo/spec_user_store.md --transport subprocess  # 子进程隔离调用
python -m workflow run --spec demo/spec_user_store.md --blocking low          # 更严格门禁（退出码 1）
python -m workflow plan --mermaid                                            # 贴进 GitHub/Typora
python -m workflow replay runs/<run-id>/events.jsonl                          # 回放（<run-id> 换成实际目录名）
```

`--coding-profile clean` 时第 1 轮阻断级 0 条、健康分 100、**修复 Agent 一次都没被调用**，只剩
4 条 info 级 `CG307`（缺返回值类型注解）。`--transport subprocess` 同样跑通，区别只是每次 Agent
调用多一次 `python -m <pkg>.worker` 的进程往返（约 0.1s/次）。

## 6. 流程图

`python -m workflow plan` 的真实输出（这棵树是**由代码结构生成**的，不是手画的）：

```
流程结构（max_rounds=3）
════════════════════════════════════════════════════════════════════
◉ pipeline
├── ▭ prepare
├── ▭ coding
├── ◉ review-fix-loop  (max_rounds=3)
│   └── ◉ round
│       ├── ▭ review
│       ├── ▭ gate
│       ├── ◇ fix-if-needed  (门禁未通过且存在可自动修复的问题)
│       │   ├── ▭ fix
│       │   └── ▭ skip-fix
│       └── ▭ round-close
├── ◇ final-recheck  (最后一次动作是修复，需要补一次复审确认)
│   └── ◉ recheck
│       ├── ▭ review
│       ├── ▭ gate
│       └── ▭ round-close
└── ▭ wrap-up
════════════════════════════════════════════════════════════════════
图例：◉ 组合节点　◇ 条件分支　▭ 业务阶段
```

`python -m workflow plan --mermaid` 输出同一棵树的 Mermaid（完整输出 34 行，下面是原文前 10 行）：

```mermaid
flowchart TD
    n1["⟲ 顺序 · pipeline"]
    n2["▭ prepare"]
    n3["▭ coding"]
    n4["↻ 循环 · review-fix-loop<br/>最多 3 轮"]
    n5["⟲ 顺序 · round"]
    n6["▭ review"]
    n7["▭ gate"]
    n8["◇ 分支 · fix-if-needed<br/>门禁未通过且存在可自动修复的问题"]
    n9["▭ fix"]
    ... 其余为 n10~n17 的节点定义与 n1 --> n2 形式的边；分支边写作 n8 -->|成立| n9
```

两个细节：`describe()` 是递归的，**改流程结构图自动跟着变**；分支节点的边只走"带标签的边"（`|成立|` /
`|不成立|`），否则图上会同时出现无标签边与标签边、形成重影。

## 7. 项目结构

```
workflow/
  ★ nodes.py     流程控制原语：Sequence / Branch / Loop / Parallel / Functional
  ★ stages.py    具体流程：7 个业务阶段 + build_pipeline + merge_reviews
  ★ runner.py    编排器：装配 → 执行 → 收尾，resume 入口
  ★ state.py     SharedState / Checkpoint：业务状态 + 执行进度 + 原子存档
  ★ bus.py       MessageBus / Inbox：同步发布订阅，消息全量留存
  ★ protocol.py  契约信封（四个仓库各存一份等价副本）· context.py RunContext（call_agent 唯一出入口）
    events.py · registry.py · reporting.py   事件流 / Agent 发现与装配 / Markdown·HTML 渲染
    models.py · config.py · cli.py · errors.py · __init__.py   模型/配置/CLI/异常/公开 API
tests/     96 个用例（runner / nodes / state / bus / models / stages / reporting / contract / e2e）
demo/spec_user_store.md  演示需求：4 个函数 + SQLite + 参数化/密钥约束
run_tests.py · pyproject.toml · .env.example          ★ = 核心文件
```

## 8. 架构设计

```
接口层  cli.py run/plan/agents/replay/info
编排层  runner.py :: Orchestrator（装配→执行→收尾→写报告）· context.py :: RunContext（横切收口）
流程层  stages.py 7 个业务阶段 · nodes.py 4 种控制原语 + Functional 叶子
协作层  bus.py（消息）· state.py（状态+存档）· registry.py（Agent 发现与调用）
观测层  events.py（JSONL）· reporting.py（Markdown/HTML）· models.py · protocol.py
```

| 节点 | 契约 | 在流程里的用处 |
| --- | --- | --- |
| `SequenceNode` | 顺序执行子节点，`on_error=abort\|continue` | 整个 pipeline、每轮的 `round`、`recheck` |
| `BranchNode` | `condition(ctx)` 为真走 `then`，否则走 `else`（可为空=跳过） | `fix-if-needed`、`final-recheck` |
| `LoopNode` | 最多 `max_rounds` 轮，`until(ctx)` 为真则提前退出，带循环游标 | `review-fix-loop` |
| `ParallelNode` | 并发执行**无副作用**子节点，**按定义顺序**合并结果 | 单测覆盖；审查按文件 fan-out 是它的同构实现 |
| `FunctionalNode` | 用函数实现的叶子节点 | `skip-fix` 这类小步骤 |

每个节点都继承 `Node.execute()` 这个**模板方法**：判定"是否已完成（resume）"→ 执行 → 成功则写
完成标记并 `checkpoint()` → 发 `node.end` 事件。放基类而非每个节点各写一遍，是因为 resume 的
正确性依赖它**没有遗漏**（漏一处就会出现"跳过了但状态没写"的静默错误）。`RunContext.child()`
只改 `path` 与 `depth`，路径编码节点在树中的位置（如 `2:review-fix-loop/#1/2:fix-if-needed/then:fix`），
它同时是**事件定位符、resume 完成标记、循环游标的键** —— 一份标识三处用。

## 9. Agent 间通信：两套机制

| 机制 | 解决什么 | 不解决什么 |
| --- | --- | --- |
| **消息总线**（`bus.py`） | "**发生了什么**"：`code.ready` / `review.completed` / `gate.evaluated` / `fix.applied` / `pipeline.done`；发布者不知道谁在听，订阅者声明"我关心什么" | 不承载"当前状态是什么"；同步投递、不做异步队列（消息历史会随存档落盘，见 §10） |
| **共享状态**（`state.py`） | "**现在是什么样**"：需求文本、代码清单、每轮发现、门禁结论、残余问题、轮次轨迹；单一事实来源、可存档 | 不表达顺序与因果关系 |

**为什么两个都要**（`bus.py` 注释原话）：只有共享状态的话，Agent 之间的耦合是隐式的 —— 修复 Agent
读到什么，取决于它被别人在什么时候写了什么，读代码根本看不出来。具体到代码：`FixStage` **不接收**
审查结果这个参数，它通过 `Inbox` 订阅 `review.completed` 并读 `message.body["findings"]`（`runner.py`
启动时把 `Inbox` 注入流程树）。于是"新增一个 Agent"变成"多一个订阅者"：将来加"修复结果归档 Agent"，
订阅 `fix.applied` 即可，`FixStage` 一行都不用改。总线还有三条刻意的设计：**同步无队列**（顺序流程里
异步会引入"消息在被写之前就被读"的时序问题；消息全量留在 `history`，将来换异步队列只需替换投递实现）；
**广播 vs 定向**（定向消息只给"订阅了广播"或"订阅了自己"的接收者，`test_directed_subscriber_receives_
broadcast` 守着一个曾经写反过的判断）；**单个订阅者抛异常不拖垮发布方**（记一条 `error` 事件后继续投递）。

## 10. 状态管理与断点续跑

`state.json` 是一份**扁平 JSON**（不用 pickle：可读、可 diff、可手改、跨版本安全），同时装业务
状态与执行进度：

```json
{"run_id": "run-…", "version": 1,
 "data": {"spec": {...}, "paths": {...}, "code": {...}, "review": {...}, "findings": [...],
          "gate": {"passed": true, "blocking_count": 0, "score": 96, "can_improve": false},
          "rounds": [...], "residual": [...], "needs_human": [...], "current_round": 2,
          "last_action": "review", "needs_recheck": false},
 "completed": ["0:prepare", "1:coding", "2:review-fix-loop", "2:review-fix-loop/#1", "..."],
 "cursors": {"2:review-fix-loop": 2},
 "meta": {"outcome": "passed", "config": "…", "stages": [...], "messages": [...]}}
```

一次典型运行的 `completed` 有 **18 个节点**、`cursors` 记录循环跑到第 2 轮。两者放同一个文件是因为
断点续跑要求"业务状态与执行进度必须一致" —— 分两个文件会出现"进度说跑完了、业务数据还没落盘"
的不一致。存档是**原子写**：先写带 `pid.threadid.序号` 后缀的临时文件再 `replace()` 覆盖，配合
`RLock` 保证并行节点同时触发存档也不写坏（固定临时文件名曾在 Windows 上直接 `Permission denied`）。

**`--resume` 的确切语义**

* **跳过 `completed` 里已有的节点**：`Node.execute()` 逐层判定，命中就发一条 `node.skip` 事件；
  **运行目录以存档所在目录为准**（不是新 run-id 的目录），轮次、残余问题、开放问题、`rounds` 都从
  存档恢复，所以续跑结论与第一次跑完一致；
* **不重放已跳过节点的副作用**。`resume` 不是 `rewind`：已经写进 `worktree` 的修复不会被撤销，也
  不会被重新执行，只是"不再跑一遍"。想回到干净状态请重新 `run`（新 run-id、新工作副本）；
* **可观测性一并恢复**：`EventLog` 会重绑到存档所在目录并改为追加（序号接着排），总线消息与阶段记录
  从存档的 `meta` 里重建，因此续跑后的报告仍有完整的消息表与阶段耗时表。

**曾经踩过的两个坑（已修，留作记录）**：① 早期续跑会把 `events.jsonl` 写进另一个新建的空运行目录
（`EventLog` 在 `__init__` 就按新 run-id 绑定路径并立即建目录），原目录的轨迹变成另一份文件；② 总线
消息与阶段记录不在存档里，续跑后报告的"Agent 消息传递""阶段耗时"两节直接变空。修法是
`EventLog(path, append=True)`（首次写入才建文件、序号接最大值）+ `MessageBus.restore()` +
`checkpoint()` 把 `stages`/`messages` 写进存档 + `resume()` 重绑事件日志。实测：`events.jsonl` 60 行 →
续跑后 64 行且**只有一份文件**、运行目录数不变，报告仍是 7 条消息、8 行阶段记录
（回归测试 `test_resume_preserves_observability`）。

## 11. 执行日志与状态追踪

事件流写成 **JSON Lines**（一行一条 JSON，`events.jsonl`）：不需要解析器就能被 `jq` / pandas 直接读、
可以流式追加（崩溃前已落盘的行仍可读）、同一份事件流既能给人看也能喂给报告生成器。一次运行 **60 条事件**，
`replay` 统计的类型分布：`agent.call×4、agent.result×4、bus.message×7、gate.evaluated×2、node.branch×3、
node.end×18、run.end×1、run.start×1、state.checkpoint×20`。

单条事件（`data` 里是可断言的结构化字段，`message` 是给人看的一句话）：

```json
{"seq": 14, "kind": "gate.evaluated", "level": "info", "round": 1, "node": "2:review-fix-loop/#1/1:gate",
 "message": "⚖ 门禁：未通过（阻断 3 条，可修 3 条）", "data": {"passed": false, "blocking": 3}}
```

三种消费方式：`--trace` 边跑边打印（按节点深度缩进，输出到 stderr）；`python -m workflow replay
runs/<run-id>/events.jsonl` 事后逐条复现；`--filter gate.evaluated` / `--json` 只看某类事件或交给程序。

## 12. 并行执行与它的硬约束

`ParallelNode` 只有一条硬约束，写在 `nodes.py` 模块注释里：**并行子节点不得写共享状态**。理由：并发写共享
状态需要锁、需要定序、需要在失败时回滚，MVP 阶段不值得。所以并行子节点"只返回结果"，由并行节点在汇合后
**按子节点定义的顺序**确定性合并 —— 如果按完成顺序合并，同样输入会产出不同报告
（`test_parallel_merges_in_definition_order`）。真实流程里的用法是**审查阶段按文件 fan-out**
（`ReviewStage._review_many`）：每个分片只读一份文件、产出一份只读 payload，最后由纯函数 `merge_reviews`
合并 —— `findings` 按 `finding_id` 去重后按严重级别排序；`counts` / `blocking` / `total` 由合并后的清单
**重新计算**（不取分片之和，分片可能重复报同一处）；`score` 取最小值、`overall_risk` 取最严重的一个。
fan-out 放在编排器而非审查 Agent 内部，换来"编排器能拿到每个文件各自的报告""任何 Agent 都能享受并行
能力"，代价是要自己合并 —— 所以 `merge_reviews` 写成纯函数、可直接单测（`test_stages.py` 10 个用例）。
**诚实说明**：演示需求只生成 1 个文件，所以 `shards=1`，跑 demo 时**看不到**真正的并发；并行能力由
`test_nodes.py`（合并顺序、失败上报）与 `merge_reviews` 的单测覆盖。

## 13. 质量门禁

门禁（`GateStage`）把审查结论翻译成一个**每个字段都能解释**的判定：

```json
{"passed": false, "blocking": ["critical","high"], "blocking_count": 3, "total": 15,
 "counts": {"critical":1,"high":2,"medium":3,"low":5,"info":4}, "fixable": 7, "needs_human": 8,
 "score": 38, "overall_risk": "high", "can_improve": true, "round": 1, "reason": "存在 3 条阻断级问题，其中 3 条可自动修复"}
```

* **阻断级别可配置**：`--blocking` 可重复传（默认 `critical`+`high`）；编排器启动时把同一个值强制写进
  审查 Agent 的配置，避免"审查说不合格、编排器说合格"的口径打架。`blocking_count` / `fixable` /
  `needs_human` / `score` / `reason` 一起给出，不允许出现"pass/fail 但不知道为什么"；
* **`can_improve` 为什么存在**：循环的退出条件有两个 —— 门禁通过，或"没得修了"。如果只有"门禁通过才
  退出"，遇到"阻断级问题全都不可自动修"（例如函数过长 `CG301`）时会白白再跑 `max_rounds` 轮、每轮
  重复同样的审查与跳过。有了 `can_improve=false`，第一轮结束就停、结论直接给 `needs_human`
  （`test_loop_stops_when_nothing_fixable` 守着这条）。跑一次"没人能修"的门禁看退出码 1：
  `python -m workflow run --spec demo/spec_user_store.md --blocking low`。

## 14. 报告产物说明

```
runs/run-<时间戳>-<4位>/
├── report.md      人读报告（概要/门禁/流程图/收敛曲线/消息/阶段/残余/最终代码/产物清单 9 节）
├── report.html    单文件可视化报告，零外部依赖，可离线双击打开 · run.json 机器可读（CI 断言用）
├── events.jsonl   JSONL 事件流（60 条），可 replay        ├── state.json  存档：状态+完成标记+游标
├── reports/       coding/（响应+草案副本）· review-r1/ · review-r2/ · fix-r1/（fix.json+fix.diff+summary.md）
└── worktree/      最终代码 user_store.py + draft_manifest.json
```

| 文件 | 给谁看 | 关键内容 |
| --- | --- | --- |
| `report.md` | 人（也进版本库） | 结论、门禁明细、收敛曲线、**消息传递记录（Agent 协作证据）**、最终代码全文；消息摘要形如 `命中 15 条（critical 1、high 2、medium 3、low 5、info 4），阻断 3` |
| `report.html` / `run.json` | 演示 / 程序 | 内联 CSS 条形图与流程图（`prefers-color-scheme: dark` 自动深色）/ `outcome`·`rounds[]`·`stages[]`·`gate`·`messages[]` |
| `events.jsonl` / `state.json` / `reports/fix-r1/` | 复盘 / resume / 人+程序 | 事件流（§11）/ 存档（§10）/ `fix.diff`（标准 unified diff）+ `fix.json`（逐条 `PatchRecord` + 理由） |

两条原则：**机器可读数据与渲染分离**（报告只是数据的视图，改样式不影响自动化断言）；**报告失败也要
写**（`_finalize_reports` 包在 try 里，失败只记事件、不掩盖主结果）。

## 15. 四个仓库的关系图

```
     ┌──────────────────────────────────────────┐
     │  code-review-workflow（本仓库，workflow）  │  编排器：节点树 / 总线 / 状态 / 报告
     └───┬──────────────┬──────────────┬────────┘
         │ 契约：AgentRequest / AgentResponse（JSON 信封）
 ┌───────▼──────┐ ┌─────▼─────────┐ ┌────▼──────────┐
 │ coding-agent │ │ review-agent  │ │ fix-agent     │
 │ 需求→代码草案 │ │ 静态审查+门禁  │ │ 逐条改写+自检  │
 └──────────────┘ └─────┬─────────┘ └────┬──────────┘
                    └─── 复用 ───────────┴─ Homework 1: codeguard（27 条规则引擎）
```

| 仓库 | 包名 | 角色 | 地址 |
| --- | --- | --- | --- |
| Homework 1 | `codeguard` | 代码审查 Agent（规则引擎 + LLM 工具调用） | <https://github.com/0x697551b0/codeguard> |
| 编码 Agent | `coding_agent` | 需求 → 代码草案（离线模板 / LLM 双引擎） | <https://github.com/0x697551b0/hw2-coding-agent> |
| 审查 Agent | `review_agent` | 复用 codeguard，产出 `FindingContract[]` + `fixable` 标记 | <https://github.com/0x697551b0/hw2-review-agent> |
| 修复 Agent | `fix_agent` | 9 条策略 + 三条自检标准 | <https://github.com/0x697551b0/hw2-fix-agent> |
| **编排器（本仓库）** | `workflow` | 流程控制、Agent 通信、状态与存档、报告 | <https://github.com/0x697551b0/hw2-code-review-workflow> |

四个仓库各自独立可跑（各有 `run_tests.py`），合并在一起才是一条完整流水线。

## 16. 测试与自检

```bash
python run_tests.py        # 96 个用例（-v 列出用例名，e2e 只跑 e2e）；pytest -q 亦可
```

真实输出结尾：`结果：96/96 通过，全部通过`（Python 3.14.7）。

| 文件 | 用例 | 守住什么 |
| --- | --- | --- |
| `test_runner.py` | 17 | 主流程：收敛、分支跳过修复、无解时转人工、轮次上限、缺 Agent 快速失败、重试、存档与续跑、报告落盘、**续跑保留可观测性**、**消息摘要的总数统计** |
| `test_nodes.py` | 19 | 四种控制原语的契约 + resume 跳过语义（含**从循环游标继续**）+ **失败不标完成** + 分支条件只读可重复 |
| `test_state.py` / `test_bus.py` / `test_models.py` | 14 / 13 / 9 | 点号路径与**原子写无 tmp 残留**、版本不兼容提示 / 广播与定向投递（含回归用例）、订阅者异常隔离 / 门禁可解释性（`can_improve`、可配置阻断级别） |
| `test_stages.py` | 10 | `merge_reviews` 合并规则 + 多文件 fan-out 合并（纯函数）+ 流程结构契约 |
| `test_contract.py` | 4 | 跨仓库一致性：四份 `protocol.py` 逐字节相同、契约版本一致、审查侧"可修清单"与修复侧策略注册表一一对应 |
| `test_reporting.py` / `test_e2e.py` | 6 / 4 | Mermaid/ASCII 由流程树生成、分支边无重影、Markdown 必备小节、HTML 无外链 / 真实三 Agent：至少两轮收敛、消息序列完整、clean 档位短路、产物齐全 |

两层设计很关键：**单元测试用伪 Agent**（`tests/support.py::FakeHandle`）驱动编排逻辑，**e2e 用真 Agent**。
前者让"编排逻辑对不对"不被"隔壁仓库今天改了什么"污染；后者在找不到 Agent 时**明确跳过**并打印原因
（"跳过：未找齐三个 Agent，请把四个仓库放在同一目录下"）。`test_contract.py` 同样允许跳过，但只要四个仓库
在一起，它就会真的比对四份 `protocol.py` 与两份可修复性词汇表。

## 17. 配置项

优先级：**命令行参数 > 环境变量 > `.env` > 默认值**。前缀 `WF_`。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WF_SPEC_PATH` / `WF_OUTPUT_ROOT` | — / `./runs` | 需求文件 / 产物根目录（其下每次新建 `<run-id>/`） |
| `WF_AGENTS_HOME` / `HW2_AGENTS_HOME` | 空 | Agent 仓库目录（空=自动搜索）/ 额外的包搜索根目录（`os.pathsep` 分隔） |
| `WF_BLOCKING` / `WF_MAX_ROUNDS` / `WF_PATTERNS` / `WF_TRACE` / `WF_VERBOSE` / `WF_CHECKPOINT` | `critical,high` / `3` / `*.py` / `0` / `0` / `1` | 门禁与轮次 / 实时轨迹 / 调试日志 / 阶段存档（关掉失去 resume） |
| `WF_CODING_OPTIONS` / `WF_REVIEW_OPTIONS` / `WF_FIX_OPTIONS` / `WF_*_TRANSPORT` | 见 `.env.example` / `inproc` | 透传给各 Agent 配置类（`KEY=VALUE;KEY=VALUE`）/ 调用方式 |

编排器只知道"给编码 Agent 传 `{"engine": "offline", "profile": "realistic"}`"这样的键值对，**不解释它们的
含义** —— 那是各 Agent 自己配置类的事。所以给现有 Agent 加参数、或新增一个 Agent，配置层都不用改。

## 18. 设计取舍

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 流程表达 / 传输方式 | **可组合节点树**（不用写死的 `if/for`）；默认 `inproc`、可选 `subprocess` | 流程图能从结构生成、resume 标记有天然层级、加节点不改既有代码；进程内快且好调试，子进程隔离性好（Agent 崩了不带走编排器） |
| Agent 耦合 / 协作方式 | **只共享 JSON 信封**，不 import 对方内部类型；消息总线 **+** 共享状态 | 一方换实现（甚至换成别的语言写的进程）另一方不用改；消息表达"发生了什么"、状态表达"现在是什么样" |
| 存档格式 / 失败 / 并行 / 可视化 | **扁平 JSON**，业务状态与执行进度同文件；失败默认 `abort`（可配 `continue`）；并行只在**无副作用**处用；**不做 GUI**，机器可读数据与渲染分离 | 可读可 diff 可手改、跨版本安全（pickle 把"读数据"变成"执行任意代码"），分开存又会出现"进度说完成、数据没落盘"；不知道结果就不假装成功；并发写共享状态需要锁+定序+回滚 |

## 19. 常见问题

**Q：一定要有 API Key 吗？`agents` 说找不到 Agent 怎么办？** 不需要 Key：三个 Agent 的默认路径全是
离线的确定性实现（编码用模板、审查用 codeguard 规则、修复用规则级修补器），只有 `--coding-engine llm`
/ `--review-mode llm` / `--fix-engine llm` 要 Key。找不到 Agent 就把四个仓库放同一目录，或用
`--agents-home <目录>` / `WF_AGENTS_HOME` —— 注意它是**追加**到候选搜索列表、不是唯一来源，即使传错，
向上搜索仍可能找到别处的同名包。

**Q：`--trace` 在 PowerShell 里满屏红字？报告里消息表的"命中 N 条"以前一直显示 0？** 前者是 stderr 被
包成 `NativeCommandError`，命令本身成功（退出码 0），用 `2>$null` 丢弃或改用 `--json`（见 §11）。后者是
一个**已修**的 bug：早期 `_message_digest` 直接取 `body["total"]`，而 `review.completed` 的消息体没有这个
字段，于是总数恒为 0（分布与阻断数都对，这种"部分正确"最难发现）。现在 `ReviewStage` 会带上 `total`，
`_message_digest` 也加了"缺 `total` 时由 `counts` 求和"的兜底（`test_message_digest_counts_total_from_counts`）。

**Q：跑完不满意，能只重跑某一步吗？能审查非 Python 项目吗？** `resume` 是"跳过已完成节点"，不是
"重做某一节点"（见 §10）：要么整体重跑（新 run-id），要么手工改 `state.json` 的 `completed`。编排器本身
与语言无关（`--pattern` 可配），但审查 Agent 的规则引擎是 Python 专用的、修复 Agent 只改 `.py`，其他
语言会退化成"没有可用规则"。

## 20. 许可

本项目为课程作业，采用 MIT 许可。详细设计见 [`Design.md`](Design.md)。
