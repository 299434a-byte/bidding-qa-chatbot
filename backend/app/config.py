
"""config：全局配置汇总（读 .env 与环境变量，frozen dataclass 单例）。

职责：
  - 用 _str/_int/_float/_bool/_path 解析器集中读取全部配置键
  - 提供带缓存的 get_settings() 单例（改 .env 必须重启生效）
  - 汇总检索、重排、限流、缓存、各数据库连接等默认值

依赖：无（python-dotenv 第三方）
被谁调用：backend 绝大多数模块（24 个）
"""

from __future__ import annotations

import os

from dataclasses import dataclass, replace

from functools import lru_cache

from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

load_dotenv(ENV_FILE)

DEFAULT_TABLE_LABELS = {
    "bidding_notices": "招标公告",
    "bidding_project": "招投标项目",
    "companies": "公司信息",
}


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return default if value is None or not value.strip() else value.strip()


def _int(key: str, default: int) -> int:
    raw = _str(key)
    try:
        return int(raw)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    raw = _str(key)
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    """布尔开关：'1/true/yes/on' 为真，'0/false/no/off' 为假，其余回落默认值。"""
    raw = _str(key).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _path(key: str, default: str) -> Path:
    """相对路径统一以项目根目录为基准解析。"""
    raw = Path(_str(key, default))
    return raw if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()


def _parse_table_labels(raw: str) -> dict[str, str]:
    labels = dict(DEFAULT_TABLE_LABELS)
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, label = item.partition("=")
        if name.strip() and label.strip():
            labels[name.strip()] = label.strip()
    return labels


def _parse_primary_keys(raw: str) -> dict[str, str]:
    """解析「表名=列名」映射，用于给「无主键表」声明主键兜底。"""
    result: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        table, _, column = item.partition("=")
        if table.strip() and column.strip():
            result[table.strip()] = column.strip()
    return result


@dataclass(frozen=True)
class Settings:
    """运行期配置快照。"""

    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    mysql_charset: str
    mysql_connect_timeout: int
    kb_tables: tuple[str, ...]
    table_labels: dict[str, str]
    kb_source: str
    kb_primary_keys: dict[str, str]

    qdrant_path: Path
    qdrant_collection: str

    embed_model: str
    embed_dim: int
    embed_batch_size: int
    embed_query_prefix: str

    bm25_index_path: Path
    bm25_k1: float
    bm25_b: float

    retrieve_dense_top_k: int
    retrieve_sparse_top_k: int
    retrieve_final_top_k: int
    rrf_k: int
    dense_min_score: float
    coverage_min: float

    rerank_candidates: int
    rerank_model: str
    rerank_max_chars: int
    rerank_timeout: int
    rerank_mode: str
    rerank_auto_gap: float
    rerank_auto_min_dense: float

    max_doc_chars: int
    chunk_size: int
    chunk_overlap: int

    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_model_display: str
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout: int

    agent_model: str
    agent_max_tokens: int
    agent_history_turns: int
    agent_max_steps: int

    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_database: str
    pg_schema: str
    pg_tables: tuple[str, ...]
    pg_connect_timeout: int
    pg_statement_timeout: int
    pg_max_rows: int
    pg_model: str
    pg_max_tokens: int
    pg_timeout: int
    pg_schema_ttl: int

    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str
    neo4j_timeout: int
    neo4j_query_timeout: int
    neo4j_max_rows: int
    graph_model: str
    graph_max_tokens: int
    graph_timeout: int
    graph_schema_ttl: int

    zhipuai_api_key: str
    zhipu_base_url: str
    vision_model: str
    vision_timeout: int
    vision_max_bytes: int

    tavily_api_key: str
    tavily_search_depth: str
    tavily_max_results: int
    tavily_timeout: int
    web_search_default: bool

    history_enabled: bool
    history_db_path: Path
    history_page_size: int
    history_search_limit: int
    history_title_max_chars: int
    history_max_message_chars: int

    auth_enabled: bool
    auth_token_ttl_days: int
    auth_pbkdf2_iterations: int
    auth_max_login_attempts: int
    auth_lockout_seconds: int
    auth_admin_users: tuple[str, ...]

    cache_db_path: Path
    cache_ttl: int
    metrics_db_path: Path
    log_level: str
    log_file_path: Path

    ratelimit_capacity: int
    ratelimit_refill_per_sec: float

    cors_origins: tuple[str, ...]

    deep_max_steps: int
    deep_step_max_steps: int
    deep_reflect_rounds: int
    deep_max_tokens: int
    deep_timeout: int
    deep_observation_chars: int
    deep_tool_call_limit: int
    deep_retrieval_limit: int
    deep_llm_call_limit: int

    @property
    def llm_configured(self) -> bool:
        """未配置时不调用大模型，自动降级为「仅检索」模式。"""
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def mysql_configured(self) -> bool:
        return bool(self.mysql_host and self.mysql_user)

    @property
    def rerank_configured(self) -> bool:
        """重排同样走大模型接口；未配置大模型时自动跳过重排，沿用 RRF 顺序。"""
        return self.llm_configured

    @property
    def rerank_model_or_default(self) -> str:
        return self.rerank_model or self.llm_model

    @property
    def agent_configured(self) -> bool:
        """Agent 依赖大模型；未配置时 /api/agent/chat 直接降级为纯 RAG 通道。"""
        return self.llm_configured

    @property
    def agent_model_or_default(self) -> str:
        return self.agent_model or self.llm_model

    @property
    def pg_configured(self) -> bool:
        """PostgreSQL 查询工具只在连接信息齐备时才注册给 Agent。"""
        return bool(self.pg_host and self.pg_user and self.pg_password and self.pg_database)

    @property
    def pg_model_or_default(self) -> str:
        return self.pg_model or self.llm_model

    @property
    def llm_display_name(self) -> str:
        """界面展示用的大模型名：优先 LLM_MODEL_DISPLAY，否则回落到真实 model id。"""
        return self.llm_model_display or self.llm_model

    @property
    def graph_configured(self) -> bool:
        """Neo4j 图谱查询工具只在连接信息齐备时才注册给 Agent。"""
        return bool(self.neo4j_uri and self.neo4j_user and self.neo4j_password)

    @property
    def graph_model_or_default(self) -> str:
        return self.graph_model or self.llm_model

    @property
    def vision_configured(self) -> bool:
        """图片解析依赖智谱 API Key；未配置时 /analyze-image 返回 502。"""
        return bool(self.zhipuai_api_key)

    @property
    def web_search_configured(self) -> bool:
        """联网搜索工具只在配置了 Tavily Key 时才注册给 Agent。"""
        return bool(self.tavily_api_key)

    @property
    def history_configured(self) -> bool:
        """对话历史可用需同时开启「历史存储」与「账号体系」——历史按用户分区，没有用户就没法隔离。"""
        return bool(self.history_enabled and self.auth_enabled)

    def label_of(self, table: str) -> str:
        return self.table_labels.get(table, table)

    @property
    def kb_source_is_postgres(self) -> bool:
        """True = 走 PG 数据源；False = 走 MySQL（默认，保持改动前行为）。"""
        return self.kb_source.strip().lower() in {"pg", "postgres", "postgresql"}


def _build_settings() -> Settings:
    tables = tuple(
        t.strip() for t in _str("KB_TABLES", "bidding_notices,bidding_project,companies").split(",") if t.strip()
    )
    cors_origins = tuple(
        o.strip()
        for o in _str("CORS_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000").split(",")
        if o.strip()
    )
    return Settings(
        mysql_host=_str("MYSQL_HOST", "127.0.0.1"),
        mysql_port=_int("MYSQL_PORT", 3306),
        mysql_user=_str("MYSQL_USER"),
        mysql_password=_str("MYSQL_PASSWORD"),
        mysql_database=_str("MYSQL_DATABASE", "bidding_data"),
        mysql_charset=_str("MYSQL_CHARSET", "utf8mb4"),
        mysql_connect_timeout=_int("MYSQL_CONNECT_TIMEOUT", 10),
        kb_tables=tables,
        table_labels=_parse_table_labels(_str("TABLE_LABELS")),
        kb_source=_str("KB_SOURCE", "mysql"),
        kb_primary_keys=_parse_primary_keys(_str("KB_PRIMARY_KEYS")),
        qdrant_path=_path("QDRANT_PATH", "./data/qdrant"),
        qdrant_collection=_str("QDRANT_COLLECTION", "bidding_kb"),
        embed_model=_str("EMBED_MODEL", "BAAI/bge-small-zh-v1.5"),
        embed_dim=_int("EMBED_DIM", 512),
        embed_batch_size=_int("EMBED_BATCH_SIZE", 32),
        embed_query_prefix=_str("EMBED_QUERY_PREFIX", "为这个句子生成表示以用于检索相关文章："),
        bm25_index_path=_path("BM25_INDEX_PATH", "./data/bm25_index.json"),
        bm25_k1=_float("BM25_K1", 1.5),
        bm25_b=_float("BM25_B", 0.75),
        retrieve_dense_top_k=_int("RETRIEVE_DENSE_TOP_K", 10),
        retrieve_sparse_top_k=_int("RETRIEVE_SPARSE_TOP_K", 10),
        retrieve_final_top_k=_int("RETRIEVE_FINAL_TOP_K", 5),
        rrf_k=_int("RRF_K", 60),
        dense_min_score=_float("DENSE_MIN_SCORE", 0.35),
        coverage_min=_float("COVERAGE_MIN", 0.45),
        rerank_candidates=_int("RERANK_CANDIDATES", 10),
        rerank_model=_str("RERANK_MODEL"),
        rerank_max_chars=_int("RERANK_MAX_CHARS", 300),
        rerank_timeout=_int("RERANK_TIMEOUT", 45),
        rerank_mode=_str("RERANK_MODE", "auto").lower(),
        rerank_auto_gap=_float("RERANK_AUTO_GAP", 0.1),
        rerank_auto_min_dense=_float("RERANK_AUTO_MIN_DENSE", 0.35),
        max_doc_chars=_int("MAX_DOC_CHARS", 1200),
        chunk_size=_int("CHUNK_SIZE", 500),
        chunk_overlap=_int("CHUNK_OVERLAP", 80),
        llm_base_url=_str("LLM_BASE_URL"),
        llm_api_key=_str("LLM_API_KEY"),
        llm_model=_str("LLM_MODEL"),
        llm_model_display=_str("LLM_MODEL_DISPLAY"),
        llm_temperature=_float("LLM_TEMPERATURE", 0.2),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 1024),
        llm_timeout=_int("LLM_TIMEOUT", 60),
        agent_model=_str("AGENT_MODEL"),
        agent_max_tokens=_int("AGENT_MAX_TOKENS", 2048),
        agent_history_turns=_int("AGENT_HISTORY_TURNS", 6),
        agent_max_steps=_int("AGENT_MAX_STEPS", 6),
        pg_host=_str("PG_HOST", "127.0.0.1"),
        pg_port=_int("PG_PORT", 5432),
        pg_user=_str("PG_USER"),
        pg_password=_str("PG_PASSWORD"),
        pg_database=_str("PG_DATABASE", "chatbot"),
        pg_schema=_str("PG_SCHEMA", "public"),
        pg_tables=tuple(t.strip() for t in _str("PG_TABLES").split(",") if t.strip()),
        pg_connect_timeout=_int("PG_CONNECT_TIMEOUT", 10),
        pg_statement_timeout=_int("PG_STATEMENT_TIMEOUT", 15),
        pg_max_rows=_int("PG_MAX_ROWS", 50),
        pg_model=_str("PG_MODEL"),
        pg_max_tokens=_int("PG_MAX_TOKENS", 3072),
        pg_timeout=_int("PG_TIMEOUT", 45),
        pg_schema_ttl=_int("PG_SCHEMA_TTL", 600),
        neo4j_uri=_str("NEO4J_URI"),
        neo4j_user=_str("NEO4J_USER"),
        neo4j_password=_str("NEO4J_PASSWORD"),
        neo4j_database=_str("NEO4J_DATABASE", "neo4j"),
        neo4j_timeout=_int("NEO4J_TIMEOUT", 30),
        neo4j_query_timeout=_int("NEO4J_QUERY_TIMEOUT", 30),
        neo4j_max_rows=_int("NEO4J_MAX_ROWS", 50),
        graph_model=_str("GRAPH_MODEL"),
        graph_max_tokens=_int("GRAPH_MAX_TOKENS", 3072),
        graph_timeout=_int("GRAPH_TIMEOUT", 45),
        graph_schema_ttl=_int("GRAPH_SCHEMA_TTL", 600),
        zhipuai_api_key=_str("ZHIPUAI_API_KEY"),
        zhipu_base_url=_str("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        vision_model=_str("VISION_MODEL", "glm-4.6v-flashx"),
        vision_timeout=_int("VISION_TIMEOUT", 60),
        vision_max_bytes=_int("VISION_MAX_BYTES", 10 * 1024 * 1024),
        tavily_api_key=_str("TAVILY_API_KEY"),
        tavily_search_depth=_str("TAVILY_SEARCH_DEPTH", "basic"),
        tavily_max_results=_int("TAVILY_MAX_RESULTS", 5),
        tavily_timeout=_int("TAVILY_TIMEOUT", 20),
        web_search_default=_bool("WEB_SEARCH_DEFAULT", True),
        history_enabled=_bool("HISTORY_ENABLED", True),
        history_db_path=_path("HISTORY_DB_PATH", "./data/conversations.db"),
        history_page_size=_int("HISTORY_PAGE_SIZE", 20),
        history_search_limit=_int("HISTORY_SEARCH_LIMIT", 20),
        history_title_max_chars=_int("HISTORY_TITLE_MAX_CHARS", 30),
        history_max_message_chars=_int("HISTORY_MAX_MESSAGE_CHARS", 20000),
        auth_enabled=_bool("AUTH_ENABLED", True),
        auth_token_ttl_days=_int("AUTH_TOKEN_TTL_DAYS", 30),
        auth_pbkdf2_iterations=_int("AUTH_PBKDF2_ITERATIONS", 200000),
        auth_max_login_attempts=_int("AUTH_MAX_LOGIN_ATTEMPTS", 5),
        auth_lockout_seconds=_int("AUTH_LOCKOUT_SECONDS", 300),
        auth_admin_users=tuple(u.strip() for u in _str("AUTH_ADMIN_USERS").split(",") if u.strip()),
        cache_db_path=_path("CACHE_DB_PATH", "./backend/data/answer_cache.db"),
        cache_ttl=_int("CACHE_TTL", 3600),
        metrics_db_path=_path("METRICS_DB_PATH", "./backend/data/metrics.db"),
        log_level=_str("LOG_LEVEL", "INFO"),
        log_file_path=_path("LOG_FILE_PATH", "./backend/data/app.log"),
        ratelimit_capacity=_int("RATELIMIT_CAPACITY", 10),
        ratelimit_refill_per_sec=_float("RATELIMIT_REFILL_PER_SEC", 0.5),
        cors_origins=cors_origins,
        deep_max_steps=_int("DEEP_MAX_STEPS", 4),
        deep_step_max_steps=_int("DEEP_STEP_MAX_STEPS", 6),
        deep_reflect_rounds=_int("DEEP_REFLECT_ROUNDS", 1),
        deep_max_tokens=_int("DEEP_MAX_TOKENS", 3072),
        deep_timeout=_int("DEEP_TIMEOUT", 120),
        deep_observation_chars=_int("DEEP_OBSERVATION_CHARS", 1200),
        deep_tool_call_limit=_int("DEEP_TOOL_CALL_LIMIT", 12),
        deep_retrieval_limit=_int("DEEP_RETRIEVAL_LIMIT", 8),
        deep_llm_call_limit=_int("DEEP_LLM_CALL_LIMIT", 10),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _build_settings()


# 启动告警要核对的敏感配置键（对照 .env.example 的同名示例值）
TEMPLATE_DEFAULT_KEYS = ("LLM_API_KEY", "PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY", "ZHIPUAI_API_KEY")


@lru_cache(maxsize=1)
def _env_example_values() -> dict[str, str]:
    """解析 .env.example 的 KEY=VALUE（跳过注释与空行）；文件缺失返回空表。"""
    try:
        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def template_default_keys(s: Settings, example: dict[str, str] | None = None) -> list[str]:
    """返回「当前生效值仍等于 .env.example 示例值」的配置键。

    空值也判命中：.env.example 里没给示例密钥的键，当前值仍是空就说明
    「从没改过模板」（未配置 + 未替换），同样如实报进风险列表。
    只用于启动 WARNING 与 /api/health 的风险标记，绝不影响启动与请求。
    example 可注入（测试用），缺省读项目根的 .env.example。
    """
    env_example = _env_example_values() if example is None else example
    hits: list[str] = []
    for key in TEMPLATE_DEFAULT_KEYS:
        if key not in env_example:
            continue
        current = str(getattr(s, key.lower(), "") or "").strip()
        if current == env_example[key].strip():
            hits.append(key)
    return hits


class ModelOverrideError(ValueError):
    """请求体 model 字段非法或目标供应商未配置（绝不静默回退默认模型）。"""


def resolve_model_settings(s: Settings, model: str | None) -> Settings:
    """把请求体的可选 model 字段解析成本轮生效的 Settings。

    - 未填 / 空串：原样返回（用 .env 默认供应商与模型）；
    - ``glm-*``：切到智谱 OpenAI 兼容端点（ZHIPU_BASE_URL + ZHIPUAI_API_KEY）；
      未配 Key 时抛 ModelOverrideError——请求了智谱就绝不静默换回默认供应商；
    - 其他值：抛 ModelOverrideError（不支持的模型名，不猜测供应商）。

    切换只影响生成模型：llm_model 与 agent_model（留空即跟随 llm_model）；
    重排 / SQL / 图谱各有独立模型配置，不随请求切换。
    """
    name = (model or "").strip()
    if not name:
        return s
    if name.lower().startswith("glm-"):
        if not s.zhipu_base_url:
            raise ModelOverrideError("未配置 ZHIPU_BASE_URL，无法使用智谱模型（不会静默回退默认模型）")
        if not s.zhipuai_api_key:
            raise ModelOverrideError(
                f"请求指定了智谱模型「{name}」，但后端未配置 ZHIPUAI_API_KEY，已拒绝切换（不会静默回退默认模型）"
            )
        return replace(
            s,
            llm_base_url=s.zhipu_base_url,
            llm_api_key=s.zhipuai_api_key,
            llm_model=name,
            llm_model_display=name,
            agent_model="",
        )
    raise ModelOverrideError(f"不支持的模型「{name}」：目前支持留空（.env 默认）或 glm-*（智谱）")