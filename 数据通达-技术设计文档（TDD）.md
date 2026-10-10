# 数据通达 — 技术设计文档（TDD）

| 项目名称 | 数据通达（Data-Agent） |
|---|---|
| 文档类型 | 技术设计文档（TDD） |
| 文档版本 | V3.0 |
| 编写日期 | 2026-08-18 |
| 对应需求 | 《数据通达-需求规格说明书（PRD）》V3.0 |

---

## 修订历史

| 版本 | 日期 | 修订说明 |
|---|---|---|
| V1.0 | — | 单轮 NL2SQL 架构 |
| V2.0 | — | 认证、流式、统一异常 |
| V3.0 | 2026-08-18 | 架构重构：多轮对话、混合检索、可解释性、评测体系、数据权限治理 |

---

## 1. 架构总览

### 1.1 分层架构

依赖方向自上而下，上层依赖下层，下层不感知上层。

```
┌──────────────────────────────────────────────────────────────┐
│                    接入层（前端 Vue 3 + TypeScript）            │
│    Vue 3 + Pinia + Vue Router + Element Plus + ECharts         │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTP / SSE（JSON）
┌──────────────────────────▼───────────────────────────────────┐
│                     接口层（FastAPI）                           │
│   认证 / 会话 / 问答 / 历史 / 管理 / 权限 / 评测 / 健康探针        │
│   统一响应 + 统一异常 + CORS + request_id 追踪                   │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                 智能体层（LangGraph，带 checkpointer）           │
│   抽取 → 混合检索 → 合并过滤 → 生成 → 校验 → 校正 → 执行 → 解释 → 可视化推荐 │
│   SQL 白名单安全治理 + 数据权限注入                              │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                      存储层                                    │
│   MySQL（meta+dw）│ Qdrant（向量）│ ES（全文）│ Redis（会话/缓存） │
└──────────────────────────────────────────────────────────────┘

            离线：知识构建层 + 评测层 + 可观测层（OTel/Langfuse）
```

### 1.2 依赖方向原则

- 接口层依赖智能体层与存储层，智能体层依赖存储层。
- 业务通过 Repository 访问存储，通过 ClientManager 管理连接生命周期。
- State 与 Context 用 TypedDict 定义，贯穿 Agent 全流程。

---

## 2. 技术选型

| 类别 | 技术 | 版本 | 选型理由 |
|---|---|---|---|
| 语言 | Python | ≥3.12 | 新式泛型语法 |
| Web 框架 | FastAPI | ≥0.128 | 原生异步、自动 OpenAPI、SSE 友好 |
| Agent 编排 | LangGraph | ≥1.0 | 图式编排、checkpointer、stream 模式 |
| LLM 接入 | langchain-openai | ≥1.0 | ChatOpenAI 直连 OpenAI 兼容端点（DeepSeek/Ollama），自带超时、重试与流式；只装 langchain-core 这一支，重量级的 langchain（chains/agents）本方案用不到 |
| SQL 解析 | sqlparse | ≥0.5 | 按 token 类型判只读；正则挡不住 `WITH x AS (...) DELETE` 这类以 SELECT 类关键字开头、实则写入的语句（见 §13） |
| 模型 | DeepSeek/Qwen/GLM（可插拔） | OpenAI 兼容 | 中文友好、成本可控 |
| 嵌入模型 | bge-m3（独立服务） | — | 多语言、语义强 |
| ORM | SQLAlchemy 2.0（async）+ Alembic | ≥2.0 | 异步 + 数据库迁移 |
| MySQL 驱动 | asyncmy | ≥0.2 | 异步驱动 |
| 向量库 | Qdrant | ≥1.16 | 独立部署、相似度检索（开发可用 qdrant-client 嵌入式模式，见 §11） |
| 全文检索 | Elasticsearch | 8.x | ik 中文分词（索引期 ik_max_word / 检索期 ik_smart） |
| 缓存/会话 | Redis | ≥5.0 | checkpointer + 缓存 |
| 配置 | pydantic-settings | ≥2.0 | 类型安全、环境变量注入 |
| 认证 | PyJWT + bcrypt | — | 标准 JWT + 哈希（直连 bcrypt，不经 passlib，见 §13 说明） |
| 日志 | loguru | ≥0.7 | 结构化、可 patch request_id |
| 可观测 | OpenTelemetry + Langfuse | — | 链路追踪 + LLM 追踪 |
| 测试 | pytest | — | 单测 + 评测 |
| 前端 | Vue 3 + TypeScript + Pinia + Vue Router + Element Plus + ECharts | — | 组合式 API、企业级组件 |

---

## 3. 目录结构

```
data-agent/                        # 后端
├── main.py                        # 入口
├── pyproject.toml                 # 依赖
├── alembic/                       # 数据库迁移
├── conf/
│   ├── app_config.yaml            # 基础配置
│   └── meta_config.yaml           # 元数据知识库定义
├── prompts/                       # 提示词文件
├── app/
│   ├── api/                       # 接口层
│   │   ├── middleware.py
│   │   ├── dependencies.py
│   │   └── v1/{auth,conversation,query,history,admin,permission,eval}.py
│   ├── agent/                     # 智能体层
│   │   ├── graph.py               # 图编排（带 checkpointer）
│   │   ├── checkpoint.py          # checkpointer 选型与降级（阶段 7）
│   │   ├── redis_checkpointer.py  # 自研 Redis 版 BaseCheckpointSaver（阶段 7）
│   │   ├── state.py / context.py
│   │   ├── events.py              # SSE 事件构造与节点进度上报
│   │   ├── llm.py                 # LLM 实例
│   │   ├── prompts.py             # 提示词装载与占位符渲染
│   │   ├── parsing.py             # 模型 JSON 输出的容错解析
│   │   ├── knowledge.py           # 表/字段/指标口径获取
│   │   ├── rendering.py           # 上下文拼装（表结构/指标/日期/库环境/结果摘要）
│   │   ├── visualization.py       # 图表类型推荐规则（纯函数，阶段 8）
│   │   ├── permissions.py         # 数据权限：范围计算与裁剪（阶段 9）
│   │   └── nodes/*.py             # 各节点
│   ├── retrievers/                # 检索层：dense/fulltext/fusion/candidate + retrieval 网关
│   ├── clients/                   # 连接管理器
│   ├── repositories/              # 数据访问层（meta 注入式会话；dw 自开只读连接）
│   ├── entities/                  # 领域实体
│   ├── models/                    # ORM 模型
│   ├── schemas/                   # 请求/响应
│   ├── services/                  # 服务层
│   ├── core/                      # 配置/安全/异常/日志/响应（含 sql_security.py）
│   │   ├── log_setup.py           # loguru 出口 + 接管标准库 logging（阶段 11）
│   │   ├── request_context.py     # request_id 的 ContextVar 与请求头白名单（阶段 11）
│   │   └── tracing.py             # OTel provider、节点/LLM 埋点、Langfuse（阶段 11）
│   └── scripts/                   # 离线脚本
├── evals/                         # 评测集与评测脚本
└── tests/                         # 单元测试
data-agent-frontend/               # 前端
├── vite.config.ts                 # 代理 /api 与 /health；固定端口 5173；构建分块
└── src/
    ├── api/                       # http（信封拆解/令牌/续期）、query（SSE）、
    │                              # types（后端契约的前端副本）、各业务模块
    ├── stores/                    # Pinia：auth（登录态与角色）
    ├── router/                    # 路由与守卫（登录校验 + meta.requiresRole）
    ├── layouts/                   # DefaultLayout：侧栏 + 顶栏 + 内容区
    ├── components/                # EmptyState / LoadingBlock / EChart /
    │                              # SqlBlock / ResultViewer（表格·图表·导出）
    ├── views/                     # 登录 / 问答 / 历史 / 看板 / 个人设置 /
    │                              # 用户管理 / 权限管理 / 评测看板 / 403 / 404
    ├── utils/                     # storage / role / labels / csv / chart / echarts
    └── styles/                    # 设计 Token
```

---

## 4. 模块设计

| 模块 | 职责 |
|---|---|
| core.config | pydantic-settings 分层配置 |
| core.security | JWT、bcrypt、角色依赖 |
| core.response / exceptions | 统一响应与错误码 |
| agent.graph | 节点与边定义，编译带 checkpointer 的图（阶段 5 起） |
| agent.nodes.* | 单一职责节点 |
| agent.events / rendering / parsing / prompts | 事件构造、上下文拼装、模型输出解析、提示词装载 |
| core.sql_security | SQL 只读校验（白名单解析 + 关键词黑名单） |
| core.request_context / log_setup | request_id 的生成与携带；日志出口与标准库 logging 的接管（阶段 11） |
| core.tracing | 链路追踪：OTel provider、节点/LLM/汇总埋点、Langfuse 处理器与内容闸门（阶段 11） |
| repositories.dw | 数仓只读执行与三重限制 |
| retrievers.dense / fulltext | 单路检索（Qdrant 向量 / ES 全文） |
| retrievers.fusion | RRF 融合（纯函数，无 IO） |
| retrievers.retrieval | 检索网关：并发两路 → 融合 → 精排 → 候选（节点只依赖它） |
| clients.* | 外部连接生命周期 |
| repositories.* | 存储访问封装 |
| services.* | 业务编排 |

### 分层约束

- 节点签名统一 `async def node(state, runtime)`，依赖经 `runtime.context`，流式经 `runtime.stream_writer`。
- 节点间通过 State 传数据，禁止直接 import 其它节点。
- Repository 只存取，不含 LLM 逻辑；LLM 逻辑集中在节点与提示词。

---

## 5. 核心流程

### 5.1 Agent 编排图

```
START
  │
  ▼
resolve_reference        （指代消解："那/换成/再/对比" → 补全为完整问题）
  │
  ▼
extract_keywords ────────┬──────────────────────────────┐
  │                      │                              │
  ▼                      ▼                              ▼
recall_column        recall_value                  recall_metric
  │                      │                              │
  └──────────────────────┴──────────────────────────────┘
                         ▼
              merge_retrieved_info
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
         filter_table          filter_metric
              │                     │
              └──────────┬──────────┘
                         ▼
                add_extra_context      （注入日期/数据库/会话历史/数据权限）
                         │
                         ▼
                  generate_sql
                         │
                         ▼
                  validate_sql
                         │
              ┌──────────┴─────────────────────┐
              │ error == None                  │ error != None
              ▼                                ▼
       execute_sql                    correction_count < 3 ?
              │                          ├─ 是 ─► correct_sql ─► validate_sql（count + 1）
              │                          └─ 否 ─► END（终止并返回明确错误）
              ▼
       generate_explain                     （生成自然语言解释）
              │
              ▼
       recommend_visualization              （按结果结构推荐图表类型）
              │
              ▼
             END
```

关键编排逻辑：

- resolve_reference 为图入口节点，先做指代消解再抽取关键字；首轮无会话历史时原样透传（对应 FR-CONV-004）。
- 三路召回并行，汇聚到 merge。
- filter_table 与 filter_metric 并行，汇聚到 add_extra_context。
- validate_sql 条件边分流：校验通过进 execute_sql；校验失败且 correction_count < 3 进 correct_sql、计数加 1 后回边重校；correction_count ≥ 3 时终止图并返回明确错误（对应 BR-SQL-005、FR-QA-008）。
- 新增 generate_explain 节点，在执行成功后生成解释；随后 recommend_visualization 按结果结构推荐图表类型。

**State 关键字段（TypedDict）**

| 字段 | 类型 | 说明 |
|---|---|---|
| query | str | 用户本轮原始问题 |
| resolved_query | str | 指代消解后的完整问题；resolve_reference 之后的节点统一消费该字段 |
| session_id | str \| None | 会话标识，首轮为空 |
| history | list | 会话历史（上文问题/SQL/过滤条件/授权范围），由 checkpointer 注入 |
| correction_count | int | SQL 校正次数，初值 0，上限 3（BR-SQL-005） |
| error | str \| None | validate_sql 写回，驱动条件边分流 |
| error_code | int \| None | 与 error 配套的业务码（12.1），供 SSE 的 error 事件使用 |
| explain_text | str \| None | generate_explain 写回；None 表示"这一轮没有解释"，不是"还没生成" |
| chart_type | str \| None | recommend_visualization 写回；仅用于推事件，**不落库**（见阶段 8 实现范围）|

> **阶段 5 的实现范围**：图上只做 `resolve_reference` 之后、`generate_explain` 之前的
> 那一段（十二个节点），不存在 checkpointer 与 resolve_reference（阶段 7 引入），
> 也没有 generate_explain / recommend_visualization（阶段 8 引入）。入口把
> `resolved_query` 直接填成 `query`，而**所有下游节点一律只读 `resolved_query`**：
> 阶段 7 只需把"原样透传"换成"指代消解"，十二个节点一行都不用改；反过来若阶段 5
> 图省事让下游读 `query`，阶段 7 就得挨个改且极易漏掉某一处。
>
> **阶段 7 的实现范围**：补上入口的 `resolve_reference`（13 个节点齐了）与 Redis
> checkpointer，下游那十二个节点确实一行未改。一次性提问（不带 `session_id`）与
> 多轮走**两张不同的编译图**，理由见 §5.3.1。`generate_explain` 与
> `recommend_visualization` 仍留待阶段 8，所以 `message.explain_text` 与
> `query_history.explain_text` 现在如实写 NULL，不拿 EXPLAIN 语句充数。
>
> **阶段 8 的实现范围**：补上 `generate_explain` 与 `recommend_visualization`
> （15 个节点齐了），两者串行接在 `execute_sql` 之后；`execute_sql` 之前那十三个
> 节点一行未改。两处落点值得记明：
>
> - `explain` 是 **P1 且允许降级**——`execute_sql` 早已把结果推给前端，此刻模型抖动
>   不该判死整轮问答，因此 `generate_explain` 捕获 `LLMServiceError` 后写回
>   `explain_text=None`、不推事件，`done` 照常。它**不受** §5.3 中 `resolve_reference`
>   那类长度上限的约束：解释是链路终点（不回流进任何提示词），输出长度是用户可读性问题，
>   不是提示词放大问题。
> - `message.explain_text` 与 `query_history.explain_text` 自此真实写入，阶段 7 的
>   NULL 占位到此关闭。`chart_type` **不落库**：阶段 2 的 DDL 里没有对应列，而它完全
>   由结果结构决定，凭同一份 `result_preview` 可重算——存一份只会与结果脱节。
>
> **阶段 9 的实现范围**：不新增节点，数据权限**注入已有的四个点**——`filter_table`
> 与 `filter_metric` 在候选集进提示词之前按角色裁剪，`add_extra_context` 丢掉
> 引用了已无权限对象的历史轮次，`validate_sql` 在只读校验之后、EXPLAIN 之前做兜底
> 校验。前三个点让模型根本看不到未授权的名字，第四个点拦住"模型自己编的表名"与
> "从历史里带进来的旧表名"，缺一不可。
>
> 判定核心（`agent/permissions.py` 的 `build_scope`）是**纯函数**，入参是简单元组，
> 因此"谁看得到哪张表的哪一列"能在不连库的机器上逐条验；网关
> `PermissionGateway` 只负责读库与把 ORM 行摊平成元组。范围在**实例内**按角色缓存，
> 而一个 `QueryContext` 对应一轮问答，所以缓存生命周期正好是这一轮：同一轮里三处
> 调用必须看到同一个范围（否则会出现"表被拦了、校验却放行"的中间态），而**跨请求
> 不缓存**，于是没有"改完配置要清缓存"这一步。
>
> 前一个阶段沉淀的两条纪律在这里继续用：`role` 由接口层从 `user.role` 显式传下来
> （`QueryService.stream` 的默认值只服务单测与评测干跑），权限作为安全边界不依赖
> "忘了传也不会报错"的默认值；越权被拒时复用 **40203**，让既有的 correct_sql
> 回边照常重写——model 手里有**裁剪过的**表结构，照着重写即可。

### 5.2 混合检索流程

```
关键字（jieba + LLM 扩展）
        │
        ├──→ Embedding 服务 → Qdrant 向量检索 ──┐
        │                                      ├──→ RRF 融合 → Rerank 精排 → 最终召回
        └──→ ES 全文检索（ik 分词）─────────────┘
```

#### 5.2.1 实现说明（阶段 6）

三个阶段各自独立成文件，节点只认最后的网关，不知道中间有几路：

| 文件 | 职责 |
| --- | --- |
| `retrievers/dense.py` | Qdrant 向量检索，每个关键词一次查询，同一实体取最高分 |
| `retrievers/fulltext.py` | ES `match` 全文检索（索引级 `ik_max_word`，查询级 `ik_smart`） |
| `retrievers/fusion.py` | RRF 融合，纯函数，无 IO |
| `retrievers/candidate.py` | 两路共用的候选结构，`identity() = (kind, id, name)` 是合并与去重的唯一依据 |
| `retrievers/retrieval.py` | 网关：并发跑两路 → 融合 → 截断 → 精排；节点只调这里 |

**为什么必须是 RRF 而不是加权求和。** 向量分是余弦相似度（0~1），BM25 分无上界且
随语料长度漂移，两路的分数量纲根本不可比，任何固定的加权系数都只是在某一个语料上
拟合出来的。RRF 只用名次：

```
score(d) = Σ_i  1 / (k + rank_i(d))        k = 60，rank 从 1 起
```

k=60 是刻意的：此时第 1 名与第 2 名只差 `1/61 - 1/62 ≈ 0.0003`，而另一路投出的一票
至少值 `1/61 ≈ 0.0164`——**任何两路共识的命中都必然压过任何单路命中**，无论单路里
它领先多少。这正是"融合"与"把两个列表拼起来"的区别，验收用例
（`test_a_consensus_hit_outranks_a_single_path_hit_on_real_indexes`）钉的就是这条。

**两路看到的是同一段文本。** 建库时把拼好的文本同时写进 Qdrant 的 `text` payload 与
ES 的 `text` 字段，doc id 两处都用 MySQL 的 uuid 主键。于是：合并靠 id 直接进行，
不需要映射表；精排的文档直接取这段原文，不必在检索侧重新拼一遍——否则"排的依据"
和"召的依据"会悄悄分叉，名次对不上且无从解释。

**精排是可选的，缺了不阻断。** 重排服务是独立进程（ADR-005），本环境未必部署。
未配置或调用失败时降级为 RRF 顺序，每个进程只打一条 WARNING（避免每问一次日志刷屏），
恢复成功后标志位复位、下次故障会再提醒一次。降级而不是报错，是因为精排只影响
排序质量、不影响是否有结果；把"没有重排服务"变成查询失败，代价与收益不成比例。

**两种重排协议在客户端归一化**（`clients/rerank.py`），上层不必关心用的是谁：

| 服务 | 响应形状 |
| --- | --- |
| TEI / Xinference / vLLM | `[{"index": 0, "score": 0.9}, ...]` |
| SiliconFlow / Jina | `{"results": [{"index": 0, "relevance_score": 0.9}]}` |

请求体统一为 `{"model", "query", "documents", "top_n"}`。4xx 立即失败（重试不会变好），
5xx 与网络错误重试 2 次后再失败。

**已知局限与归属**：向量那一路至今仍把"销售额"排给 `sales_quantity` 而非 `GMV`
（§8.3 记录的近同义词问题），融合后 GMV 回到第一——这是本阶段唯一可量化的质量提升，
由 `test_the_fulltext_path_fixes_the_near_synonym_case` 钉住。

**真实重排服务的部署形态**：本机用 llama.cpp 的 `llama-server --rerank` 加载
`bge-reranker-v2-m3` 的 Q8_0 GGUF（606MB），监听 127.0.0.1:8002（即 `.env` 里
`RERANK__HOST` 的默认值，故本机无需改配置），启动脚本 `infra/start-rerank.cmd`。
llama.cpp 的响应形状是 `{"results":[{"index","relevance_score"}]}`，与客户端
`_parse` 天然吻合。**它返回的是原始 logit，不在 0~1 之间、可能为负**——下游只用它
排序、不与阈值比较，因此安全；但任何新增的"按分数过滤"都必须先意识到这一点。
另有一条实测结论：线程数不同会改变分数（12 线程 0.3796 / 单线程 0.3991，同一输入），
属浮点归约顺序差异，同一实例内可复现，不影响判序。

桩服务能证明的是"网关按服务的分数重排、且送进去的是库里的原文"；模型**排得对**
由 `@pytest.mark.requires_rerank` 的用例对着真模型断言（没起服务时跳过）。
更全面的精排质量仍由阶段 10 的评测集度量。


### 5.3 多轮会话流程

```
用户问题（带 session_id）
        │
        ▼
  加载会话历史（Redis checkpointer）
        │
        ▼
  指代消解 resolve_reference：解析"那/换成/再/对比" → resolved_query
        │
        ▼
  执行 Agent 图（add_extra_context 注入上文问题/SQL/过滤条件与数据权限）
        │
        ▼
  生成新结果 → 写回会话状态（message）→ 落 query_history
```

#### 5.3.1 实现说明（阶段 7）

| 文件 | 职责 |
|---|---|
| `agent/nodes/resolve_reference.py` | 入口节点：读 `history` 把本轮问题补成完整问句 |
| `prompts/resolve_reference.prompt` | 改写提示词，**输出纯文本**（改写后的问句），不要 JSON、不要解释 |
| `agent/checkpoint.py` | checkpointer 选型（Redis / 内存降级）、`release_thread` |
| `agent/redis_checkpointer.py` | 自研 Redis 版 `BaseCheckpointSaver`（ADR-006） |
| `repositories/conversation.py` / `query_history.py` | 会话、消息、历史三张表的读写 |
| `repositories/ids.py` | 时间有序主键，会话消息与查询历史共用 |
| `services/conversation_service.py` / `history_service.py` | 归属校验与编排 |

**改写为什么单独占一个节点**：它的产出被三路召回、两个筛选、生成 SQL、校正六处
消费。藏在 `extract_keywords` 里的话，那六处都得自己判断"手里的是原文还是改写后的"。
放在入口，这件事只发生一次、只有一个出口，"这一轮改写了什么"在日志里是一条独立、
可对照的记录。**首轮由代码判断历史为空并原样透传**，一次模型调用都不发：没有历史
时任何改写都是凭空发挥，让模型自己"决定要不要改写"，它偶尔会把一个本来完整的
问题改坏（补出一个历史里根本没有的年份）。

**为什么编译两张图**。langgraph 规定挂了 checkpointer 的图必须有 `thread_id`。
一次性提问没有会话 id，硬凑一个的话，每问一句都会在 Redis 里留下一个永不回收的
线程（会话 id 是 uuid4，不会有第二个会话复用它），而 `checkpointer=None` 的图又
不能带 `config`。所以 `agent.graph.get_graph(persist=...)` 按需编译并缓存两张。

**开流前必须做完的事**。SSE 的响应头在第一个事件推出去时就发出去了（
`StreamingResponse` 先发 `http.response.start` 再拉第一块内容），此后 HTTP 状态码
已经定型。"会话不存在""这个会话不归你"若发生在流里，就只能表现为**连接被掐断**——
没有状态码、没有原因，而 §7.1 要求它们是一个正常的 404 信封（§12.2 的"统一异常
转 JSON"在这里落不了地，因为那时响应头已经发出去了）。所以归属校验、取上文、
选 checkpointer 全部挪进 `QueryService.prepare()`，在开流之前做完；流一旦开始，
所有失败都只能变成 `type=error` 事件。落地路径就是 `GET`/`DELETE` 会话与历史一律
**越权按"不存在"返回 404 而非 403**——403 等于承认这个 id 存在、只是不归你。

**一轮一行、role=assistant**。问题与它自己的答案写在同一行里，拆成 user / assistant
两行看起来更规范，但 MySQL 的 `DATETIME` **精度只到秒**，同一秒内写入的两行
`created_at` 完全相同，按它排序得到的次序是数据库说不上来的；顺序一旦反过来，
上文就会把上一轮的问题配到这一轮的 SQL 上。所以：(1) 一轮只落一行；
(2) 主键用"13 位毫秒时间戳 + 16 位随机"自带顺序（`repositories/ids.py`），
排序用主键而不是 `created_at`，它保证跨毫秒严格按时间先后，同一毫秒内次序稳定
（无需保证真实先后：真实链路上两轮相隔的是一次完整问答，量级是秒）。
消息行、会话 `updated_at`、查询历史行**一次提交**写出，分开提交只会得到
"会话详情里看得到这一轮、历史列表里却少一条"这种半份记录。落库失败只记日志
（NFR-STAB-003）：用户已经看到答案了，此刻抛异常只会让他看到完整答案之后连接被掐断。

**持久化与清理**。图状态以 `session_id` 作 `thread_id` 存在 Redis（ADR-006），
删会话时调 `checkpoint.release_thread` 丢弃快照，否则 Redis 里的线程只增不减。
Redis 不可用时降级为进程内 `InMemorySaver`：单进程内语义相同，丢的是跨重启的
图状态；用户看到的多轮对话仍然正常，因为上文是从 MySQL 的 `message` 表读的。
**降级在首次选型时定下就不再翻转**（checkpointer 是编译图时烘进图里的，
中途换一个就得重编译图，而"正在跑的请求"与"新编译的图"会落在两个不同的 state 上）。

### 5.4 端到端时序

```
用户 → 前端(SSE) → FastAPI /query
                     │  校验 JWT
                     │  加载会话（Redis）
                     │  构建 Context + State
                     ▼
              graph.astream(...)  ── 逐节点流式
                     │  resolve → extract → 混合检索 → merge → filter → context
                     │  generate → validate → (correct) → execute → explain → visualize
                     ▼
              SQL/结果/解释/图表事件 → 前端渲染
                     │
                     ▼
              异步落 query_history + 会话状态（失败不影响主流程）
```

---

## 6. 提示词设计

提示词独立存放 `prompts/*.prompt`，支持版本管理。

| 提示词 | 用途 | 输出 |
|---|---|---|
| resolve_reference | 指代消解/问题改写（结合会话历史补全指代） | 纯文本（改写后的完整问题） |
| extend_keywords_for_column_recall | 字段召回扩展 | JSON 数组 |
| extend_keywords_for_metric_recall | 指标召回扩展 | JSON 数组 |
| extend_keywords_for_value_recall | 取值召回扩展 | JSON 数组 |
| filter_table_info | 裁剪表字段 | JSON 对象 |
| filter_metric_info | 筛选指标 | JSON 数组 |
| generate_sql | 生成 SQL | 纯文本 SQL |
| correct_sql | 修正 SQL | 纯文本 SQL |
| generate_explain | 生成解释 | 纯文本 |
| recommend_visualization | 图表类型推荐 | JSON 对象 —— **阶段 8 未按提示词实现，见下** |

### 核心提示词示例（generate_sql）

```
【角色】
你是一个资深的数据库专家和数据分析师。你的任务是根据提供的【上下文信息】，
将用户的自然语言查询转换为语法正确、性能优化的 SQL 语句。

【上下文信息】
可用数据表信息如下：{table_infos}
可参考的指标信息如下：{metric_infos}
当前的时间信息如下：{date_info}
数据库环境如下：{db_info}

【任务要求】
1. 仅允许使用数据表信息中真实存在的表与字段名称，禁止编造、猜测或引入未提供的表和字段。
2. 若指标信息中存在相关指标定义，必须严格遵循其业务口径、计算逻辑、过滤规则与时间口径。
3. 生成的SQL只能用于查询，不能涉及数据写入、更新、删除等操作。
4. 生成的SQL的语法必须严格符合数据库环境中指定的数据库类型与版本。
5. 默认只生成一条SQL，不可生成多条SQL。
6. 输出必须仅包含一条完整 SQL 语句的纯文本，严禁使用 Markdown 代码块或任何格式化符号。

用户查询如下：{query}
输出：
```

### 提示词设计原则

- 输出约束严格：SQL 纯文本、过滤纯 JSON，便于程序化解析。
- 防注入：明确"仅使用提供的表/字段"，降低幻觉与越权。
- 指标口径锁定：要求"严格遵循指标定义，不得偏离"。

**占位符约定**（阶段 5 起）：模板里的变量写成 `{name}`（小写标识符）。渲染器按这一
形态逐个替换，因此 JSON 结构示例里的花括号（`{"tables": [...]}`）原样保留——
这也是渲染不走 `str.format` 的原因：format 会把示例里的花括号当成未转义占位符直接报错，
而要求作者写成 `{{` `}}` 又极易被改坏。

**`recommend_visualization` 改用规则实现，不走提示词**（阶段 8 的有意偏离）：
本表原先列了这份提示词，实现时改为 `app/agent/visualization.py` 里的纯函数。三条理由——
(a) FR-VIZ-001 的验收标准本身就是一张规则表，"推荐符合规则"只有把规则写进代码才验得了，
交给模型则同一份结果今天推柱状、明天推折线；(b) "按结果结构推荐"是结构化查表，
不需要语义理解，模型在这里只增加延迟、成本与不确定性；(c) 它因此是编排图上唯一
**不会失败**的节点，无需降级路径。规则表见 README「可解释性与可视化推荐（阶段 8）」。
若日后要按业务语义调整推荐（例如"看构成"改成"看趋势"），那是产品决策，届时再评估
是否值得引入模型——现在的规则是可解释、可回归的。

**相对时间由代码算，不交给模型**：`date_info` 是一张由代码生成的换算表
（今天 / 昨天 / 近 7 天 / 本月 / 上月 / 本季度 / 上季度 / 今年 / 去年…），
模型只需挑一行照抄。日期算术（跨月、闰年、季度边界）交给模型既容易错又没法测，
放进上下文等于把不确定性从"推理"挪到"查表"。数据表的时间字段形状不写死：
既可能是 `yyyymmdd` 的整数主键，也可能是独立的年 / 季 / 月整数列，用哪一列由表结构决定。

---

## 7. 接口设计

### 7.1 统一响应结构

```json
{ "code": 0, "message": "ok", "data": {} }
```

分页结构：`{ "items": [], "total": 0, "page": 1, "page_size": 20 }`。

### 7.2 REST 端点清单

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | /api/v1/auth/login | 无 | 登录 |
| POST | /api/v1/auth/refresh | 无 | 刷新令牌 |
| GET | /api/v1/auth/me | Bearer | 当前用户 |
| PUT | /api/v1/auth/password | Bearer | 改密码 |
| POST | /api/v1/conversations | Bearer | 创建会话 |
| GET | /api/v1/conversations | Bearer | 会话列表 |
| GET | /api/v1/conversations/{id} | Bearer | 会话详情（含消息列表，仅本人） |
| DELETE | /api/v1/conversations/{id} | Bearer | 删除会话 |
| POST | /api/v1/query | Bearer | 问答（SSE，含 session_id） |
| GET | /api/v1/history | Bearer | 历史列表 |
| GET | /api/v1/history/{id} | Bearer | 历史详情 |
| DELETE | /api/v1/history/{id} | Bearer | 删除历史 |
| GET | /api/v1/admin/users | admin | 用户列表 |
| POST | /api/v1/admin/users | admin | 创建用户 |
| PUT | /api/v1/admin/users/{id}/role | admin | 改角色 |
| PUT | /api/v1/admin/users/{id}/status | admin | 启停 |
| GET | /api/v1/permissions | admin | 权限映射列表（role 可筛选） |
| POST | /api/v1/permissions | admin | 新增一条角色-资源映射 |
| PUT | /api/v1/permissions/{id} | admin | 整条替换（三个字段都必填） |
| DELETE | /api/v1/permissions/{id} | admin | 删除一条映射 |
| GET | /api/v1/permissions/resources | admin | 可授权的表/字段清单（id ↔ 名字对照，供配置界面渲染勾选项） |
| GET | /api/v1/permissions/roles/{role}/scope | admin | 该角色**实际生效**的可见范围 |
| GET | /api/v1/eval/cases | admin | 评测集列表 |
| POST | /api/v1/eval/cases | admin | 批量导入评测样本 |
| DELETE | /api/v1/eval/cases/{id} | admin | 删除评测样本 |
| POST | /api/v1/eval/run | admin | 发起评测运行（立刻返回 running 记录，批处理走后台，客户端轮询详情） |
| GET | /api/v1/eval/runs | admin | 评测运行记录（看板趋势数据） |
| GET | /api/v1/eval/runs/{id} | admin | 评测运行详情（四个指标 + 逐条明细，也是轮询用的接口） |
| GET | /api/v1/dashboard/metrics | analyst 及以上 | 数据看板指标趋势（dimension 必填；metrics/start/end 可选） |
| GET | /api/v1/dashboard/dimensions | analyst 及以上 | 数据看板可用维度与指标（按角色裁剪） |
| GET | /health | 无 | 存活 |
| GET | /health/ready | 无 | 就绪 |

> 权限组的最后两个端点（阶段 9 补）回答的是两个不同的问题：
> `/permissions/resources` 给出 `resource_id` 与表名/字段名的对照——id 是 uuid5，
> 人认不出来，配置界面没有它就画不出勾选框；`/permissions/roles/{role}/scope`
> 回答"**算出来是什么**"，与列表接口的"配了什么"不是一回事。两者在三种情况下不一致：
> 该角色一条都没配（于是不设限）、配的字段已被知识库重建删掉（于是那条权限悬空）、
> 以及字段级授权把整表收窄了。管理界面同时显示两者才对得上账。

> 评测与看板组（阶段 10 落地时补的说明）：
>
> - **`POST /eval/run` 不阻塞**。一次几百条的评测要跑几十分钟，同步等待既过不了网关
>   超时，也让前端分不清"在跑"与"卡死"。它只写一条 `running` 记录并返回，
>   批处理交给后台任务；客户端拿 id 去 `GET /eval/runs/{id}` 轮询。
>   后台任务失败时把记录置为 `failed` 并留下原因，不会永远停在 running。
> - **看板只接 analyst 及以上**。看板给的是全公司口径的聚合数，不是 viewer 的日常工具。
> - **看板取数是确定性拼装，不过模型**：聚合表达式取 `metric_info.calculation`、
>   过滤谓词取 `metric_info.filter_sql`、维度取代码内的白名单。同一个问题两次刷新
>   必须是同一个数，而经过一次模型生成就给不了这个保证。
> - **`filter_sql` 是阶段 10 给 `metric_info` 新增的列**（见 8.1 的 DDL 注释）。
>   原设计里 `filter_condition` 是散文、`calculation` 是聚合表达式，两者都不含可执行的
>   过滤谓词；而"GMV 只算已支付订单"正是最要紧的那半句口径，缺了它看板与问答报出来的
>   数会不一样，且两个数都"看着正常"。新增列是加法，`filter_condition` 原样保留。
> - **准确率以结果集判，且列名只用来对齐、不用来判等**（FR-EVAL-002 的判分口径）。
>   三条路依次退：样本的 `expected_result` → 执行 `expected_sql` 拿结果比 →
>   退化为归一化 SQL 文本比对（并把退化原因写进判分说明）。
>   列名不能当判等依据：标准 SQL 写 `COUNT(dim_region.province)`，数仓给出的列名就是
>   那串字面文本，而模型写 `... AS 省份数`——两边是同一张表。**列别名与 SQL 文本一样，
>   是写法不是答案**。列名对得上就按名字对齐，对不上而列数相同就按列位置比。

### 7.3 SSE 事件协议

| type | 含义 | 字段 |
|---|---|---|
| progress | 节点进度 | step（节点名）、status（start / done） |
| sql | 生成的 SQL | sql |
| result | 查询结果 | data.columns、data.rows、data.row_count、data.truncated |
| explain | 自然语言解释 | text |
| viz | 图表类型推荐 | chart_type（bar / line / pie / table / null）、message |
| error | 出错 | code（业务码，见 12.1）、message |
| done | 本次问答正常结束 | correction_count、row_count |

传输采用标准 SSE 帧：`event: <type>` 行 + `data: <json>` 行 + 空行。`data` 里同时保留
`type` 字段，使单帧脱离上下文也可读。

事件发出者分工：

- `progress` 由节点自己推（进入推 start、正常返回推 done，step 用节点名）；
- `sql` / `result` 由 execute_sql 推（sql 先于 result，前端可先展示语句再等结果集）；
- `explain` / `viz` 由产出它们的节点自己推（阶段 8 起）：`generate_explain`、
  `recommend_visualization`。这条"谁算出来谁推"是阶段 8 立的规矩——服务层只兜终态，
  不再替节点代推产出型事件，否则每加一个产出节点就得改一遍服务层。
  成功一轮的实际次序：`sql → result → explain → viz → done`。其中 `explain` 允许缺席
  （P1 降级，见 §5.1 阶段 8 实现范围），前端据此不渲染解释区即可；
  `viz` 必定出现，无数据时 `chart_type` 为 null 且 `message` 明说"无数据"；
- `error` **只由服务层在终态推一次**——校正循环中间会有多次校验失败，逐个推事件会让
  前端弹出一串马上被修好的错误。终态无错则推 `done`。
- 开流之后不再回改 HTTP 状态码（响应头已发出），所有失败一律走 `error` 事件；
  开流之前（鉴权、入参校验）仍按 7.1 的统一信封返回。

---

## 8. 数据模型

### 8.1 元数据库（meta）

**user**

```sql
CREATE TABLE user (
  id VARCHAR(64) PRIMARY KEY,
  username VARCHAR(128) NOT NULL,
  password_hash VARCHAR(256) NOT NULL,
  role VARCHAR(32) NOT NULL,           -- viewer / analyst / admin
  display_name VARCHAR(128),
  is_active TINYINT(1) DEFAULT 1,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uk_username (username)
);
```

**query_history**

```sql
CREATE TABLE query_history (
  id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(64),
  query_text TEXT,
  generated_sql TEXT,
  result_preview JSON,
  explain_text TEXT,
  status VARCHAR(32),                  -- success / failed
  duration_ms INT,
  created_at DATETIME NOT NULL,
  INDEX idx_user_id (user_id)
);
```

**conversation**

```sql
CREATE TABLE conversation (
  id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  title VARCHAR(256),
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  INDEX idx_user_id (user_id)
);
```

**message**

```sql
CREATE TABLE message (
  id VARCHAR(64) PRIMARY KEY,
  conversation_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  role VARCHAR(16) NOT NULL,          -- user / assistant
  query_text TEXT,
  generated_sql TEXT,
  result_preview JSON,
  explain_text TEXT,
  created_at DATETIME NOT NULL,
  INDEX idx_conversation_id (conversation_id),
  INDEX idx_user_id (user_id)
);
```

**table_info**

```sql
CREATE TABLE table_info (
  id VARCHAR(64) PRIMARY KEY,
  db_name VARCHAR(64) NOT NULL,
  table_name VARCHAR(128) NOT NULL,
  table_alias VARCHAR(128),
  description TEXT,
  is_dimension TINYINT(1) DEFAULT 0,
  created_at DATETIME NOT NULL,
  UNIQUE KEY uk_db_table (db_name, table_name)
);
```

**column_info**

```sql
CREATE TABLE column_info (
  id VARCHAR(64) PRIMARY KEY,
  table_id VARCHAR(64) NOT NULL,
  column_name VARCHAR(128) NOT NULL,
  column_type VARCHAR(64),
  column_alias VARCHAR(128),
  description TEXT,
  is_primary_key TINYINT(1) DEFAULT 0,
  is_foreign_key TINYINT(1) DEFAULT 0,
  ref_table_id VARCHAR(64),
  ref_column_id VARCHAR(64),
  created_at DATETIME NOT NULL,
  INDEX idx_table_id (table_id)
);
```

**metric_info**

```sql
CREATE TABLE metric_info (
  id VARCHAR(64) PRIMARY KEY,
  metric_name VARCHAR(128) NOT NULL,
  metric_alias VARCHAR(128),
  description TEXT,
  calculation TEXT,                    -- 计算逻辑/口径
  filter_condition TEXT,               -- 过滤规则（散文，给模型看）
  filter_sql TEXT,                     -- 过滤谓词的 SQL 形式（阶段 10 新增，见下）
  time_range VARCHAR(128),             -- 时间口径
  relevant_columns TEXT,               -- 相关字段（column_id 列表）
  created_at DATETIME NOT NULL
);
```

> `filter_sql`（阶段 10 新增，可为空）：过滤口径的**可执行谓词形式**，表名限定、
> 不带 `WHERE` 关键字，例如 `fact_order.order_status = '已支付'`。
> 它给数据看板拼模板 SQL 用——散文程序读不懂，运行时去解析必然有解析错的那一天，
> 而错的那天给出的是一个与问答链路口径不一致的数字。
> `filter_condition` 原样保留：它进提示词与向量文本，是给模型看的口径说明。
> **两者口径必须一致，但用途不同**，配置里同时写、构建时一并校验
> （禁止 `;` / `--` / `/*` / `*/`，每处列引用必须带表名限定且确实存在）。
> **没写 `filter_sql`（`None`）的指标不上看板**：看板会拒绝它并说明原因，而不是按
> 不过滤出数——后者给出的是一个与问答链路口径不一致的数字，而它看着完全正常，
> 两个数都"正常"时没有人会去对账。**确实不需要过滤的指标写 `filter_sql: ""` 明说**，
> 与"忘了写"区分开：`""` 是想清楚了，`None` 是漏写。同一次查询里所有指标必须共用
> 同一份口径（`""` 与真实谓词混用也算不一致，那等于把谓词放开了）。

**column_metric**

```sql
CREATE TABLE column_metric (
  column_id VARCHAR(64) NOT NULL,
  metric_id VARCHAR(64) NOT NULL,
  PRIMARY KEY (column_id, metric_id),
  INDEX idx_metric_id (metric_id)
);
```

**permission**

```sql
CREATE TABLE permission (
  id VARCHAR(64) PRIMARY KEY,
  role VARCHAR(32) NOT NULL,
  resource_type VARCHAR(16) NOT NULL, -- table / column
  resource_id VARCHAR(64) NOT NULL,
  created_at DATETIME NOT NULL,
  INDEX idx_role (role)
);
```

> 阶段 9 落地时的三条口径：
>
> - **没有唯一约束**（阶段 2 定稿时如此），(role, resource_type, resource_id) 的判重在
>   服务层做，重复创建返回 400 而不是静默成功——静默成功会让"配了两次"与"配了一次"
>   看起来一样，而界面上的勾选状态是从这份数据反推的。
> - `resource_type` 取 `table` / `column`，`resource_id` 指向 `table_info.id` /
>   `column_info.id`（uuid5，由自然键确定）。指向不存在的资源返回 400：配一条永远
>   不生效的权限比报错危险得多，界面上它会显示成"已授权"。
> - **该角色一条记录都没有 = 不设限**。这是全新环境的默认语义，也是一处必须明写的
>   约定：按白名单严格解读的话，刚部署完、还没人配过权限时所有角色都取不到任何一张表。
>   一旦有了哪怕一条记录就按白名单严格执行，所以"开始配置"本身就是开关；
>   反过来，删掉某角色最后一条配置等于把它退回不设限。

**eval_case**

```sql
CREATE TABLE eval_case (
  id VARCHAR(64) PRIMARY KEY,
  query_text TEXT NOT NULL,
  expected_sql TEXT,
  expected_result JSON,
  created_at DATETIME NOT NULL
);
```

**eval_run**

```sql
CREATE TABLE eval_run (
  id VARCHAR(64) PRIMARY KEY,
  name VARCHAR(256),                   -- 本次评测名称
  total_cases INT NOT NULL DEFAULT 0,
  accuracy DECIMAL(6,4),               -- 准确率
  recall_hit_rate DECIMAL(6,4),        -- 召回命中率
  execute_success_rate DECIMAL(6,4),   -- 执行成功率
  avg_duration_ms INT,                 -- 平均耗时
  detail JSON,                         -- 逐条样本结果
  status VARCHAR(32),                  -- running / finished / failed
  created_at DATETIME NOT NULL,
  INDEX idx_created_at (created_at)
);
```

> 说明：评测看板的"趋势"（FR-EVAL-003）依赖 eval_run 按 created_at 的历史记录；评测过程不写入 conversation/message/query_history（BR-EVAL-001）。

### 8.2 数仓库（dw）

| 表 | 关键字段 |
|---|---|
| dim_region | region_id, province, region_name, country |
| dim_customer | customer_id, customer_name, gender, member_level |
| dim_product | product_id, product_name, category, brand |
| dim_date | date_id, year, quarter, month, day |
| fact_order | order_id, customer_id, product_id, date_id, region_id, order_quantity, order_amount |

> 说明：上表为**关键字段**，实际 DDL 见下方。`fact_order` 额外含 `order_status`
> （已支付/已取消/已退款）——指标口径（如 GMV 排除已取消订单）需要它才能表达，
> 否则 BR-META-002 的"严格遵循口径"无法落地。

建表与样例数据由 `app/scripts/seed_dw.py` 生成（`python -m app.scripts.seed_dw`），
表结构必须与 `conf/meta_config.yaml` 的声明一致：

```sql
CREATE TABLE dim_region (
    region_id   INT         NOT NULL COMMENT '地区主键',
    province    VARCHAR(32) NOT NULL COMMENT '省份名称',
    region_name VARCHAR(32) NOT NULL COMMENT '大区名称',
    country     VARCHAR(32) NOT NULL DEFAULT '中国',
    PRIMARY KEY (region_id), KEY idx_region_name (region_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE dim_customer (
    customer_id   INT         NOT NULL COMMENT '客户主键',
    customer_name VARCHAR(64) NOT NULL COMMENT '客户姓名',
    gender        VARCHAR(8)  NULL COMMENT '性别：男/女',
    member_level  VARCHAR(16) NULL COMMENT '会员等级：普通/银卡/金卡/钻石',
    PRIMARY KEY (customer_id), KEY idx_member_level (member_level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE dim_product (
    product_id   INT          NOT NULL COMMENT '商品主键',
    product_name VARCHAR(128) NOT NULL COMMENT '商品名称',
    category     VARCHAR(64)  NULL COMMENT '一级类目',
    brand        VARCHAR(64)  NULL COMMENT '品牌',
    PRIMARY KEY (product_id), KEY idx_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- year / month / day 是 MySQL 保留字，必须加反引号
CREATE TABLE dim_date (
    date_id INT NOT NULL COMMENT '日期主键，格式 yyyymmdd',
    `year`  INT NOT NULL COMMENT '自然年',
    quarter INT NOT NULL COMMENT '自然季度 1-4',
    `month` INT NOT NULL COMMENT '自然月 1-12',
    `day`   INT NOT NULL COMMENT '月内第几天',
    PRIMARY KEY (date_id), KEY idx_year_month (`year`, `month`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE fact_order (
    order_id       BIGINT        NOT NULL COMMENT '订单主键',
    customer_id    INT           NOT NULL COMMENT '客户ID -> dim_customer',
    product_id     INT           NOT NULL COMMENT '商品ID -> dim_product',
    date_id        INT           NOT NULL COMMENT '下单日期 -> dim_date',
    region_id      INT           NOT NULL COMMENT '地区ID -> dim_region',
    order_quantity INT           NOT NULL COMMENT '购买件数',
    order_amount   DECIMAL(18,2) NOT NULL COMMENT '订单金额（元，不含运费）',
    order_status   VARCHAR(16)   NOT NULL COMMENT '订单状态：已支付/已取消/已退款',
    PRIMARY KEY (order_id),
    KEY idx_customer_id (customer_id), KEY idx_product_id (product_id),
    KEY idx_date_id (date_id), KEY idx_region_id (region_id),
    KEY idx_order_status (order_status),
    CONSTRAINT fk_order_customer FOREIGN KEY (customer_id) REFERENCES dim_customer (customer_id),
    CONSTRAINT fk_order_product  FOREIGN KEY (product_id)  REFERENCES dim_product (product_id),
    CONSTRAINT fk_order_date     FOREIGN KEY (date_id)     REFERENCES dim_date (date_id),
    CONSTRAINT fk_order_region   FOREIGN KEY (region_id)   REFERENCES dim_region (region_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

样例数据规模：28 个地区 / 60 个客户 / 20 个商品 / 1024 个日期（2024-01-01 起）/
约 4000 笔订单。日期区间截至**运行当天**而非硬编码，保证"去年""近三个月"这类
相对时间问法始终有数据可查；随机种子固定，同一份数据可复现。

### 8.3 向量库与全文索引

| 存储 | 集合/索引 | 内容 | 检索方式 |
|---|---|---|---|
| Qdrant | data-agent-column | 字段向量 | 语义（余弦） |
| Qdrant | data-agent-metric | 指标向量 | 语义（余弦） |
| Elasticsearch | data-agent-column-text | 字段全文（阶段 6 新增） | BM25（ik 分词） |
| Elasticsearch | data-agent-metric-text | 指标全文（阶段 6 新增） | BM25（ik 分词） |
| Elasticsearch | data-agent-value | 取值全文索引 | BM25（ik 分词） |

字段与指标各有一个全文索引（与各自的向量集合配对，构成 §5.2 的两路），
取值只有全文一路——取值是精确的字面匹配，"华东"不需要语义近似，
给它配向量只会引入误召回。故 ADR-002 的"三路并行"在阶段 6 展开为：
字段两路、指标两路、取值一路，共五条检索路径。

**点/文档 id 与 MySQL 主键一致**：MySQL 侧 `table_info.id` / `column_info.id` /
`metric_info.id` 由 `uuid5(命名空间, 自然键)` 确定性生成，Qdrant 点 id 与 ES 文档 id
直接复用同一个 id。因此各存储天然一一对应，两路结果凭 id 直接合并、无需映射表，
重复构建也不会产生副本或孤儿数据。取值索引是唯一例外：它的文档是"取值"而不是
"字段"，id 取 `md5(字段 id + 取值)`，同一 (字段, 取值) 重建时覆盖同一文档。
索引名可在配置里改（`es.value_index` / `es.column_index` / `es.metric_index`）。

**向量化文本**（决定召回质量，改动前先想清楚检索侧会怎么问）：

- 字段：`别名（字段名）。所属表：<表别名>。<描述>[。外键，关联 x.y]`
- 指标：`别名（指标名）。<描述>。计算方式：<calculation>。过滤条件：<filter_condition>`

> `filter_sql` **不进这段文本**：它是同一条口径的机器可读形式，与 `filter_condition`
> 说的是同一件事，一起拼进去只是把同一句话对检索侧说两遍（一遍中文、一遍 SQL），
> 反而让向量偏向写法。看板取数直接从 `metric_info` 行里读它（见 7.2 的说明）。

别名放在最前，因为用户问的是"订单金额"而不是 `order_amount`。这段文本**原样**
存进 Qdrant payload 的 `text` 与 ES 文档的 `text`（字段索引的取值文本为
`别名（字段名）：取值`），检索侧不再重新拼装（理由见 §5.2.1）。

**ES 索引 mapping**：索引期用 `ik_max_word`（多切词，提高命中率），检索期用
`ik_smart`（粗粒度，避免把用户输入切碎导致误召回）；`value` 字段带 `raw`
子字段做精确匹配，防止"广东省"被切碎后模糊命中。ik 是第三方插件，未安装时
建索引直接报错而不退回内置分词器——中文用 standard 分词基本等于单字匹配，
静默降级会让召回质量悄悄变差。

**已知限制：单路向量召回分不开近义词**。阶段 4 实测，bge-m3 单路稠密向量对
"销售额"稳定排序为 `sales_quantity`(销量) 优于 `GMV`(成交总额)，分差极小
（0.64 vs 0.62）。已验证这与索引文本结构无关：去掉"计算方式/过滤条件"后
13 条问法里 12 条排序不变，说明不是 SQL 尾巴稀释语义；把口语同义词写进
`description` 也只缩小分差、不改变结论（已保留该改写，它使"一共卖了多少钱"
等问法由错变对，且无回退）。

因此**阶段 4 保证的是召回@k，不是 top-1 精度**：正确项一定进入候选集
（上例中 GMV 稳定排第 2），精排是阶段 6 的职责。这类近义混淆需要
阶段 5 的 LLM 关键词抽取把"销售额"显式归到 GMV 指标，以及阶段 6 的
RRF 融合 + rerank 交叉编码来纠正，不能靠继续调索引文本解决。

**阶段 6 实测结局**：这个限制已由融合纠正，且**不是**靠换模型或调索引文本——
上面那段描述里的"口语常称销售额、营收、卖了多少钱"本来就是 BM25 的命中点，
全文那一路靠它召回了 GMV，融合后 GMV 回到第一。单路向量至今仍把
`sales_quantity` 排第一（用时例钉住），说明纠正确实来自第二路。
`test_the_fulltext_path_fixes_the_near_synonym_case` 监控这条；若哪天单路向量的
top-1 自己变了，用例会失败并要求同步更新本节——避免"限制其实已经不存在了"
被一句过期的说明继续写在文档里。

---

## 9. 关键设计决策（ADR）

### ADR-001 采用 LangGraph 而非手写编排
- **理由**：图结构分叉-汇聚-条件边，LangGraph 原生支持并行分支、条件路由、流式与 checkpointer。

### ADR-002 三路召回并行
- **理由**：字段/指标走语义向量，取值走精确全文，覆盖语义与精确两类需求。

### ADR-003 采用 SSE 而非 WebSocket
- **理由**：查询是单向服务端推送，SSE 基于 HTTP，实现简单、代理友好、支持重连。

### ADR-004 meta 与 dw 分库
- **理由**：职责分离，dw 只读、meta 可写，便于权限隔离。

### ADR-005 Embedding 独立服务化
- **理由**：bge-m3 本地 CPU 推理延迟高，独立服务化支持 GPU 加速与水平扩展。

### ADR-006 会话采用 LangGraph checkpointer + Redis
- **理由**：复用状态快照能力，Redis 保证多实例共享与低延迟，重启可恢复。
- **阶段 7 补充（checkpointer 自己实现，不用 `langgraph-checkpoint-redis`）**：
  该官方包硬依赖 RediSearch 与 RedisJSON 两个模块（建索引、存 JSON 都用它），
  而 Windows 便携版不带这两个模块（`FT.INFO` 报 unknown command），装上也起不来。
  `app/agent/redis_checkpointer.py` 只用 string / list / set 这些基础结构，按
  `BaseCheckpointSaver` 的接口实现，语义照抄 `InMemorySaver`（含重放去重与版本号
  规则）。代价是快照以序列化后的字节存放在列表里、服务端不可查询，本阶段不需要查。
  两条容易踩的坑记在这里：**msgpack 把数组一律解成 list**，从列表里读回的
  `(task_id, channel)` 直接塞进 set 会抛 `TypeError: unhashable type: 'list'`
  （去重时必须先 `tuple()`）；**客户端不能开 `decode_responses=True`**，
  快照是二进制载荷，按文本解码会损坏它。序列化要登记应用自定义类型
  （`Candidate` / `TableSpec` / `MetricSpec` 等），登记表在 `agent/checkpoint.py`
  的 `STATE_TYPES`——不登记现在只打告警，未来 langgraph 会直接拒绝。
- **阶段 7 补充（降级）**：Redis 不可用时不阻塞问答，checkpointer 退为进程内
  `InMemorySaver`（单进程内语义相同，丢的是跨重启的图状态）。这条降级成立的前提是
  **会话消息本身在 MySQL 的 `message` 表**，与 checkpointer 无关：用户看到的多轮
  对话从 SQL 落库那一侧读，降级丢掉的只是每个节点后的中间态快照（状态回放与调试
  用得上）。所以"重启后能接着聊"在降级下依然成立，而 `/health/ready` 会如实报
  `fail`，不会悄悄降级成人人以为有、其实没有的持久化。

### ADR-007 混合检索采用 RRF + rerank
- **理由**：RRF 无需调权、对异构分数鲁棒；rerank 交叉编码提升排序精度。
- **阶段 6 补充**：两路结果只在名次层面融合（`k=60`，共识压过单路，见 §5.2.1）。
  rerank **是可选依赖**：它只影响排序质量、不影响是否有结果，所以未配置或故障时
  降级为 RRF 顺序并每进程告警一次，而不是让整条问答链路失败。重排服务是独立进程
  （同 ADR-005），本环境无 GPU 时可不部署。

### ADR-008 SQL 安全采用白名单解析 + 只读账号
- **理由**：黑名单可被混淆绕过，白名单（sqlparse 解析 + 只读账号 + 最小权限）从机制上杜绝写操作。

---

## 10. 非功能落地方案

| 需求 | 落地方式 |
|---|---|
| 请求追踪 | 中间件 uuid 注入 ContextVar，loguru patch（实现说明见 §10.1） |
| 链路追踪 | OpenTelemetry 采集（含节点耗时与召回命中数），Langfuse 追踪 LLM 调用与 token（实现说明见 §10.1） |
| 安全防护 | 见"安全设计" |
| 健康探针 | /health 存活；/health/ready 逐项探测 MySQL(meta/dw)、Qdrant、ES、Embedding、Redis（阶段 7 起接入，真发一次 PING，报的是它自己 `enabled`/连接的那份配置而不是进程内的选型结果），任一不可用返 503。ES 一项只在三个索引齐备时算就绪，但缺索引不判 failed——它属于"没建库"而非"服务故障"，如实报"已建 N/3"更利于定位；Rerank 是可选依赖，不参与就绪判定 |
| 优雅启停 | lifespan 中 init/close ClientManager |
| 超时与重试 | LLM 超时+重试；SQL max_execution_time + 行数上限 |
| 日志分级 | loguru 统一出口（阶段 11 接管标准库 logging），级别由 `observability.log_level` 配置 |
| 界面一致性 | 设计 Token 统一色值/圆角/阴影/字号/图表配色，页面不写死值；空态与加载态用统一组件（实现说明见 §10.2） |
| 数据库迁移 | Alembic 管理 schema 演进 |

> **日志目前只输出到 stderr，没有配文件 sink**（也没有 rotation/retention）：
> 容器化部署下由运行时负责落盘与轮转，应用内再写一份文件只会产生两套互不同步的
> 日志。需要文件时在这里补 `logger.add(...)`，别在业务代码里另起一个 sink。

### 10.1 实现说明（阶段 11）

**一条管线，不是两套埋点。** 服务端 span 由 FastAPI 0.128 的原生 telemetry 产生
（`FastAPI(telemetry=telemetry_config(...))`），节点、LLM 调用、整轮汇总由
`core.tracing` 自己埋，两者写同一个全局 provider。这一点是硬要求：各写一个 provider
的话，Langfuse 上看到的是一个个孤立的节点，而"这次问答慢在哪"恰恰要顺着树看。

**不用 `opentelemetry-instrumentation-fastapi`。** 实测三种组合后的结论：一旦挂上
那个 instrumentor，FastAPI 的原生 telemetry 会主动让路
（`fastapi/telemetry/_asgi.py::_legacy_otel` 直接翻中间件栈找
`OpenTelemetryMiddleware`），trace 反而变粗——语义约定退回旧的 `http.target`，
丢掉 `fastapi.dependencies` / `fastapi.endpoint` / `fastapi.serialization` 三层 span，
还多出两条 `GET /x http send`。因此依赖也不声明它（见 `pyproject.toml` 的说明）。
**`FastAPIInstrumentor.instrument_app()` 不要加**：它不报错，只是让 trace 变差。

**数据出境这件事写进配置，不交给环境变量。** `telemetry_config` 里
`auto_configure=False` 是必须的：它默认会在启动时读环境里的
`OTEL_EXPORTER_OTLP_ENDPOINT` 并自行挂导出器，那等于"数据往哪儿发"由一个不在仓库
里的变量决定（NFR-SEC-003 的同一条道理）。要发往通用 OTLP 后端就配
`observability.otlp_endpoint`，那条路是我们自己接的。`metrics` / `logs` 同样关掉，
只接 trace。`/health` 探针不进链路。

**Langfuse 的两道坎，都是实测出来的。**

1. 默认的导出过滤器（`langfuse.span_filter.is_default_export_span`）只放行 Langfuse
   自己的 scope、带 `gen_ai*` 属性的、以及它认识的几个 instrumentation。我们的节点
   span 一个都不沾，结果是**只有 LLM 调用上报**、每次调用自成一条 trace
   （`isRootObservation: true`）。必须显式传 `should_export_span=`。
2. 属性名决定它在界面上叫什么。实测（发一条只带普通属性的 span，再用
   `fields=metadata` 读回来）：

   | 写在 span 上 | Langfuse 那边落成 |
   |---|---|
   | `agent.recall.columns` | `attributes.agent.recall.columns`（兜底命名空间） |
   | `langfuse.observation.metadata.agent.recall.columns` | `agent.recall.columns` |
   | `langfuse.trace.metadata.agent.duration_ms` | `agent.duration_ms` |

   `attributes.` 是"不认识的属性"的倾倒口，与 `http.route`、`resourceAttributes.*`
   混在一起。所以命中数与整轮汇总各写两份：中性名给通用 OTLP 后端与测试断言，
   Langfuse 认的名字给面板，两处由同一个函数产出。

   另外，**`langfuse.trace.metadata.*` 只有在根 span 上写才算数**——实测同一棵树里
   父/子 span 各写一个，读回来只剩父的那个，子 span 的静默丢弃。整轮汇总因此必须
   落在服务端 span 上。

**日志接管的两个细节**（`log_setup.InterceptHandler`）：转接标准库记录时要用
`logger.patch` 把 `{name}` 换成标准库那边的 logger 名（否则 uvicorn 的私有 logger
会被推成 `uvicorn.protocols.http.h11_impl` 这类内部模块名，按 "uvicorn.access" 搜不到
访问日志），并按调用栈回溯算出真实调用点（否则每行都写着 `logging:callHandlers:1762`）。
`diagnose` 必须关闭：它会把栈帧局部变量打进日志，而登录/改密的局部变量里有明文口令。

**request_id 的边界**：客户端带的值要过白名单 `[A-Za-z0-9._:-]{1,64}` 才采用，
其余换成自己生成的——它会进日志行，带换行的值能把一条日志伪装成两条。不合规
**不拒绝请求**：request_id 是排障线索，不是安全边界。中间件是裸 ASGI 而非
`BaseHTTPMiddleware`，后者会把响应包进额外的 anyio 任务，SSE 的边生成边推送会被
缓冲成最后一次性吐出。

### 10.2 实现说明（阶段 12）

**Token 是唯一色源，且要接到 Element Plus 上。** `styles/tokens.css` 定义
`--da-*`，文件末尾把 `--el-color-primary` 等映射过去——不做这层映射，就会出现
"按钮是 EP 默认蓝、旁边标签是我们自己的蓝"的两套蓝并存，这正是"配色散乱"最常见的
来源。样式加载顺序因此有语义：EP 样式 → tokens.css → base.css；把 Token 提到 EP
之前，映射会被 EP 的默认值覆盖。图表调色板也是 Token（`--da-chart-*`），JS 侧经
`utils/chart.ts::tokenColor()` 读取，不在代码里重写一份——同一个指标在问答页与看板页
必须是同一个颜色。

**空态与加载态是组件，不是各自的 `v-if`。** 空态统一走 `EmptyState`，文案要说
**为什么空**（"查询成功，但没有匹配的数据" ≠ "还没有查询结果"），不同原因指向
不同的下一步。加载态统一走 `LoadingBlock`（骨架屏）而不是 `v-loading` 遮罩：
遮罩底下什么都没有，用户不知道将来会出现几行什么，而且数据到达时布局会跳一下。

**与后端行为绑定的三处前端实现**（写错了都不会报错，只会表现成"偶发怪事"）：

| 位置 | 必须这么做 | 否则 |
|---|---|---|
| SSE 客户端用 `fetch` 而非 `EventSource` | 问答接口是 POST（问题原文进请求体，不进 URL、不进访问日志） | 迁就 `EventSource` 就得把问题塞进 query string |
| SSE 帧按**空行**切分 | 一帧内部本来就有换行（`event:` + `data:`） | 按换行切会把每行当成一帧，`event:` 行被丢弃、`data:` 行失去类型 |
| 只有业务码 **40101** 触发令牌续期 | 40100 是凭据不对、40102 是令牌无效 | 登录页输错密码就会触发一次 refresh，变成莫名其妙的登录状态变化 |

并发请求同时过期时共享同一个 refresh Promise：各续各的会有多个 refresh 同时在飞，
后到的被先到的作废（表现为"刷新页面偶尔被踢回登录页"）。

**图表推荐只给类型，不给字段映射。** 后端 `visualization.Recommendation` 只有
`chart_type` 与 `message`，所以前端要按"这列的值是不是数字"自己分维度与度量——
与后端判"该不该画折线"用的是同一套朴素规则。折线图还要把**多个时间维度拼成一个标签**
（`year` + `quarter` 是本项目时间轴的常见形状），只取第一列会出现"2024、2024、2024…"
一格一格的重复标签，走势图失去可读性。

**看板的默认图表类型是前端补的**：`DashboardMetricsResponse` 与问答链路的
`viz` 事件不同，它不带推荐字段（看板走确定性模板 SQL，不经过
`recommend_visualization` 节点）。这里按同一条规则给个初始值，且**它只是初始视图**，
用户随时可切——一个默认值判错，代价是一次点击。

**角色门槛写在路由 `meta.requiresRole`，侧栏菜单由同一份数据过滤生成。**
这是**体验层的拦截，不是安全边界**：真正的拒绝在后端依赖（`require_min_role` /
`require_admin`，返回 40300）。前端判断错了，后果是"某个入口该藏没藏"，
不是"越权拿到了数据"——所以它不需要与后端权限表严格对齐，只需与本阶段的
角色定义（PRD 4.1）一致。

---

## 11. 配置清单

| 配置项 | 默认值 | 来源 |
|---|---|---|
| app_name / debug | 数据通达 / false | YAML / env |
| db_meta / db_dw | host/port/user/password/database | env（口令） |
| llm.model_name / base_url / api_key | 可插拔 | env（api_key） |
| embedding.host / model | bge-m3 | YAML |
| rerank.enabled / host / path / model | true / http://127.0.0.1:8002 / /rerank | YAML |
| rerank.api_key / timeout / top_n | 空 / 30s / 0（不截断） | env（api_key） |
| jwt.secret_key / 过期 | HS256 / 30min / 7d | env（secret） |
| qdrant.mode / host / port / embedding_size | server / 6333 / 1024 | YAML / env（mode） |
| qdrant.local_path | — | env（mode=local 时必填） |
| es.scheme / host / port | http / 9200 | YAML |
| es.value_index / column_index / metric_index | data-agent-value / data-agent-column-text / data-agent-metric-text | YAML |
| redis.enabled / host / port / db / password | true / 127.0.0.1 / 6379 / 0 | YAML / env（password） |
| sql_security.max_execution_time_ms / max_result_rows | 30000 / 10000 | 配置 |
| observability.enabled / log_level | true / INFO | YAML / env |
| observability.otlp_endpoint | 空（只走 Langfuse） | YAML / env |
| langfuse.enabled / host / timeout | true / https://cloud.langfuse.com / 5s | YAML / env |
| langfuse.public_key / secret_key | — | env（密钥） |
| langfuse.capture_content | false（只上报结构信息，不上报内容） | YAML / env |
| cors_origins | 白名单 | env |

> **embedding.host 指向服务的 OpenAI 兼容根路径**（以 `/v1` 结尾），即调用
> `POST {host}/embeddings`。这样 Ollama / Xinference / TEI / 云厂商服务都能只改
> 这一个地址直接替换，不必改代码。
>
> **qdrant.mode**：`server`（默认，连远程实例，容器编排下的标准形态）或
> `local`（用 qdrant-client 自带的嵌入式引擎，数据落 `local_path`，不开独立进程）。
> 两种模式 API 完全一致，业务代码不感知差异。`local` 仅用于**没有 Qdrant 服务的
> 本地开发**，其限制是 payload 索引不生效（只影响按字段过滤的性能，过滤语义不变）。
>
> **rerank**：`enabled=false` 或 `host` 为空时不调用精排，检索直接用 RRF 顺序；
> `path` 单独配置是因为各家服务的路由不同（TEI/本地服务常见 `/rerank`，
> 云厂商可能是 `/v1/rerank`）；`api_key` 走环境变量 `RERANK__API_KEY`，只写入
> `.env`，与 LLM/数据库口令同级对待，不进代码与 YAML。没有部署重排服务时应显式
> 设 `RERANK__ENABLED=false`——否则每次查询都会因连不上而打一条 WARNING（功能正常，
> 但日志会脏）。协议适配见 §5.2.1。
>
> **redis**：`enabled` 与 `rerank.enabled` 同一条理由——Redis 的可用性有两种表达
> 方式，"没配"和"配了但连不上"，而后者每次都要白等一轮连接超时，所以开关显式化，
> 运维不必靠"配一个连不上的地址"来表达"本机没有 Redis"。置 false 则会话状态退回
> 进程内存储（见 ADR-006 的阶段 7 补充）。`password` 走环境变量 `REDIS__PASSWORD`，
> 只写入 `.env`，与 LLM/数据库口令同级对待。仅 Redis 用作缓存时需要选 `db`，
> 本阶段只用 0 号库，checkpointer 的键统一带 `prefix`（默认 `checkpoint`），
> 不与将来可能的缓存用途混在一个命名空间里。
>
> **langfuse.capture_content**（阶段 11）：唯一一项管的不是"往哪儿发"而是
> **"允不允许把内容发出去"**的配置。`false`（默认）只上报结构信息（节点名、模型名、
> token 数、耗时、命中数），业务问题原文、生成的 SQL、查询结果不出本机；`true` 则
> 提示词与模型输出原样上报。默认取保守的一侧，两个方向的纠错成本不对称：默认不传、
> 需要时打开只是改一行配置；默认传了、事后才发现不该传，数据已经在第三方服务器上。
> `public_key` / `secret_key` 只写入 `.env`，与数据库口令同级；缺 key 时不报错也不建
> 客户端，本地没注册 Langfuse 照常跑。域名按区域区分（EU `cloud.langfuse.com`，
> US `us.cloud.langfuse.com`），密钥只在所属区域有效，填错拿到的是 401。
>
> **observability.enabled**（阶段 11）：置 false 后进程里不再产生任何 span——
> 连 FastAPI 的原生 telemetry 也一并关掉（见 §10.1），而不是"产生了但我们不看"。
> `otlp_endpoint` 是不依赖 Langfuse 的那条路，填了就把 span 同时送到任意 OTLP 后端；
> 用这一项而**不**用 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境变量，理由见 §10.1。

---

## 12. 错误处理

### 12.1 错误码体系

| 范围 | 类别 | 示例 |
|---|---|---|
| 400xx | 参数校验 | 40000 ValidationError |
| 401xx | 认证 | 40100 AuthError、TokenExpired、InvalidToken |
| 402xx | 查询 | 40200 QueryError、SQLGeneration、SQLExecution、SQLSecurity、Timeout |
| 403xx | 权限 | 40300 PermissionDenied |
| 404xx | 资源 | 40400 NotFound |
| 500xx | 服务 | 50000 ServiceError、LLMServiceError |

> 说明：HTTP 状态码与业务码同步返回——`code` 字段承载业务码（如 40100/40300/40000），HTTP 状态码按类别映射（400→400xx、401→401xx、403→403xx、404→404xx、500→500xx）。PRD 验收标准中的「返回 401/403/400」均指 HTTP 状态码。

402xx 一族的 HTTP 映射按同一原则补齐（调用方自己能纠正的 → 4xx，服务端或上游故障 → 5xx）：

| 业务码 | 异常 | HTTP | 触发场景 |
|---|---|---|---|
| 40200 | QueryError | 400 | 问题本身不可用：为空、超长、召回不到任何数据表 |
| 40201 | SQLGenerationError | 500 | 模型答复了但不是能用的 SQL；也用于 EXPLAIN 预检未通过 |
| 40202 | SQLExecutionError | 500 | 预检通过但真正执行时报错（缺表、类型不符） |
| 40203 | SQLSecurityError | 403 | 未通过只读安全校验（BR-SQL-001）；阶段 9 起也用于「SQL 引用了当前角色未授权的表或字段」（BR-PERM-001） |
| 40204 | QueryTimeoutError | 504 | 超过 max_execution_time_ms（BR-SQL-003） |

> 40201 与 50001 的分工：前者是「模型答了，但答的不是我们能用的」（空串、纯解释文字），
> 后者是「模型根本没答上来」（超时、限流、鉴权失败），归 500xx。

### 12.2 处理策略

- 业务异常抛 AppException 子类，全局 handler 统一转 JSON。
- 未捕获异常由兜底 handler 转 500。
- SSE 流内异常以 type=error 事件推送，不中断连接。
- 历史落库失败隔离捕获，不影响主流程。

---

## 13. 安全设计

| 威胁 | 防护 |
|---|---|
| SQL 注入 | sqlparse 白名单解析 + 只读账号 + 前缀/关键词校验 |
| 越权访问 | JWT 鉴权 + 角色依赖 + 资源归属校验（user_id 过滤） |
| 数据越权 | 表/字段级权限注入 SQL 生成上下文（前置裁剪 + validate_sql 兜底校验；被拒时不向用户透露具体表名/字段名，名字只进日志） |
| 凭据泄露 | bcrypt 哈希、敏感配置环境变量注入、密钥不进代码库 |
| Prompt 注入 | 提示词约束"仅使用提供的表/字段"，隔离用户输入 |
| 令牌滥用 | 访问/刷新令牌分离、短期过期、类型校验 |
| 慢查询 | max_execution_time + max_result_rows 双重限制 |

> **密码哈希为何不用 passlib**：passlib 最新版仍停留在 1.7.4（2020 年，已停止维护），其 bcrypt 后端在初始化自检阶段即调用 `hashpw` 探测历史 wrap bug，而 bcrypt 4.1+ 对超 72 字节输入改为抛 `ValueError`，导致 `CryptContext(schemes=["bcrypt"])` 在 bcrypt 5.x 下直接构造失败。因此改为直连 `bcrypt` 库的 `hashpw` / `checkpw`；产物仍是标准 `$2b$` 哈希，与 passlib 历史数据可互通，后续如需迁移到 argon2 也只需替换 core.security 内两个函数。

---

## 14. 部署与运行

### 14.1 容器编排

```
nginx（前端静态 + 反向代理）
backend（FastAPI）
mysql（meta + dw）
qdrant（向量）
elasticsearch（全文）
redis（缓存/会话）
embedding-service（向量化）
rerank-service（重排）
```

- 前端经 Nginx 托管，`/api` 反向代理至后端，关闭 SSE 缓冲。
- 后端经 lifespan 初始化连接，依赖健康检查顺序启动。

### 14.2 运行命令

```
make up          # 启动全部服务
make init-admin  # 初始化管理员
make seed        # 生成数仓测试数据
make build-meta  # 构建元数据知识库
make migrate     # 执行数据库迁移
make test        # 运行测试
make eval        # 运行评测
```

---

## 15. 开发任务拆解

| 阶段 | 任务 | 产出 |
|---|---|---|
| 1 | 项目骨架：目录、配置、依赖、Alembic、迁移 | 可运行空应用 |
| 2 | 数据模型：meta 11 张表（含 table_info/column_info/metric_info/column_metric/eval_run） | 迁移脚本 |
| 3 | 认证与用户管理 | 登录/权限闭环 |
| 4 | 知识库构建：元数据 + 向量 + 全文索引 | 离线构建脚本 |
| 5 | 单轮 Agent：抽取→召回→生成→校验→执行 | 单轮 NL2SQL 闭环 |
| 6 | 混合检索：RRF + rerank 接入 | 召回优化 |
| 7 | 多轮会话与查询历史：checkpointer + 指代消解 + 上下文注入 + history 接口 | 多轮问答 + 历史管理 |
| 8 | 可解释性与可视化：generate_explain + recommend_visualization 节点 | SQL 解释 + 图表推荐 |
| 9 | 数据权限：权限模型 + SQL 生成上下文注入 | 权限治理 |
| 10 | 评测体系与数据看板：评测集 + 批量评测 + eval_run 记录 + 看板接口 | 效果度量 |
| 11 | 可观测：request_id 贯穿日志、OTel 链路、Langfuse LLM 追踪、节点耗时与召回命中数 | 链路追踪（实现说明见 §10.1） |
| 12 | 前端：设计系统 + 会话 UI + 可解释展示 + 个人设置 + 数据看板 + 评测看板 | 交互落地（实现说明见 §10.2） |
