"""Application configuration using Pydantic Settings."""

from pydantic import model_validator
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Global application settings."""

    app_name: str = "Forum Memory Agent"
    debug: bool = False

    # Database — sync driver (psycopg2)
    # 必须通过环境变量 FM_DATABASE_URL 或 .env 文件设置
    database_url: str = ""
    database_echo: bool = False

    # Elasticsearch
    es_url: str = "http://localhost:9200"
    es_index_prefix: str = "forum_memory"
    es_enabled: bool = True
    es_username: str = ""
    es_password: str = ""
    es_verify_certs: bool = True
    es_knn_num_candidates: int = 100

    # LLM
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_main_model: str = "gpt-4o"
    llm_embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    llm_timeout: int = 60  # seconds, applies to all LLM/embedding/rerank calls

    # Custom provider (when llm_provider == "custom")
    custom_llm_url: str = ""
    custom_embed_url: str = ""
    custom_rerank_url: str = ""
    custom_api_key: str = ""
    custom_llm_model: str = ""
    custom_embed_model: str = ""
    custom_rerank_model: str = ""

    # Forum defaults
    thread_timeout_days: int = 7
    max_compress_messages: int = 10
    similarity_threshold: float = 0.75
    reranker_top_k: int = 5
    recall_top_k: int = 50

    # RAG knowledge base
    rag_base_url: str = ""
    rag_timeout: int = 30

    # JWT authentication
    jwt_secret_key: str = ""  # Required when jwt_enabled=True; set via FM_JWT_SECRET_KEY
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24
    jwt_enabled: bool = False  # Set True to require JWT; False keeps X-Employee-Id fallback

    # SSO Cookie authentication
    sso_enabled: bool = False  # Set True to enable cookie-based SSO auth
    sso_verify_url: str = ""  # Cookie verification API endpoint
    sso_ak: str = ""  # Access key for signing JWT
    sso_sk: str = ""  # Secret key for signing JWT
    sso_tenant_id: str = ""
    sso_callback_url: str = ""  # URL parameter for cookie verification
    sso_user_scope: str = ""  # User scope parameter

    # External user directory (iData)
    idata_app_token: str = ""
    idata_user_info_url: str = ""
    idata_dept_employee_url: str = ""
    idata_member_search_url: str = ""

    # File uploads
    upload_dir: str = "uploads"
    upload_max_size_mb: int = 5

    # Quality thresholds
    wrong_feedback_threshold: int = 3
    promote_useful_ratio: float = 0.8
    promote_min_feedback: int = 10
    cold_inactive_days: int = 180
    archive_inactive_days: int = 365

    model_config = {"env_file": ".env", "env_prefix": "FM_"}

    @model_validator(mode="after")
    def _validate_settings(self):
        # Database URL is required
        if not self.database_url:
            raise ValueError(
                "FM_DATABASE_URL is required. "
                "Set it in .env file or as environment variable, e.g.: "
                "FM_DATABASE_URL=postgresql://user:password@host:5432/dbname"
            )

        # LLM provider-specific validation
        if self.llm_provider == "openai" and not self.llm_api_key:
            raise ValueError("FM_LLM_API_KEY is required when llm_provider is 'openai'")
        if self.llm_provider == "custom":
            missing = []
            if not self.custom_llm_url:
                missing.append("FM_CUSTOM_LLM_URL")
            if not self.custom_embed_url:
                missing.append("FM_CUSTOM_EMBED_URL")
            if not self.custom_rerank_url:
                missing.append("FM_CUSTOM_RERANK_URL")
            if missing:
                raise ValueError(f"Required for custom provider: {', '.join(missing)}")

        # JWT validation
        if self.jwt_enabled and not self.jwt_secret_key:
            raise ValueError("FM_JWT_SECRET_KEY is required when jwt_enabled is True")

        # SSO validation
        if self.sso_enabled:
            sso_missing = []
            if not self.sso_verify_url:
                sso_missing.append("FM_SSO_VERIFY_URL")
            if not self.sso_ak:
                sso_missing.append("FM_SSO_AK")
            if not self.sso_sk:
                sso_missing.append("FM_SSO_SK")
            if sso_missing:
                raise ValueError(f"Required for SSO auth: {', '.join(sso_missing)}")

        # Lifecycle day ordering
        if self.cold_inactive_days >= self.archive_inactive_days:
            raise ValueError(
                f"cold_inactive_days ({self.cold_inactive_days}) must be < "
                f"archive_inactive_days ({self.archive_inactive_days})"
            )

        # Embedding dimension
        if self.embedding_dimension <= 0:
            raise ValueError(f"embedding_dimension must be > 0, got {self.embedding_dimension}")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
