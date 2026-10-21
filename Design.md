# code-review-workflow 设计文档

> 软件工程实践 Homework 2 · 多 Agent 代码审查工作流（`workflow`）
>
> 本文说明编排器的**架构决策、控制流原语、Agent 通信与状态设计**，以及开发过程中真实踩到
> 的工程问题。所有数字（测试数、消息数、节点数、事件数）都能在仓库与运行产物里核对；所有
> "为什么"都对应代码注释。

**目录**

1. [需求与目标](#1-需求与目标)
2. [总体架构](#2-总体架构)
3. [流程控制原语](#3-流程控制原语)
4. [为什么顺序流水线里还要消息总线](#4-为什么顺序流水线里还要消息总线)
5. [状态设计](#5-状态设计)
6. [断点续跑的实现与边界](#6-断点续跑的实现与边界)
7. [质量门禁与循环收敛](#7-质量门禁与循环收敛)
8. [横切关注点的收敛点](#8-横切关注点的收敛点)
9. [报告与可视化](#9-报告与可视化)
10. [失败处理与降级](#10-失败处理与降级)
11. [可扩展性](#11-可扩展性)
12. [测试策略](#12-测试策略)
13. [开发中遇到的工程问题](#13-开发中遇到的工程问题)
14. [已知局限与改进方向](#14-已知局限与改进方向)
15. [如果重来一次](#15-如果重来一次)
16. [附录：关键文件速查](#16-附录关键文件速查)

## 1. 需求与目标

PPT 的「代码审查流程」方向给出的是：**编码 Agent + 审查 Agent + 修复 Agent**，跑通
"编码 → 审查 → 修复"。技术要求分"必须实现"与"推荐实现"两档，本项目的取舍如下。

| PPT 要求 | 档位 | 本项目的实现 |
| --- | --- | --- |
| 至少 2 个 Agent 协作完成完整流程 | 必须 | 3 个（编码 / 审查 / 修复），各自独立仓库，`test_e2e.py` 用真实三件套验证 |
| 实现 Agent 间的消息传递**或**状态共享 | 必须 | **两者都做**：`MessageBus`（显式消息）+ `SharedState`（单一事实来源），见 §4、§5 |
| 支持基本的流程控制（顺序执行） | 必须 | `SequenceNode`；此外还有分支、循环、并行（§3） |
| MVP 可运行即可，不需要复杂调度 | 必须 | 单进程、单机、同步；没有队列、没有调度器、没有分布式 |
| 条件分支 / 循环 / 并行 | 推荐 | `BranchNode` / `LoopNode` / `ParallelNode` 三种原语 |
| 可视化 Workflow 编排界面（可选） | 推荐 | **不做 GUI**，改为"流程图由流程树生成 + 单文件 HTML 报告"（§9） |
| 执行日志与状态追踪 | 推荐 | JSONL 事件流 + `StageReport` + `--trace` + `replay` |
| 错误处理与恢复机制 | 推荐 | 分类型异常 + 重试 + 降级 + 退出码 + `--resume`（§6、§10） |
| 从项目 1 复用 | 推荐 | 审查 Agent 直接复用 Homework 1 的 codeguard 规则引擎 |

### 1.1 明确的非目标

| 非目标 | 为什么不做 |
| --- | --- |
| **不做分布式调度** | 三个 Agent 都是按需实例化的轻量对象，一次运行 0.1–0.7 秒；上分布式只会把"编排逻辑"和"部署问题"混在一起，反而看不清前者 |
| **不做可视化拖拽编排界面** | 一个会与代码脱节的拖拽图，比一个"改代码图自动变"的流程图更有害。PPT 里这一项本身标着"可选" |
| **不做通用 DAG 引擎 / Agent 热插拔** | 4 种原语、一棵静态树就够表达这条流程；通用引擎的抽象成本（条件 DSL、动态拓扑、调度策略）在这里没有回报。运行中替换 Agent 还会让"这次运行到底跑了什么"不可复现 |
| **不保证跨轮次"同一个问题"的绝对身份** | 匹配键是 `(file, line, rule_id)`；修复会移动行号，所以跨轮次对齐只能做到"大致对齐"，报告靠 `finding_id` 与规则统计说话 |

## 2. 总体架构

### 2.1 分层视图

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 接口层   cli.py run/plan/agents/replay/info        ·  __main__.py         │
│ 编排层   runner.py :: Orchestrator                                        │
│            装配（发现 Agent / 准备状态·总线·事件 / 恢复存档）              │
│            → 执行（调流程树，捕获异常）→ 收尾（写报告，返回 RunOutcome）    │
│ 流程层   stages.py 7 个业务阶段 + build_pipeline + merge_reviews           │
│          nodes.py  4 种控制原语 + Functional / Abort 叶子节点              │
│ 协作层   bus.py 同步发布订阅 · state.py 共享状态+原子存档                  │
│          registry.py Agent 发现与装配（inproc/subprocess）                │
│          context.py RunContext：把上面三者 + 配置/事件/阶段记录打包        │
│ 观测层   events.py JSONL 事件流 · reporting.py Markdown/HTML/Mermaid      │
│          models.py 阶段与运行模型 · protocol.py 契约信封                   │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 依赖方向

```
cli.py ──▶ runner.py ──▶ stages.py ──▶ nodes.py ──▶ context.py ──▶ {bus, state, events, registry}
                │                          │
                └──▶ reporting.py ◀────────┘（只读 Orchestrator 与 RunOutcome）
```

* `nodes.py` 不知道任何业务阶段：它只认 `Node` 契约，因此可以脱离本项目复用；
* `stages.py` 是唯一"知道业务"的层：它依赖节点契约、总线主题、状态键名与协议模型；
* `context.py` 是**所有横切能力**的汇聚点（事件、重试、共享状态快照、存档），节点只管调
  `ctx.call_agent(...)`；
* `reporting.py` 只读不写状态，所以它可以在任何时刻被调用（包括运行失败之后）。

### 2.3 模块职责（16 个模块）

| 模块 | 职责 | 不负责什么 |
| --- | --- | --- |
| `__init__.py` | 对外公开 API 与版本号 | 不含逻辑 |
| `__main__.py` | `python -m workflow` 入口 | — |
| `nodes.py` | 控制原语与 `Node.execute()` 模板方法、`NodeResult` | 不含业务语义 |
| `stages.py` | 7 个业务阶段、`build_pipeline()`、`merge_reviews()` | 不做 Agent 调用细节（走 ctx） |
| `runner.py` | 装配、执行、异常兜底、收尾写报告、`resume()` | 不实现任何阶段 |
| `context.py` | `RunContext`、`child()` 派生、`call_agent()`、`checkpoint()` | 不决定流程走向 |
| `state.py` | `SharedState` / `Checkpoint`：业务状态 + 执行进度 + 原子存档 | 不含业务键的含义 |
| `bus.py` | `MessageBus` / `Inbox` / `Message`：同步发布订阅 | 不持久化、不做异步投递 |
| `events.py` | `EventLog` / `ConsoleListener` / `ListenerBuffer` / `read_events` | 不决定记录什么 |
| `registry.py` | `AgentRegistry` / `AgentHandle`：发现、装配、两种传输、健康检查 | 不解释 Agent 的私有配置 |
| `reporting.py` | Markdown / HTML / Mermaid / ASCII 渲染与写盘 | 不改状态、不做判断 |
| `models.py` | `StageReport` / `GateResult` / `RoundSummary` / `RunOutcome` | 无 I/O |
| `protocol.py` | 契约信封（`AgentRequest` / `AgentResponse` / `FindingContract`） | 不含编排逻辑 |
| `config.py` | `WorkflowConfig` / `AgentSettings`（`WF_*` 环境变量、校验） | 不解释 Agent 选项 |
| `cli.py` | 参数解析、五种命令、退出码映射 | 不含业务逻辑 |
| `errors.py` | 异常体系（按"谁来负责"分类） | — |

### 2.4 节点树（一次运行实际经过的路径）

```
pipeline                        （SequenceNode）
├── 0:prepare                  （PrepareStage：解析需求、建工作副本与报告目录）
├── 1:coding                   （CodingStage → coding Agent，发 code.ready）
├── 2:review-fix-loop          （LoopNode，max_rounds=3）
│   └── #1                     （SequenceNode "round"）
│       ├── 0:review           （ReviewStage → 多文件时 fan-out）
│       ├── 1:gate             （GateStage：passed / can_improve / reason）
│       ├── 2:fix-if-needed    （BranchNode：then:fix → FixStage / else:skip-fix）
│       └── 3:round-close      （RoundCloseStage：写收敛曲线）
├── 3:final-recheck            （BranchNode，needs_recheck 才进 then:recheck）
└── 4:wrap-up                  （WrapUpStage：残余问题、open_questions、pipeline.done）
```

路径里的每一段都由 `RunContext.child(segment)` 拼出，例如
`2:review-fix-loop/#1/2:fix-if-needed/then:fix`。这条字符串同时是**事件定位符**、
**`state.completed` 的完成标记**、**循环游标的键** —— 一份标识三处用（§5、§6）。

## 3. 流程控制原语

### 3.1 四种原语的契约

| 原语 | 构造 | 契约 | 关键实现细节 |
| --- | --- | --- | --- |
| `SequenceNode` | `(name, children, on_error="abort"\|"continue")` | 顺序执行；任子节点抛 `NodeFailed` 时按策略中止或继续 | 子节点上下文是 `ctx.child(f"{index}:{name}")`；最后一个子节点失败总是向上抛 |
| `BranchNode` | `(name, condition, then_node, else_node=None, description="")` | `condition(ctx)` 为真走 `then`，否则走 `else`；`else` 为空则返回 `skipped` | 条件必须是**只读纯函数**（resume 会重复求值）；返回值里塞 `branch_taken` |
| `LoopNode` | `(name, body, max_rounds=3, until=None, round_observer=None)` | 最多 `max_rounds` 轮，`until(ctx)` 为真提前退出 | 起始轮次 = `state.get_cursor(path)+1`（resume）；每轮结束 `checkpoint(force=True)`；退出原因写进返回值 |
| `ParallelNode` | `(name, children, max_workers=4)` | 并发执行**无副作用**子节点，按**定义顺序**合并结果 | 只有一个子节点时退化为同步调用；任一子节点失败则整体 `fail`；每个子节点用 `ctx.child()` 派生独立上下文 |

另有 `FunctionalNode`（用函数实现的叶子，`skip-fix` 就是它）与 `AbortNode`（主动中止）。
`collect_nodes()` 深度优先列出整棵树，供报告与测试断言。

### 3.2 为什么做成可组合节点而不是写死的 if/for

如果把流程写成 `def run(): prepare(); coding(); while ...: review(); if ...: fix()`，会同时失去三样东西：

1. **流程图**：手画的图会与代码脱节；`describe()` 递归出的树永远与代码同构（§9）。
2. **resume 的定位点**：`state.completed` 需要一个稳定的节点标识。函数体里没有"节点"这个
   概念，只能靠人肉编号；节点树里每个节点天生有路径。
3. **组合能力**：`final-recheck` 复用 `ReviewStage` / `GateStage` / `RoundCloseStage`，只是换了
   一个 `label`（`recheck`）；`fix-if-needed` 的 `else` 分支可以是一个"什么也不做"的
   `FunctionalNode`。这些都不需要给业务阶段加参数。

`Node.execute()` 是模板方法，统一做四件事：**resume 跳过判定 → 执行 → 写完成标记 → 存档 +
发 `node.end` 事件**。放在基类而不是每个节点各写一遍，是因为 resume 的正确性依赖它**没有遗漏**
（漏一处就会出现"跳过了但状态没写"的静默错误，`test_failure_is_not_marked_completed` 守着反向
约束：失败不能标完成）。

### 3.3 `ParallelNode` 为什么要"子节点无副作用"

并发写共享状态需要锁、需要定序、需要在失败时回滚 —— 三样都要，而 MVP 阶段一样都不值得。
所以这里选择一条硬约束：**并行子节点只返回结果，由并行节点在汇合后确定性合并**。

"按定义顺序合并"不是洁癖：并发完成顺序不确定，如果按完成顺序合并，同样的输入会产出不同的
报告 —— 那会让测试、diff、报告全部失去可比性（`test_parallel_merges_in_definition_order`）。

这条约束在真实流程里的落地形式是"审查按文件 fan-out"：每个分片只读一份文件、产出一份只读
payload，最后由纯函数 `merge_reviews` 合并。合并规则写死在函数里（去重 → 重算 counts → 取最小
score → 取最差 risk），因为它是**纯函数所以可以直接单测**，不必靠"看起来对"。

## 4. 为什么顺序流水线里还要消息总线

只有共享状态时，Agent 之间的耦合是**隐式的**：修复 Agent 读到什么，取决于它被别人在什么时候写了
什么 —— 读代码根本看不出来。显式消息把协作关系变成**可声明、可观测、可替换**的东西：

* 审查 Agent 只管 `publish("review.completed", ...)`，它不知道谁在听；
* 修复 Agent 用 `subscribe("review.completed", ...)` 声明"我对审查结论感兴趣"；
* 编排器在中间转发，并顺手把每条消息记进事件日志与消息历史（报告第 5 节就是它）。

于是"**新增一个 Agent**"变成"**多一个订阅者**"，而不是"改动别人的代码"。这正是 PPT 里
"可扩展性"那一项要考察的东西。

### 4.1 具体实现上的三个决定

| 决定 | 做法 | 理由 |
| --- | --- | --- |
| **同步、无队列** | `publish()` 内部直接调用订阅者回调 | 顺序流程里异步会引入"消息在被写之前就被读"的时序问题；消息全量留在 `history`，将来换异步队列只需替换投递实现，订阅方代码不动 |
| **广播 vs 定向** | `BROADCAST = "*"`；定向消息只投给"订阅了广播"或"订阅了自己"的接收者 | 支持"这条消息只给 fix"的语义，同时不牺牲"我在监听全部"的能力 |
| **订阅者异常隔离** | `publish()` 里 `try/except` 包住每个 handler，记一条 `error` 事件后继续 | 一个坏扩展点不能让整条流水线停摆（`test_subscriber_exception_does_not_break_publish`） |

投递成功数记在 `message.body["_delivered"]`，方便断言"消息确实有人收"。

### 4.2 `FixStage` 怎么"订阅"到发现清单

`runner.py` 在启动时创建 `Inbox(bus, recipient="fix").listen(TOPIC_REVIEW_COMPLETED)` 并注入
`FixStage`。`FixStage.run()` 里做的事是：

```python
message = inbox.latest(TOPIC_REVIEW_COMPLETED)      # 拿最新一条审查完成消息
if message is None: raise NodeFailed("修复阶段收不到 review.completed 消息 ...")
findings = message.body["findings"]
```

它**不接收**审查结果这个参数，也不读 `state["findings"]`。`FixStage` 也允许不注入 `Inbox`
（构造时为空就现场 `listen`），语义更直白。

## 5. 状态设计

### 5.1 为什么业务状态与执行状态放同一个存档

`SharedState` 里两类数据并存：

* **领域数据**（`data`）：需求文本、路径、代码清单、每轮审查结论、门禁结论、修复结果、轮次轨迹、
  残余问题、开放问题；
* **执行状态**（`completed` / `cursors`）：哪些节点已完成、循环跑到第几轮。

另外 `meta` 里还存**观测数据**（`stages` 阶段记录、`messages` 总线消息），它们不是"跑流程必需的"，
但决定续跑之后报告能否还原 —— 见 §6.4 的踩坑记录。两份 JSON 很便宜，丢了却很难补。

断点续跑要求"**业务状态与执行进度必须一致**"。分成两个文件就一定存在"进度说跑完了、业务数据
还没落盘"的窗口，恢复后行为不可预测。放同一个 JSON 里，一次原子替换同时更新两者。

`SharedState` 实现 `MutableMapping`，所以用起来就像 dict（`state["gate"]`、`state.get_path(
"gate.passed", False)`）；额外提供点号路径读写、完成标记、循环游标、存档与深拷贝快照。`snapshot()`
做 `json.loads(json.dumps(...))`，保证交给 Agent 的是"当时的样子"（`test_snapshot_is_deep_copy`）。

### 5.2 为什么用扁平 JSON 而不是 pickle

| 需求 | JSON | pickle |
| --- | --- | --- |
| 人能读、能 diff、能手工改一行再续跑 | ✅ | ❌ |
| 跨 Python 版本安全 | ✅ | 与类定义、模块路径、版本强绑定 |
| 安全性 | ✅ 纯数据 | ❌ **读一个数据文件变成执行任意代码** |

代价是只能存 JSON 可表达的类型，而这其实是好事：它逼着状态保持"扁平的、可序列化的数据"，而不是把一个活
的对象图塞进去。`Checkpoint.version` 做格式版本校验，不兼容时给"请重新开始一次运行（--no-resume）"这种
可执行提示，而不是让 `json` 的解码错误直接冒出来。

### 5.3 存档必须原子写

```python
with self._lock:                                    # 同进程写互斥
    if not self._dirty and not force and target.exists(): return target
    tag = f"{os.getpid()}.{threading.get_ident()}.{next(self._temp_counter)}"
    temp = target.with_name(f"{target.name}.{tag}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n"); temp.replace(target)   # 原子替换
```

三点都来自真实问题（§13.2）：中途崩溃留下半截 JSON 会直接毁掉 resume；固定 `.tmp` 文件名在并行
路径下会互相覆盖；无改动时重复写盘会把 mtime 刷成噪音（`test_skip_write_when_clean`）。

## 6. 断点续跑的实现与边界

### 6.1 一份标识三处用

`ctx.path` 是节点的**限定名**，例如 `2:review-fix-loop/#1/0:review`。它同时用于：

| 用途 | 位置 |
| --- | --- |
| 事件定位符（日志里 `node` 字段） | `RunContext.emit/debug/warn/error` 自动注入 |
| resume 完成标记（`state.completed` 的成员） | `Node.execute()` 里 `is_done(qid)` / `mark_done(qid)` |
| 循环游标的键（`state.cursors`） | `LoopNode.run()` 里 `get_cursor/set_cursor` |

一份标识三处用，避免"事件日志里叫 A、存档里叫 B、游标里叫 C"这种三套命名各说各话的局面。

### 6.2 `--resume` 的实现

```python
restored = SharedState.load(path)
self.run_id = restored.run_id                 # 存档里的 run_id 才是权威
self.state = restored; self.state.bind(path)
self.run_dir = path.parent                     # 运行目录以存档所在目录为准
self.outcome.workdir = ...; self.outcome.rounds = [RoundSummary(**r) for r in state.get_path("rounds")]
return True                                    # → Orchestrator.run(resumable=True)
```

`resumable=True` 之后，`Node.execute()` 在每层先问 `is_done(qid)`，命中就发一条 `node.skip`
事件并返回 `skipped`。因此"从第 N 轮中间继续"是自然发生的：循环游标 + 完成标记两层配合，
要么整轮跳过，要么整轮重跑。一个容易漏掉的细节（注释里写着）：**已完成的运行"续跑"等于立刻结束**，
此时 `outcome` 里什么都没有，所以必须从 state 里补回工作目录、历史轮次与残余问题。

### 6.3 它**不做**什么（必须写清楚）

**`resume` 不是 `rewind`，它不会重放已跳过节点的副作用。**

* 已经写进 `worktree` 的修复不会被撤销，也不会被重新执行；它只是"不再跑一遍"。想回到干净状态请
  重新 `run`（新 run-id、新工作副本），而不是指望 resume 回滚。
* 手工改 `state.json` 的 `completed` 可以"重做某一节点"，但业务状态不会自动跟着回退 —— 需要自己
  保证一致（这也是为什么 README 把它列为"可行但要小心"的做法）。

### 6.4 曾经的两个坑（已修，留作记录）

| 现象（修复前） | 原因 | 修法 |
| --- | --- | --- |
| 续跑后 `report.md` 第 5 节消息表为空、`总线消息 0 条`；"阶段耗时"一节也空 | **总线消息与阶段记录不在存档里**（只存在于内存与事件流），续跑时新进程从零开始 | `checkpoint()` 把 `stages`/`messages` 写进存档 `meta`；`resume()` 用 `StageReport(**row)` 与 `MessageBus.restore()` 重建，并把 `outcome.stages` 一起补上 |
| 续跑时的 `events.jsonl` 出现在**另一个新建的空运行目录**里，原目录的轨迹变成另一份文件 | `EventLog` 在 `Orchestrator.__init__` 就用当时的新 `run_id` 绑定路径，而且 `__init__` 就 `mkdir`；`resume()` 只改了 `run_dir`，没重绑 `events.path` | `EventLog(path, append=True)`：**首次写入才建文件**、读回已有最大 `seq` 接着编号；`resume()` 重绑到 `run_dir/events.jsonl` 并把新日志挂回总线 |

这两条都是写文档时**实际跑出来的**（对一次已完成的运行执行 `--resume runs/<run-id>/state.json`），
修复后同一条命令的表现是：`events.jsonl` 60 行 → **64 行且只有一份文件**、运行目录数不变、
报告仍有 7 条消息与 8 行阶段记录。回归测试 `test_resume_preserves_observability` 把这三件事
（不新建目录、事件只增不减、消息与阶段数恢复）都断言住了。

顺带一个同类的坑也一并修了：`EventLog.emit` 的序号递增原先没有加锁，并行 fan-out 时多个线程
同时写会让 `seq` 重复、JSONL 行交错 —— 又一个"只在并行路径下复现"的 bug，现在用一个
`threading.Lock` 把序号分配与写盘串起来（`events.py` 的注释里记着这段）。

## 7. 质量门禁与循环收敛

### 7.1 门禁结论的每个字段都要能解释

`GateStage` 把审查 payload 翻译成一个 gate 字典（同时写入 `state["gate"]` 并发出
`gate.evaluated` 消息）：

| 字段 | 含义 |
| --- | --- |
| `passed` | `blocking_count == 0` |
| `blocking` / `blocking_count` | 配置的阻断级别（默认 `critical`+`high`）与实际命中数 |
| `total` / `counts` | 全部发现数与五个级别的分布 |
| `fixable` / `needs_human` | 有自动修复策略 / 只能人工处理的条数 |
| `score` / `overall_risk` | 审查 Agent 给的健康分与整体风险（门禁只搬运，不重算） |
| `can_improve` | **是否还存在"可自动修的阻断级问题"** |
| `reason` | 一句话解释（三种分支：通过 / 有可修 / 全都不可修） |

阻断级别由 `--blocking` 决定，并且编排器在启动时把这个值**强制写进审查 Agent 的配置**
（`Orchestrator._align_blocking`）—— 否则会出现"审查说不合格、编排器说合格"的口径打架。

### 7.2 `can_improve` 为什么必须存在

循环的退出条件是：

```python
until = lambda ctx: gate.passed or not gate.can_improve
```

如果只有"门禁通过才退出"，那么遇到"阻断级问题全都不可自动修"（例如函数过长 `CG301`）时，
循环会白跑满 `max_rounds` 轮，每轮重复同样的审查 + 一次 `skip-fix`。有了 `can_improve`，
第一轮结束就停，结论直接给 `needs_human`（`test_loop_stops_when_nothing_fixable` 守着这条）。

### 7.3 为什么"达到上限但没收敛"是正常结果，不是异常

真实项目里常见的是"能自动修的修完了，剩下的必须人来判断"。`LoopNode` 因此把退出原因写进返回值
（`converged` / `max_rounds`），`RunOutcome.outcome` 也只有四种取值：`passed` /
`needs_human` / `failed` / `aborted`。`needs_human` 是**一种成功的运行结果**，退出码为 1
（而不是 2），这样 CI 才能把"需要人看"与"跑挂了"分开处置。

### 7.4 收尾复审为什么必要

`final-recheck` 的条件是 `state["needs_recheck"]`（由 `FixStage` 在**确实改动了文件**时置真）。
理由：如果最后一轮的最后一个动作是修复，那么本轮 `gate` 的结论是**修复之前**的过期数据 ——
报告结尾说"门禁通过"而代码是刚被改过的，是不诚实的。所以补一次 `review → gate → round-close`，
用 `label="recheck"` 把报告写到 `reports/recheck-rN/`，不覆盖本轮正式审查报告。

本次演示运行里 `needs_recheck=False`（第 2 轮最后动作是审查），所以 `final-recheck` 走
`else`（没有分支体）→ 返回 `skipped`，事件流里能看到这条记录。

## 8. 横切关注点的收敛点

`RunContext.call_agent()` 是整个编排器**唯一**的 Agent 出入口。它一次做了六件事：

```python
for attempt in 1..settings.max_attempts:
    request = AgentRequest(agent, task_id, run_id, round, objective, payload,
                           shared=self.state.snapshot(),          # ① 只读快照
                           metadata={... "blocking": config.blocking})
    events.emit(EVENT_AGENT_CALL, ...)                            # ② 调用事件
    response = handle.send(request, timeout=settings.timeout)      # ③ 传输与超时
    if response.ok: events.emit(EVENT_AGENT_RESULT, elapsed); return response   # ④ 结果事件 + 耗时
    if attempt >= attempts: raise NodeFailed(...)                  # ⑤ 重试或放弃
```

第六件事是"把 `metrics.elapsed_seconds` 补进响应"（由 `AgentHandle.send` 做）。
`agent.retry` 事件在重试时可观测，`retryable` 属性决定要不要重试（连接/超时类可重试，
"Agent 明确返回 failed"在还有次数时会重试、用尽后抛 `NodeFailed`）。

**为什么不能让每个节点各写一遍重试**：那样会出现三套重试策略、三套日志格式、三套"什么算失败"
的判断，而且新加一个节点时很容易忘记套上。收敛到一个函数之后，"新增一个 Agent 阶段"不需要
考虑重试、事件、状态快照这些事，只需要写业务。

同一思路还有 `RunContext.emit/debug/warn/error`：它们自动把 `node=self.path`、`round=self.round`
注入事件，所以调用方**不能再自己传 `node=` 或 `round=`** —— 传了会得到
`TypeError: EventLog.debug() got multiple values for keyword argument 'node'`。这是一个刻意的
约定（路径与轮次由上下文决定，不该由调用点覆盖），但踩到时错误信息并不直观，所以写在这里。

## 9. 报告与可视化

### 9.1 为什么流程图由流程树生成

手画的图会和代码脱节，一周后就是谎话。`Node.describe()` 递归返回一棵纯数据结构
（`{"type": "loop", "name": ..., "max_rounds": 3, "body": {...}}`），三处消费它：

| 消费方 | 产出 | 特点 |
| --- | --- | --- |
| `render_ascii_tree()` | 终端里那棵 `◉ ├── ◇ ▭` 的树 | `python -m workflow plan` 与 `report.md` 都用它 |
| `render_mermaid()` | `flowchart TD` 文本 | 可贴进 GitHub / Typora；分支边走 `-->|成立|` 标签边 |
| `_flow_boxes()` | HTML 里一列带箭头的盒子 | 纯 CSS，不依赖任何前端库 |

`tests/test_stages.py::test_pipeline_structure` 还把"流程结构本身"当契约来断言：改名或删阶段
必须被测出来。

### 9.2 HTML 为什么零外部依赖

演示电脑可能断网、评审者可能只下载一个文件。所以 `report.html` 把样式全部内联、图表用 CSS 画
（条形图就是一串 `div` + `width: …%`），`prefers-color-scheme: dark` 自动深色。
`tests/test_reporting.py` 里有一条断言：`<style>` 之前不能出现 `http://` —— 这条检查比人工看一遍
更可靠。

### 9.3 机器可读数据与渲染分离

`run.json` 存 `RunOutcome.to_dict()` + 消息记录 + 流程树描述；`report.md` / `report.html` 只是它的两个
**视图**。这样报告样式怎么改，都不会影响 CI 里的断言；反过来，把一个字段加进 `RunOutcome` 就自动出现在
三个地方。`write_final_reports()` 在 `runner._finalize_reports()` 里被 `try/except OSError` 包住：
**报告写失败不能掩盖真正的运行结果**，只记一条 `error` 事件。

## 10. 失败处理与降级

异常体系按"**谁来负责**"分类，而不是按"哪一层抛的"：

| 异常 | 责任方 | 处置 |
| --- | --- | --- |
| `WorkflowConfigError` | 用户（配置） | CLI 打印后退出码 2；重试无意义 |
| `AgentNotFound` | 用户（环境） | `registry.require()` 启动时快速失败，列出缺哪个 + 搜索过的根目录数 |
| `NodeFailed` | 单个节点 | 按 `retryable` 决定重试；用尽后向上抛，由编排器捕获 → `outcome=failed` |
| `NodeTimeout` | 单个节点（子进程） | `subprocess` 传输的超时，`retryable=True` |
| `CheckpointError` | 存档 | 版本不兼容 / 文件损坏 / 读不到 → 给出可执行提示 |
| `AbortWorkflow` | 主动中止 | `prepare` 缺需求文件、`review` 找不到可审查文件时用 → `outcome=aborted` |

编排器顶层（`Orchestrator.run`）的兜底顺序是：`AbortWorkflow` → `AgentNotFound` → `NodeFailed`
→ `WorkflowError` → `Exception`。最后一条带 `verbose` 时 `traceback.print_exc()`，但**绝不会
把裸 traceback 抛给 CLI**。降级表：

| 失败点 | 谁兜住 | 结果 |
| --- | --- | --- |
| 编码阶段失败 | `CodingStage` → `call_agent` 重试（`max_attempts=1`，不重试） | `outcome=failed`，退出码 2 |
| 审查阶段失败 | `max_attempts=2`，两次都失败才抛 | 同上；多文件 fan-out 时报告"哪个文件审崩了" |
| 修复阶段失败 | `max_attempts=1` | 同上（修复失败不该被掩盖，因为工作副本可能已被部分改写） |
| 单个订阅者抛异常 | `MessageBus.publish` | 记 `error` 事件，继续投递，不影响流程 |
| 事件写盘失败 | `EventLog.emit` 捕获 `OSError` | 静默（日志写不进去不该中断流程） |
| 报告写入失败 | `_finalize_reports` 捕获 `OSError` | 记 `error` 事件，主结果照常返回 |
| `state.save` 失败 | `checkpoint()` 内部 | 抛 `CheckpointError`（存档坏了必须让人知道，否则 resume 是假的） |
| Agent 装配失败 | `_send_inproc` 捕获 `Exception` | 变成契约内的 `AgentResponse.failure`，走正常失败路径 |

## 11. 可扩展性

### 11.1 加第 4 个 Agent（例如"文档 Agent"）

1. `registry.py`：在 `AGENT_SPECS` 里加 `"docs": ("docs_agent", "DocsAgent", "DocsConfig")`，
   `AGENT_LABELS` 里加中文名；
2. `config.py`：`DEFAULT_AGENT_OPTIONS` 里加默认选项，`WorkflowConfig` 加一个
   `docs: AgentSettings` 字段（`from_env` / `validate` / `describe` 里的三元组也要加）；
3. `bus.py`：加两个主题常量（如 `TOPIC_DOCS_DONE`）；
4. `stages.py`：写一个 `DocsStage(Node)`，在 `build_pipeline()` 的合适位置插入；
   `runner.REQUIRED_AGENTS` 决定它是必须的还是可选的；
5. 测试：`tests/support.py` 的 `scripted_registry()` 加一个假句柄，再补一条流程结构断言。

现有三个阶段的代码**一行都不用改** —— 这是"只共享 JSON 信封 + 消息总线"的直接回报。

### 11.2 改流程结构

* **改轮次上限**：`--max-rounds` / `WF_MAX_ROUNDS`（配置层已通）；
* **改门禁口径**：`--blocking`（会自动同步给审查 Agent）；
* **插入新阶段**：在 `build_pipeline()` 里构造节点并插进 `SequenceNode`；如果是"每轮都跑"，
  插进 `loop_body` 的 `SequenceNode("round", [...])`；
* **改判据**：`until=` / `condition=` 都是接受 `ctx` 的纯函数，改一行即可；
* **改完记得**：`plan` / `report.md` 的图会**自动**跟着变（`describe()` 递归），而 `test_pipeline_structure` 会告诉你结构契约变了。

### 11.3 换传输方式

* 全局：`--transport subprocess`（三个 Agent 一起切）；
* 单个：`WF_REVIEW_TRANSPORT=subprocess` / `AgentSettings(transport="subprocess")`；
* 换实现：`AgentHandle.send()` 是唯一的分派点，加一个 `elif settings.transport == "http": ...`
  即可接入远程 Agent（契约本来就是 JSON，不需要改任何阶段代码）。

## 12. 测试策略

### 12.1 两层设计

| 层 | 用什么驱动 | 覆盖 | 为什么这样分 |
| --- | --- | --- | --- |
| 单元 / 集成（88 个用例） | `tests/support.py::FakeHandle` + `scripted_registry()` | 编排逻辑：控制原语、resume、门禁、合并、报告、状态、总线 | 如果编排测试直接依赖真实 Agent，"编排逻辑对不对"就会被"隔壁仓库今天改了什么"污染，测试也就无法定位问题 |
| 跨仓库契约（4 个用例） | 四份 `protocol.py` 与两份可修复性词汇表 | 副本逐字节一致、契约版本一致、审查侧"可修清单" == 修复侧策略注册表 | 这类一致性只能靠跨仓库断言守住；对不上时流程不会报错，只会每轮"报可修、却修不动" |
| 端到端（4 个用例） | 真实三件套 | 至少两轮收敛、消息序列完整、clean 档位短路、产物齐全 | 只有真 Agent 才能证明"契约真的对得上" |

`FakeHandle` 只实现契约要求的 `send`（外加 `health`），`responses` 可以是固定 payload、列表，
或 `(request, call_no) -> payload` 的函数。`scripted_registry(findings_per_round=[[...], []])`
可以精确驱动"未通过门禁 → 修复 → 复审通过"这条路径。

一个容易被忽略的细节：假编码 Agent **必须真的把 `mod.py` 写出来**。否则审查阶段会在"没有可审查
的文件"处 `AbortWorkflow`，那样测到的就不是编排逻辑了 —— 夹具必须把副作用也模拟出来。

### 12.2 e2e 为什么允许"跳过"

`test_e2e.py::_agents_available()` 在找不到任何一个 Agent 时打印原因并 `return`（测试算通过）。
理由：**"隔壁仓库没 clone 下来"不是本仓库的缺陷**，不该让整套测试变红。但一旦三个仓库都在，
这些用例就必须真的跑通，且判据不是"退出码为 0"，而是三条硬结论：

1. 第一轮必须检出阻断级问题（否则流程没意义）；
2. 最后一轮阻断级归零、门禁通过、`total_fixed >= 5`；
3. 总线上留下 `code.ready → review.completed → gate.evaluated → fix.applied → pipeline.done`
   的完整序列，且 `review.completed` 至少出现两次（复审）。

### 12.3 用结构契约锁住"流程形状"

`test_pipeline_structure` 断言的是 `build_pipeline(max_rounds=4).describe()` 里
`["prepare", "coding", "review-fix-loop", "final-recheck", "wrap-up"]` 这个顺序、循环体是
`["review", "gate", "fix-if-needed", "round-close"]`、分支类型是 `branch`。这类断言的价值在于：
重构时如果不小心把 `final-recheck` 挪进循环里，测试立刻会告诉你。

## 13. 开发中遇到的工程问题

下面每一条都能在代码注释或测试名里找到对应位置。

### 13.1 消息总线的广播/定向判断写反

第一版 `publish()` 里的过滤条件写成了"广播时只投给广播订阅者"。结果是：`Inbox(recipient="fix")`
订阅上了，但永远收不到广播消息 —— 一个"看起来订阅成功、实际收不到"的静默错误。`FixStage` 因此报
"收不到 review.completed 消息，无法确定要修什么"，而**消息确实发出来了**，日志里两行都在，非常难查。
正确逻辑（现在的代码注释就写着这句）：

```python
# 广播消息人人都收；定向消息只给"订阅了广播"或"订阅了自己"的接收者
if recipient != BROADCAST and subscribed_recipient not in (BROADCAST, recipient):
    continue
```

回归测试是 `test_directed_subscriber_receives_broadcast`（名字就是结论），配套的还有
`test_directed_message_not_delivered_to_others` 与 `test_broadcast_subscriber_receives_everything`。
教训：**投递规则的四个象限必须全部写成测试**，只测"能收到"永远抓不到这种反向错误。

### 13.2 并行节点并发写存档，Windows 上 `Permission denied`

`Node.execute()` 成功后 `checkpoint()`。并行 fan-out 时多个线程几乎同时进来，第一版用固定文件名
`state.json.tmp`：一个线程刚 `open` 写，另一个线程 `replace` 掉它，第三个线程再写就被 Windows
拒绝 —— 报 `Permission denied`。这个 bug **只在并行路径下复现**，单文件 demo 永远碰不到。修法写在
`SharedState.save()` 里：`RLock` 做同进程写互斥 + 临时文件名带 `pid.threadid.序号` 三重标签。
`test_save_is_atomic_no_tmp_left` 断言"没有 `.tmp` 残留"。

### 13.3 `ctx.round` 回到父上下文就变回 0

`LoopNode` 每轮 `ctx.round = iteration`，但那是**循环自己的上下文**；循环结束回到父上下文后
`ctx.round` 又是 0，于是收尾复审记出的轮次是 `round 0`，报告里出现"第 0 轮"。修法是循环里把轮次
**同时写进共享状态**（`ctx.state["current_round"] = iteration`），并让所有阶段用 `_current_round(ctx)`
（先读 state，再退回 `ctx.round`）。注释解释了原因：上下文是派生对象、活不过节点边界；共享状态才是
活过边界的那个。

### 13.4 Mermaid 分支边重影

第一版画分支时，先给分支节点连一条普通边到 `then`，又额外连一条带标签的边 —— 图上同一个目标出现
两条边（一条无标签、一条 `|成立|`），看起来像重影。修法是**分支的子节点只走带标签的边**（父节点传
`None`，不生成普通边）。回归测试 `test_mermaid_has_no_duplicate_parent_edges` 直接断言"没有任何一条
普通边指向 fix 节点"。

### 13.5 假 Agent 不落盘，流程在"没有可审查文件"处中止

如果假编码 Agent 只返回 JSON 而不真的写文件，`ReviewStage._collect_targets()` 就扫不到任何 `*.py`，
抛 `AbortWorkflow("工作目录里没有匹配 ... 的源码，审查无从开始")`。看起来像编排器 bug，实际是夹具
不完整 —— `tests/support.py::coding_response` 的注释就写着这一点：**副作用也是契约的一部分**。

### 13.6 resume 的三个隐蔽前提

`runner.resume()` 的注释列了三条：**`run_id` 以存档为准**（用户可能只给 `state.json` 路径，此时运行
目录必须改成存档所在目录，否则续跑会往一个新建的空目录里写报告，表面上"跑成功了"、产物却是空的）；
**事件日志要重绑并改为追加**；**阶段记录与总线消息要从存档恢复**。后两条曾经是坏的，§6.4 记了症状、
修法与回归测试 —— 这类"恢复后报告变空"的问题不会抛异常，只会让一份看起来正常的报告失去最该有的证据。

## 14. 已知局限与改进方向

| 局限 | 说明 | 可能的改进 |
| --- | --- | --- |
| **单进程、无分布式** | 三个 Agent 都在编排器进程内（默认 `inproc`），没有队列、没有 worker 池 | `subprocess` 传输已经把契约验证成"纯 JSON"，下一步可接 HTTP/消息队列 |
| **`inproc` 传输不强制超时** | `AgentHandle.send()` 只把 `timeout` 传给子进程路径；进程内调用是同步的，一个卡住的 Agent 会卡住整条流程 | 用线程 + `future.result(timeout=…)` 包住 inproc 调用；或干脆默认走 subprocess |
| **演示不触发并行** | 演示需求只生成 1 个文件 → `shards=1`，看不到真正的并发 | 提供多文件 demo 需求；或让编码 Agent 按需求拆多个模块 |
| **跨轮次发现身份是近似的** | 匹配靠 `(file, line, rule_id)`，修复移动行号后同一问题会被当成新的 | 用"规则 + 归一化代码片段"做指纹 |
| **续跑后报告的"事件记录"只计本次进程** | `reporting` 用的是 `len(orchestrator.events)`（内存里的），而文件是累计的（实测 60 → 64 行） | 让 `EventLog` 在 append 模式下把已有事件也读回内存，或报告改读文件行数 |
| **没有 GUI 编排界面 / 审查规则只覆盖 Python** | 只有 `plan` 的文本流程图与 HTML 报告；规则引擎基于 `ast`，其他语言会退化成"没有可用规则" | 若真需要 GUI，可从 `describe()` 的 JSON 直接生成前端图，但收益有限；跨语言支持与审查 Agent 的定位一起决定 |
| **`--agents-home` 不是排他的** | 它只是**追加**候选根目录，向上搜索仍可能找到别处的同名包 | 增加 `--agents-strict` 或把搜索根目录打印到 `agents` 输出里（现在只打印前 8 个） |

## 15. 如果重来一次

1. **先定契约再写编排器**。现在 `protocol.py` 有四份等价副本、并由契约测试守着一致性；如果一开始
   就有这份 150 行的信封，三个 Agent 的字段名不会经历一次改名。
2. **把"路径/轮次"从一开始就放进共享状态**（§13.3），而不是先依赖 `ctx.round` 再回头修。
3. **`EventLog` 一开始就设计成 append + 重绑**（现已按此实现）：续跑不该"另起一份日志"，而应"同一个 run 目录、同一条事件流往后接"。
4. **给 `inproc` 也上超时**：现在只有子进程路径受 `timeout` 保护，这在"Agent 卡住"时是个隐患。
5. **测试从"四个象限"开始写**：广播/定向、成功/失败、有/无副作用 —— §13.1 的 bug 就是漏象限造成的，
   先写测试的成本远低于事后排查。
6. **演示需求准备两份**（单文件 / 多文件），让"按文件并行"在 demo 里就能看见，而不是只在注释里
   声称它有。

## 16. 附录：关键文件速查

| 文件 | 行数量级 | 关键类 / 函数 | 一句话 |
| --- | --- | --- | --- |
| `workflow/nodes.py` | ~390 | `Node.execute` / `SequenceNode` / `BranchNode` / `LoopNode` / `ParallelNode` | 控制原语与 resume 模板方法 |
| `workflow/stages.py` | ~840 | `build_pipeline` / 7 个 Stage / `merge_reviews` / `_current_round` | 唯一"知道业务"的一层 |
| `workflow/runner.py` | ~280 | `Orchestrator.run` / `resume` / `run_workflow` | 装配 → 执行 → 收尾 |
| `workflow/context.py` | ~225 | `RunContext.child` / `call_agent` / `checkpoint` | 横切关注点唯一出入口 |
| `workflow/state.py` / `workflow/bus.py` / `workflow/events.py` | ~295 / ~250 / ~280 | `SharedState` · `MessageBus.publish` · `EventLog` | 单一事实来源+原子存档 / 同步发布订阅 / JSONL 事件流 |
| `workflow/registry.py` | ~355 | `AgentRegistry.discover` / `AgentHandle.send` | 发现与两种传输 |
| `workflow/reporting.py` | ~610 | `render_markdown` / `render_html` / `render_mermaid` / `render_ascii_tree` | 报告与流程图 |
| `workflow/models.py` · `protocol.py` · `config.py` · `cli.py` · `tests/support.py` | ~235 / ~350 / ~235 / ~340 / ~250 | 模型 · 契约 · 配置 · CLI · 夹具 | 前四项见各节；`support.py` 提供 `FakeHandle`/`scripted_registry`/`tempdir` |
