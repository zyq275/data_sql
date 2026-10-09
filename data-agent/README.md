# 数据通达 — 后端（data-agent）

Text-to-SQL 智能数据问答 Agent。需求见《数据通达-需求规格说明书（PRD）.md》，
设计见《数据通达-技术设计文档（TDD）.md》，开发节奏见《数据通达-开发步骤文档.md》。

当前进度：**阶段 4 知识库构建**。

## 环境要求

- Python ≥ 3.12
- MySQL 8.x（meta 库 + dw 库）
- 阶段 4 起还需要：
  - **Qdrant** —— 或用 `qdrant.mode=local` 走嵌入式模式（见下）
  - **Elasticsearch 8.x + analysis-ik 插件** —— 版本必须完全一致
  - **Embedding HTTP 服务** —— 本项目用 Ollama 提供 bge-m3（ADR-005：不在应用内做本地推理）
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
而不是等到第一次登录才 500。

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
- 就绪探针：`GET /health/ready`（逐项探测 MySQL meta/dw、Qdrant、ES、Embedding；
  任一不可用返回 503，并列出每一项的状态与失败原因）

就绪探针每项有独立超时（默认 3s，Embedding 10s——冷启动要加载权重）。
探针挂起比探针失败更糟：编排器等不到结论，既不敢摘流量也不敢放流量。
Redis 到阶段 6 才接入，当前如实报 `skipped` 而不是伪报 ok。

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

分两类：配置解析、探针骨架、口令脱敏这类**纯逻辑测试不需要任何外部服务**，
随时可跑；涉及真实存储与知识库的用例标了 `requires_stack`，外部服务没起时
**跳过并说明缺什么**，而不是失败——失败会掩盖真正的问题。

跳过只判定「进程有没有在监听」：服务在跑但口令错、知识库没建、字段对不上，
那是真问题，测试必须失败，不会被跳过掩盖。

## 验证清单（阶段 1 验收）

| 验收项 | 方法 |
|---|---|
| 后端启动成功，`/health` 返回 200 | `curl http://127.0.0.1:8000/health` |
| 配置能从环境变量读取 | `APP__DEBUG=true` 时 `/health` 的 `debug` 字段变为 `true` |
| CORS 通配符被拦截 | 生产模式（`APP__DEBUG=false`）下设 `CORS_ORIGINS=["*"]`，启动即报错 |
| Alembic 能执行一次空迁移 | `alembic upgrade head`（需先建库并配置口令） |

## 接口清单（阶段 3）

| 方法 | 路径 | 鉴权 |
|---|---|---|
| POST | `/api/v1/auth/login` | 无 |
| POST | `/api/v1/auth/refresh` | 无（refresh_token 走请求体） |
| GET | `/api/v1/auth/me` | Bearer |
| PUT | `/api/v1/auth/password` | Bearer |
| GET | `/api/v1/admin/users` | admin（支持 `page`/`page_size`/`keyword`） |
| POST | `/api/v1/admin/users` | admin |
| PUT | `/api/v1/admin/users/{id}/role` | admin |
| PUT | `/api/v1/admin/users/{id}/status` | admin |

响应统一为 `{"code": 0, "message": "ok", "data": ...}`；出错时 `code` 为业务码
（40000 参数/业务规则、40100 认证、40101 令牌过期、40102 令牌无效、40300 权限、
40400 资源、50000 服务），HTTP 状态码按类别同步返回。`/health` 与 `/health/ready`
是探针，按约定返回裸结构、不套信封。

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

### 2. 准备三个外部服务

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

### 3. 构建

```bash
.venv/Scripts/python.exe -m app.scripts.build_meta                  # 全量重建 MySQL + Qdrant + ES
.venv/Scripts/python.exe -m app.scripts.build_meta --no-value-index # 只重建 MySQL + Qdrant
.venv/Scripts/python.exe -m app.scripts.build_meta --embed-only     # 配置没变，只重建向量
```

构建是**全量重建**：先清空四张知识表与两个集合、索引再写入。MySQL 主键由
`uuid5(命名空间, 自然键)` 确定性生成，Qdrant 点 id 与 ES 文档 id 直接复用同一个
id，因此重建后 id 不变（`permission.resource_id` 不会失效），也不会产生副本。

结束后会做入库自检——核对"配置声明"与"实际入库"的行数，不一致直接失败。

## 目录结构

```
data-agent/
├── main.py                    入口（create_app + lifespan）
├── pyproject.toml             依赖清单
├── alembic/                   数据库迁移（async 引擎，仅 meta 库）
├── conf/
│   ├── app_config.yaml        基础配置（非敏感默认值）
│   └── meta_config.yaml       元数据知识库定义（阶段 4）
├── prompts/                   提示词文件（阶段 5 起）
├── app/
│   ├── api/                   接口层：middleware / health / v1 路由
│   ├── agent/                 智能体层：graph、state、nodes（阶段 5）
│   ├── retrievers/            混合检索与 rerank（阶段 6）
│   ├── clients/               外部连接生命周期管理
│   ├── repositories/          数据访问层
│   ├── entities/              领域实体
│   ├── models/                ORM 模型（阶段 2）
│   ├── schemas/               请求/响应模型
│   ├── services/              业务编排
│   ├── core/                  配置/安全/异常/日志/响应
│   └── scripts/               离线脚本（init_admin；阶段 4 起加知识库构建）
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
