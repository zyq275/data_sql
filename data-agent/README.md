# 数据通达 — 后端（data-agent）

Text-to-SQL 智能数据问答 Agent。需求见《数据通达-需求规格说明书（PRD）.md》，
设计见《数据通达-技术设计文档（TDD）.md》，开发节奏见《数据通达-开发步骤文档.md》。

当前进度：**阶段 9 数据权限治理**。

## 环境要求

- Python ≥ 3.12
- MySQL 8.x（meta 库 + dw 库）
- 阶段 4 起还需要：
  - **Qdrant** —— 或用 `qdrant.mode=local` 走嵌入式模式（见下）
  - **Elasticsearch 8.x + analysis-ik 插件** —— 版本必须完全一致
  - **Embedding HTTP 服务** —— 本项目用 Ollama 提供 bge-m3（ADR-005：不在应用内做本地推理）
- 阶段 5 起还需要：**大模型服务**（OpenAI 兼容端点，默认 DeepSeek 云，需自备 API Key）
- 阶段 6 可选：**Rerank 服务**（OpenAI 兼容的重排端点）。**没有也能跑**——
  检索降级为 RRF 融合顺序，每一步照常；此时请设 `RERANK__ENABLED=false`，
  否则每次查询都会打一条连不上的 WARNING
- Node ≥ 20（前端独立目录 `../data-agent-frontend`）

## 首次准备

```bash
uv venv --python 3.12
uv pip install -e .
cp .env.example .env      # 填入 DB_META__PASSWORD 等真实值，.env 不入库
# JWT 密钥可用下面这条生成后填进 .env 的 JWT__SECRET_KEY：
.venv/Scripts/python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

未配置 `JWT__SECRET_KEY` 时应用会**拒绝启动**（启动自检，NFR-SEC-003），
而不是等到第一次登录才 500。阶段 5 起 `LLM__API_KEY` 同样纳入启动自检：
没有它整个问答链路一次都跑不通，失败会以"每个请求都报 50001"的形式出现——
现象在业务接口、原因在配置，排查要绕一大圈，不如启动即拒。

大模型走 OpenAI 兼容端点，换供应商只改 `.env`（`LLM__MODEL_NAME` / `LLM__BASE_URL` /
`LLM__API_KEY`），代码不动。本项目默认 DeepSeek：

```
LLM__MODEL_NAME=deepseek-chat
LLM__BASE_URL=https://api.deepseek.com/v1
LLM__API_KEY=<自备>
```

初始化第一个管理员（管理员无法通过接口创建自己）：

```bash
ADMIN_PASSWORD='<自定口令>' .venv/Scripts/python.exe -m app.scripts.init_admin --username admin
# 省略 ADMIN_PASSWORD 则交互式输入，避免口令进 shell 历史
```

配置优先级：环境变量 > `.env` > `conf/app_config.yaml` > 代码默认值。
层级用双下划线表示，如 `DB_META__PASSWORD`、`LLM__API_KEY`、`JWT__SECRET_KEY`
（敏感项只允许从环境变量注入，NFR-SEC-003）。

## 启动

```bash
.venv/Scripts/python.exe -m uvicorn main:app --reload        # Windows
# .venv/bin/python -m uvicorn main:app --reload              # macOS / Linux
```

- 服务地址：http://127.0.0.1:8000
- 交互文档：http://127.0.0.1:8000/docs
- 存活探针：`GET /health`（进程活着就 200，不碰外部依赖）
- 就绪探针：`GET /health/ready`（逐项探测 MySQL meta/dw、Qdrant、ES、Embedding、Redis；
  任一不可用返回 503，并列出每一项的状态与失败原因）

就绪探针每项有独立超时（默认 3s，Embedding 10s——冷启动要加载权重）。
探针挂起比探针失败更糟：编排器等不到结论，既不敢摘流量也不敢放流量。
自阶段 7 起 Redis 纳入探测（真发一次 `PING`，报的还是它自己 `enabled`/连接的那份配置，
不是进程内的选型结果）：多轮会话的图状态要靠它持久化，探测的就是这份依赖。

## 数据库迁移

迁移只作用于 meta 库（ADR-004）。首次需先建库：

```sql
CREATE DATABASE IF NOT EXISTS data_agent_meta
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
```

```bash
.venv/Scripts/python.exe -m alembic upgrade head         # 在线执行（需 .env 口令）
.venv/Scripts/python.exe -m alembic upgrade head --sql   # 离线生成 SQL，不连库
.venv/Scripts/python.exe -m alembic revision --autogenerate -m "..."  # 阶段 2 起使用
```

## 测试

```bash
uv pip install -e ".[dev]"            # 测试依赖
.venv/Scripts/python.exe -m pytest    # 全量
```

按依赖分几类：

- **纯逻辑测试**（配置解析、探针骨架、口令脱敏、SQL 只读校验、图的拓扑与校正回边、
  RRF 融合与重排客户端）不需要任何外部服务，随时可跑；
- 涉及真实存储与知识库的用例标 `requires_stack`，外部服务没起时**跳过并说明缺什么**，
  而不是失败——失败会掩盖真正的问题；
- 需要真调大模型的用例标 `requires_llm`，`.env` 里没配真实的 `LLM__API_KEY` 时跳过；
- 需要真调重排模型的用例标 `requires_rerank`，没配或连不上时跳过。它**不并进
  `requires_stack`**：重排是可选依赖，把它算进"外部依赖"会把所有 requires_stack
  的用例一并跳掉，比故障本身更糟；
- 需要真实 Redis（会话状态持久化）的用例标 `requires_redis`，同样不并进
  `requires_stack`——同上，Redis 没起不该让整批用例变成"跳过"。

跳过只判定「进程有没有在监听」：服务在跑但口令错、知识库没建、字段对不上，
那是真问题，测试必须失败，不会被跳过掩盖。

跑阶段 5 / 阶段 7 / 阶段 8 / 阶段 9 的端到端验收（需要完整服务栈 + 真实 LLM Key）：

```bash
.venv/Scripts/python.exe -m pytest tests/test_stage5_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage7_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage8_acceptance.py -v -s
.venv/Scripts/python.exe -m pytest tests/test_stage9_acceptance.py -v -s
```

阶段 9 那组会往 `permission` 表里写配置，用例进出两头各做一次快照与还原
（见 `test_permission_api.py` 的 `_isolate`）——角色只有三个固定取值，用不了
"每次跑一个随机名字"那套隔离办法，而直接删又会吃掉管理员手配的权限。

## 验证清单（阶段 1 验收）

| 验收项 | 方法 |
|---|---|
| 后端启动成功，`/health` 返回 200 | `curl http://127.0.0.1:8000/health` |
| 配置能从环境变量读取 | `APP__DEBUG=true` 时 `/health` 的 `debug` 字段变为 `true` |
| CORS 通配符被拦截 | 生产模式（`APP__DEBUG=false`）下设 `CORS_ORIGINS=["*"]`，启动即报错 |
| Alembic 能执行一次空迁移 | `alembic upgrade head`（需先建库并配置口令） |

## 接口清单（阶段 3 + 阶段 5 + 阶段 7 + 阶段 9）

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

响应统一为 `{"code": 0, "message": "ok", "data": ...}`；出错时 `code` 为业务码
（40000 参数/业务规则、40100 认证、40101 令牌过期、40102 令牌无效、40200 查询请求不合法、
40201 SQL 生成失败、40202 SQL 执行失败、40203 SQL 未通过安全校验、40204 查询超时、
40300 权限、40400 资源、50000 服务、50001 LLM 上游不可用），HTTP 状态码按类别同步返回。
40203 自阶段 9 起兼表「SQL 引用了当前角色未授权的表或字段」（BR-PERM-001）：两者都是
"这条语句不该被执行"，且都应当走 correct_sql 重写一遍再试，故复用同一个码。
`/health` 与 `/health/ready` 是探针，按约定返回裸结构、不套信封；`/api/v1/query` 是 SSE，
响应体是事件序列、同样不套信封。

## 验证清单（阶段 4 验收）

| 验收项 | 方法 |
|---|---|
| 表/字段/指标完整入库 | `build_meta` 末尾的入库自检（核对"配置声明"与"实际入库"行数，不一致直接失败） |
| 字段与指标向量索引建立 | 查 `data-agent-column` / `data-agent-metric` 两个集合的点数是否为字段数 / 指标数 |
| 字段取值全文索引建立 | 查 `data-agent-value` 文档数；`POST /_analyze` 确认 ik 分词生效 |
| 召回能命中对应项 | 对"订单金额""会员等级"等问法做向量检索，看 top-1 是否为对应字段 |

召回这一条的口径是**召回@k，不是 top-1 精度**：阶段 4 交付的是索引，能保证正确项
进入候选集；单路稠密向量分不开近义词（实测"销售额"会被排到"销量"之前），
精排由阶段 6 的 RRF + rerank 负责，详见 TDD 8.3 的"已知限制"。

四条标准已落成自动化用例：`pytest tests/test_meta_knowledge.py`（对应用例见 1/2/3/4
四个分节）。入库一项不读构建脚本的输出，而是直接查 information_schema 与四张知识表
后与配置声明逐项比对——脚本说"成功"不算数，存储里真的有才算。

## 元数据知识库构建（阶段 4）

知识库由 `conf/meta_config.yaml` 声明，改配置即可扩展，不改代码（NFR-MAINT-001）。

### 1. 建数仓库与样例数据

先建 dw 库和只读账号（口令从环境变量读，不要写进命令行参数）：

```bash
DW_READONLY_PASSWORD='<自定口令>' .venv/Scripts/python.exe -m app.scripts.seed_dw --create-account
.venv/Scripts/python.exe -m app.scripts.seed_dw        # 建库建表 + 造样例数据
```

`--create-account` 建出 `DB_DW__USER` 指定的账号，**只授 SELECT**（ADR-008 /
NFR-SEC-007：写操作在机制上不可能发生）。记得把口令填进 `.env` 的 `DB_DW__PASSWORD`。

样例数据用固定随机种子生成，日期区间截至运行当天，保证"去年""近三个月"这类
相对时间问法有数据可查。

### 2. 准备外部服务

**Embedding（必须）** —— Ollama 起 bge-m3，并让 `.env` 的 `EMBEDDING__HOST`
指向其 OpenAI 兼容根路径：

```bash
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS='D:\agent\ollama-models' ollama serve &
OLLAMA_HOST=127.0.0.1:11435 ollama pull bge-m3
curl -s http://127.0.0.1:11435/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"bge-m3","input":["测试"]}' | head -c 120
```

**Elasticsearch + ik（必须）** —— ik 插件版本必须与 ES 完全一致，否则插件加载失败。
TDD 只限定"Elasticsearch 8.x"，本项目实测使用 **8.9.2**（国内镜像的 8.x 线止于
8.9.2，官网直连下载约 43KB/s，华为云镜像约 7.6MB/s 且 sha512 与官网一致）：

```bash
# 国内可用镜像：https://mirrors.huaweicloud.com/elasticsearch/8.9.2/elasticsearch-8.9.2-windows-x86_64.zip
unzip elasticsearch-8.9.2-windows-x86_64.zip
# ik 从 infinilabs 的发布站取对应版本（GitHub release 常连不上）
unzip analysis-ik-8.9.2.zip -d elasticsearch-8.9.2/plugins/analysis-ik
elasticsearch-8.9.2/bin/elasticsearch-plugin install -b file:///<绝对路径>/analysis-ik-8.9.2.zip
elasticsearch-8.9.2/bin/elasticsearch.bat
```

`-b` 是必需的：ik 会申请 `SocketPermission * connect,resolve`（远程词典功能），
不加会停在 `Continue with installation? [y/N]` 等输入。
装完用 `GET /_cat/plugins` 确认列出 `analysis-ik`。

开发机还要**限制堆内存**——默认自动堆会按物理内存算到 ~7.8GB，很容易把
提交内存吃穿（见下面"内存与页面文件"）。在 `config/jvm.options.d/heap.options` 写：

```
-Xms1g
-Xmx1g
```

未装 ik 时构建脚本会显式报错，不会静默退回内置分词器（中文用 standard 分词
基本等于单字匹配，降级会让召回质量悄悄变差）。

**Qdrant（可选）** —— 没有 Qdrant 服务时，在 `.env` 里切到嵌入式模式：

```
QDRANT__MODE=local
QDRANT__LOCAL_PATH=D:\agent\infra\qdrant-local-data
```

嵌入式模式与 server 模式 API 一致，业务代码不感知差异；区别只是 payload 索引
不生效（只影响按字段过滤的性能，过滤语义不变），且数据落在本地目录。

**Rerank（可选，阶段 6）** —— 任何返回 `[{index, score}]` 或
`{"results":[{"index","relevance_score"}]}` 的重排端点都能接，改 `.env` 的
`RERANK__HOST` / `RERANK__PATH` / `RERANK__MODEL` 即可（云服务的 Key 填
`RERANK__API_KEY`）。**没部署就不要留着默认值**：

```
RERANK__ENABLED=false
```

否则每次查询都会白等约 0.5 秒（连接被拒后重试一次 + backoff）并打一条 WARNING。
功能不受影响（降级为 RRF 顺序），但代价是白等。

本机实际用的是 **llama.cpp + bge-reranker-v2-m3（Q8_0，606MB）**，全程离线：

```
D:\agent\infra\start-rerank.cmd          # 一键起，监听 127.0.0.1:8002
```

它对应的正是 `.env` 里 `RERANK__HOST=http://127.0.0.1:8002`，所以本机不用改任何配置。
手工起就是这一条：

```bash
D:\agent\infra\llama-cpp\llama-server.exe \
  -m D:\agent\infra\models\bge-reranker-v2-m3-Q8_0.gguf \
  --rerank --host 127.0.0.1 --port 8002
```

`--rerank` 不可省（它同时把 pooling 切到 rank）。实测每条召回多 0.85 秒
（20 条候选约 1.1 秒），一条问答有字段、指标两路走精排，合计约 +1.7 秒。

关于分数的两点提醒，都是实测过的：**分数是原始 logit，不在 0~1 之间、可能是负数**
——下游只拿它排序，没有任何地方跟阈值比较，所以没问题，但别在别处按 `score > 0.5`
之类的口径用它；**换线程数会改变分数**（12 线程 0.3796 / 单线程 0.3991，同一输入），
这是浮点归约顺序的差异，同一实例内可复现，判序不受影响（同一批候选下 GMV 恒在销量前）。

**Redis（阶段 7，多轮会话状态）** —— 会话的图状态快照存在 Redis 里（ADR-006）：

```
D:\agent\infra\start-redis.cmd           # 一键起，监听 127.0.0.1:6379
```

本机用的是 **Redis 8.10.2 的 Windows 便携版（msys2 构建）**，解压在 `infra/redis/`
下，不需要 Docker、也不需要 WSL。它对应的正是 `.env` 里 `REDIS__HOST=127.0.0.1`，
所以本机不用改配置。启动参数里关掉了持久化（`--save "" --appendonly no`）：
这里存的只是**还活着**的会话的状态快照，会话一删就随之丢弃（
`agent.checkpoint.release_thread`），重启丢掉的是一次进行中的追问，不是用户数据——
会话消息与查询历史都在 MySQL 里，不受影响。

`redis.enabled=false` 或连不上时**不阻塞问答**：checkpointer 降级为进程内
`InMemorySaver`，单进程内语义完全相同，只是进程一停图状态就没了（用户看到的多轮
对话仍然正常，因为上文是从 MySQL 的 `message` 表读的，与这里无关）。降级会打一条
WARNING，`/health/ready` 的 `redis` 项则如实报 `fail`。**降级在首次选型时定下就不再
翻转**，Redis 中途起来也要重启进程才会切回去（checkpointer 是编译图时烘进图里的）。

**为什么不用 `langgraph-checkpoint-redis`**：那个官方包硬依赖 RediSearch 与 RedisJSON
两个模块（建索引、存 JSON 都用它），而 Windows 便携版不带这两个模块
（`FT.INFO` 直接报 unknown command），装上也起不来。`app/agent/redis_checkpointer.py`
只用 string / list / set 这些基础结构，按 `BaseCheckpointSaver` 的接口实现，
语义照抄 `InMemorySaver`（含重放去重与版本号规则），可选项 `prefix`、`allowlist` 见
TDD 11。代价是快照以序列化后的字节存放在列表里，服务端不可查询——本阶段不需要查。

### 3. 构建

```bash
.venv/Scripts/python.exe -m app.scripts.build_meta                  # 全量重建 MySQL + Qdrant + ES
.venv/Scripts/python.exe -m app.scripts.build_meta --no-text-index  # 只重建 MySQL + Qdrant
.venv/Scripts/python.exe -m app.scripts.build_meta --embed-only     # 配置没变，只重建向量
```

构建是**全量重建**：先清空四张知识表、两个向量集合与三个 ES 索引再写入。
MySQL 主键由 `uuid5(命名空间, 自然键)` 确定性生成，Qdrant 点 id 与 ES 文档 id
直接复用同一个 id，因此重建后 id 不变（`permission.resource_id` 不会失效），
也不会产生副本。三个索引是 `data-agent-value`（取值）、`data-agent-column-text`
与 `data-agent-metric-text`（阶段 6 的全文两路，索引名可配，见 TDD 11）。

结束后会做入库自检——核对"配置声明"与"实际入库"的行数，不一致直接失败。

## 单轮问答链路（阶段 5）

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

两处并行都用"扇出 + 汇合"表达：汇合节点等齐所有上游才开跑。这不是写法偏好——
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

### SSE 事件（TDD 7.3）

帧格式 `event: <type>` + `data: <json>` + 空行。

| type | 字段 |
|---|---|
| progress | step（节点名）、status（start / done） |
| sql | sql |
| result | data.columns、data.rows、data.row_count、data.truncated |
| explain | text（自然语言解释） |
| viz | chart_type（`bar` / `line` / `pie` / `table` / null）、message |
| error | code、message |
| done | correction_count、row_count |

`progress` 由各节点推；`sql` / `result` / `explain` / `viz` 各由产出它们的那个节点推
（`execute_sql` / `generate_explain` / `recommend_visualization`）；`error` **只在终态推一次**
（校正循环中间的失败不推，否则前端会弹出一串马上被修好的报错）；终态无错则推 `done`。
开流之后响应头已发出、状态码改不了，所有失败一律走 `error` 事件；开流之前的失败
（鉴权、入参）仍按统一信封返回。

成功一轮的实际次序：`sql → result → explain → viz → done`（中间夹着各节点的 `progress`）。
`explain` 允许缺席——它是 P1 且允许降级，模型抖动时整轮照常结束，只是没有解释；
前端据此不渲染解释区即可，不必把"没有解释"当成错误。

### SQL 安全

只读校验在 `app/core/sql_security.py`，按 token 类型判定而非正则：首关键字白名单
（SELECT / WITH / EXPLAIN / SHOW / DESC）+ 危险关键字黑名单 + 多语句拦截 + `/*!` 可执行
注释拦截。字符串字面量与注释里的关键字不算命中。`validate_sql` 校验通过后再用只读
账号做 EXPLAIN 预检；`execute_sql` 执行前再校验一次——安全闸门要装在自己身上，
不能指望上一个节点一定先跑过。

第二道防线是数据库账号本身：dw 只读账号只有 SELECT 权限（ADR-008 / NFR-SEC-007），
校验被绕过也写不进去。执行侧还有三重限制：会话级 `MAX_EXECUTION_TIME`、
`max_result_rows` 流式截断（截断会如实写进 `result.truncated`）、墙钟超时兜底。

### 提示词

独立放 `prompts/*.prompt`，占位符写成 `{name}`。渲染不经 `str.format`：模板里有 JSON
结构示例，format 会把示例的花括号当占位符报错。`date_info` 里的相对时间换算
（去年 / 上季度 / 近 7 天…）由代码算好给模型照抄——日期算术交给模型既容易错又没法测。

## 验证清单（阶段 5 验收）

| 验收项 | 方法 |
|---|---|
| 输入"去年华东区销售额前三的省份"返回正确结果 | `pytest tests/test_stage5_acceptance.py -v -s` 里会打印生成 SQL 与结果行 |
| SSE 流式推送进度、结果与 SQL | 同上，断言 `progress` / `sql` / `result` / `done` 都在且 sql 先于 result；也可用上面的 `curl -N` 肉眼观察逐帧推送 |
| SQL 只读校验生效、写操作被拒 | 用桩把模型输出换成 DROP/DELETE/UPDATE/多语句，断言 40203 且没有任何结果集；另测只读账号本身建表被 MySQL 拒绝 |
| 连续校正 3 次仍不通过时终止并返回明确错误 | 喂一条"只读但列不存在"的 SQL，断言 `correct_sql` 恰好执行 3 次、终态 error 事件带根因且只报一次 |

四条标准已落成自动化用例（`tests/test_stage5_acceptance.py`）。第 3、4 条特意用桩喂
恶意/错误 SQL，而不是指望真实模型主动生成写语句——指望它触发安全闸门，
等于这道闸门没被测过。第 1、2 条必须真调大模型，否则验的是桩不是链路。

## 混合检索（阶段 6）

字段与指标各走两路，融合成一路（TDD 5.2）；取值只走全文一路——"华东"是字面匹配，
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

k=60 时第 1 名与第 2 名只差 ≈0.0003，而另一路投的一票 ≥1/61≈0.0164——
**任何两路共识的命中都压过任何单路命中**，这正是融合与"把两个列表拼起来"的分界。

**两路看到的是同一段文本**：建库时把拼好的文本同时写进 Qdrant payload 的 `text`
和 ES 的 `text`，doc id 两处都用 MySQL 主键。合并靠 id 直接进行，精排也直接取这段
原文，不必在检索侧重拼（否则"排的依据"与"召的依据"会悄悄分叉）。

**rerank 是可选依赖**。未配置或调用失败时降级为 RRF 顺序，每进程只告警一次
（恢复后标志复位，下次故障会再提醒）。降级而不是报错：精排只影响排序质量，
不影响是否有结果。两种响应协议（`[{index,score}]` 与 `{"results":[{index,relevance_score}]}`）
在 `app/clients/rerank.py` 里归一化，`top_n` / `path` / `api_key` 可配（TDD 11）。

**本阶段唯一可量化的质量提升**：单路向量至今把"销售额"排给 `sales_quantity`（销量）
而非 `GMV`（TDD 8.3 记录的近同义词问题）；指标的 description 里写着"口语常称销售额、
营收"，全文那一路据此召回 GMV，融合后 GMV 回到第一。

## 验证清单（阶段 6 验收）

| 验收项 | 方法 |
|---|---|
| 两路召回结果经 RRF 融合 | `pytest tests/test_stage6_acceptance.py -v -s`：逐条候选核对融合分是否等于两路名次倒数之和，并断言共识命中排在单路命中之前 |
| rerank 精排后相关项位于不相关项之前 | 同上：用本地桩重排服务（按文档内容打分）断言网关确实按精排分数重排；桩也记录了送到服务端的文档，比对建库原文 |
| 同上，但用真实重排模型 | 起 `start-rerank.cmd` 后跑 `pytest tests/test_stage6_acceptance.py -m requires_rerank -v -s`：断言问"销售额"时 GMV 排在销量之前 |
| 召回命中率达标 | 同上：19 条语料（指标 5 / 字段 7 / 取值 5）实测召回@3 与 top-1 均为 100%，门槛为 100% / 85% |

第一条与第三条必须真跑两路索引（`pytest -m requires_stack`）；第二条用桩，
因为桩能测"网关有没有按服务的分数重排"，而**用桩测出"模型排得对"是自欺**，
所以精排质量单独用 `-m requires_rerank` 对着真模型测。该用例在没起服务时会跳过
（并说明缺什么），起了就是实跑——写这份 README 时它已经在 llama.cpp 上跑通。
更全面的精排质量仍由阶段 10 的评测集度量。

## 多轮会话与查询历史（阶段 7）

`/api/v1/query` 多了一个可选参数 `session_id`：带上它就是这轮会话的下一轮，
不带就是一次性提问。两组新接口（会话、历史）都按 `user_id` 隔离，
**越权一律按"资源不存在"返回 404，而不是 403**——403 等于承认"这个 id 确实存在，
只是不归你"，那是一条可以被逐个 id 问出来的信息。

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
而且"这一轮到底改写了什么"在日志里是一条独立、可对照的记录。
首轮**由代码判断历史为空即原样透传**，一次模型调用都不发：让模型面对空历史去
"决定要不要改写"，它偶尔会把一个本来完整的问题改坏（补出一个历史里根本没有的年份）。

**为什么编译两张图**。langgraph 规定：挂了 checkpointer 的图必须有 `thread_id`。
一次性提问没有会话 id，硬凑一个的话，每问一句都会在 Redis 里留下一个永不回收的
线程；而 `checkpointer=None` 的图又不能带 `config`。所以 `agent.graph.get_graph(persist=...)`
按需编译并缓存两张——多轮用带 checkpointer 的那张，单轮用不带的。

**开流前必须做完的事**。SSE 的响应头在第一个事件推出去时就发出去了，此后 HTTP 状态码
已经定型；"会话不存在""这个会话不归你"若发生在流里，就只能表现为**连接被掐断**。
所以归属校验、取上文、选 checkpointer 全部挪进 `QueryService.prepare()`，在开流之前
做完。流一旦开始，所有失败都只能变成 `type=error` 事件。

**一轮一行、role=assistant**。问题与它自己的答案写在同一行里，拆成 user / assistant
两行看起来更规范，但 MySQL 的 `DATETIME` 只到秒，同一秒内写入的两行无法可靠定序——
一旦反过来，上文就会把上一轮的问题配到这一轮的 SQL 上。同一理由也决定了主键形态
（`repositories/ids.py`：13 位毫秒时间戳 + 16 位随机），排序全靠主键自己，
不依赖 `created_at`。消息行、会话活跃时间、查询历史行**一次提交**写出，
分开提交只会得到"会话详情里看得到这一轮、历史列表里却少一条"这种半份记录。

## 验证清单（阶段 7 验收）

| 验收项 | 方法 |
|---|---|
| 问"各季度销售额"后追问"只看华东"能理解上下文 | `pytest tests/test_stage7_acceptance.py -v`：断言追问生成的 SQL 里仍有第一轮那张事实表（没换主题），并且带上了 `华东` 这个新条件 |
| 指代消解改写成独立问句，首轮原样透传 | 同上（`-m requires_llm`）：直接调节点 + 真模型，断言改写结果同时含上一轮主题与本轮新条件；无历史时断言返回值与输入逐字相同 |
| 会话状态持久化，重启后可继续 | 同上：用**另一个** checkpointer 实例（新对象、新连接）去 Redis 读回该 thread 的状态 |
| 会话仅本人可见；详情返回消息列表 | 同上：换一个用户去读详情、列列表、并拿该 `session_id` 提问，三处都必须 404 |
| 历史列表分页、可按状态/关键词筛选、仅本人可见 | 同上：`page_size=1` 只返回一条；`keyword` 命中唯一一条；`status=failed` 为空（这两轮都成功了）；另一个用户看到 0 条 |
| 历史详情含问题/SQL/结果预览/解释/耗时/状态 | 同上：六项逐一核对，`result_preview` 的列与行数要与 SSE 里 `result` 事件给用户看的那一份对得上 |

验收用例**用的是真模型、真检索、真数仓**：多轮理解对不对只能由端到端结果回答，
拿桩模型测多轮，测的其实是"我们把上文拼进了提示词"，而拼错了的提示词照样能让桩
给出预期输出。用例还会真的往 conversation / message / query_history 三张表里写行，
每个用例用**本次运行专属的 user_id**（`u-acc7-<用例>-<运行随机串>`）——这些表不会
自动清空（删会话也不删历史，历史本来就是独立的），共用 id 的话"列表里该有几条"
就会被上一次运行留下的行搅乱，症状是"第一次跑绿、第二次跑红"。
`tests/test_conversation_api.py` 与 `tests/test_history_api.py` 出于同样的理由
也带运行随机后缀。

## 可解释性与可视化推荐（阶段 8）

| 文件 | 职责 |
|---|---|
| `agent/nodes/generate_explain.py` | 生成自然语言解释，推 `explain` 事件 |
| `agent/nodes/recommend_visualization.py` | 取结果、调规则、推 `viz` 事件（本文件很薄，见下） |
| `agent/visualization.py` | 图表推荐规则本身（纯函数） |
| `agent/rendering.py` 的 `result_summary_text` | 把结果集渲染成给解释节点看的摘要 |
| `prompts/generate_explain.prompt` | 解释提示词，输出纯文本 |

**图表推荐是规则，不是提示词**。TDD 6 的提示词表里列过一份 `recommend_visualization`，
实现时没有采用，理由三条：FR-VIZ-001 的验收标准本身就是一张规则表，而"符合规则"
只有把规则写进代码才验得了——交给模型的话，同一份结果今天推柱状、明天推折线；
"按结果结构推荐"本来就是结构化判断，不需要理解语义；它因此也是图上**唯一一个
不会失败的节点**（纯计算，没有上游可抖，所以 `recommend_visualization.py` 里
没有一行 try/except）。这是一处对 TDD 的有意偏离，已在 TDD §6 记明。

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
  "各季度销售额"会被认成两个度量而退回表格——不报错，只是永远画不出折线。
- **标识列（`id` / `*_id`）永远不是度量**，哪怕它是整数。否则明细查询会被算成
  "三维度两度量"，结论碰巧也是表格，但理由全错。
- **占比可以带一个原始金额列**。实测问"各地区的销售额占比"，模型返回
  `大区 / 销售额 / 销售额占比` 三列——原始金额不是干扰，占比正是由它算出来的。
  要求"恰好一个度量"会把这种最典型的占比查询判成表格。

**解释失败不判死整轮问答**：它是 P1，而 `execute_sql` 早把结果推出去了，
所以失败只告警、`explain_text` 写 NULL、`done` 照常推。注意它**没有**照抄
`resolve_reference` 的长度上限——后者的输出会灌进此后每一次 LLM 调用，
失控一行就拖慢整条链路；解释的输出是终点，只推给前端、只写进一个 TEXT 列。

**落库**：`explain_text` 写进 `message` 与 `query_history`（阶段 2 就建好了这两列，
阶段 7 如实写 NULL 并注明"由阶段 8 产出"，到此关闭）。`chart_type` **不落库**——
表结构里没有对应列，而它完全由结果结构决定，需要时用同一份 `result_preview`
重算即可，存一份反而会旧。

## 验证清单（阶段 8 验收）

| 验收项 | 方法 |
|---|---|
| 结果页展示 SQL 与解释 | `pytest tests/test_stage8_acceptance.py -v`：一次真问答里 `sql` 与 `explain` 两条事件都在且解释非空，且 `result` 排在他们前面 |
| 解释与 SQL 实际逻辑一致 | 同上：问一个带显著过滤条件的问题，先自证 SQL 里确实筛了华东，再断言解释提到了华东、且**没有**提到别的大区（编造条件比漏说条件更能说明问题） |
| 图表推荐符合规则 | 同上：四条规则各走一遍真链路，断言具体的 `chart_type`（不是"和规则函数算得一样"——那是拿规则验规则，接错线也照样绿） |
| 结果为空不推荐图表、提示"无数据" | 同上：问一个年份越界的问句（种子数据 2024 年起），断言 `row_count == 0`、`chart_type is None`、`message == "无数据"` |

规则的逐条正反例另在 `tests/test_visualization.py` 里用纯函数覆盖（不碰图、不碰 LLM），
四条规则之外的边界——整数时间列、标识列、bool 列、全 NULL 列、退化行——都在那边。

## 数据权限治理（阶段 9）

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
全新环境里 `permission` 表是空的，按白名单严格解读的话，所有角色——包括管理员——
的每一次提问都取不到任何一张表。一个刚部署完、还没人配过权限的系统不该是这个样子。
一旦某角色有了哪怕一条记录，就按白名单严格执行：**"开始配置"这个动作本身就是开关**。
它的另一面是：把某角色最后一条配置删掉，等于把它退回不设限，而不是"清空权限"。

授权语义有两条值得单独说（都由 `agent/permissions.py` 的用例钉住）：

- **一张表只要有一个字段级授权，整张表就按字段级收窄**。这样"先授整表、再收窄到几列"
  才表达得出来；反之只有表级授权（没有任何字段行）时视为整表可用，**含以后新增的字段**
  ——知识库重建后不必再配一遍。这也是 `tables` 的值用 `None` 表示"整表"、
  而不是把当前字段名列出来的原因。
- **指标按它关联的字段判可见**（口径里写着 `SUM(某字段)`，它本身就是说明书）。
  一个字段都没关联的指标在受限范围内一律不可见，并记 WARNING：没有依据能证明它只碰
  已授权的字段，宁可不给，而"指标凭空消失"是最难查的一类故障。

**两道闸门，缺一不可**。前置裁剪让模型根本看不到未授权的名字，后置兜底校验
（`validate_sql` 的第二道关）拦住模型自己编出来的表名、以及从历史轮次带进来的旧表名。
四处泄漏点都堵了：`filter_table_info` / `filter_metric_info` 两个提示词的候选集、
`generate_sql` / `correct_sql` 的表结构与指标文本、以及 `history_text`——
**引用已无权限对象的那些历史轮次会被整轮丢掉**（留半句上下文比不留更坏）。

后置校验里有两条口径是实测出来的：

- **字段要按"它出现在哪张表上"判，不能平铺成一个名字集合**。字段名在数仓里不唯一
  （`fact_order.product_id` 与 `dim_product.product_id` 同名），平铺的话
  "dim_product 只授了 brand"会让 `SELECT product_id FROM fact_order` 被判越权——
  而它碰的是一张整表授权的表。后果不是少给数据而是**模型改不掉**：问题不在 SQL 上，
  拿着同一份裁剪过的表结构重写多少次都还是那句话，连试几轮之后整句失败，
  用户看到的是"这个问法不行"。所以 `forbidden_columns` 是 `表名 → 字段名` 的分组。
- **越权提示不点名**。`error` 事件最终会推给用户，里面若带着表名，就等于向无权者
  确认了那张表存在、它大概长什么样。名字只进日志，给用户的那句话只说结果。

**一轮问答内缓存，跨请求不缓存**：网关按角色缓存算出来的范围，而一个 `QueryContext`
对应一轮问答，所以缓存的生命周期正好是这一轮。于是**没有"改完配置要清缓存"这一步**——
管理员配完，下一句提问读到的就是新配置。反过来也要注意：同一轮里三处调用（表、指标、
兜底校验）必须看到同一个范围，否则会出现"表被拦了、校验却放行"的自相矛盾中间态。

## 验证清单（阶段 9 验收）

| 验收项 | 方法 |
|---|---|
| 无权限的表/字段不出现在生成的 SQL 中 | `pytest tests/test_stage9_acceptance.py -v`：正向给一个角色开三张表，问出来的 SQL 只能引用这三张；反向只授 `dim_product` 的一个字段，拿一个它答不了的问题去问，"用户看得见的任何地方"（`sql` / `explain` / `error`）都不许出现未授权的名字 |
| admin 可配置角色-表/字段映射 | 同上：同一句问题、同一个用户，配置前后**必须问出不一样的东西**（配置前能查到订单事实表，配完之后不能）——只断言"配完不越权"在"配置根本没生效"时也是绿的 |

判定核心（`build_scope` 与 `PermissionScope`）的逐条用例另在 `tests/test_permissions.py`
里用纯函数覆盖，不碰数据库、不碰 LLM：表级/字段级的叠加、整表授权含新增字段、
字段名跨表同名、指标可见性、悬空引用（权限指向已从知识库删掉的表）。
配置接口另在 `tests/test_permission_api.py` 里覆盖（重复配置、资源不存在、非 admin 403）。


## 目录结构

```
data-agent/
├── main.py                    入口（create_app + lifespan）
├── pyproject.toml             依赖清单
├── alembic/                   数据库迁移（async 引擎，仅 meta 库）
├── conf/
│   ├── app_config.yaml        基础配置（非敏感默认值）
│   └── meta_config.yaml       元数据知识库定义（阶段 4）
├── prompts/                   提示词文件（9 个，与 TDD 6 的表格对应）
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
│   │                          会话、消息、查询历史）
│   ├── entities/              领域实体
│   ├── models/                ORM 模型（阶段 2）
│   ├── schemas/               请求/响应模型
│   ├── services/              业务编排
│   ├── core/                  配置/安全/异常/日志/响应（含 sql_security.py）
│   └── scripts/               离线脚本（init_admin、build_meta、seed_dw）
├── evals/                     评测集与评测脚本（阶段 10）
└── tests/                     单元测试
```

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
  `cryptography` 提供该能力，缺失则报 `RuntimeError: 'cryptography' package is required`，
  表现为"昨天还能连、今天连不上"。`pyproject.toml` 已显式声明该依赖。
  另外 `CREATE USER`/`GRANT` 是即时生效的 DDL，**不要**在其后 `FLUSH PRIVILEGES`——
  没有意义，只会清空认证缓存，把紧接着的下一步连接打挂。
- **内存与页面文件**：ES 默认要 ~1GB 堆，bge-m3 权重 1.07GiB，两者都需要提交内存。
  若 C 盘空间枯竭导致页面文件无法扩张，Windows 提交上限会被卡死，表现为
  `failed to allocate buffer` / `GGML_ASSERT(ctx->mem_buffer != NULL) failed`
  这类**看起来像模型损坏、实则是内存不足**的报错。排查先看提交内存而不是模型。
- **Ollama 用独立实例**：项目在 11435 端口另起一个实例（`OLLAMA_MODELS` 指向项目
  目录），与用户已有的 11434 实例互不影响。好处是模型存储位置可控、不受个人实例
  的模型增删影响。
