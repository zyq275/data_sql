# 数据通达 — 后端（data-agent）

Text-to-SQL 智能数据问答 Agent。需求见《数据通达-需求规格说明书（PRD）.md》，
设计见《数据通达-技术设计文档（TDD）.md》，开发节奏见《数据通达-开发步骤文档.md》。

当前进度：**阶段 3 认证与用户管理**。

## 环境要求

- Python ≥ 3.12
- MySQL 8.x（meta 库；阶段 4 起还需要 Qdrant / Elasticsearch / Redis）
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
- 存活探针：`GET /health`
- 就绪探针：`GET /health/ready`（依赖检查在阶段 4/6/7 接入）

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
- **密码哈希不用 passlib**：passlib 最新版仍是 1.7.4（2020 年，已停维护），其 bcrypt
  后端在自检阶段会喂超 72 字节输入给 `hashpw`，与 bcrypt 4.1+ 的严格校验冲突，
  `CryptContext(schemes=["bcrypt"])` 直接构造失败。因此 `app/core/security.py`
  直连 `bcrypt` 的 `hashpw`/`checkpw`，产物仍是标准 `$2b$` 哈希。
