# code-review-workflow 设计文档

> 软件工程实践 Homework 2 · 多 Agent 代码审查工作流（`workflow` 包）
>
> 这份文档记录编排器的架构决定、控制流原语、Agent 通信与状态设计，以及开发过程中真实踩过的
> 工程问题。文中出现的数字（测试数、消息数、节点数、事件数）都能在仓库和运行产物里核对。

## 要做什么、不做什么

PPT 里「代码审查流程」方向给的是编码 Agent + 审查 Agent + 修复 Agent，跑通"编码 → 审查 →
修复"。要求分"必须实现"和"推荐实现"两档。必须的那几条我都没打折：三个 Agent 各自独立仓库、
用真实三件套跑 e2e；消息传递和状态共享都做了（`MessageBus` 显式消息 + `SharedState` 单一
事实来源）；顺序执行是最基本的要求，在此之上还有分支、循环、并行；调度上保持单进程单机同步，
没有队列、没有调度器、没有分布式。推荐项里做了条件分支、循环、并行、JSONL 事件流与状态追踪、
错误分类与 `--resume` 续跑，以及复用 Homework 1 的 codeguard 规则引擎。

没做的是这几样，理由都是"现在做不值"：

不做分布式调度。三个 Agent 都是按需实例化的轻量对象，一次运行 0.1~0.7 秒；上分布式只会把编排
逻辑和部署问题搅在一起，反而看不清前者。

不做可视化拖拽编排界面。一个会和代码脱节的拖拽图，比一个"改代码图自动变"的流程图更有害，PPT
里这项本身也标着可选。

不做通用 DAG 引擎和 Agent 热插拔。四种原语、一棵静态树足够表达这条流程，通用引擎的抽象成本
（条件 DSL、动态拓扑、调度策略）在这里没有回报，运行中替换 Agent 还会让"这次运行到底跑了什么"
没法复现。

不保证跨轮次"同一个问题"的绝对身份。匹配键是 `(file, line, rule_id)`，修复会移动行号，所以跨轮
只能做到大致对齐，报告靠 `finding_id` 和规则统计说话。

## 总体结构

分层视图：

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

依赖方向：

```
cli.py ──▶ runner.py ──▶ stages.py ──▶ nodes.py ──▶ context.py ──▶ {bus, state, events, registry}
                │                          │
                └──▶ reporting.py ◀────────┘（只读 Orchestrator 与 RunOutcome）
```

`nodes.py` 不认识任何业务阶段，它只认 `Node` 契约，所以能脱离本项目复用。`stages.py` 是唯一
知道业务的一层，节点契约、总线主题、状态键名、协议模型都在它这儿汇合。横切能力（事件、重试、
共享状态快照、存档）全部收在 `context.py` 的 `RunContext` 里，节点只管调 `ctx.call_agent(...)`。
`reporting.py` 只读不写，所以运行失败之后它照样能被调用。

16 个模块的大致分工：

| 模块 | 职责 |
| --- | --- |
| `__init__.py` / `__main__.py` | 对外公开 API 与 `python -m workflow` 入口 |
| `nodes.py` | 控制原语、`Node.execute()` 模板方法、`NodeResult` |
| `stages.py` | 7 个业务阶段、`build_pipeline()`、`merge_reviews()` |
| `runner.py` | 装配、执行、异常兜底、收尾写报告、`resume()` |
| `context.py` | `RunContext`、`child()` 派生、`call_agent()`、`checkpoint()` |
| `state.py` | `SharedState` / `Checkpoint`：业务状态 + 执行进度 + 原子存档 |
| `bus.py` | `MessageBus` / `Inbox` / `Message`：同步发布订阅 |
| `events.py` | `EventLog` / `ConsoleListener` / `ListenerBuffer` / `read_events` |
| `registry.py` | `AgentRegistry` / `AgentHandle`：发现、装配、两种传输、健康检查 |
| `reporting.py` | Markdown / HTML / Mermaid / ASCII 渲染与写盘 |
| `models.py` | `StageReport` / `GateResult` / `RoundSummary` / `RunOutcome`，无 I/O |
| `protocol.py` | 契约信封：`AgentRequest` / `AgentResponse` / `FindingContract` |
| `config.py` | `WorkflowConfig` / `AgentSettings`，`WF_*` 环境变量与校验 |
| `cli.py` | 参数解析、五种命令、退出码映射 |
| `errors.py` | 异常体系，按"谁来负责"分类 |

各自不碰什么也值得写一句：`nodes.py` 没有业务语义，`stages.py` 不直接调 Agent（走 `ctx`），
`runner.py` 不实现任何阶段，`state.py` 不解释业务键的含义，`bus.py` 不持久化也不做异步投递，
`registry.py` 不解释 Agent 的私有配置，`reporting.py` 不改状态也不做判断，`cli.py` 不含业务逻辑。

一次运行实际经过的路径：

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

路径的每一段由 `RunContext.child(segment)` 拼出来，比如
`2:review-fix-loop/#1/2:fix-if-needed/then:fix`。这条字符串同时是事件里的定位符、
`state.completed` 的完成标记、循环游标的键，同一份标识到处复用，省掉了"日志里叫 A、存档里叫 B、
游标里叫 C"的麻烦。

## 流程控制原语

| 原语 | 构造 | 行为 |
| --- | --- | --- |
| `SequenceNode` | `(name, children, on_error="abort"\|"continue")` | 顺序执行；某个子节点抛 `NodeFailed` 时按策略中止或继续；最后一个子节点失败总是向上抛 |
| `BranchNode` | `(name, condition, then_node, else_node=None, description="")` | `condition(ctx)` 为真走 `then`，否则走 `else`；`else` 为空返回 `skipped`；返回值里带 `branch_taken` |
| `LoopNode` | `(name, body, max_rounds=3, until=None, round_observer=None)` | 最多 `max_rounds` 轮，`until(ctx)` 为真提前退出；起始轮次取自 `state.get_cursor(path)+1`；每轮结束 `checkpoint(force=True)`；退出原因写进返回值 |
| `ParallelNode` | `(name, children, max_workers=4)` | 并发执行无副作用子节点，按定义顺序合并结果；只有一个子节点时退化为同步调用；任一子节点失败整体失败 |
| `FunctionalNode` | `(name, fn)` | 用函数实现的叶子，`skip-fix` 就是它；另有 `AbortNode` 主动中止 |

四个关键行为散在实现里：分支条件是只读纯函数，因为 resume 会重复求值；并行子节点用
`ctx.child()` 派生独立上下文；`collect_nodes()` 深度优先列出整棵树，供报告和测试断言。

如果把流程写成 `def run(): prepare(); coding(); while ...: review(); if ...: fix()`，会同时丢掉几样东西。流程图没了，手画的图会和代码脱节，而 `describe()` 递归出来的树永远和代码同构。
resume 的定位点也没了，`state.completed` 需要一个稳定的节点标识，函数体里没有"节点"这个概念，
只能靠人肉编号。组合能力同样没了：`final-recheck` 复用 `ReviewStage`、`GateStage`、
`RoundCloseStage`，只是换了个 `label`（`recheck`）；`fix-if-needed` 的 `else` 分支可以是一个
"什么也不做"的 `FunctionalNode`，这些都不需要给业务阶段加参数。

`Node.execute()` 是模板方法，统一做这几件事：resume 跳过判定 → 执行 → 写完成标记 → 存档并发
`node.end` 事件。放在基类里统一实现，没有让每个节点各写一遍，因为 resume 的正确性依赖它不漏
任何一处；漏一处就会出现"跳过了但状态没写"的静默错误。反向约束也测了：
`test_failure_is_not_marked_completed` 断言失败不能标完成。

`ParallelNode` 有一条限制：并行子节点不得写共享状态。并发写共享状态要锁、要定序、失败还得
回滚，每一样在 MVP 阶段都不值得做。所以这里的做法是子节点只返回结果，汇合后由并行节点
在合并阶段做确定性合并。

按定义顺序合并不是强迫症。并发完成顺序不确定，如果按完成顺序合并，同样的输入会产出不同的报告，
测试、diff、报告就全失去可比性了（`test_parallel_merges_in_definition_order`）。

这条限制在真实流程里落成"审查按文件 fan-out"：每个分片只读一份文件、产出一份只读 payload，
最后由纯函数 `merge_reviews` 合并。合并规则写死在函数里（去重 → 重算 counts → 取最小 score →
取最差 risk），因为它是纯函数，可以直接单测，不必靠"看起来对"。

## 消息总线

只有共享状态的时候，Agent 之间的耦合是隐式的：修复 Agent 读到什么，取决于别人在什么时候写了
什么，光看代码根本看不出来。显式消息把协作关系变成可声明、可观测、可替换的东西。审查 Agent
只管 `publish("review.completed", ...)`，不知道谁在听；修复 Agent 用
`subscribe("review.completed", ...)` 声明自己对审查结论感兴趣；编排器在中间转发，顺手把每条
消息记进事件日志和消息历史，报告里的"Agent 消息传递"一节就是它。

于是"新增一个 Agent"变成"多一个订阅者"，不用改动别人的代码。PPT 里"可扩展性"那一项要考察的
基本上就是这个。

实现上有几个决定。投递是同步的、没有队列，`publish()` 内部直接调订阅者回调；顺序流程里异步会
引入"消息在被写之前就被读"的时序问题，而消息全量留在 `history` 里，将来真要换异步队列，只需要
替换投递实现，订阅方代码不动。广播和定向分开处理，`BROADCAST = "*"`，定向消息只投给"订阅了
广播"或"订阅了自己"的接收者，这样既能表达"这条消息只给 fix"，也不牺牲"我在监听全部"。
订阅者异常做了隔离，`publish()` 里用 `try/except` 包住每个 handler，记一条 `error` 事件之后
继续投递，一个坏扩展点不该让整条流水线停摆（`test_subscriber_exception_does_not_break_publish`）。
投递成功数记在 `message.body["_delivered"]`，方便断言"消息确实有人收"。

`FixStage` 拿到发现清单的方式就靠这套机制。`runner.py` 启动时创建
`Inbox(bus, recipient="fix").listen(TOPIC_REVIEW_COMPLETED)` 并注入 `FixStage`，它 `run()` 里
只做这几步：

```python
message = inbox.latest(TOPIC_REVIEW_COMPLETED)      # 拿最新一条审查完成消息
if message is None: raise NodeFailed("修复阶段收不到 review.completed 消息 ...")
findings = message.body["findings"]
```

它不接收审查结果这个参数，也不读 `state["findings"]`。构造时不注入 `Inbox` 也行，那就现场
`listen`，语义更直白。

## 状态与存档

`SharedState` 里数据分两块。领域数据放在 `data` 下，有需求文本、路径、代码清单、每轮审查
结论、门禁结论、修复结果、轮次轨迹、残余问题、开放问题。执行状态是 `completed` 和 `cursors`，
分别记哪些节点已完成、循环跑到第几轮。`meta` 里还存观测数据，也就是 `stages` 阶段记录和
`messages` 总线消息，它们不是跑流程必需的，但决定续跑之后报告能不能还原，这件事在"断点续跑"
一节里有踩坑记录。两份 JSON 很便宜，丢了却很难补。

断点续跑要求业务状态和执行进度一致。分成两个文件就一定存在"进度说跑完了、业务数据还没落盘"的
窗口，恢复后行为不可预测。放同一个 JSON 里，一次原子替换就能同时更新两者。

`SharedState` 实现 `MutableMapping`，用起来像个 dict（`state["gate"]`、
`state.get_path("gate.passed", False)`），额外提供点号路径读写、完成标记、循环游标、存档和
深拷贝快照。`snapshot()` 走 `json.loads(json.dumps(...))`，保证交给 Agent 的是"当时的样子"
（`test_snapshot_is_deep_copy`）。

不用 pickle 的理由与人能读的那份一样朴素。JSON 存的是纯数据，人能读、能 diff、能手工改一行
再续跑，换 Python 版本也不会出事；pickle 反过来把"读一个数据文件"变成"执行任意代码"，还和类
定义、模块路径、版本绑死，改个名字就废。代价是状态只能存 JSON 能表达的类型，这反而是好事，
它逼着状态保持扁平、可序列化，不会把一个活的对象图塞进去。`Checkpoint.version` 做格式版本
校验，不兼容时给人一句能照着做的话（"请重新开始一次运行（--no-resume）"），别让 `json` 的解码
错误直接冒出来。

存档必须原子写：

```python
with self._lock:                                    # 同进程写互斥
    if not self._dirty and not force and target.exists(): return target
    tag = f"{os.getpid()}.{threading.get_ident()}.{next(self._temp_counter)}"
    temp = target.with_name(f"{target.name}.{tag}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n"); temp.replace(target)   # 原子替换
```

这几点都来自真实问题（"踩过的坑"一节里记了一条）：中途崩溃留下半截 JSON 会直接毁掉 resume；
固定 `.tmp` 文件名在并行路径下会互相覆盖；无改动时重复写盘会把 mtime 刷成噪音
（`test_skip_write_when_clean`）。

## 断点续跑

`ctx.path` 是节点的限定名，比如 `2:review-fix-loop/#1/0:review`。事件定位符用的是它，
`RunContext.emit/debug/warn/error` 会自动注入；resume 的完成标记也是它，`Node.execute()`
里用 `is_done(qid)` / `mark_done(qid)`；循环游标的键还是它，`LoopNode.run()` 里用
`get_cursor/set_cursor`。

`--resume` 的实现不复杂：

```python
restored = SharedState.load(path)
self.run_id = restored.run_id                 # 存档里的 run_id 才是权威
self.state = restored; self.state.bind(path)
self.run_dir = path.parent                     # 运行目录以存档所在目录为准
self.outcome.workdir = ...; self.outcome.rounds = [RoundSummary(**r) for r in state.get_path("rounds")]
return True                                    # → Orchestrator.run(resumable=True)
```

`resumable=True` 之后，`Node.execute()` 在每层先问 `is_done(qid)`，命中就发一条 `node.skip`
事件并返回 `skipped`。"从第 N 轮中间继续"因此是自然发生的：循环游标和完成标记一起配合，要么
整轮跳过，要么整轮重跑。这里有个容易漏的细节，注释里也写着：已完成的运行"续跑"等于立刻结束，
此时 `outcome` 里什么都没有，所以必须从 state 里补回工作目录、历史轮次和残余问题。

它不做的事得写清楚。`resume` 不是 `rewind`，不会重放已跳过节点的副作用：已经写进 `worktree`
的修复不会被撤销，也不会被重新执行，只是不再跑一遍；想回到干净状态就重新 `run`（新 run-id、
新工作副本），别指望 resume 回滚。手工改 `state.json` 的 `completed` 可以重做某一个节点，但
业务状态不会跟着回退，需要自己保证一致，这也是 README 把它列为"可行但要小心"的原因。

续跑现在的表现：对一次已完成的运行执行 `--resume runs/<run-id>/state.json`，
`events.jsonl` 从 60 行变成 64 行、只有一份文件、运行目录数不变，报告里仍是 7 条消息和 8 行
阶段记录。当初为什么不长这样，"开发中踩过的坑"那一节里有记录。

## 质量门禁与循环收敛

`GateStage` 把审查 payload 翻译成一个每个字段都能解释的 gate 字典，同时写进 `state["gate"]`
并发出 `gate.evaluated` 消息：

| 字段 | 含义 |
| --- | --- |
| `passed` | `blocking_count == 0` |
| `blocking` / `blocking_count` | 配置的阻断级别（默认 `critical`+`high`）与实际命中数 |
| `total` / `counts` | 全部发现数与五个级别的分布 |
| `fixable` / `needs_human` | 有自动修复策略的条数 / 只能人工处理的条数 |
| `score` / `overall_risk` | 审查 Agent 给的健康分与整体风险，门禁只搬运不重算 |
| `can_improve` | 是否还存在可自动修的阻断级问题 |
| `reason` | 给人看的结论，按通过 / 有可修 / 全都不可修三种情况分支 |

阻断级别由 `--blocking` 决定，编排器启动时会把这个值强制写进审查 Agent 的配置
（`Orchestrator._align_blocking`），否则就会出现"审查说不合格、编排器说合格"的口径打架。

循环的退出条件是 `until = lambda ctx: gate.passed or not gate.can_improve`。如果只认"门禁通过
才退出"，遇到阻断级问题全都不可自动修（比如函数过长 `CG301`）时会白跑满 `max_rounds` 轮，每轮
重复同样的审查加一次 `skip-fix`。有了 `can_improve`，第一轮结束就停，结论直接给 `needs_human`
（`test_loop_stops_when_nothing_fixable` 守着这条）。

"达到上限但没收敛"是正常结果，不是异常。真实项目里常见的情况是能自动修的修完了，剩下的必须人
来判断。`LoopNode` 因此把退出原因写进返回值（`converged` / `max_rounds`），
`RunOutcome.outcome` 的取值也只有 `passed`、`needs_human`、`failed`、`aborted`。
`needs_human` 是一种成功的运行结果，退出码是 1，这样 CI 才能把"需要人看"和"跑挂了"分开处置。

收尾复审是必要的。`final-recheck` 的条件是 `state["needs_recheck"]`，由 `FixStage` 在确实
改动了文件时置真。理由是：如果最后一轮的最后一个动作是修复，那么本轮 `gate` 的结论是修复之前
的过期数据，报告结尾说"门禁通过"而代码刚被改过，说不过去。所以补一次
`review → gate → round-close`，用 `label="recheck"` 把报告写到 `reports/recheck-rN/`，不覆盖
本轮的正式审查报告。演示运行里 `needs_recheck=False`（第 2 轮最后动作是审查），`final-recheck`
走 `else`，没有分支体，返回 `skipped`，事件流里能看到这条记录。

## 横切关注点与报告

`RunContext.call_agent()` 是整个编排器唯一的 Agent 出入口，一次做了这些事：

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

还有一件事是把 `metrics.elapsed_seconds` 补进响应，由 `AgentHandle.send` 负责。重试过程有
`agent.retry` 事件可观测，`retryable` 属性决定要不要重试：连接和超时类可重试，"Agent 明确
返回 failed"在还有次数时会重试、用尽后抛 `NodeFailed`。

不能让每个节点各写一遍重试。那样会出现好几套重试策略、好几套日志格式、好几套"什么算失败"的判断，
而且新加节点时很容易忘了套上。收敛到一个函数之后，新增一个 Agent 阶段只需要写业务，不用考虑
重试、事件、状态快照这些事。

同一思路还有 `RunContext.emit/debug/warn/error`，它们自动把 `node=self.path`、`round=self.round`
注入事件，所以调用方不能再自己传 `node=` 或 `round=`，传了会得到
`TypeError: EventLog.debug() got multiple values for keyword argument 'node'`。这个约定是想让
路径和轮次只由上下文决定，不该被调用点覆盖，但踩到时错误信息并不直观，所以写在这里。

流程图是流程树生成的，没人手画。手画的图会和代码脱节，一周后就是谎话。`Node.describe()` 递归
返回一棵纯数据结构（`{"type": "loop", "name": ..., "max_rounds": 3, "body": {...}}`），
`render_ascii_tree()` 出终端里那棵 `◉ ├── ◇ ▭` 的树，`plan` 和
`report.md` 都用它；`render_mermaid()` 出 `flowchart TD` 文本，能贴进 GitHub 或 Typora，分支边
走 `-->|成立|` 标签边；`_flow_boxes()` 出 HTML 里一列带箭头的盒子，纯 CSS，不依赖任何前端库。
`tests/test_stages.py::test_pipeline_structure` 还把流程结构本身当契约来断言，改名或删阶段必须
被测出来。

HTML 做成零外部依赖是有现实原因的：演示电脑可能断网，评审者可能只下载一个文件。所以
`report.html` 的样式全部内联，图表用 CSS 画（条形图就是一串 `div` 加 `width: …%`），
`prefers-color-scheme: dark` 自动深色。`tests/test_reporting.py` 里有一条断言管着：`<style>`
之前不能出现 `http://`，这条检查比人工看一遍更可靠。

机器可读数据和渲染是分开的。`run.json` 存 `RunOutcome.to_dict()` 加消息记录和流程树描述，
`report.md`、`report.html` 只当它的两个视图。这样报告样式怎么改都不会影响 CI 里的断言，反过来，
往 `RunOutcome` 里加一个字段，三个地方自动都有。`write_final_reports()` 在
`runner._finalize_reports()` 里被 `try/except OSError` 包住：报告写失败不能掩盖真正的运行结果，
只记一条 `error` 事件。

## 失败处理与降级

异常体系按"谁来负责"分类，不按"哪一层抛的"分类：

| 异常 | 责任方 | 处置 |
| --- | --- | --- |
| `WorkflowConfigError` | 用户（配置） | CLI 打印后退出码 2，重试无意义 |
| `AgentNotFound` | 用户（环境） | `registry.require()` 启动时快速失败，列出缺哪个和搜索过的根目录数 |
| `NodeFailed` | 单个节点 | 按 `retryable` 决定重试；用尽后向上抛，由编排器捕获，`outcome=failed` |
| `NodeTimeout` | 单个节点（子进程） | `subprocess` 传输的超时，`retryable=True` |
| `CheckpointError` | 存档 | 版本不兼容、文件损坏、读不到，都给出可执行提示 |
| `AbortWorkflow` | 主动中止 | `prepare` 缺需求文件、`review` 找不到可审查文件时用，`outcome=aborted` |

编排器顶层（`Orchestrator.run`）的兜底顺序是 `AbortWorkflow` → `AgentNotFound` → `NodeFailed`
→ `WorkflowError` → `Exception`。最后一条在 `verbose` 时会 `traceback.print_exc()`，但绝不把
裸 traceback 抛给 CLI。各失败点的处置：

| 失败点 | 谁兜住 | 结果 |
| --- | --- | --- |
| 编码阶段失败 | `CodingStage` → `call_agent`，`max_attempts=1` 不重试 | `outcome=failed`，退出码 2 |
| 审查阶段失败 | `max_attempts=2`，两次都失败才抛 | 同上；多文件 fan-out 时报告哪个文件审崩了 |
| 修复阶段失败 | `max_attempts=1` | 同上，修复失败不该被掩盖，工作副本可能已被部分改写 |
| 单个订阅者抛异常 | `MessageBus.publish` | 记 `error` 事件，继续投递 |
| 事件写盘失败 | `EventLog.emit` 捕获 `OSError` | 静默，日志写不进去不该中断流程 |
| 报告写入失败 | `_finalize_reports` 捕获 `OSError` | 记 `error` 事件，主结果照常返回 |
| `state.save` 失败 | `checkpoint()` 内部 | 抛 `CheckpointError`，存档坏了必须让人知道，否则 resume 是假的 |
| Agent 装配失败 | `_send_inproc` 捕获 `Exception` | 变成契约内的 `AgentResponse.failure`，走正常失败路径 |

## 可扩展性与测试策略

加一个 Agent，比如"文档 Agent"，改动面是这样的：`registry.py` 的 `AGENT_SPECS` 里加一行
`"docs": ("docs_agent", "DocsAgent", "DocsConfig")`，`AGENT_LABELS` 里加中文名；`config.py` 的
`DEFAULT_AGENT_OPTIONS` 加默认选项，`WorkflowConfig` 加一个 `docs: AgentSettings` 字段，
`from_env` / `validate` / `describe` 里的三元组跟着加；`bus.py` 加主题常量；`stages.py` 写一个
`DocsStage(Node)`，在 `build_pipeline()` 的合适位置插进去，`runner.REQUIRED_AGENTS` 决定它是
必需还是可选；测试里给 `scripted_registry()` 加个假句柄，再补一条流程结构断言。现有三个阶段
的代码一行都不用改，这是"只共享 JSON 信封 + 消息总线"换来的。

改流程结构更容易。轮次上限走 `--max-rounds` 或 `WF_MAX_ROUNDS`；门禁口径走 `--blocking`，会
自动同步给审查 Agent；插入新阶段就在 `build_pipeline()` 里构造节点插进 `SequenceNode`，如果是
"每轮都跑"就插进 `loop_body` 的 `SequenceNode("round", [...])`；判据 `until=` / `condition=`
都是接受 `ctx` 的纯函数，改一行就行。改完 `plan` 和报告里的图会自动跟着变，因为
`describe()` 是递归的；结构契约变了 `test_pipeline_structure` 会报出来。

换传输方式也一样机械：全局切用 `--transport subprocess`，单个切用
`WF_REVIEW_TRANSPORT=subprocess` 或 `AgentSettings(transport="subprocess")`；接远程 Agent 只需
在 `AgentHandle.send()` 这个唯一分派点上加一个分支，契约本来就是 JSON，任何阶段代码都不用动。

测试里 88 个用例跑编排逻辑，驱动方式是 `tests/support.py::FakeHandle` 加
`scripted_registry()`，覆盖控制原语、resume、门禁、合并、报告、状态和总线。为什么不直接用真
Agent：那样"编排逻辑对不对"就会被"隔壁仓库今天改了什么"污染，测试也无法定位问题。4 个用例是
跨仓库契约，比对四份 `protocol.py` 与两份可修复性词汇表，断言副本逐字节一致、契约版本一致、
审查侧的"可修清单"和修复侧的策略注册表一一对应；这类一致性只能靠跨仓库断言守住，对不上时
流程不会报错，只会每轮"报可修、却修不动"。剩下 4 个端到端用例用真实三件套，至少证明契约真的
对得上。

`FakeHandle` 只实现契约要求的 `send` 和 `health`，`responses` 可以是固定 payload、一个列表，
或 `(request, call_no) -> payload` 的函数。`scripted_registry(findings_per_round=[[...], []])`
能精确驱动"未通过门禁 → 修复 → 复审通过"这条路径。有个容易被忽略的细节：假编码 Agent 必须
真的把 `mod.py` 写出来，否则审查阶段会在"没有可审查的文件"处 `AbortWorkflow`，那样测到的就
不是编排逻辑了，夹具得把副作用也模拟出来。

e2e 允许"跳过"。`test_e2e.py::_agents_available()` 在找不到任何一个 Agent 时打印原因并
`return`，测试算通过。理由是"隔壁仓库没 clone 下来"不是本仓库的缺陷，不该让整套测试变红。三个
仓库都在的时候，这些用例必须真的跑通，判据也不看退出码，要看的是：第一轮必须检出阻断级
问题；最后一轮阻断级归零、门禁通过、`total_fixed >= 5`；总线上留下
`code.ready → review.completed → gate.evaluated → fix.applied → pipeline.done` 的完整序列，且
`review.completed` 至少出现两次。

结构契约还锁住了"流程形状"。`test_pipeline_structure` 断言的是
`build_pipeline(max_rounds=4).describe()` 里 `["prepare", "coding", "review-fix-loop",
"final-recheck", "wrap-up"]` 这个顺序、循环体是
`["review", "gate", "fix-if-needed", "round-close"]`、分支类型是 `branch`。这类断言的价值在
重构时体现：不小心把 `final-recheck` 挪进循环里，测试立刻会告诉你。

## 开发中踩过的坑

下面每一条都能在代码注释或测试名里找到对应位置。

消息总线的广播/定向判断写反过。第一版 `publish()` 里的过滤条件写成了"广播时只投给广播订阅者"，
结果是 `Inbox(recipient="fix")` 订阅上了却永远收不到广播消息，一个"看起来订阅成功、实际收不到"
的静默错误。`FixStage` 因此报"收不到 review.completed 消息，无法确定要修什么"，而消息确实发
出来了，日志里两行都在，非常难查。正确逻辑是：

```python
# 广播消息人人都收；定向消息只给"订阅了广播"或"订阅了自己"的接收者
if recipient != BROADCAST and subscribed_recipient not in (BROADCAST, recipient):
    continue
```

回归测试是 `test_directed_subscriber_receives_broadcast`，名字就是结论，配套的还有
`test_directed_message_not_delivered_to_others` 和 `test_broadcast_subscriber_receives_everything`。
教训是投递规则的四个象限都得写成测试，只测"能收到"永远抓不到这种反向错误。

并行节点并发写存档，在 Windows 上报过 `Permission denied`。`Node.execute()` 成功后
`checkpoint()`，并行 fan-out 时多个线程几乎同时进来，第一版用固定文件名 `state.json.tmp`：
一个线程刚 `open` 写，另一个线程 `replace` 掉它，第三个线程再写就被 Windows 拒绝。这个 bug 只在
并行路径下复现，单文件 demo 永远碰不到。修法在 `SharedState.save()` 里：`RLock` 做同进程写
互斥，临时文件名带 `pid.threadid.序号` 三重标签，`test_save_is_atomic_no_tmp_left` 断言没有
`.tmp` 残留。

`ctx.round` 回到父上下文就变回 0。`LoopNode` 每轮 `ctx.round = iteration`，但那只是循环自己的
上下文；循环结束回到父上下文后 `ctx.round` 又是 0，于是收尾复审记出的轮次是 `round 0`，报告里
出现"第 0 轮"。修法是循环里把轮次同时写进共享状态（`ctx.state["current_round"] = iteration`），
并让所有阶段用 `_current_round(ctx)`（先读 state，再退回 `ctx.round`）。注释解释了原因：上下文是
派生对象、活不过节点边界，共享状态才活得过。

Mermaid 分支边出现过重影。第一版画分支时，先给分支节点连一条普通边到 `then`，又额外连一条带
标签的边，图上同一个目标出现两条边，一条无标签、一条 `|成立|`。修法是分支的子节点只走带标签的
边（父节点传 `None`，不生成普通边），回归测试 `test_mermaid_has_no_duplicate_parent_edges`
直接断言没有任何一条普通边指向 fix 节点。

假 Agent 不落盘，流程在"没有可审查文件"处中止。如果假编码 Agent 只返回 JSON 而不真的写文件，
`ReviewStage._collect_targets()` 就扫不到任何 `*.py`，抛
`AbortWorkflow("工作目录里没有匹配 ... 的源码，审查无从开始")`。看起来像编排器 bug，实际是夹具
不完整，`tests/support.py::coding_response` 的注释就写着这一点：副作用也是契约的一部分。

resume 有几个容易漏的前提，`runner.resume()` 的注释里写着：`run_id` 以存档为准（用户可能只给
`state.json` 路径，此时运行目录必须改成存档所在目录，否则续跑会往新建的空目录里写报告，表面上
"跑成功了"、产物却是空的）；事件日志要重绑并改为追加；阶段记录与总线消息要从存档恢复。后两条
曾经是坏的，症状是续跑后报告里"Agent 消息传递"一节变成 `总线消息 0 条`、"阶段耗时"一节整个
空了，因为没有哪一处抛异常，表现只是一份看起来正常的报告失去了最该有的证据。修法有两半：
`checkpoint()` 把 `stages` / `messages` 写进存档 `meta`，`resume()` 用 `StageReport(**row)` 和
`MessageBus.restore()` 重建并把 `outcome.stages` 补上；`EventLog` 改成
`EventLog(path, append=True)`，首次写入才建文件、读回已有最大 `seq` 接着编号，`resume()` 重绑
到 `run_dir/events.jsonl` 并挂回总线。这两条都是写文档时对着一次真实运行执行
`--resume runs/<run-id>/state.json` 跑出来的。

还有一个只在并行路径下复现的 bug：`EventLog.emit` 的序号递增原先没加锁，并行 fan-out 时多个
线程同时写会让 `seq` 重复、JSONL 行交错。现在用一个 `threading.Lock` 把序号分配和写盘串起来，
`events.py` 的注释里记着这段。

## 局限、重来一次与关键文件速查

已经知道的问题：

单进程、无分布式。三个 Agent 都在编排器进程内（默认 `inproc`），没有队列也没有 worker 池。
`subprocess` 传输已经把契约验证成"纯 JSON"，下一步接 HTTP 或消息队列是顺的。

`inproc` 传输不强制超时。`AgentHandle.send()` 只把 `timeout` 传给子进程路径，进程内调用是同步
的，一个卡住的 Agent 会卡住整条流程。改进方向是用线程加 `future.result(timeout=…)` 包住
inproc 调用，或者干脆默认走 subprocess。

演示不触发并行。演示需求只生成 1 个文件，`shards=1`，看不到真正的并发。可以提供多文件 demo
需求，或让编码 Agent 按需求拆多个模块。

跨轮次发现身份是近似的。匹配靠 `(file, line, rule_id)`，修复移动行号后同一问题会被当成新的。
用"规则 + 归一化代码片段"做指纹会稳一些。

续跑后报告的"事件记录"只计本次进程。`reporting` 用的是 `len(orchestrator.events)`（内存里的），
而文件是累计的，实测 60 → 64 行。可以让 `EventLog` 在 append 模式下把已有事件读回内存，或者
报告直接从文件读行数。

没有 GUI 编排界面，审查规则也只覆盖 Python。现在只有 `plan` 的文本流程图和 HTML 报告；规则引擎
基于 `ast`，其他语言会退化成"没有可用规则"。真需要 GUI 的话，从 `describe()` 的 JSON 直接生成
前端图不难，但收益有限；跨语言支持得跟审查 Agent 的定位一起决定。

`--agents-home` 不是排他的。它只是追加候选根目录，向上搜索仍可能找到别处的同名包。可以加
`--agents-strict`，或者把搜索根目录打印到 `agents` 输出里（现在只打印前 8 个）。

如果重来一次，会改这些地方。先定契约再写编排器：现在 `protocol.py` 有四份等价副本、由契约测试
守着一致性，如果一开始就有这份契约文件，三个 Agent
的字段名不会经历一次改名。把
"路径/轮次"一开始就放进共享状态，别先依赖 `ctx.round` 再回头修。`EventLog` 一开始就设计成
append 加重绑，续跑不该另起一份日志，而应同一个 run 目录、同一条事件流往后接。给 `inproc` 也
上超时，现在只有子进程路径受 `timeout` 保护，这在 Agent 卡住时是个隐患。测试从"四个象限"开始
写：广播/定向、成功/失败、有/无副作用，前面那个投递判断写反的 bug 就是漏象限造成的，先写测试
比事后排查便宜得多。演示需求准备两份（单文件、多文件），让"按文件并行"在 demo 里就能看见，
不用只在注释里声称它有。

关键文件速查：

| 文件 | 行数量级 | 关键类 / 函数 | 说明 |
| --- | --- | --- | --- |
| `workflow/nodes.py` | ~390 | `Node.execute` / `SequenceNode` / `BranchNode` / `LoopNode` / `ParallelNode` | 控制原语与 resume 模板方法 |
| `workflow/stages.py` | ~840 | `build_pipeline` / 7 个 Stage / `merge_reviews` / `_current_round` | 唯一"知道业务"的一层 |
| `workflow/runner.py` | ~280 | `Orchestrator.run` / `resume` / `run_workflow` | 装配 → 执行 → 收尾 |
| `workflow/context.py` | ~225 | `RunContext.child` / `call_agent` / `checkpoint` | 横切关注点唯一出入口 |
| `workflow/state.py` / `workflow/bus.py` / `workflow/events.py` | ~295 / ~250 / ~280 | `SharedState` · `MessageBus.publish` · `EventLog` | 单一事实来源+原子存档 / 同步发布订阅 / JSONL 事件流 |
| `workflow/registry.py` | ~355 | `AgentRegistry.discover` / `AgentHandle.send` | 发现与两种传输 |
| `workflow/reporting.py` | ~610 | `render_markdown` / `render_html` / `render_mermaid` / `render_ascii_tree` | 报告与流程图 |
| `workflow/models.py` · `protocol.py` · `config.py` · `cli.py` · `tests/support.py` | ~235 / ~350 / ~235 / ~340 / ~250 | 模型 · 契约 · 配置 · CLI · 夹具 | `support.py` 提供 `FakeHandle` / `scripted_registry` / `tempdir` |
