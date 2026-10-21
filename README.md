# code-review-workflow —— 多 Agent 代码审查工作流

> 编码 → 审查 → 修复，带条件分支、循环收敛、按文件并行与断点续跑　·　软件工程实践 Homework 2
>
> 源码：<https://github.com/0x697551b0/hw2-code-review-workflow>　作者：刘浩天 / 2412190219

本仓库是 Homework 2「代码审查流程」方向的编排器。它把三个独立仓库里的 Agent 当成可调用的节点，
串成一条自己会收敛的流水线：编码 Agent 出代码草案，审查 Agent 用 codeguard 的规则引擎查一遍，
门禁不通过就交给修复 Agent 改，改完再复审，一直跑到阻断级问题清零或者轮数用完。

作业的要求是至少两个 Agent 协作、有消息传递或状态共享、能顺序跑通就算 MVP。这里做了三个 Agent
（编码、审查、修复），消息总线和共享状态两样都用上了：Agent 之间靠总线发事件，编排器靠共享状态
记住跑到第几轮。分支、循环、并行也顺手做了，因为真实流程很快就不够线性。

版本 `1.0.0`，Python 要求 3.10 以上（我开发和验证用的是 3.14.7）。零第三方依赖，状态、总线、
事件流、报告、流程图全部用标准库手写。测试 96 个用例，其中 4 个是真实三 Agent 的端到端用例、
4 个是四个仓库之间的契约一致性用例。一次典型运行 0.1~0.7 秒，全离线，不调 LLM。

## 快速开始

四个仓库要放在同一个目录下，编排器靠"在附近找 `包名/__init__.py`"来发现 Agent：

```
homework2/
├── coding-agent/  → coding_agent      ├── fix-agent/            → fix_agent
├── review-agent/  → review_agent      └── code-review-workflow/ → workflow（本仓库）
```

```bash
cd code-review-workflow
python -m workflow agents    # 先确认三个 Agent 都在（只读）
python -m workflow run --spec demo/spec_user_store.md
```

不用装依赖、不用联网、不用 API Key，三个 Agent 的主路径都是离线的确定性实现。产物默认写到
`runs/run-<时间戳>-<4位>/`，跑完终端会把每个产物的绝对路径列出来。

## 一次运行长什么样

`python -m workflow run --spec demo/spec_user_store.md` 在终端里打出来是这样（运行 ID、耗时和
产物路径每次都不同）：

```
 __        ___  __        __ _                 _
════════════════════════════════════════════════════════════════════════
运行 run-20261022-052057-ee24　结论：✅ 通过　耗时 0.14s
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

残余非阻断问题 8 条：
  · low      CG304   user_store.py:14 缺少文档字符串：create_user（在 create_user 中）
  · low      CG304   user_store.py:20 缺少文档字符串：get_user（在 get_user 中）
  · low      CG304   user_store.py:31 缺少文档字符串：find_users（在 find_users 中）
  · low      CG304   user_store.py:40 缺少文档字符串：load_config（在 load_config 中）
  · info     CG307   user_store.py:14 缺少返回值类型注解：create_user（在 create_user 中）
  · info     CG307   user_store.py:20 缺少返回值类型注解：get_user（在 get_user 中）
  · info     CG307   user_store.py:31 缺少返回值类型注解：find_users（在 find_users 中）
  · info     CG307   user_store.py:40 缺少返回值类型注解：load_config（在 load_config 中）

产物
  report.md    D:\软件工程实践\homework2\code-review-workflow\runs\run-20261022-052057-ee24\report.md
  report.html  D:\软件工程实践\homework2\code-review-workflow\runs\run-20261022-052057-ee24\report.html
  run.json     D:\软件工程实践\homework2\code-review-workflow\runs\run-20261022-052057-ee24\run.json
  events.jsonl D:\软件工程实践\homework2\code-review-workflow\runs\run-20261022-052057-ee24\events.jsonl
  state.json   D:\软件工程实践\homework2\code-review-workflow\runs\run-20261022-052057-ee24\state.json
  工作副本     D:\软件工程实践\homework2\code-review-workflow\runs\run-20261022-052057-ee24\worktree
════════════════════════════════════════════════════════════════════════
```

这一次运行的过程，数字都能在产物里对上。第 1 轮编码 Agent 产出一份 37 行的草案，审查命中 15 条
（1 critical、2 high、3 medium、5 low、4 info），阻断 3 条，健康分 38，门禁没过；修复 Agent
改掉 7 条，完整补丁在 `reports/fix-r1/fix.diff`。第 2 轮复审命中 8 条，全是 low 和 info，
阻断清零，健康分 96，门禁通过，于是 `fix-if-needed` 走 `else:skip-fix`，修复 Agent 这次没被
调用。收尾时 `final-recheck` 看到 `needs_recheck=False` 直接跳过，`wrap-up` 把 8 条残余问题记下来
并发出 `pipeline.done`。剩下的 8 条是 4 条缺文档字符串和 4 条缺返回值类型注解，都不影响门禁。

`report.html` 是单文件的可视化报告，样式内联、零外部依赖、断网也能双击打开。里面有结论与 KPI
（轮次、自动修复、健康分、总线消息、完成节点）、问题分布条形图、由流程树生成的流程图、收敛曲线、
Agent 消息表、阶段耗时表、残余问题表和产物目录树。

## 命令行、环境变量与退出码

五个子命令。`run` 跑完整流程，也是唯一会写文件、会调用 Agent 的那个；`plan` 打印流程结构；
`agents` 检查三个 Agent 能不能找到；`replay` 回放某次运行的事件流；`info` 打印版本和环境。
后四个都是只读的，基本瞬时返回。

`run` 的参数：

| 参数 | 说明 |
| --- | --- |
| `--spec <path>` | 必填，需求文件（Markdown），例如 `demo/spec_user_store.md` |
| `--output-root <dir>` / `--agents-home <dir>` | 产物根目录（默认 `./runs`）/ Agent 仓库目录（追加到搜索候选，不是唯一来源） |
| `--codeguard-path <dir>` / `--pattern <glob>` | codeguard 源码根目录（同时传给审查与修复）/ 审查文件 glob（默认 `*.py`） |
| `--blocking <level>` / `--max-rounds <N>` | 阻断级别，可重复（默认 `critical`+`high`）/ 最多几轮审查→修复（默认 3） |
| `--coding-engine offline\|llm`、`--coding-profile realistic\|clean` | 编码 Agent 的引擎与草案档位 |
| `--review-mode static\|llm` / `--fix-engine rules\|llm` / `--transport inproc\|subprocess` | 审查模式（默认 `static`，走 codeguard）/ 修复引擎 / 三个 Agent 的调用方式（默认 `inproc`） |
| `--resume <state.json>` / `--no-checkpoint` | 从存档继续（不会重放副作用）/ 关闭存档 |
| `--trace` / `--json` / `--quiet` / `--verbose` | 实时轨迹 / JSON 结论 / 只打一行 / 调试日志 |

`plan` 还支持 `--max-rounds`、`--json`、`--mermaid`；`agents` 支持 `--agents-home`、`--json`；
`replay` 接 `<events.jsonl>`，另有 `--filter <kind>` 和 `--json`。

退出码：

| 退出码 | 含义 | 什么时候出现 |
| --- | --- | --- |
| `0` | 跑完了，门禁通过 | `outcome = passed` |
| `1` | 跑完了但没通过门禁，需要人看 | `outcome = needs_human`，比如 `--blocking low` 时剩下的问题都不可自动修 |
| `2` | 跑挂了（配置、环境或运行错误） | `outcome = failed` 或 `aborted`，比如 `--spec nope.md` |

`agents` 在没找齐三个 Agent 时返回 `1`，可以当 CI 的环境自检。

环境变量的前缀是 `WF_`，优先级是命令行参数 > 环境变量 > `.env` > 默认值：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WF_SPEC_PATH` / `WF_OUTPUT_ROOT` | — / `./runs` | 需求文件 / 产物根目录（其下每次新建 `<run-id>/`） |
| `WF_AGENTS_HOME` / `HW2_AGENTS_HOME` | 空 | Agent 仓库目录（空=自动搜索）/ 额外的包搜索根目录（`os.pathsep` 分隔） |
| `WF_BLOCKING` / `WF_MAX_ROUNDS` / `WF_PATTERNS` / `WF_TRACE` / `WF_VERBOSE` / `WF_CHECKPOINT` | `critical,high` / `3` / `*.py` / `0` / `0` / `1` | 门禁与轮次 / 实时轨迹 / 调试日志 / 阶段存档（关掉就没有 resume 了） |
| `WF_CODING_OPTIONS` / `WF_REVIEW_OPTIONS` / `WF_FIX_OPTIONS` / `WF_*_TRANSPORT` | 见 `.env.example` / `inproc` | 透传给各 Agent 的配置类（`KEY=VALUE;KEY=VALUE`）/ 调用方式 |

编排器只知道"给编码 Agent 传 `{"engine": "offline", "profile": "realistic"}`"这样的键值对，
不去解释它们的含义，那是各 Agent 自己配置类的事。所以给现有 Agent 加参数、或者塞一个新 Agent 进来，
配置层都不用改。

几个常用组合：

```bash
python -m workflow run --spec demo/spec_user_store.md --trace                 # 看实时轨迹
python -m workflow run --spec demo/spec_user_store.md --coding-profile clean  # 干净草案：第 1 轮就过
python -m workflow run --spec demo/spec_user_store.md --transport subprocess  # 子进程隔离调用
python -m workflow run --spec demo/spec_user_store.md --blocking low          # 更严格门禁（退出码 1）
python -m workflow plan --mermaid                                            # 贴进 GitHub/Typora
python -m workflow replay runs/<run-id>/events.jsonl                          # 回放（<run-id> 换成实际目录名）
```

`--coding-profile clean` 时第 1 轮阻断级 0 条、健康分 100，修复 Agent 一次都没被调用，只剩 4 条
info 级的 `CG307`（缺返回值类型注解）。`--transport subprocess` 同样跑得通，区别只是每次 Agent
调用多一次 `python -m <pkg>.worker` 的进程往返，大约 0.1s。

## 流程结构

`python -m workflow plan` 的输出，这棵树是 `describe()` 从代码结构递归出来的，不是手画的：

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

`plan --mermaid` 输出同一棵树的 Mermaid，完整输出 34 行，下面是前 10 行：

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

改流程结构，这两张图都会自动跟着变。分支节点的边只走带标签的那条（`|成立|` / `|不成立|`），
早期两条都画，图上同一个目标会同时出现无标签边和标签边，看起来像重影。

四种控制原语加一个函数叶子：

| 节点 | 契约 | 在流程里的用处 |
| --- | --- | --- |
| `SequenceNode` | 顺序执行子节点，`on_error=abort\|continue` | 整个 pipeline、每轮的 `round`、`recheck` |
| `BranchNode` | `condition(ctx)` 为真走 `then`，否则走 `else`（可以为空=跳过） | `fix-if-needed`、`final-recheck` |
| `LoopNode` | 最多 `max_rounds` 轮，`until(ctx)` 为真则提前退出，带循环游标 | `review-fix-loop` |
| `ParallelNode` | 并发执行无副作用的子节点，按定义顺序合并结果 | 单测覆盖；审查按文件 fan-out 是它的同构实现 |
| `FunctionalNode` | 用函数实现的叶子节点 | `skip-fix` 这类小步骤 |

每个节点都用 `Node.execute()` 这个模板方法：判定是否已完成（resume 的基础）→ 执行 → 成功就写
完成标记并存档 → 发一条 `node.end` 事件。放在基类里统一实现，没有让每个节点各写一遍，是因为
resume 的正确性依赖它不漏任何一处，漏一处就会出现"跳过了但状态没写"的静默错误。`RunContext.child()` 只改
`path` 和 `depth`，路径编码节点在树里的位置（例如 `2:review-fix-loop/#1/2:fix-if-needed/then:fix`），
它同时是事件里的定位符、`state.completed` 的完成标记、循环游标的键。

`ParallelNode` 的约束只有一条：并行子节点不得写共享状态。并发写共享状态要锁、要定序、失败还得
回滚，MVP 阶段不值得。所以子节点只返回结果，汇合后由并行节点按定义的顺序确定性合并。要是按完成
顺序合并，同样的输入会产出不同的报告，测试和 diff 就失去可比性了
（`test_parallel_merges_in_definition_order`）。

真实流程里它落在审查阶段按文件 fan-out 上（`ReviewStage._review_many`）：每个分片只读一份文件、
产出一份只读 payload，最后交给纯函数 `merge_reviews` 合并。`findings` 按 `finding_id` 去重后按
级别排序；`counts`、`blocking`、`total` 由合并后的清单重新算，不取分片之和，因为分片可能重复报
同一处；`score` 取最小值，`overall_risk` 取最坏的那个。fan-out 放在编排器这一层，审查 Agent 自己
不管并发，换来的是编排器能拿到每个文件各自的报告、任何 Agent 都能享受并行，代价是合并逻辑得自己写，所以
写成了纯函数，可以直接单测（`test_stages.py` 里 10 个用例）。

演示需求只生成 1 个文件，`shards=1`，跑 demo 的时候看不到真正的并发。这部分目前只有单测覆盖，
而且只测了 happy path 和一条失败上报。

## 代码结构与测试

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

分层是这样的：

```
接口层  cli.py run/plan/agents/replay/info
编排层  runner.py :: Orchestrator（装配→执行→收尾→写报告）· context.py :: RunContext（横切收口）
流程层  stages.py 7 个业务阶段 · nodes.py 4 种控制原语 + Functional 叶子
协作层  bus.py（消息）· state.py（状态+存档）· registry.py（Agent 发现与调用）
观测层  events.py（JSONL）· reporting.py（Markdown/HTML）· models.py · protocol.py
```

依赖方向是单向的：cli → runner → stages → nodes → context，context 再往下才碰 bus、state、events、
registry。`nodes.py` 不认识任何业务阶段，所以能脱离本项目复用；`stages.py` 是唯一知道业务的层；
`reporting.py` 只读不写，所以运行失败之后它照样能出报告。

`python run_tests.py` 跑 96 个用例，结尾是 `结果：96/96 通过，全部通过`（Python 3.14.7）；
`-v` 会列出用例名，也支持只跑 e2e，`pytest -q` 也可以。分布大概是这样：

- `test_runner.py` 17 个：主流程，收敛、分支跳过修复、无解时转人工、轮次上限、缺 Agent 快速失败、
  重试、存档与续跑、报告落盘，外加续跑保留可观测性和消息摘要的总数统计；
- `test_nodes.py` 19 个：四种原语的契约、resume 跳过语义（含从循环游标继续）、失败不标完成、
  分支条件只读可重复求值；
- `test_state.py` / `test_bus.py` / `test_models.py` 分别 14 / 13 / 9 个：点号路径与原子写不留
  tmp、版本不兼容提示；广播与定向投递（含回归用例）、订阅者异常隔离；门禁可解释性；
- `test_stages.py` 10 个：`merge_reviews` 的合并规则、多文件 fan-out 合并、流程结构契约；
- `test_contract.py` 4 个：四份 `protocol.py` 逐字节一致、契约版本一致、审查侧的可修清单与修复侧
  的策略注册表一一对应；
- `test_reporting.py` 6 个、`test_e2e.py` 4 个：Mermaid 和 ASCII 由流程树生成、分支边无重影、
  Markdown 必备小节、HTML 无外链；真实三 Agent 的两轮收敛、消息序列完整、clean 档位短路、产物齐全。

单元测试用伪 Agent（`tests/support.py::FakeHandle`）驱动编排逻辑，e2e 用真 Agent。前面的好处是
"编排逻辑对不对"不会被隔壁仓库今天的改动牵进来；后面的在找不到 Agent 时会明确跳过并打印原因
（"跳过：未找齐三个 Agent，请把四个仓库放在同一目录下"），不计为失败。`test_contract.py` 同样
允许跳过，但只要四个仓库在一起，它就会真的去比对四份 `protocol.py` 和两份可修复性词汇表。

## Agent 之间怎么对话

两套机制并存。消息总线（`bus.py`）回答"发生了什么"，主题有 `code.ready`、`review.completed`、
`gate.evaluated`、`fix.applied`、`pipeline.done`，发布者不知道谁在听，订阅者自己声明关心什么。
共享状态（`state.py`）回答"现在是什么样"，装着需求文本、代码清单、每轮发现、门禁结论、残余问题
和轮次轨迹，是单一事实来源，也负责存档。

如果只有共享状态，Agent 之间的耦合就是隐式的：修复 Agent 读到什么，取决于别人在什么时候写了什么，
光看代码看不出来。具体到实现，`FixStage` 不接收审查结果这个参数，它通过 `Inbox` 订阅
`review.completed`，再从 `message.body["findings"]` 里取清单（`runner.py` 启动时把 `Inbox` 注入
流程树）。于是加一个 Agent 就变成多一个订阅者：将来加一个"修复结果归档 Agent"，订阅
`fix.applied` 就够了，`FixStage` 一行都不用改。

总线本身还有几个决定。投递是同步的、没有队列，顺序流程里异步会引入"消息在被写之前就被读"的时序
问题，消息全量留在 `history` 里，将来真要换异步队列只需要替换投递实现。广播和定向分开处理，定向
消息只投给订阅了广播或订阅了自己的接收者，`test_directed_subscriber_receives_broadcast` 守着
这个判断，它以前写反过。某个订阅者抛异常也不会拖垮发布方，记一条 `error` 事件之后继续投递。

## 存档、断点续跑与事件日志

`state.json` 是一份扁平 JSON，业务状态和执行进度装在同一个文件里：

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

一次典型运行的 `completed` 有 18 个节点，`cursors` 记着循环跑到第 2 轮。不用 pickle 是因为这份
文件要能读、能 diff、能手工改一行再续跑，而 pickle 读数据的同时就在执行代码。业务状态和执行进度
放同一个文件，是因为续跑要求两者一致，分成两个文件就一定存在"进度说跑完了、业务数据还没落盘"
的窗口。存档是原子写：先写一个带 `pid.threadid.序号` 后缀的临时文件，再 `replace()` 覆盖，外面
套 `RLock`，这样并行节点同时触发存档也写不坏。固定临时文件名曾经在 Windows 上直接报
`Permission denied`。

`--resume` 的语义：

* 跳过 `completed` 里已有的节点。`Node.execute()` 逐层判定，命中就发一条 `node.skip` 事件。运行
  目录以存档所在的目录为准，不是新 run-id 的目录，轮次、残余问题、开放问题都从存档恢复，所以续跑
  的结论和第一次跑完一致。
* 不重放已跳过节点的副作用。`resume` 不是 `rewind`，已经写进 `worktree` 的修复不会被撤销，也不会
  被重新执行，只是不再跑一遍。想回到干净状态就重新 `run`，新 run-id、新工作副本。
* 可观测性一起恢复。`EventLog` 重绑到存档所在目录并改为追加（序号接着排），总线消息和阶段记录从
  存档的 `meta` 里重建，所以续跑后的报告仍然有完整的消息表和阶段耗时表。

这里踩过几个坑，都已经修了。早期续跑会把 `events.jsonl` 写进另一个新建的空运行目录，因为
`EventLog` 在 `__init__` 里就按新 run-id 绑了路径、还顺手建了目录，原目录的轨迹就变成了另一份
文件。另一处是总线消息和阶段记录当时不在存档里，续跑后报告里"Agent 消息传递""阶段耗时"两节直接
空了。修法是 `EventLog(path, append=True)`（首次写入才建文件，序号接已有最大值）、
`MessageBus.restore()`，外加 `checkpoint()` 把 `stages`/`messages` 写进存档、`resume()` 重绑
事件日志。实测 `events.jsonl` 从 60 行变成 64 行，只有一份文件，运行目录数不变，报告仍是 7 条
消息和 8 行阶段记录（`test_resume_preserves_observability`）。

事件流写的是 JSON Lines，一行一条 JSON：不用解析器就能被 `jq` 或 pandas 直接读，可以流式追加，
崩溃前已经落盘的行还在，同一份数据既能给人看也能喂给报告生成器。一次运行 60 条事件，`replay`
统计出来的分布是 `agent.call×4、agent.result×4、bus.message×7、gate.evaluated×2、node.branch×3、
node.end×18、run.end×1、run.start×1、state.checkpoint×20`。单条事件长这样，`data` 里是可以断言
的结构化字段，`message` 是给人看的说明：

```json
{"seq": 14, "ts": "2026-10-21T21:20:57.373+00:00", "kind": "gate.evaluated", "level": "info", "round": 1,
 "node": "2:review-fix-loop/#1/1:gate", "message": "门禁：未通过（阻断 3 条，可修 3 条）",
 "data": {"passed": false, "blocking": 3, "fixable_blocking": 3}}
```

消费方式：`--trace` 边跑边打印，按节点深度缩进，输出到 stderr；`python -m workflow replay
runs/<run-id>/events.jsonl` 事后逐条复现；`--filter gate.evaluated` 或 `--json` 只看某一类事件，
或者干脆交给程序处理。

## 质量门禁与报告产物

`GateStage` 把审查结果翻译成一个每个字段都能解释的判定，第 1 轮跑出来是这样：

```json
{"passed": false, "blocking": ["critical","high"], "blocking_count": 3, "total": 15,
 "counts": {"critical":1,"high":2,"medium":3,"low":5,"info":4}, "fixable": 7, "needs_human": 8,
 "score": 38, "overall_risk": "critical", "can_improve": true, "round": 1,
 "reason": "存在 3 条阻断级问题，其中 3 条可自动修复"}
```

阻断级别可配置，`--blocking` 可以传多次（默认 `critical`+`high`）。编排器启动时会把这个值强制
写进审查 Agent 的配置，免得出现审查说不合格、编排器说合格的口径打架。`can_improve` 存在的理由
是让循环能提前停下：退出条件有两个，门禁通过，或者没得修了。如果只认前一个，碰到"阻断级问题全都
不可自动修"（比如函数过长 `CG301`）会白跑满 `max_rounds` 轮，每轮重复同样的审查和跳过。有了
`can_improve=false`，第一轮结束就停，结论直接给 `needs_human`（`test_loop_stops_when_nothing_fixable`
守着这条）。想看这种情形，跑 `python -m workflow run --spec demo/spec_user_store.md --blocking low`，
退出码是 1。

产物目录：

```
runs/run-<时间戳>-<4位>/
├── report.md      人读报告（概要/门禁/流程图/收敛曲线/消息/阶段/残余/最终代码/产物清单 9 节）
├── report.html    单文件可视化报告，零外部依赖，可离线双击打开 · run.json 机器可读（CI 断言用）
├── events.jsonl   JSONL 事件流（60 条），可 replay        ├── state.json  存档：状态+完成标记+游标
├── reports/       coding/（响应+草案副本）· review-r1/ · review-r2/ · fix-r1/（fix.json+fix.diff+summary.md）
└── worktree/      最终代码 user_store.py + draft_manifest.json
```

| 产物 | 给谁看 | 里面有什么 |
| --- | --- | --- |
| `report.md` | 人，也进版本库 | 结论、门禁明细、收敛曲线、消息传递记录、最终代码全文；消息摘要形如 `命中 15 条（critical 1、high 2、medium 3、low 5、info 4），阻断 3` |
| `report.html` / `run.json` | 演示 / 程序 | 内联 CSS 画的条形图与流程图，`prefers-color-scheme: dark` 自动深色 / `outcome`、`rounds[]`、`stages[]`、`gate`、`messages[]` |
| `events.jsonl` / `state.json` / `reports/fix-r1/` | 复盘 / resume / 人和程序 | 事件流 / 存档 / `fix.diff`（标准 unified diff）加 `fix.json`（逐条 `PatchRecord` 带理由） |

报告这边有几条约定：机器可读数据和渲染分离，`report.md`、`report.html` 只是 `run.json` 的视图，
改样式不影响自动化断言；报告写失败也要照常返回主结果，`_finalize_reports` 包在 try 里，失败只记
一条事件。

## 四个仓库、设计取舍与常见问题

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

`codeguard`（Homework 1）是审查 Agent 的核心，27 条规则的引擎，地址
<https://github.com/0x697551b0/codeguard>；`coding_agent` 把需求翻成代码草案，有离线模板和 LLM
两个引擎，<https://github.com/0x697551b0/hw2-coding-agent>；`review_agent` 复用 codeguard，产出
`FindingContract[]` 和 `fixable` 标记，<https://github.com/0x697551b0/hw2-review-agent>；
`fix_agent` 有 9 条修补策略和三道自检，<https://github.com/0x697551b0/hw2-fix-agent>；`workflow`
就是本仓库，负责流程控制、Agent 通信、状态与存档、报告，
<https://github.com/0x697551b0/hw2-code-review-workflow>。四个仓库各自独立可跑（各有
`run_tests.py`），合在一起才是一条完整流水线。

几个主要的取舍。流程用可组合的节点树来表达，不写死 `if/for`：流程图能从结构生成，resume 标记有
天然层级，加节点不用改既有代码。Agent 之间只共享 JSON 信封，不 import 对方内部类型：一方换实现
（哪怕换成别的语言写的进程）另一方不用改。传输默认进程内，可选子进程，前者快且好调试，后者隔离
性好，Agent 崩了不带走编排器。存档用扁平 JSON。失败默认 `abort`，也可以配成 `continue`。
并行只用在无副作用的地方。不做 GUI，机器可读数据和渲染分离。

问：一定要 API Key 吗？`agents` 说找不到 Agent 怎么办？不需要 Key。三个 Agent 的默认路径都是
离线的确定性实现，编码用模板、审查用 codeguard 规则、修复用规则级修补器，只有
`--coding-engine llm`、`--review-mode llm`、`--fix-engine llm` 这三种要 Key。找不到 Agent 就把
四个仓库放到同一目录，或者用 `--agents-home <目录>` / `WF_AGENTS_HOME`；注意它是追加到候选搜索
列表、不是唯一来源，就算传错了，向上搜索仍可能找到别处的同名包。

问：`--trace` 在 PowerShell 里满屏红字？报告里消息表的"命中 N 条"以前一直显示 0？前者是 stderr
被 PowerShell 包成了 `NativeCommandError`，命令本身是成功的（退出码 0），用 `2>$null` 丢掉或者
改用 `--json`。后者是个已修的 bug：早期 `_message_digest` 直接取 `body["total"]`，而
`review.completed` 的消息体里没有这个字段，于是总数恒为 0，分布和阻断数都是对的，这种"部分正确"
最难发现。现在 `ReviewStage` 会带上 `total`，`_message_digest` 也加了"缺 `total` 时由 `counts`
求和"的兜底（`test_message_digest_counts_total_from_counts`）。

问：跑完不满意，能只重跑某一步吗？能审查非 Python 项目吗？`resume` 只跳过已完成的节点，不会
重做某一节点，要么整体重跑（新 run-id），要么手工改 `state.json` 的 `completed`。编排器本身与
语言无关（`--pattern` 可配），但审查 Agent 的规则引擎是基于 `ast` 的 Python 专用实现，修复 Agent
也只改 `.py`，换成别的语言会退化成"没有可用规则"。

本项目为课程作业，采用 MIT 许可。详细设计见 [`Design.md`](Design.md)。
