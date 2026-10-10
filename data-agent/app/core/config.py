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

from pydantic import AliasChoices, BaseModel, Field, model_validator
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
    """会话 checkpointer 与缓存（ADR-006）。

    ``enabled`` 与 ``RerankSettings.enabled`` 同一条理由：Redis 的可用性有两种
    表达方式——"没配"和"配了但连不上"，而后者每次都要白等一轮连接超时。
    开关显式化，运维不必靠"配一个连不上的地址"来表达"本机没有 Redis"。

    关掉之后会话状态降级为进程内 MemorySaver（见 ``agent.checkpoint``）：
    单实例开发不受影响，多实例部署与进程重启后的状态恢复会失效。
    """

    enabled: bool = True
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


class LangfuseSettings(BaseModel):
    """Langfuse 追踪配置（阶段 11 / NFR-OBS）。

    这一段与其他段最不同的一点是 ``capture_content``：它不是"往哪儿发"，
    而是**"允不允许把内容发出去"**。

    - ``False``（默认）：只有节点名、模型名、token 数、耗时、命中数这些**结构**
      信息出境。业务问题原文、生成的 SQL、结果数据一个字节都不出本机。
    - ``True``：提示词与模型输出原样上报，Langfuse 上能直接看到"问了什么、
      答了什么"，排查效率高得多，代价是业务数据进了第三方 SaaS。

    默认给了保守的一侧，是因为这两个方向的**纠错成本不对称**：默认不传、
    需要时打开，只是下次改一个配置；默认传了、事后才发现不该传，数据已经在别人
    的服务器上了，删不回来。所以开关叫 ``capture_content``（"允许抓内容"）
    而不是 ``mask_content``——默认值必须等于"什么都不发"。
    """

    enabled: bool = True
    public_key: str = ""
    secret_key: str = ""
    host: str = "https://cloud.langfuse.com"
    """区域不同的域名不同：EU 是 cloud.langfuse.com，US 是 us.cloud.langfuse.com。
    密钥只在它所属区域的域名下有效，填错了得到的是 401 而不是一个说得清的错误。"""
    timeout: int = 5
    capture_content: bool = False

    @property
    def configured(self) -> bool:
        """是否可以真的建客户端。缺 key 就是没配——**不报错，只降级**。"""
        return bool(self.enabled and self.public_key and self.secret_key)


class ObservabilitySettings(BaseModel):
    """可观测性总开关（阶段 11 / NFR-OBS）。

    ``otlp_endpoint`` 是**不依赖 Langfuse** 的那条路：填了就把 span 同时送到
    任意 OTLP 后端（自建 collector、Jaeger、Tempo……）。留空则只走 Langfuse；
    两者都不用时把 ``enabled`` 关掉，进程里就不会再产生任何链路数据。
    """

    enabled: bool = True
    log_level: str = "INFO"
    otlp_endpoint: str = ""


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
    observability: ObservabilitySettings = ObservabilitySettings()
    langfuse: LangfuseSettings = LangfuseSettings()
    cors_origins: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Langfuse 凭证的另一种拼法（见下）
    #
    # Langfuse 官方 SDK 读的环境变量是**单下划线**的（LANGFUSE_PUBLIC_KEY /
    # LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL），与本项目 SECTION__FIELD 的双
    # 下划线惯例不同。这里两个都认：AliasChoices 里每一项都是同一个东西的拼法。
    #
    # 为什么迁就 SDK 的拼法：这三个名字是 Langfuse 的事实标准——官方文档、
    # 其余集成、CI 里现成的注入都长这样，为了整齐要求改名，收益只是好看；
    # 而代价是每个人第一次配的时候都要去查一遍"这个项目里它叫什么"。
    #
    # 为什么它们必须声明在**顶层**：嵌套模型里的字段指向不了"不带前缀"的变量名。
    # 实测给 langfuse.public_key 设 validation_alias 之后，连 LANGFUSE__PUBLIC_KEY
    # 这种符合惯例的拼法也读不到了（pydantic-settings 的嵌套查找被别名整个替掉）。
    # 声明在顶层之后，由下面的 validator 并回 langfuse 那一节，
    # 业务代码因此只需认一个地方：settings.langfuse.*
    # ------------------------------------------------------------------
    langfuse_public_key: str = Field(
        "", validation_alias=AliasChoices("LANGFUSE_PUBLIC_KEY", "LANGFUSE__PUBLIC_KEY")
    )
    langfuse_secret_key: str = Field(
        "", validation_alias=AliasChoices("LANGFUSE_SECRET_KEY", "LANGFUSE__SECRET_KEY")
    )
    langfuse_base_url: str = Field(
        "", validation_alias=AliasChoices("LANGFUSE_BASE_URL", "LANGFUSE_HOST", "LANGFUSE__HOST")
    )

    @model_validator(mode="after")
    def _merge_langfuse_credentials(self) -> Settings:
        """把上面三个平铺字段并进 ``langfuse`` 那一节（显式写的值优先）。"""
        if self.langfuse_public_key:
            self.langfuse.public_key = self.langfuse_public_key
        if self.langfuse_secret_key:
            self.langfuse.secret_key = self.langfuse_secret_key
        if self.langfuse_base_url:
            self.langfuse.host = self.langfuse_base_url
        return self

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
