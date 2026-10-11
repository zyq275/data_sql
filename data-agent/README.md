# 数据通达 · 后端服务（data-agent）

Text-to-SQL 智能数据问答 Agent 的后端。用户用自然语言提问，系统生成只读 SQL、
在数仓上执行、返回结果表格、自然语言解释与图表推荐，全过程以 SSE 流式推给前端。
支持多轮追问与指代消解、按角色的数据权限治理、批量评测与确定性数据看板。

需求见《数据通达-需求规格说明书（PRD）.md》，设计见《数据通达-技术设计文档（TDD）.md》，
开发节奏见《数据通达-开发步骤文档.md》，人工验收步骤见《人工验收手册.md》，
仓库整体说明与前端见仓库根 `README.md`。

**技术栈**：Python 3.12 · FastAPI · LangGraph · SQLAlchemy 2.0（async） · Alembic ·
MySQL 8 · Qdrant · Elasticsearch 8 · Redis · OpenTelemetry / Langfuse

---

## 目录

- [架构总览](#架构总览)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [依赖服务](#依赖服务)
- [元数据知识库构建](#元数据知识库构建)
- [问答链路](#问答链路)
- [混合检索](#混合检索)
- [多轮会话与查询历史](#多轮会话与查询历史)
- [可解释性与可视化推荐](#可解释性与可视化推荐)
- [数据权限治理](#数据权限治理)
- [评测体系与数据看板](#评测体系与数据看板)
- [可观测性](#可观测性)
- [接口清单](#接口清单)
- [统一响应与错误码](#统一响应与错误码)
- [测试与验收](#测试与验收)
- [目录结构](#目录结构)
- [关键设计决策](#关键设计决策)
- [已知环境注意事项](#已知环境注意事项)

---

## 架构总览

```
                    ┌──────────── 前端 Vue 3（../data-agent-frontend）────────────┐
                    │  登录 / 问答 / 历史 / 看板 / 评测 / 用户与权限管理          │
                    └───────────────────────────┬────────────────────────────────┘
                                     REST + SSE │（统一信封；问答走事件流）
┌───────────────────────────────────────────────▼────────────────────────────────┐
│  API 层  api/  —— 中间件（request_id / CORS）· 健康探针 · v1 路由               │
├────────────────────────────────────────────────────────────────────────────────┤
│  服务层  services/  —— 问答编排 · 会话与历史 · 权限配置 · 评测执行 · 看板取数    │
├────────────────────────────────────────────────────────────────────────────────┤
│  智能体层  agent/  —— LangGraph 图：召回 → 筛选 → 生成 SQL → 校验 → 校正 → 执行 │
│                      → 解释 → 图表推荐；含权限裁剪、会话状态、图渲染            │
├────────────────────────────────────────────────────────────────────────────────┤
│  检索层  retrievers/  —— 向量（Qdrant） + 全文（ES/ik） → RRF 融合 → 精排      │
├────────────────────────────────────────────────────────────────────────────────┤
│  数据访问层  repositories/  —— meta 注入式会话；dw 自开只读连接；ORM models/     │
├────────────────────────────────────────────────────────────────────────────────┤
│  外部客户端  clients/  —— MySQL · Qdrant · Elasticsearch · Redis · LLM · 重排   │
└────────────────────────────────────────────────────────────────────────────────┘
        │                 │                │               │            │
   MySQL(meta)      MySQL(dw, 只读)     Qdrant / ES       Redis       LLM / 重排
  业务数据        数仓事实与维度表      向量与全文索引   会话状态快照    OpenAI 兼容端点
```

分层是单向的：接口调服务，服务调智能体与仓储，谁都不反向依赖。`core/` 里的配置、
安全、异常、日志、链路追踪被各层共用，但不依赖任何业务层。

---

## 环境要求

- **Python ≥ 3.12**
- **MySQL 8.x** —— 两个库：`data_agent_meta`（应用元数据）与数仓库 dw
- **Qdrant** —— 或用 `qdrant.mode=local` 走嵌入式模式（见下）
- **Elasticsearch 8.x + analysis-ik 插件** —— 版本必须完全一致
- **Embedding HTTP 服务** —— 本项目用 Ollama 提供 bge-m3（ADR-005：不在应用内做本地推理）
- **大模型服务** —— OpenAI 兼容端点，默认 DeepSeek 云，需自备 API Key
- **Redis** —— 多轮会话的图状态快照（ADR-006）。没有也能跑，降级为进程内存储
- **Rerank 服务（可选）** —— 没有也能跑，检索降级为 RRF 融合顺序。此时请设
  `RERANK__ENABLED=false`，否则每次查询都会打一条连不上的 WARNING
- **Langfuse 账号（可选）** —— 追踪 LLM 调用与节点耗时。没有也能跑：request_id、
  节点耗时、召回命中数的埋点与日志都在应用内，span 只是没地方导出；没配 key 时不报错，
  也不建客户端
- **Node ≥ 20** —— 前端在独立目录 `../data-agent-frontend`

---

## 快速开始

### 1. 安装依赖

```bash
uv venv --python 3.12
uv pip install -e .
```

### 2. 配置

```bash
cp .env.example .env      # 填入 DB_META__PASSWORD 等真实值，.env 不入库
# JWT 密钥可用下面这条生成后填进 .env 的 JWT__SECRET_KEY：
.venv/Scripts/python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

配置优先级：**环境变量 > `.env` > `conf/app_config.yaml` > 代码默认值**。
层级用双下划线表示，如 `DB_META__PASSWORD`、`LLM__API_KEY`、`JWT__SECRET_KEY`
（敏感项只允许从环境变量注入，NFR-SEC-003）。

应用有**启动自检**，宁可起不来也不带着坏配置跑：

- 未配置 `JWT__SECRET_KEY` 时**拒绝启动**，而不是等到第一次登录才 500；
- 未配置 `LLM__API_KEY` 同样拒绝启动 —— 没有它整个问答链路一次都跑不通，失败会以
  "每个请求都报 50001"的形式出现。现象在业务接口、原因在配置，排查要绕一大圈，
  不如启动即拒。

大模型走 OpenAI 兼容端点，换供应商只改 `.env`，代码不动。默认 DeepSeek：

```
LLM__MODEL_NAME=deepseek-chat
LLM__BASE_URL=https://api.deepseek.com/v1
LLM__API_KEY=<自备>
```

### 3. 建库与迁移

先建 meta 库，再跑迁移（迁移只作用于 meta 库，ADR-004）：

```sql
CREATE DATABASE IF NOT EXISTS data_agent_meta
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
```

```bash
.venv/Scripts/python.exe -m alembic upgrade head          # 在线执行（需 .env 口令）
.venv/Scripts/python.exe -m alembic upgrade head --sql    # 离线生成 SQL，不连库
.venv/Scripts/python.exe -m alembic revision --autogenerate -m "..."   # 生成新迁移
```

> **两步都要做的迁移**：迁移 `b71e4a0c95d3` 给 `metric_info` 加了 `filter_sql` 列，
> 而这一列的值来自 `conf/meta_config.yaml`、由构建脚本写进去。只跑迁移不会报错，
> 只会让看板的指标因为"没有过滤口径"而被拒：
>
> ```bash
> .venv/Scripts/python.exe -m alembic upgrade head    # 1. 加列
> .venv/Scripts/python.exe -m app.scripts.build_meta  # 2. 重建知识库，把 filter_sql 填进去
> ```

### 4. 初始化管理员

管理员无法通过接口创建自己，只能用离线脚本（口令从环境变量读，不进 shell 历史）：

```bash
ADMIN_PASSWORD='<自定口令>' .venv/Scripts/python.exe -m app.scripts.init_admin --username admin
# 省略 ADMIN_PASSWORD 则交互式输入
```

### 5. 启动

```bash
.venv/Scripts/python.exe -m uvicorn main:app --reload        # Windows
# .venv/bin/python -m uvicorn main:app --reload              # macOS / Linux
```

- 服务地址：http://127.0.0.1:8000
- 交互文档：http://127.0.0.1:8000/docs
- 存活探针：`GET /health` —— 进程活着就 200，不碰外部依赖
- 就绪探针：`GET /health/ready` —— 逐项探测 MySQL meta/dw、Qdrant、ES、Embedding、
  Redis；任一不可用返回 503，并列出每一项的状态与失败原因

就绪探针每项有独立超时（默认 3s，Embedding 10s —— 冷启动要加载权重）。
**探针挂起比探针失败更糟**：编排器等不到结论，既不敢摘流量也不敢放流量。
Redis 也纳入探测（真发一次 `PING`，报的还是它自己 `enabled` / 连接的那份配置，
不是进程内的选型结果）——多轮会话的图状态要靠它持久化，探测的就是这份依赖。

---

## 依赖服务

### MySQL

两个库，用途分明：

| 库 | 用途 | 访问方式 |
|---|---|---|
| `data_agent_meta` | 用户、会话、消息、查询历史、权限配置、评测样本与运行记录、元数据知识表 | 注入式会话，随请求提交 |
| dw 数仓库 | 业务事实表与维度表 —— **只读** | 每请求自开只读连接，用完即还 |

### 数仓库与只读账号

建 dw 库、样例数据与只读账号（口令从环境变量读，不要写进命令行参数）：

```bash
DW_READONLY_PASSWORD='<自定口令>' .venv/Scripts/python.exe -m app.scripts.seed_dw --create-account
.venv/Scripts/python.exe -m app.scripts.seed_dw        # 建库建表 + 造样例数据
```

`--create-account` 建出 `DB_DW__USER` 指定的账号，**只授 SELECT**
（ADR-008 / NFR-SEC-007：写操作在机制上不可能发生）。记得把口令填进 `.env` 的
`DB_DW__PASSWORD`。

样例数据用固定随机种子生成，日期区间截至运行当天，保证"去年""近三个月"这类相对
时间问法有数据可查。

### Embedding（必须）

用 Ollama 提供 bge-m3，`.env` 的 `EMBEDDING__HOST` 指向其 OpenAI 兼容根路径：

```bash
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS='D:\agent\ollama-models' ollama serve &
OLLAMA_HOST=127.0.0.1:11435 ollama pull bge-m3
curl -s http://127.0.0.1:11435/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"bge-m3","input":["测试"]}' | head -c 120
```

> 项目在 **11435** 端口另起一个独立实例（`OLLAMA_MODELS` 指向项目目录），
> 与用户已有的 11434 实例互不影响 —— 模型存储位置可控，也不受个人实例增删模型的影响。

### Elasticsearch + ik（必须）

**ik 插件版本必须与 ES 完全一致**，否则插件加载失败。TDD 只限定"Elasticsearch 8.x"，
本项目实测使用 **8.9.2**（国内镜像的 8.x 线止于 8.9.2；官网直连下载约 43KB/s，
华为云镜像约 7.6MB/s 且 sha512 与官网一致）：

```bash
# 国内可用镜像：https://mirrors.huaweicloud.com/elasticsearch/8.9.2/elasticsearch-8.9.2-windows-x86_64.zip
unzip elasticsearch-8.9.2-windows-x86_64.zip
# ik 从 infinilabs 的发布站取对应版本（GitHub release 常连不上）
unzip analysis-ik-8.9.2.zip -d elasticsearch-8.9.2/plugins/analysis-ik
elasticsearch-8.9.2/bin/elasticsearch-plugin install -b file:///<绝对路径>/analysis-ik-8.9.2.zip
elasticsearch-8.9.2/bin/elasticsearch.bat
```

`-b` 是必需的：ik 会申请 `SocketPermission * connect,resolve`（远程词典功能），
不加会停在 `Continue with installation? [y/N]` 等输入。装完用 `GET /_cat/plugins`
确认列出 `analysis-ik`。

**开发机要限制堆内存** —— 默认自动堆会按物理内存算到 ~7.8GB，很容易把提交内存吃穿
（见「已知环境注意事项」）。在 `config/jvm.options.d/heap.options` 写：

```
-Xms1g
-Xmx1g
```

未装 ik 时构建脚本会**显式报错**，不会静默退回内置分词器 —— 中文用 standard 分词
基本等于单字匹配，降级会让召回质量悄悄变差。

### Qdrant

没有 Qdrant 服务时，在 `.env` 里切到嵌入式模式：

```
QDRANT__MODE=local
QDRANT__LOCAL_PATH=D:\agent\infra\qdrant-local-data
```

嵌入式模式与 server 模式 API 一致，业务代码不感知差异；区别只是 payload 索引不生效
（只影响按字段过滤的性能，过滤语义不变），且数据落在本地目录。

### Redis

会话的图状态快照存在 Redis 里（ADR-006）：

```
D:\agent\infra\start-redis.cmd           # 一键起，监听 127.0.0.1:6379
```

本机用的是 **Redis 8.10.2 的 Windows 便携版（msys2 构建）**，解压在 `infra/redis/` 下，
不需要 Docker、也不需要 WSL。启动参数里关掉了持久化（`--save "" --appendonly no`）：
这里存的只是**还活着**的会话的状态快照，会话一删就随之丢弃
（`agent.checkpoint.release_thread`），重启丢掉的是一次进行中的追问，不是用户数据 ——
会话消息与查询历史都在 MySQL 里，不受影响。

`redis.enabled=false` 或连不上时**不阻塞问答**：checkpointer 降级为进程内
`InMemorySaver`，单进程内语义完全相同，只是进程一停图状态就没了（用户看到的多轮对话
仍然正常，因为上文是从 MySQL 的 `message` 表读的，与这里无关）。降级会打一条 WARNING，
`/health/ready` 的 `redis` 项则如实报 `fail`。**降级在首次选型时定下就不再翻转**，
Redis 中途起来也要重启进程才会切回去（checkpointer 是编译图时烘进图里的）。

> **为什么不用 `langgraph-checkpoint-redis`**：那个官方包硬依赖 RediSearch 与
> RedisJSON 两个模块（建索引、存 JSON 都用它），而 Windows 便携版不带这两个模块
> （`FT.INFO` 直接报 unknown command），装上也起不来。`app/agent/redis_checkpointer.py`
> 只用 string / list / set 这些基础结构，按 `BaseCheckpointSaver` 的接口实现，
> 语义照抄 `InMemorySaver`（含重放去重与版本号规则）。代价是快照以序列化后的字节存放
> 在列表里，服务端不可查询 —— 本阶段不需要查。

### Rerank（可选）

任何返回 `[{index, score}]` 或 `{"results":[{"index","relevance_score"}]}` 的重排端点
都能接，改 `.env` 的 `RERANK__HOST` / `RERANK__PATH` / `RERANK__MODEL` 即可
（云服务的 Key 填 `RERANK__API_KEY`）。**没部署就不要留着默认值**：

```
RERANK__ENABLED=false
```

否则每次查询都会白等约 0.5 秒（连接被拒后重试一次 + backoff）并打一条 WARNING。
功能不受影响（降级为 RRF 顺序），但代价是白等。

本机实际用的是 **llama.cpp + bge-reranker-v2-m3（Q8_0，606MB）**，全程离线：

```bash
D:\agent\infra\start-rerank.cmd          # 一键起，监听 127.0.0.1:8002
# 手工起就是这一条：
D:\agent\infra\llama-cpp\llama-server.exe \
  -m D:\agent\infra\models\bge-reranker-v2-m3-Q8_0.gguf \
  --rerank --host 127.0.0.1 --port 8002
```

`--rerank` 不可省（它同时把 pooling 切到 rank）。实测每条召回多 0.85 秒
（20 条候选约 1.1 秒），一条问答有字段、指标两路走精排，合计约 +1.7 秒。

**关于分数的两点提醒，都是实测过的**：分数是原始 logit，**不在 0~1 之间、可能是负数**
—— 下游只拿它排序，没有任何地方跟阈值比较，所以没问题，但别在别处按 `score > 0.5`
之类的口径用它；**换线程数会改变分数**（12 线程 0.3796 / 单线程 0.3991，同一输入），
这是浮点归约顺序的差异，同一实例内可复现，判序不受影响
（同一批候选下 GMV 恒在销量前）。

### Langfuse（可选）

见 [可观测性](#可观测性)。没配 key 时不建客户端，埋点仍在应用内。

---

## 元数据知识库构建

知识库由 `conf/meta_config.yaml` **声明**，改配置即可扩展，不改代码（NFR-MAINT-001）。

```bash
.venv/Scripts/python.exe -m app.scripts.build_meta                  # 全量重建 MySQL + Qdrant + ES
.venv/Scripts/python.exe -m app.scripts.build_meta --no-text-index  # 只重建 MySQL + Qdrant
.venv/Scripts/python.exe -m app.scripts.build_meta --embed-only     # 配置没变，只重建向量
```

构建是**全量重建**：先清空四张知识表、两个向量集合与三个 ES 索引再写入。
MySQL 主键由 `uuid5(命名空间, 自然键)` 确定性生成，Qdrant 点 id 与 ES 文档 id
直接复用同一个 id，因此**重建后 id 不变**（`permission.resource_id` 不会失效），
也不会产生副本。

三个 ES 索引：`data-agent-value`（取值）、`data-agent-column-text` 与
`data-agent-metric-text`（全文两路，索引名可配）。

结束后会做**入库自检** —— 核对"配置声明"与"实际入库"的行数，不一致直接失败。

---

## 问答链路

`POST /api/v1/query` 把一句自然语言问题跑成一条 SSE 事件流。图按 TDD 5.1 编排：

```
extract_keywords ─┬─ recall_column ─┐
                  ├─ recall_value  ─┼─ merge_retrieved_info ─┬─ filter_table  ─┐
                  └─ recall_metric ─┘                        └─ filter_metric ─┴─ add_extra_context
                                                                                      ↓
                                        ┌──────────────────────────► generate_sql ─► validate_sql
                                        │                                              │
                                  correct_sql ◄── 未通过且 < 3 次 ──────────────────────┤
                                        │                                              │
                                        └──────────────────────────────────────────────┤
                                        execute_sql ◄────────── 通过 ───────────────────┤
                                               │                                       │
                                               ▼                                       │
                                       generate_explain                                │
                                               │                                       │
                                               ▼                                       │
                                    recommend_visualization                            │
                                               │                                       │
                                               ▼                                       │
                                        END ◄────────── 未通过且 ≥ 3 次 ────────────────┘
```

**两处并行都用"扇出 + 汇合"表达**：汇合节点等齐所有上游才开跑。这不是写法偏好 ——
先到的分支会让汇合节点看到半份候选集，最终 SQL 少一个 JOIN，且不报任何错。

试一下（先登录拿令牌）：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<口令>"}' | python -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")

curl -N -X POST http://127.0.0.1:8000/api/v1/query \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"去年华东区销售额前三的省份"}'
```

### SSE 事件

帧格式 `event: <type>` + `data: <json>` + 空行。

| type | 字段 |
|---|---|
| `progress` | step（节点名）、status（start / done） |
| `sql` | sql |
| `result` | data.columns、data.rows、data.row_count、data.truncated |
| `explain` | text（自然语言解释） |
| `viz` | chart_type（`bar` / `line` / `pie` / `table` / null）、message |
| `error` | code、message |
| `done` | correction_count、row_count |

`progress` 由各节点推；`sql` / `result` / `explain` / `viz` 各由产出它们的那个节点推
（`execute_sql` / `generate_explain` / `recommend_visualization`）；`error`
**只在终态推一次**（校正循环中间的失败不推，否则前端会弹出一串马上被修好的报错）；
终态无错则推 `done`。

**开流之后响应头已发出、状态码改不了，所有失败一律走 `error` 事件**；开流之前的失败
（鉴权、入参）仍按统一信封返回。这也是多轮会话里归属校验必须前置的原因，见
[多轮会话](#多轮会话与查询历史)。

成功一轮的实际次序：`sql → result → explain → viz → done`（中间夹着各节点的
`progress`）。`explain` 允许缺席 —— 它是 P1 且允许降级，模型抖动时整轮照常结束，
只是没有解释；前端据此不渲染解释区即可，不必把"没有解释"当成错误。

### SQL 安全

只读校验在 `app/core/sql_security.py`，**按 token 类型判定而非正则**：首关键字白名单
（`SELECT` / `WITH` / `EXPLAIN` / `SHOW` / `DESC`）+ 危险关键字黑名单 + 多语句拦截 +
`/*!` 可执行注释拦截。字符串字面量与注释里的关键字不算命中。

`validate_sql` 校验通过后再用只读账号做 **EXPLAIN 预检**；`execute_sql` 执行前
**再校验一次** —— 安全闸门要装在自己身上，不能指望上一个节点一定先跑过。

**第二道防线是数据库账号本身**：dw 只读账号只有 SELECT 权限（ADR-008 / NFR-SEC-007），
校验被绕过也写不进去。执行侧还有三重限制：会话级 `MAX_EXECUTION_TIME`、
`max_result_rows` 流式截断（截断会如实写进 `result.truncated`）、墙钟超时兜底。

### 提示词

独立放 `prompts/*.prompt`，占位符写成 `{name}`。**渲染不经 `str.format`**：模板里有
JSON 结构示例，format 会把示例的花括号当占位符报错。`date_info` 里的相对时间换算
（去年 / 上季度 / 近 7 天…）由**代码算好给模型照抄** —— 日期算术交给模型既容易错又没法测。

---

## 混合检索

字段与指标各走两路，融合成一路（TDD 5.2）；**取值只走全文一路** —— "华东"是字面匹配，
给取值配向量只会引入误召回：

```
关键词 ─┬─ Embedding → Qdrant 向量检索 ─┐
        │                               ├─ RRF 融合 → Rerank 精排 → 候选
        └─ ES 全文检索（ik）───────────┘
```

| 文件 | 职责 |
|---|---|
| `retrievers/dense.py` | Qdrant 向量检索（每路取 10 条） |
| `retrievers/fulltext.py` | ES `match` 全文检索（每路取 10 条） |
| `retrievers/fusion.py` | RRF 融合，纯函数无 IO |
| `retrievers/candidate.py` | 两路共用候选，`identity() = (kind, id, name)` 是合并依据 |
| `retrievers/retrieval.py` | 网关：并发两路 → 融合 → 截断 20 条 → 精排 |

**为什么要 RRF 而不是加权求和**：向量分是余弦（0~1），BM25 分无上界且随语料漂移，
两者量纲不可比，任何固定权重都只是在某个语料上拟合出来的。RRF 只用名次：

```
score(d) = Σ 1 / (60 + rank_i(d))     # rank 从 1 起
```

k=60 时第 1 名与第 2 名只差 ≈0.0003，而另一路投的一票 ≥1/61≈0.0164 ——
**任何两路共识的命中都压过任何单路命中**，这正是融合与"把两个列表拼起来"的分界。

**两路看到的是同一段文本**：建库时把拼好的文本同时写进 Qdrant payload 的 `text`
和 ES 的 `text`，doc id 两处都用 MySQL 主键。合并靠 id 直接进行，精排也直接取这段
原文，不必在检索侧重拼（否则"排的依据"与"召的依据"会悄悄分叉）。

**rerank 是可选依赖**。未配置或调用失败时降级为 RRF 顺序，每进程只告警一次
（恢复后标志复位，下次故障会再提醒）。**降级而不是报错**：精排只影响排序质量，
不影响是否有结果。两种响应协议在 `app/clients/rerank.py` 里归一化，
`top_n` / `path` / `api_key` 可配。

**可量化的质量提升**：单路向量至今把"销售额"排给 `sales_quantity`（销量）而非 `GMV`
（TDD 8.3 记录的近同义词问题）；指标的 description 里写着"口语常称销售额、营收"，
全文那一路据此召回 GMV，融合后 GMV 回到第一。

> 召回质量的口径是**召回@k，不是 top-1 精度**。单路稠密向量分不开近义词，
> 精排由 RRF + rerank 负责。19 条语料（指标 5 / 字段 7 / 取值 5）实测召回@3 与
> top-1 均为 100%，门槛为 100% / 85%。

---

## 多轮会话与查询历史

`/api/v1/query` 多了一个可选参数 `session_id`：带上它就是这轮会话的下一轮，
不带就是一次性提问。两组新接口（会话、历史）都按 `user_id` 隔离，**越权一律按
"资源不存在"返回 404，而不是 403** —— 403 等于承认"这个 id 确实存在，只是不归你"，
那是一条可以被逐个 id 问出来的信息。

| 文件 | 职责 |
|---|---|
| `agent/nodes/resolve_reference.py` | 图的入口节点：结合上文把"只看华东"补成完整问句 |
| `agent/checkpoint.py` | checkpointer 选型与降级（Redis → 内存），`release_thread` |
| `agent/redis_checkpointer.py` | 自研 Redis 版 `BaseCheckpointSaver` |
| `repositories/ids.py` | 时间有序主键（会话消息与历史共用） |
| `repositories/conversation.py` | 会话与消息的读写 |
| `repositories/query_history.py` | 查询历史的分页/筛选/详情 |
| `services/conversation_service.py` / `services/history_service.py` | 归属校验与编排 |
| `prompts/resolve_reference.prompt` | 改写提示词，输出纯文本 |

**改写放在图的入口，而不是并进关键字抽取**。它的产出同时被三路召回、两个筛选、
生成 SQL 与校正六处消费；藏在 `extract_keywords` 里的话，那六处都得自己判断
"手里的是原文还是改写后的"。放在入口，这件事只发生一次、只有一个出口，
而且"这一轮到底改写了什么"在日志里是一条独立、可对照的记录。首轮**由代码判断历史
为空即原样透传**，一次模型调用都不发：让模型面对空历史去"决定要不要改写"，
它偶尔会把一个本来完整的问题改坏（补出一个历史里根本没有的年份）。

**为什么编译两张图**。langgraph 规定：挂了 checkpointer 的图必须有 `thread_id`。
一次性提问没有会话 id，硬凑一个的话，每问一句都会在 Redis 里留下一个永不回收的
线程；而 `checkpointer=None` 的图又不能带 `config`。所以
`agent.graph.get_graph(persist=...)` 按需编译并缓存两张 —— 多轮用带 checkpointer 的
那张，单轮用不带的。

**开流前必须做完的事**。SSE 的响应头在第一个事件推出去时就发出去了，此后 HTTP 状态码
已经定型；"会话不存在""这个会话不归你"若发生在流里，就只能表现为**连接被掐断**。
所以归属校验、取上文、选 checkpointer 全部挪进 `QueryService.prepare()`，在开流之前
做完。流一旦开始，所有失败都只能变成 `type=error` 事件。

**一轮一行、role=assistant**。问题与它自己的答案写在同一行里。拆成 user / assistant
两行看起来更规范，但 MySQL 的 `DATETIME` 只到秒，同一秒内写入的两行无法可靠定序 ——
一旦反过来，上文就会把上一轮的问题配到这一轮的 SQL 上。同一理由也决定了主键形态
（`repositories/ids.py`：13 位毫秒时间戳 + 16 位随机），排序全靠主键自己，不依赖
`created_at`。消息行、会话活跃时间、查询历史行**一次提交**写出，分开提交只会得到
"会话详情里看得到这一轮、历史列表里却少一条"这种半份记录。

---

## 可解释性与可视化推荐

| 文件 | 职责 |
|---|---|
| `agent/nodes/generate_explain.py` | 生成自然语言解释，推 `explain` 事件 |
| `agent/nodes/recommend_visualization.py` | 取结果、调规则、推 `viz` 事件（本文件很薄） |
| `agent/visualization.py` | 图表推荐规则本身（纯函数） |
| `agent/rendering.py` 的 `result_summary_text` | 把结果集渲染成给解释节点看的摘要 |
| `prompts/generate_explain.prompt` | 解释提示词，输出纯文本 |

**图表推荐是规则，不是提示词**。TDD 6 的提示词表里列过一份 `recommend_visualization`，
实现时没有采用，理由三条：FR-VIZ-001 的验收标准本身就是一张规则表，而"符合规则"
只有把规则写进代码才验得了 —— 交给模型的话，同一份结果今天推柱状、明天推折线；
"按结果结构推荐"本来就是结构化判断，不需要理解语义；它因此也是图上**唯一一个不会
失败的节点**（纯计算，没有上游可抖，所以 `recommend_visualization.py` 里没有一行
try/except）。这是一处对 TDD 的**有意偏离**，已在 TDD §6 记明。

判定表（`agent/visualization.py`）：

| 条件 | 推荐 |
|---|---|
| 结果为空 | 不推荐（`chart_type=null`），提示"无数据" |
| 单维分组，且度量里有占比型的 | 饼图 |
| 单维分组 + 单度量，该维是时间 | 折线图 |
| 单维分组 + 单度量，该维是普通维度 | 柱状图 |
| 单度量，维度**全部**是时间（`year` + `quarter` 两列） | 折线图 |
| 其余（多列明细） | 表格 |

三处判定口径值得单独说，因为它们都是踩过才写下来的：

- **时间列靠列名认，不能靠值**。数仓的时间维度是 `dim_date` 的 `year` / `quarter` /
  `month` / `day` 四个**整数**列，光看值它们和度量一模一样。只按"值是不是数字"分类，
  "各季度销售额"会被认成两个度量而退回表格 —— 不报错，只是永远画不出折线。
- **标识列（`id` / `*_id`）永远不是度量**，哪怕它是整数。否则明细查询会被算成
  "三维度两度量"，结论碰巧也是表格，但理由全错。
- **占比可以带一个原始金额列**。实测问"各地区的销售额占比"，模型返回
  `大区 / 销售额 / 销售额占比` 三列 —— 原始金额不是干扰，占比正是由它算出来的。
  要求"恰好一个度量"会把这种最典型的占比查询判成表格。

**解释失败不判死整轮问答**：它是 P1，而 `execute_sql` 早把结果推出去了，所以失败只告警、
`explain_text` 写 NULL、`done` 照常推。注意它**没有**照抄 `resolve_reference` 的长度
上限 —— 后者的输出会灌进此后每一次 LLM 调用，失控一行就拖慢整条链路；解释的输出是
终点，只推给前端、只写进一个 TEXT 列。

**落库**：`explain_text` 写进 `message` 与 `query_history`。`chart_type` **不落库** ——
表结构里没有对应列，而它完全由结果结构决定，需要时用同一份 `result_preview` 重算即可，
存一份反而会旧。

---

## 数据权限治理

| 文件 | 职责 |
|---|---|
| `agent/permissions.py` | 范围计算与裁剪：`PermissionScope`（判定核心）、`PermissionGateway`（按角色取配置）、`build_scope`（纯函数） |
| `api/v1/permission.py` | admin 的权限配置接口（FR-PERM-002） |
| `services/permission_service.py` | 配置的增删改查 + "配了什么"与"算出什么"的对账 |
| `agent/nodes/validate_sql.py` | 兜底：SQL 里出现越权的表/字段则拒掉，交给 correct_sql 重写 |

**权限只有一层，就是 `permission` 表**：某角色有哪些 `table` / `column` 行，就只用得了
那些表与字段。没有第二套规则（没有"admin 天然全通"，也没有角色继承），于是
"某个角色能看什么"永远只有一个地方能回答。整组配置接口要求 admin。

**没有配置的角色视为不设限**（`unrestricted`）。这条最容易配错，所以写在最前面：
全新环境里 `permission` 表是空的，按白名单严格解读的话，所有角色 —— 包括管理员 ——
的每一次提问都取不到任何一张表。一个刚部署完、还没人配过权限的系统不该是这个样子。
一旦某角色有了哪怕一条记录，就按白名单严格执行：**"开始配置"这个动作本身就是开关**。
它的另一面是：把某角色最后一条配置删掉，等于把它退回不设限，而不是"清空权限"。

授权语义有两条值得单独说：

- **一张表只要有一个字段级授权，整张表就按字段级收窄**。这样"先授整表、再收窄到几列"
  才表达得出来；反之只有表级授权（没有任何字段行）时视为整表可用，**含以后新增的字段**
  —— 知识库重建后不必再配一遍。这也是 `tables` 的值用 `None` 表示"整表"、
  而不是把当前字段名列出来的原因。
- **指标按它关联的字段判可见**（口径里写着 `SUM(某字段)`，它本身就是说明书）。
  一个字段都没关联的指标在受限范围内一律不可见，并记 WARNING：没有依据能证明它只碰
  已授权的字段，宁可不给，而"指标凭空消失"是最难查的一类故障。

**两道闸门，缺一不可**。前置裁剪让模型根本看不到未授权的名字，后置兜底校验
（`validate_sql` 的第二道关）拦住模型自己编出来的表名、以及从历史轮次带进来的旧表名。
四处泄漏点都堵了：`filter_table_info` / `filter_metric_info` 两个提示词的候选集、
`generate_sql` / `correct_sql` 的表结构与指标文本、以及 `history_text` ——
**引用已无权限对象的那些历史轮次会被整轮丢掉**（留半句上下文比不留更坏）。

后置校验里有两条口径是实测出来的：

- **字段要按"它出现在哪张表上"判，不能平铺成一个名字集合**。字段名在数仓里不唯一
  （`fact_order.product_id` 与 `dim_product.product_id` 同名），平铺的话
  "dim_product 只授了 brand"会让 `SELECT product_id FROM fact_order` 被判越权 ——
  而它碰的是一张整表授权的表。后果不是少给数据而是**模型改不掉**：问题不在 SQL 上，
  拿着同一份裁剪过的表结构重写多少次都还是那句话，连试几轮之后整句失败，
  用户看到的是"这个问法不行"。所以 `forbidden_columns` 是 `表名 → 字段名` 的分组。
- **越权提示不点名**。`error` 事件最终会推给用户，里面若带着表名，就等于向无权者
  确认了那张表存在、它大概长什么样。名字只进日志，给用户的那句话只说结果。

**一轮问答内缓存，跨请求不缓存**：网关按角色缓存算出来的范围，而一个 `QueryContext`
对应一轮问答，所以缓存的生命周期正好是这一轮。于是**没有"改完配置要清缓存"这一步** ——
管理员配完，下一句提问读到的就是新配置。反过来也要注意：同一轮里三处调用（表、指标、
兜底校验）必须看到同一个范围，否则会出现"表被拦了、校验却放行"的自相矛盾中间态。

> 判定核心（`build_scope` 与 `PermissionScope`）用纯函数逐条覆盖在
> `tests/test_permissions.py`，不碰数据库、不碰 LLM：表级/字段级的叠加、整表授权含新增
> 字段、字段名跨表同名、指标可见性、悬空引用（权限指向已从知识库删掉的表）。
> 配置接口另在 `tests/test_permission_api.py` 里覆盖。

---

## 评测体系与数据看板

| 文件 | 职责 |
|---|---|
| `api/v1/eval.py` | 评测集与运行记录接口（FR-EVAL-001~003），整组要求 admin |
| `api/v1/dashboard.py` | 维度清单与聚合取数接口（FR-DASH-001~002），analyst 及以上 |
| `services/eval_runner.py` | 批量执行与判分：驱动真链路、算四个指标、写回 `eval_run` |
| `services/eval_service.py` | 评测集与运行记录的编排（导入去重、发起运行、分页查询） |
| `services/dashboard_service.py` | 按指标口径拼模板 SQL，并按角色裁维度/指标/表 |
| `repositories/eval.py` | `eval_case` / `eval_run` 的存取 |
| `entities/meta_config.py` | `filter_sql` 的构建期校验（禁危险字符、字段必须表名限定且存在） |

### 评测：跑的是线上那条链路，但不落线上数据

**判分口径：以结果集为准，不比 SQL 文本。** 同一份意图可以有无数种写法（别名、
JOIN 顺序、括号），文本不同不等于结果不同；用文本判会把正确答案判成错的，而准确率
是拿来下"这次改动到底有没有变好"这个结论的，宁可口径保守也不要它虚低。三条路依次退：

1. 样本自带 `expected_result` → 直接比（样本作者已经算好的答案）；
2. 否则执行 `expected_sql`，拿它的结果来比；
3. 标准 SQL 自己跑不动（数仓改过表、样本写错）→ 退化为归一化 SQL 文本比对，
   **并把退化的原因写进判分说明** —— 判分依据悄悄换掉是最坏的一种，这一次的准确率
   与上一次已经不是同一个口径了。

比对是**顺序无关的多重集比较**（`ORDER BY` 本来就不保证跨索引、跨机器返回同一次序）。
金额比到小数点后 6 位（Decimal 经 JSON/float 走一圈有 1e-13 级的尾巴），而整数原样
精确比 —— 订单号是 bigint，经 float 会掉精度，"金额对得上、订单号对不上"这种差异
最容易被当成噪声。

**列名只用来对齐，不用来判等**（这条是验收时实测踩出来的）。标准 SQL 写
`COUNT(dim_region.province)`，数仓给出的列名就是那串字面文本；模型写
`COUNT(...) AS 省份数`，得到的列名是 `省份数`。两边算出来的是同一张表，而按列名判等
会把这份正确答案判成错的 —— **这与"不比 SQL 文本"是同一个道理，只是往下一层**：
文本、别名都是表达，结果才是答案。三种情况：名字对得上 → 按名字对齐（换个列顺序是
同一个答案）；对不上但列数相同 → 按列位置比，并在说明里写明这是退化比对；
列数都不同 → 判不等。得说清楚的是，这个 bug 最初的形态是"准确率算得出、只是算成了
0.0" —— 一个看着像"系统答得不好"的结论，所以只在验收里断言"指标不是 None"是拦不住的。

**召回命中率**回答的是知识库维护者最关心的那个问题：这道题需要的表，召回与筛选有没有
把它漏掉。基准是 `expected_sql` 用到的表（只认知识库里真实存在的名字，CTE 名与别名
不算），看它们是否全部出现在**生成 SQL 时看得到的那份表清单**里。判不出来的样本
（没有 `expected_sql`、或标准 SQL 一张已知表都没碰）返回 `None`，**从分母里排除，
而不是算作未命中** —— 虚低的指标会让人去调一个本来没坏的召回链路。

四个指标在 `eval_run` 里分别是 `accuracy` / `recall_hit_rate` / `execute_success_rate`
/ `avg_duration_ms`，比例列是 `DECIMAL(6,4)`（先 `round(...,4)` 再转 `Decimal`，
直接把二进制浮点塞进四位列会被 MySQL 严格模式拒收）。**空运行与判不出来的指标写
`None` 而不是 0**：趋势图上 0 分是一根触底的线，看起来像"某次改动把系统搞崩了"，
而实际只是那次没测成。

**隔离靠"没有会话"，不靠"记得别写"**（BR-EVAL-001）。`QueryService(session=None)`
在代码里就没有可写的库会话，图也走 `get_graph(persist=False)` 那张不挂 checkpointer
的版本，于是 `conversation` / `message` / `query_history` / Redis 线程四样都不可能被写。
换成"用一个真会话、但要小心地不 commit"，隔离就从结构问题变成了纪律问题，早晚会破。

**发起运行立刻返回**：`POST /eval/run` 只写一条 `running` 记录并把批处理交给
`BackgroundTasks`。一次几百条的评测要跑几十分钟，接口挂在那儿等既过不了网关超时，
也让前端分不清"在跑"与"卡死" —— 客户端拿返回的 id 去 `GET /eval/runs/{id}` 轮询。
后台任务**自己开库会话**（它脱离了请求上下文），且**失败不抛出**：把 `status` 置成
`failed`、把已经跑出来的明细与原因留下，否则异常无人接收，只会变成"永远 running"。

评测以**发起人的角色**跑：权限是问答链路的一部分，按别人的角色跑出来的成绩不是这次
调用者的系统表现。未配置任何权限的角色不受限，所以全新环境里跑出来的正是模型本身的水平。

### 看板：确定性拼装，不走模型

同一个问题在两处必须有同一个答案。看板上的人要的是"一个确定的数"，让它经过一次模型
生成，同一个问题两次刷新可能给出两个数，而这类差异没有人能解释。所以这里做的是
**模板填空**：聚合表达式取 `metric_info.calculation`、过滤谓词取 `metric_info.filter_sql`、
维度取代码里的白名单，全是确定性拼装，刷新多少次都是同一个数。

**为什么新增了 `filter_sql` 这一列**：`filter_condition` 是给模型看的口径说明（散文），
`calculation` 是聚合表达式，两者都不含可执行的过滤谓词 —— 而"GMV 只算已支付订单"
正是最要紧的那半句口径。缺了它，看板的数会把已取消、已退款的订单一起算进去：数字看着
正常，只是偏大 —— 本机样例数据上 2024-01 的 GMV 是 527193.63，而按口径应为 365673.77
（已取消 679 行、已退款 660 行、已支付 2635 行），而问答与看板报出来的数不一样时，
没有人能说清哪个对。于是加一列 `filter_sql` 存**谓词形式**（表名限定、不带 `WHERE`），
与散文形式并存、口径必须一致。构建时校验：不允许 `;` / `--` / `/*` / `*/`，
每一处列引用都必须带表名限定且确实存在。

**所选指标必须共用同一份口径**，否则 400：一条 `WHERE` 对所有指标生效，混着用会让
本该过滤的多过滤一次、不该过滤的被打上，而结果看起来只是"数小了点"。

**没写 `filter_sql` 的指标会被拒**（400，并点名是哪个指标），而不是按不过滤出数 ——
按不过滤出数给出的是一个与问答链路口径不一致的数字，而它看着完全正常；两个数都
"正常"时没有人会去对账，这次的错就会一直留着。确实不需要过滤的指标写
`filter_sql: ""` 明说：`""` 是想清楚了，`None` 是漏写，两者分得开（`str | None`）。

**维度白名单写在代码里**（10 个：`dim_date` 的 year/quarter/month/day、`dim_region` 的
province/region_name、`dim_product` 的 category/brand、`dim_customer` 的 gender/
member_level）。哪些字段算"看板维度"是判断而不是事实：知识库回答"库里有什么字段"，
这里回答"哪些字段适合做业务分组" —— 每张维表都有主键，把 `region_id` 摆进下拉框对
业务人员没有意义。名字仍从 `column_info.column_alias` 取，知识库依旧是"字段叫什么"
的唯一来源。

JOIN 条件由 `column_info` 的外键元数据推出来，**不写死**：数仓加了张维表、或者把外键
挪了个位置，写死的那份不会报错，只会悄悄 JOIN 错，然后给出一个看着正常的数。

**权限两道，与数据权限治理同构**：拼装前按角色可见范围挑维度、挑指标、挑事实表与关联表
（下拉框里出现一个选了就 403 的维度，比它干脆不出现更让人困惑）；拼完再拿
`PermissionScope.violations` 兜底核一遍整句 SQL。前一道覆盖"表与字段"，覆盖不了知识库里
写歪的口径（比如某个指标的 `calculation` 引用了没登记的字段），所以要第二道。拼出来的
语句出门前同样过 `assert_readonly`。

有一处与问答链路不同：**403 的消息里点名了维度/指标**。那个名字是用户自己请求的，
不是泄露；不点名他只能靠猜是哪一项被拒了。问答链路的不点名针对的是模型自己编出来的
表名 —— 用户从没见过那个名字。

---

## 可观测性

| 文件 | 职责 |
|---|---|
| `core/request_context.py` | request_id 的上下文变量与请求头白名单（纯逻辑） |
| `core/log_setup.py` | loguru 出口 + 接管标准库 logging；给每条记录补 request_id |
| `core/tracing.py` | OTel provider、节点/LLM 埋点、Langfuse 处理器、内容闸门 |
| `api/middleware.py` | request_id 与 CORS 两层中间件 |

### request_id：一个值贯穿日志与链路

中间件在入口生成（或采用客户端给的）id，写回 `X-Request-Id` 响应头、写进 contextvar，
再由 loguru 的 **patcher** 补到每一条日志记录的 `extra` 上。**用 patcher 而不是 filter**：
filter 是 sink 级的，多一个 sink 就得记得多挂一个，漏挂的那一个的日志就没有 id；
补在记录上，任何 sink、任何格式串都能用。

**这一层是裸 ASGI，不是 `BaseHTTPMiddleware`**：后者会把响应包进一个额外的 anyio 任务，
SSE 的边生成边推送会被缓冲成"最后一次性吐出"，进度事件全部同时到达。

**标准库 logging 是接管而不是改写**：项目里一百多处 `logging.getLogger(__name__)`
一个字没动就有了 id。代价是两处细节要自己兜 —— 转接时用 `logger.patch` 把 `{name}`
换成标准库那边的 logger 名（否则 uvicorn 的私有 logger 会被推成
`uvicorn.protocols.http.h11_impl` 这类内部模块名，按 `uvicorn.access` 搜不到访问日志），
以及按调用栈回溯算出真实的调用点（否则每行都写着 `logging:callHandlers:1762`）。

**占位符要看用的是哪个 logger**：`logging.getLogger()` 走标准库语义，占位符必须是
`%s`；写成 loguru 的 `{}` 不会在写的那一行报错，而是在 emit 时由 `msg % args` 抛出
`TypeError: not all arguments converted`。往 `lifespan` 里加启动日志时就撞上了 ——
那两行只在真正 `uvicorn` 启动时执行，pytest 从不经过 `lifespan`，所以测试全绿也照不出来，
表现为"服务起不来"，堆栈却指向日志模块。转接器因此**刻意保留标准库的容错**：
格式化失败只降级成一行可读的原文，不让一条写错的日志终止进程。

客户端自带的 id 要过白名单 `[A-Za-z0-9._:-]{1,64}`：它会进日志行，带换行的值能把一条
日志伪装成两条。不合规就换一个自己生成的 —— **不拒绝请求**，request_id 是排障线索，
不是安全边界。

### 一条管线：FastAPI 原生 telemetry + 节点埋点 → 同一个 provider

服务端 span 由 **FastAPI 0.128 自带的原生 telemetry** 产生（配在
`FastAPI(telemetry=telemetry_config(...))`），节点、LLM、召回命中数由 `tracing.py`
自己埋；两者写同一个全局 provider，所以 Langfuse 上看到的是一棵树，而不是一堆各自成
trace 的孤立节点。

**没有用 `opentelemetry-instrumentation-fastapi`**，这是实测后的取舍：那个 instrumentor
一旦挂上，FastAPI 的原生 telemetry 会在检测到它的中间件时主动让路
（`fastapi/telemetry/_asgi.py` 的 `_legacy_otel` 是直接翻中间件栈找
`OpenTelemetryMiddleware` 的），trace 反而变粗 —— 语义约定退回旧的 `http.target`，
丢掉 `fastapi.dependencies` / `fastapi.endpoint` / `fastapi.serialization` 几层 span，
还平白多出两条 `GET /x http send`。三种组合都跑过对比才定的。
**别再加 `FastAPIInstrumentor.instrument_app()`**：它不会报错，只会让 trace 变差。

`telemetry_config` 里有两处是**必须**关的，都指向同一件事 —— "数据往哪儿发"不能由
运行环境里一个仓库外的变量决定（NFR-SEC-003 的同一条道理）：

- `auto_configure=False`：它默认会在启动时读环境里的 `OTEL_EXPORTER_OTLP_ENDPOINT`
  并自己挂一个导出器。要往通用 OTLP 后端发，用仓库里的 `observability.otlp_endpoint`。
- `metrics=False` / `logs=False`：只接 trace，不顺手把指标与日志也接上 —— 没配
  collector 时它们只会安静地攒在内存里。

探针路径（`/health`）不进链路：健康检查每次都要打，混进来会让"请求数"这条线失去意义。

### Langfuse：两个坑，都是实测出来的

**一、默认的导出过滤器会把我们的 span 丢掉。** `langfuse.span_filter.is_default_export_span`
只放行三类 span：Langfuse 自己 scope 的、带 `gen_ai*` 属性的、以及它认识的几个 LLM
instrumentation。我们的节点 span 一个都不沾，于是**只有 LLM 调用上了报**，每次调用
自成一条 trace、`isRootObservation: true` —— 看上去"接上了"，实际是一地碎片。修法是给
`Langfuse(...)` 传 `should_export_span=`，把服务端 span 与带 `agent.` / `langfuse.` /
`request.` 前缀的 span 一并放行。

**二、属性写什么名字，决定了它在界面上叫什么。** 实测（发一条只带普通属性的 span，
再用 `fields=metadata` 读回来）：

| 写在 span 上的 | Langfuse 那边落成 |
|---|---|
| `agent.recall.columns` | `attributes.agent.recall.columns`（"不认识的属性"杂物桶） |
| `langfuse.observation.metadata.agent.recall.columns` | `agent.recall.columns`（一级元数据） |
| `langfuse.trace.metadata.agent.duration_ms` | `agent.duration_ms`（一级 trace 元数据） |

所以不是"普通属性看不见"—— 是它们会被归进 `attributes.` 这个和 `http.route`、
`resourceAttributes.*` 混在一起的兜底命名空间。命中数与整轮汇总因此各写两份
（中性名一份给通用 OTLP 后端与测试，Langfuse 认的名字一份给面板），两处都由同一个
函数产出，不会只改了一边。

**trace metadata 只有根 span 上写的才算数**：实测在同一棵树里让父 span 与子 span 各写
一个，读回来只剩父 span 那个，子 span 的静默丢弃。整轮汇总因此落在服务端 span
（也就是根）上，验收用例里有一条断言专门钉这个位置。

### 记录了什么

| 埋点 | 落点 |
|---|---|
| 节点耗时 | 每个 Agent 节点一条 span，`agent.node.duration_ms`；Langfuse 侧还有它自己算的 `latency` |
| 召回命中数 | 各节点的 `agent.recall.*`：字段/取值/指标三路召回、合并后条数、筛选后表与指标、结果行数 |
| token 消耗 | 每次 LLM 调用一条 generation，带模型名与 `usage_details`（Langfuse 据此算费用） |
| 整轮汇总 | 服务端 span 上一条：总耗时、成功/失败、调用次数与 token 合计 |

失败的一轮**照样记**：失败请求的耗时与 token 同样是成本，而且恰恰是排查的对象；
只记成功的话，面板上的平均耗时永远是好消息。模型没返回用量时**不写 token 属性、
也不按 0 记**，而是在日志里明说"未采集" —— 0 会被聚合成"这一轮不花 token"。

**内容默认不出境**：`langfuse.capture_content=false` 时，业务问题原文、生成的 SQL、
查询结果一个字节都不上报，出去的只有结构信息。这个开关有两道闸 —— 写入时就不写
（内容连 exporter 的批次内存都进不去），导出前再按前缀抹一遍（防的是将来有人从别的
路径写进去）。默认取保守的一侧，因为纠错成本不对称：默认不传、需要时打开只是改一行
配置；默认传了、事后才发现不该传，数据已经在别人的服务器上，删不回来。

**测试不往真实账号发数据**：`tests/conftest.py` 里把 `LANGFUSE__ENABLED` 强制设为
`false`（**覆盖** `.env`，不是"没配才补"）。全量测试跑一遍会造出成百上千条 trace，
记在用户账上，事后只能一条条删，把真正要看的那些淹掉。代价为零 —— 节点耗时、命中数、
request_id 的断言读的都是 OTel 一侧；Langfuse 那侧的接线另有一条"处理器挂在同一个
provider 上"的用例守着。

真往账号里发一条的端到端检查是手工做的：`LANGFUSE__ENABLED=true` 跑一次问答，
在 Langfuse 上核对树形结构与元数据。要用 API 读回来（新组织的 legacy 接口一律 410），
走 `GET /api/public/v2/observations?fromStartTime=&toStartTime=`，
**并且必须带 `fields=metadata,usage,metrics`** —— 默认只回 core+basic，看不到元数据与用量。

---

## 接口清单

| 方法 | 路径 | 鉴权 |
|---|---|---|
| POST | `/api/v1/auth/login` | 无 |
| POST | `/api/v1/auth/refresh` | 无（refresh_token 走请求体） |
| GET | `/api/v1/auth/me` | Bearer |
| PUT | `/api/v1/auth/password` | Bearer |
| POST | `/api/v1/query` | Bearer（SSE 流式返回；带 `session_id` 即多轮） |
| POST | `/api/v1/conversations` | Bearer |
| GET | `/api/v1/conversations` | Bearer（`page`/`page_size`） |
| GET | `/api/v1/conversations/{id}` | Bearer（含该会话的消息列表） |
| DELETE | `/api/v1/conversations/{id}` | Bearer |
| GET | `/api/v1/history` | Bearer（`page`/`page_size`/`status`/`keyword`） |
| GET | `/api/v1/history/{id}` | Bearer |
| DELETE | `/api/v1/history/{id}` | Bearer |
| GET | `/api/v1/admin/users` | admin（支持 `page`/`page_size`/`keyword`） |
| POST | `/api/v1/admin/users` | admin |
| PUT | `/api/v1/admin/users/{id}/role` | admin |
| PUT | `/api/v1/admin/users/{id}/status` | admin |
| GET | `/api/v1/permissions` | admin（`page`/`page_size`/`role`） |
| POST | `/api/v1/permissions` | admin |
| PUT | `/api/v1/permissions/{id}` | admin（整条替换，三个字段都要传） |
| DELETE | `/api/v1/permissions/{id}` | admin |
| GET | `/api/v1/permissions/resources` | admin（可授权的表/字段清单，id ↔ 名字对照） |
| GET | `/api/v1/permissions/roles/{role}/scope` | admin（该角色**实际**生效的范围） |
| GET | `/api/v1/eval/cases` | admin（`page`/`page_size`） |
| POST | `/api/v1/eval/cases` | admin（批量导入，单批上限 500 条） |
| DELETE | `/api/v1/eval/cases/{id}` | admin |
| POST | `/api/v1/eval/run` | admin（立刻返回 `running` 记录，批处理走后台） |
| GET | `/api/v1/eval/runs` | admin（`page`/`page_size`，按时间倒序，供趋势图） |
| GET | `/api/v1/eval/runs/{id}` | admin（四个指标 + 逐条明细，客户端轮询此接口） |
| GET | `/api/v1/dashboard/dimensions` | analyst 及以上（可用维度 + 指标清单） |
| GET | `/api/v1/dashboard/metrics` | analyst 及以上（`dimension` 必填；`metrics`/`start`/`end` 可选） |

另有三个不走统一信封的端点：`GET /health`、`GET /health/ready`（探针，返回裸结构）、
`POST /api/v1/query`（SSE，响应体是事件序列）。

**认证**：JWT HS256，access token 30 分钟、refresh token 7 天；角色三档
`admin` / `analyst` / `viewer`。

---

## 统一响应与错误码

成功响应统一为：

```json
{ "code": 0, "message": "ok", "data": ... }
```

出错时 `code` 为业务码，HTTP 状态码按类别同步返回：

| code | 含义 |
|---|---|
| 40000 | 参数 / 业务规则 |
| 40100 | 认证失败（凭据不对） |
| 40101 | 令牌过期（前端据此触发续期） |
| 40102 | 令牌无效 |
| 40200 | 查询请求不合法 |
| 40201 | SQL 生成失败 |
| 40202 | SQL 执行失败 |
| 40203 | SQL 未通过安全校验 |
| 40204 | 查询超时 |
| 40300 | 权限不足 |
| 40400 | 资源不存在 |
| 50000 | 服务内部错误 |
| 50001 | LLM 上游不可用 |

40203 兼表「SQL 引用了当前角色未授权的表或字段」（BR-PERM-001）：两者都是
"这条语句不该被执行"，且都应当走 correct_sql 重写一遍再试，故复用同一个码。

---

## 测试与验收

```bash
uv pip install -e ".[dev]"            # 测试依赖
.venv/Scripts/python.exe -m pytest    # 全量
```

### 按依赖分类

- **纯逻辑测试**（配置解析、探针骨架、口令脱敏、SQL 只读校验、图的拓扑与校正回边、
  RRF 融合与重排客户端、权限判定、图表规则、评测判分、看板拼装）—— 不需要任何外部服务，
  随时可跑；
- 涉及真实存储与知识库的用例标 `requires_stack`，外部服务没起时**跳过并说明缺什么**，
  而不是失败 —— 失败会掩盖真正的问题；
- 需要真调大模型的用例标 `requires_llm`，`.env` 里没配真实的 `LLM__API_KEY` 时跳过；
- 需要真调重排模型的用例标 `requires_rerank`。它**不并进 `requires_stack`**：重排是
  可选依赖，把它算进"外部依赖"会把所有 requires_stack 的用例一并跳掉，比故障本身更糟；
- 需要真实 Redis 的用例标 `requires_redis`，同样不并进 `requires_stack` —— 同上，
  Redis 没起不该让整批用例变成"跳过"。

**跳过只判定「进程有没有在监听」**：服务在跑但口令错、知识库没建、字段对不上，
那是真问题，测试必须失败，不会被跳过掩盖。

### 端到端验收

七个验收文件，需要完整服务栈 + 真实 LLM Key（阶段 6 的第一、三条另需 `-m requires_stack`，
第二条用桩；精排质量单独对真模型测）：

```bash
.venv/Scripts/python.exe -m pytest tests/test_stage5_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage6_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage7_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage8_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage9_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage10_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage11_acceptance.py -v -s
```

| 文件 | 覆盖的验收标准 |
|---|---|
| `test_stage5_acceptance.py` | 自然语言问答返回正确结果；SSE 推送进度/结果/SQL；只读校验拦住写操作；连校 3 次不过即终止并返回明确错误 |
| `test_stage6_acceptance.py` | 两路召回经 RRF 融合（逐条核对融合分）；精排后相关项在前（桩 + `-m requires_rerank` 真模型）；召回@3 与 top-1 达标 |
| `test_stage7_acceptance.py` | 追问能理解上下文且不换主题；指代消解改写、首轮逐字透传；会话状态持久化（另开实例读回）；越权一律 404；历史分页/筛选/详情 |
| `test_stage8_acceptance.py` | `sql` 与 `explain` 都在且解释非空；解释与 SQL 实际逻辑一致；四条图表规则各走一遍真链路；空结果不推荐图表 |
| `test_stage9_acceptance.py` | 无权限的表/字段不出现在 SQL 中；admin 配置前后**必须问出不一样的东西** |
| `test_stage10_acceptance.py` | 四个指标都有值且落 `eval_run`；趋势可查；看板角色门槛（viewer 403）+ 只使用授权范围内表/字段；评测不污染线上数据 |
| `test_stage11_acceptance.py` | request_id 贯穿日志；LLM 调用在 Langfuse 可追踪；节点耗时/token/召回命中数可统计；总开关关掉后什么都不产生 |

### 几条约定的测试纪律

**桩只用来验"代码接对了"，不用来验"模型判得对"**。阶段 5 的第 3、4 条特意用桩喂
恶意/错误 SQL，而不是指望真实模型主动生成写语句 —— 指望它触发安全闸门，等于这道闸门
没被测过。反过来，阶段 6 的第二条用桩测"网关有没有按服务的分数重排"，
**用桩测出"模型排得对"是自欺**，所以精排质量单独用 `-m requires_rerank` 对着真模型测。

**多轮理解只能由端到端结果回答**：拿桩模型测多轮，测的其实是"我们把上文拼进了提示词"，
而拼错了的提示词照样能让桩给出预期输出。所以阶段 7 用的是真模型、真检索、真数仓。

**往库里写行的用例必须自带隔离**。这些表不会自动清空（删会话也不删历史，历史本来
就是独立的），共用 id 的话"列表里该有几条"就会被上一次运行留下的行搅乱，症状是
"第一次跑绿、第二次跑红"。所以每个用例用**本次运行专属的 id**（如
`u-acc7-<用例>-<运行随机串>`，`tests/test_conversation_api.py` 与
`tests/test_history_api.py` 同此）。评测样本与运行记录用随机后缀命名、只删自己建的。

**角色只有三个固定取值，用不了随机名字那套**，所以阶段 9、10 的用例在进出两头
各做一次 `permission` 表快照与还原（见 `test_permission_api.py` 的 `_isolate`）——
直接删会吃掉管理员手配的权限。

**阶段 11 那组只跑一次真实问答**（约 10 秒），三条验收标准共用它留下来的日志与 span ——
跑三遍就是三次模型调用、三倍等待，而且三次之间的差异还会让人怀疑"是不是哪一次没埋上"。

阶段 10 那组要跑好几次真实评测，本机大约 45 秒。

### 前端

前端没有自动化测试，验收靠构建 + 人工核对：

```bash
cd ../data-agent-frontend && npm run build    # vue-tsc 类型检查 + vite build
```

构建通过只说明类型与构建无误，**不等于**交互正确 —— 完整的人工核对清单见
仓库根目录的《人工验收手册.md》。

---

## 目录结构

```
data-agent/
├── main.py                    入口（create_app + lifespan）
├── pyproject.toml             依赖清单
├── alembic/                   数据库迁移（async 引擎，仅 meta 库）
├── conf/
│   ├── app_config.yaml        基础配置（非敏感默认值）
│   └── meta_config.yaml       元数据知识库定义
├── prompts/                   提示词文件（9 个）
├── app/
│   ├── api/                   接口层：middleware / health / v1 路由
│   ├── agent/                 智能体层：graph、state、context、events、llm、
│   │                          prompts、parsing、knowledge、rendering、
│   │                          visualization（图表推荐规则，纯函数）、
│   │                          permissions（数据权限的范围计算与裁剪）、nodes/，
│   │                          以及会话状态：checkpoint（选型/降级）、redis_checkpointer
│   ├── retrievers/            dense / fulltext / fusion / candidate + retrieval 网关
│   ├── clients/               外部连接生命周期管理（MySQL / Qdrant / ES / Redis / LLM / 重排）
│   ├── repositories/          数据访问层（meta 注入式会话；dw 自开只读连接；
│   │                          会话、消息、查询历史、评测样本与运行记录）
│   ├── entities/              领域实体
│   ├── models/                ORM 模型
│   ├── schemas/               请求/响应模型
│   ├── services/              业务编排（含评测执行 eval_runner、看板取数 dashboard_service）
│   ├── core/                  配置 / 安全 / 异常 / 日志（log_setup）/ 请求上下文
│   │                          （request_context）/ 响应 / 链路追踪（tracing，
│   │                          含 sql_security.py）
│   └── scripts/               离线脚本（init_admin、build_meta、seed_dw）
├── evals/                     预留目录，目前只有 .gitkeep
│                              —— 评测集存在 `eval_case` 表里、经接口增删，不需要文件形态
└── tests/                     单元测试
```

前端在 `../data-agent-frontend`，结构见该目录下的 `README.md`。

---

## 关键设计决策

| 决策 | 理由 |
|---|---|
| 迁移只作用于 meta 库（ADR-004） | 数仓是只读的数据源，它的结构不该由应用来管 |
| 不在应用内做本地推理（ADR-005） | Embedding / Rerank 走 HTTP 服务，应用进程保持无状态、可随时重启 |
| 会话状态存 Redis（ADR-006） | 快照是临时的，进程重启可以丢；用户数据在 MySQL |
| dw 只读账号（ADR-008 / NFR-SEC-007） | 写操作在机制上不可能发生，不依赖应用层校验不出错 |
| 敏感配置只从环境变量注入（NFR-SEC-003） | 包括 JWT 密钥、LLM Key、OTLP 端点 —— "数据往哪儿发"不能由仓库外的变量决定 |
| 知识库由 YAML 声明（NFR-MAINT-001） | 扩展元数据只改配置，不改代码 |
| 图表推荐用规则而非提示词 | 同结果必须同推荐，且规则才可验（对 TDD §6 的有意偏离） |
| 评测判分比结果集、不比 SQL 文本 | 表达不同不等于答案不同，准确率要用来下"有没有变好"的结论 |
| 看板走模板拼装、不走模型 | 同一个问题两处必须有同一个答案 |
| 越权返回 404 而非 403（会话与历史） | 403 等于承认这个 id 存在，可以被逐个问出来 |
| request_id 用 patcher 补而非 filter | filter 是 sink 级的，多一个 sink 就得记得多挂一个 |
| 未捕获异常不暴露给客户端 | `debug=False` 恒定，避免渲染帧局部变量（登录/改密请求有泄口令风险） |

---

## 已知环境注意事项

- **Windows 中文环境编码**：`conf/app_config.yaml` 以显式 UTF-8 读取；
  `alembic.ini` 必须保持纯 ASCII（Alembic 用 `encoding="locale"` 读该文件，中文注释会报
  `UnicodeDecodeError`）。

- **前端端口**：`data-agent-frontend` 固定使用 5173（`strictPort: true`），
  与 `cors_origins` 白名单一一对应；端口被占用时会直接报错，请先腾出端口。

- **`APP__DEBUG=true` 不会开启 FastAPI 调试页**（`FastAPI(debug=False)` 恒定）。
  Starlette 的调试页会绕过统一异常 handler，让 500 返回裸 traceback 而非
  `{code, message, data}`，且会渲染帧局部变量（登录/改密请求有泄口令风险）。
  它只影响 CORS 通配符放行、启动日志与 `/health` 的 `debug` 字段；
  未捕获异常的完整堆栈始终在服务端日志里。

- **密码哈希不用 passlib**：passlib 最新版仍是 1.7.4（2020 年，已停维护），其 bcrypt
  后端在自检阶段会喂超 72 字节输入给 `hashpw`，与 bcrypt 4.1+ 的严格校验冲突，
  `CryptContext(schemes=["bcrypt"])` 直接构造失败。因此 `app/core/security.py`
  直连 `bcrypt` 的 `hashpw`/`checkpw`，产物仍是标准 `$2b$` 哈希。

- **MySQL 8 的 `caching_sha2_password` 需要 `cryptography`**：服务端认证缓存失效时
  （MySQL 重启、`FLUSH PRIVILEGES`），客户端必须做 RSA 公钥加密，asyncmy 依赖
  `cryptography` 提供该能力，缺失则报
  `RuntimeError: 'cryptography' package is required`，表现为"昨天还能连、今天连不上"。
  `pyproject.toml` 已显式声明该依赖。另外 `CREATE USER`/`GRANT` 是即时生效的 DDL，
  **不要**在其后 `FLUSH PRIVILEGES` —— 没有意义，只会清空认证缓存，把紧接着的
  下一步连接打挂。

- **内存与页面文件**：ES 默认要 ~1GB 堆，bge-m3 权重 1.07GiB，两者都需要提交内存。
  若 C 盘空间枯竭导致页面文件无法扩张，Windows 提交上限会被卡死，表现为
  `failed to allocate buffer` / `GGML_ASSERT(ctx->mem_buffer != NULL) failed`
  这类**看起来像模型损坏、实则是内存不足**的报错。排查先看提交内存而不是模型。

- **Ollama 用独立实例**：项目在 11435 端口另起一个实例（`OLLAMA_MODELS` 指向项目
  目录），与用户已有的 11434 实例互不影响。好处是模型存储位置可控、不受个人实例的
  模型增删影响。
