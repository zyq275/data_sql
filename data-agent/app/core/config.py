"""分层配置：YAML 提供非敏感默认值，环境变量覆盖。

优先级：init 参数 > 环境变量 > .env 文件 > conf/app_config.yaml > 代码默认值。

敏感项（数据库口令、LLM api_key、JWT secret）不写入 YAML，仅经环境变量/.env 注入
（NFR-SEC-003）。嵌套字段的环境变量名用双下划线分隔，如 ``DB_META__PASSWORD``、
``LLM__API_KEY``、``JWT__SECRET_KEY``。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# app/core/config.py -> app -> data-agent/
BASE_DIR = Path(__file__).resolve().parents[2]
APP_CONFIG_FILE = BASE_DIR / "conf" / "app_config.yaml"


class AppMeta(BaseModel):
    """应用元信息。"""

    name: str = "数据通达"
    version: str = "3.0.0"
    debug: bool = False
    api_prefix: str = "/api/v1"


class DatabaseSettings(BaseModel):
    """MySQL 连接配置（meta 与 dw 各一份，见 ADR-004）。"""

    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""
    charset: str = "utf8mb4"
    echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20

    @property
    def url(self) -> str:
        """SQLAlchemy async 连接串（密码中的特殊字符做 URL 转义）。"""
        return (
            f"mysql+asyncmy://{self.user}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.database}?charset={self.charset}"
        )

    @property
    def safe_url(self) -> str:
        """日志用连接串，不含口令。"""
        return f"mysql+asyncmy://{self.user}@{self.host}:{self.port}/{self.database}"


class LLMSettings(BaseModel):
    """LLM 服务（OpenAI 兼容，模型可插拔）。"""

    model_name: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    temperature: float = 0.0
    timeout: int = 60
    max_retries: int = 2


class HTTPModelServiceSettings(BaseModel):
    """Embedding 独立模型服务（ADR-005）。"""

    host: str = ""
    model: str = ""
    timeout: int = 30


class RerankSettings(BaseModel):
    """Rerank 独立模型服务（ADR-005 / ADR-007）。

    与 Embedding 分开一个类而不是共用，是因为它多出三项**只对它成立**的配置：

    - ``enabled``：关掉后检索直接跳过精排、用 RRF 融合顺序返回。检索是问答链路
      上的必经环节，缺一个重排服务不该让整个问答失败，所以降级是设计的一部分
      而不是异常处理；开关显式化，运维不必靠"配一个连不上的地址"来表达"不要精排"。
    - ``path``：重排不是 OpenAI 兼容协议的一部分，各家路径不一致。TEI / Xinference /
      vLLM 是 ``POST /rerank``，SiliconFlow、Jina 等云服务是 ``POST /v1/rerank``。
      与其在代码里写一串候选路径去猜，不如把它变成一个配置项。
    - ``api_key``：云厂商的 rerank 需要鉴权。敏感项，只经环境变量/.env 注入
      （NFR-SEC-003），不写进 YAML、不进日志。
    """

    enabled: bool = True
    host: str = ""
    path: str = "/rerank"
    model: str = ""
    api_key: str = ""
    timeout: int = 30
    top_n: int = 0
    """精排后保留多少条；0 表示全部保留（由调用方决定最终截断）。"""


class JWTSettings(BaseModel):
    """JWT 令牌配置（访问/刷新分离，NFR-SEC-001）。"""

    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7


class QdrantSettings(BaseModel):
    """向量库（字段/指标两路召回）。

    mode=server 时连远程 Qdrant（host/port）；mode=local 时用 qdrant-client 自带的
    嵌入式引擎，数据落在 local_path，不开独立进程。两种模式走同一套 API，
    业务代码不感知差异，本地开发与容器部署可以只靠配置切换。
    """

    mode: str = "server"
    host: str = "127.0.0.1"
    port: int = 6333
    local_path: str = ""
    embedding_size: int = 1024
    column_collection: str = "data-agent-column"
    metric_collection: str = "data-agent-metric"

    @property
    def is_local(self) -> bool:
        return self.mode == "local"


class ElasticsearchSettings(BaseModel):
    """全文检索（阶段 6 起的混合检索第二路）。

    三个索引各管一类，与 Qdrant 的两个集合拼成"每条召回都有两路输入"：

    - ``value_index`` ：取值全文（阶段 4 建，ik 分词），取值召回的**唯一**来源
    - ``column_index``：字段全文（阶段 6 建），与 Qdrant 的字段向量融合
    - ``metric_index``：指标全文（阶段 6 建），与 Qdrant 的指标向量融合

    字段/指标也要有全文索引，是因为单路稠密向量分不开近义词——TDD 8.3 记录了
    实测结论：问"销售额"时 bge-m3 稳定把``sales_quantity``（销量）排在 ``GMV``
    （成交总额）之前，分差只有 0.02。而指标的 description 里写着"口语常称销售额、
    营收、卖了多少钱"，BM25 对这种字面共现极其敏感，正好补上向量那一路的短板。
    两条路的分数不可比（余弦 0~1 vs BM25 无上界），所以融合用的是名次而不是分数。
    """

    scheme: str = "http"
    host: str = "127.0.0.1"
    port: int = 9200
    value_index: str = "data-agent-value"
    column_index: str = "data-agent-column-text"
    metric_index: str = "data-agent-metric-text"
    username: str = ""
    password: str = ""

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


class RedisSettings(BaseModel):
    """会话 checkpointer 与缓存（ADR-006）。"""

    host: str = "127.0.0.1"
    port: int = 6379
    password: str = ""
    db: int = 0


class SQLSecuritySettings(BaseModel):
    """SQL 安全治理参数（BR-SQL-001/003/004，ADR-008）。"""

    max_execution_time_ms: int = 30_000
    max_result_rows: int = 10_000
    allowed_statement_prefixes: list[str] = Field(
        default_factory=lambda: ["SELECT", "WITH", "EXPLAIN", "DESC", "SHOW", "DESCRIBE"]
    )


class Settings(BaseSettings):
    """全局配置根对象。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    app: AppMeta = AppMeta()
    db_meta: DatabaseSettings = DatabaseSettings()
    db_dw: DatabaseSettings = DatabaseSettings()
    llm: LLMSettings = LLMSettings()
    embedding: HTTPModelServiceSettings = HTTPModelServiceSettings()
    rerank: RerankSettings = RerankSettings()
    jwt: JWTSettings = JWTSettings()
    qdrant: QdrantSettings = QdrantSettings()
    es: ElasticsearchSettings = ElasticsearchSettings()
    redis: RedisSettings = RedisSettings()
    sql_security: SQLSecuritySettings = SQLSecuritySettings()
    cors_origins: list[str] = Field(default_factory=list)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """环境变量优先于 YAML，YAML 优先于代码默认值。"""
        # 必须显式指定 UTF-8：Windows 默认用 GBK 打开文本文件，YAML 中的中文会解码失败
        yaml_settings = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=APP_CONFIG_FILE,
            yaml_file_encoding="utf-8",
        )
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            yaml_settings,
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例，避免重复读取配置。"""
    return Settings()
