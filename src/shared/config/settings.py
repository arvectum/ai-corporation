from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Corporation Sprint 1 API"
    debug: bool = False
    database_url: str = "sqlite:///./ai_corporation.db"
    site_public_root: str | None = None
    allowed_hosts: str = ""
    cors_allow_origins: str = ""
    tender_pilot_basic_auth_enabled: bool = False
    tender_pilot_basic_auth_username: str | None = None
    tender_pilot_basic_auth_password: str | None = None
    pilot_auth_enabled: bool = False
    pilot_auth_username: str | None = None
    pilot_auth_password: str | None = None
    pilot_auth_protected_prefixes: str = "/api,/demo,/pilot,/customers,/docs,/redoc,/openapi.json,/health/ready"
    pilot_auth_public_paths: str = "/health"
    llm_provider: str = "stub"
    llm_model: str | None = None
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 1
    llm_allow_raw_partner_data: bool = False
    llm_store_raw_response: bool = False
    source_graph_mode: str = "legacy"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    yandex_api_key: str | None = None
    yandex_iam_token: str | None = None
    yandex_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    yandex_search_api_key: str | None = None
    yandex_search_folder_id: str | None = None
    cloudru_api_key: str | None = None
    cloudru_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    gigachat_auth_key: str | None = None
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"

    arvectum_data_dir: str = "./data"
    tender_research_enabled: bool = True
    tender_research_batch_limit: int = 10
    tender_research_eis_mode: str = "demo"
    tender_research_eis_discovery_mode: str = "registry_numbers"
    tender_research_eis_seed_file: str = "data/eis_seed/registry_numbers.txt"
    web_search_enabled: bool = False
    web_search_provider: str = "duckduckgo_html"
    web_search_max_queries_per_tender: int = 8
    web_search_max_results_per_query: int = 10
    web_search_delay_seconds: float = 3.0
    web_search_timeout_seconds: int = 20
    web_fetch_enabled: bool = True
    web_fetch_max_pages_per_tender: int = 20
    web_fetch_delay_seconds: float = 2.0
    web_fetch_timeout_seconds: int = 30
    web_fetch_max_file_size_mb: int = 25
    web_use_playwright: bool = False
    web_save_screenshots: bool = False
    web_deny_domains: str = ""
    web_allow_domains: str = ""
    document_download_max_size_mb: int = 100
    document_extract_max_chars: int = 2_000_000
    rag_chunk_size_chars: int = 1500
    rag_chunk_overlap_chars: int = 200
    rag_min_chunk_chars: int = 120
    rag_embeddings_provider: str = "hashing"
    rag_embeddings_model: str = "local-hash-v1"
    rag_embeddings_base_url: str = "http://127.0.0.1:8090/v1"
    rag_embeddings_timeout_seconds: int = 60
    rag_embeddings_batch_size: int = 16
    rag_embeddings_dimension: str | int | None = None
    rag_vector_store: str = "json"
    rag_vector_store_path: str | None = None
    rag_embedding_dimension: str | int | None = 256
    rag_use_llm: bool = False
    local_llm_base_url: str = "http://127.0.0.1:8088/v1"
    local_llm_model: str = "qwen2.5-14b"
    local_llm_timeout_seconds: int = 120

    registry_discovery_source: str = "auto"
    registry_discovery_days_back: int = 3
    registry_discovery_limit: int = 10
    public_search_enabled: bool = True
    public_search_use_playwright: bool = False
    public_search_delay_seconds: float = 3.0
    public_search_timeout_seconds: int = 30
    public_search_bypass_proxy: bool = False
    public_search_page_size: int = 30
    public_search_no_proxy_domains: str = "zakupki.gov.ru,.zakupki.gov.ru,int.zakupki.gov.ru,int44.zakupki.gov.ru"
    allow_demo_discovery: bool = True

    etp_tls_policy_path: str | None = Field(default=None, validation_alias="ARVECTUM_ETP_TLS_POLICY_PATH")
    etp_tls_enabled: bool = Field(default=False, validation_alias="ARVECTUM_ETP_TLS_ENABLED")
    etp_tls_fail_closed: bool = Field(default=True, validation_alias="ARVECTUM_ETP_TLS_FAIL_CLOSED")
    etp_proxy_bypass_enabled: bool = Field(default=True, validation_alias="ARVECTUM_ETP_PROXY_BYPASS_ENABLED")

    # Recovered Hermes analyser remains opt-in and is never a required customer flow.
    hermes_base_url: str = "http://127.0.0.1:8099"
    hermes_timeout_seconds: int = 120
    hermes_enabled: bool = False

    # Storage capacity guardrails (ARV-010)
    arvectum_storage_root: str | None = Field(default=None, validation_alias="ARVECTUM_STORAGE_ROOT")
    arvectum_storage_warning_percent: int = Field(default=70, validation_alias="ARVECTUM_STORAGE_WARNING_PERCENT", ge=0, le=100)
    arvectum_storage_critical_percent: int = Field(default=80, validation_alias="ARVECTUM_STORAGE_CRITICAL_PERCENT", ge=0, le=100)
    arvectum_storage_ingestion_protected_percent: int = Field(default=90, validation_alias="ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT", ge=0, le=100)

    model_config = SettingsConfigDict(
        env_prefix="AI_CORP_",
        env_file=[".env", ".env.local"],
        extra="ignore",
    )

    def allowed_hosts_list(self) -> list[str]:
        return _split_csv(self.allowed_hosts)

    def cors_allow_origins_list(self) -> list[str]:
        return _split_csv(self.cors_allow_origins)

    def pilot_auth_is_enabled(self) -> bool:
        return self.pilot_auth_enabled or self.tender_pilot_basic_auth_enabled

    def pilot_auth_credentials(self) -> tuple[str | None, str | None]:
        return (self.pilot_auth_username or self.tender_pilot_basic_auth_username, self.pilot_auth_password or self.tender_pilot_basic_auth_password)

    def pilot_auth_password_safe(self) -> bool:
        password = self.pilot_auth_credentials()[1] or ""
        return bool(password) and password.lower() not in {"change_me", "change_me_local_only", "replace_me", "replace_me_do_not_commit_real_password"}

    def site_public_root_path(self) -> Path | None:
        if not self.site_public_root:
            return None
        return Path(self.site_public_root).expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _split_csv(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]
