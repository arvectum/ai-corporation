from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from src.modules.tender_operator_agent_demo.credential_resolver import (
    CredentialOwner as CredOwner,
)
from src.modules.tender_operator_agent_demo.credential_resolver import (
    resolve_getdocsip_credential,
)

PLACEHOLDER_TOKENS = {
    "",
    "replace_me",
    "replace_me_do_not_commit_real_token",
    "insert_token_here",
    "вставить_токен_сюда",
}


def _read_token_from_file(file_path: str) -> str:
    try:
        raw = Path(file_path).read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        return raw.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


_ACTIVE_TOKEN_CACHE: dict[int, str] = {}
_CREDENTIAL_OWNER_CACHE: dict[int, CredOwner] = {}

DEFAULT_LEGACY_BASE_URL = "https://int44.zakupki.gov.ru/eis-integration/services-vbs"
DEFAULT_INDIVIDUAL_BASE_URL = "https://int.zakupki.gov.ru/eis-integration/services/getDocsIP"
DEFAULT_INDIVIDUAL_XSD_URL = f"{DEFAULT_INDIVIDUAL_BASE_URL}?xsd=getDocsIP-ws-api.xsd"
DEFAULT_INDIVIDUAL_NAMESPACE = "http://zakupki.gov.ru/fz44/get-docs-ip/ws"
DEFAULT_TOKEN_HEADER_NAME = "individualPerson_token"
DEFAULT_SOAP_MODE = "PROD"
DEFAULT_ALLOWED_HOSTS = "zakupki.gov.ru,.zakupki.gov.ru,int.zakupki.gov.ru,int44.zakupki.gov.ru,int44-ttls-cert.zakupki.gov.ru"
DEFAULT_USER_AGENT = "ArvectumTenderAgent/0.1 read-only"
DEFAULT_CONTENT_TYPE = "text/xml; charset=utf-8"
DEFAULT_SOAP_ACTION_URI = "http://zakupki.gov.ru/fz44/queue/ws/get-docs-ip"
_ENV_FILES_SEEDED = False


def _validate_tls_guard() -> None:
    app_env = os.environ.get("APP_ENV") or os.environ.get("AI_CORP_APP_ENV") or "development"
    tls_verify = _read_bool("EIS_DOCS_TLS_VERIFY", False) or _read_bool("AI_CORP_EIS_DOCS_TLS_VERIFY", False)
    allow_insecure = _read_bool("EIS_ALLOW_INSECURE_TLS", True) or _read_bool("AI_CORP_EIS_ALLOW_INSECURE_TLS", True)
    if app_env.lower() == "production" and (not tls_verify or allow_insecure):
        raise RuntimeError("Production cannot run GetDocsIP with insecure TLS enabled")

TokenOwner = Literal["individual", "legal_entity"]


def _settings_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _seed_env_from_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _seed_env_from_local_files() -> None:
    global _ENV_FILES_SEEDED
    if _ENV_FILES_SEEDED:
        return
    if "pytest" in sys.modules:
        _ENV_FILES_SEEDED = True
        return
    root = _settings_root()
    _seed_env_from_file(root / ".env")
    _seed_env_from_file(root / ".env.local")
    _ENV_FILES_SEEDED = True


def _read_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_int(name: str, default: int, *, minimum: int = 1) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return max(int(value), minimum)
    except ValueError:
        return default


def _read_token_owner() -> TokenOwner:
    value = os.environ.get("ZAKUPKI_GOV_RU_SOAP_TOKEN_OWNER", "individual").strip().lower()
    if value in {"legal", "legal_entity", "company", "organization"}:
        return "legal_entity"
    return "individual"


@dataclass(frozen=True)
class ZakupkiSoapSettings:
    enabled: bool = False
    token: str = field(default="", repr=False)
    document_export_token: str = field(default="", repr=False)
    token_owner: TokenOwner = "individual"
    base_url: str = DEFAULT_LEGACY_BASE_URL
    individual_base_url: str = DEFAULT_INDIVIDUAL_BASE_URL
    individual_xsd_url: str = DEFAULT_INDIVIDUAL_XSD_URL
    individual_namespace: str = DEFAULT_INDIVIDUAL_NAMESPACE
    token_header_name: str = DEFAULT_TOKEN_HEADER_NAME
    mode: str = DEFAULT_SOAP_MODE
    disable_proxy_for_eis: bool = True
    require_direct_ru_route: bool = True
    allowed_hosts_raw: str = DEFAULT_ALLOWED_HOSTS
    user_agent: str = DEFAULT_USER_AGENT
    content_type: str = DEFAULT_CONTENT_TYPE
    use_soap_action: bool = True
    soap_action_uri: str = DEFAULT_SOAP_ACTION_URI
    search_action: str = "searchProcurements"
    details_action: str = "getProcurementDetails"
    attachments_action: str = "listAttachments"
    timeout_seconds: int = 30
    max_results: int = 10
    max_attachments: int = 20
    max_download_mb: int = 200
    trust_env_proxy: bool = False
    debug: bool = False

    @classmethod
    def from_env(cls) -> ZakupkiSoapSettings:
        _seed_env_from_local_files()
        _validate_tls_guard()
        token = os.environ.get("ZAKUPKI_GOV_RU_SOAP_TOKEN", "").strip()
        token_file = os.environ.get("ZAKUPKI_GOV_RU_SOAP_TOKEN_FILE", "").strip()
        if (not token or token.lower() in PLACEHOLDER_TOKENS) and token_file:
            token = _read_token_from_file(token_file)
        return cls(
            enabled=_read_bool("ZAKUPKI_GOV_RU_SOAP_ENABLED", False),
            token=token,
            document_export_token=os.environ.get("ZAKUPKI_GOV_RU_DOCUMENT_EXPORT_TOKEN", "").strip(),
            token_owner=_read_token_owner(),
            base_url=os.environ.get("ZAKUPKI_GOV_RU_SOAP_BASE_URL", DEFAULT_LEGACY_BASE_URL).strip()
            or DEFAULT_LEGACY_BASE_URL,
            individual_base_url=os.environ.get("ZAKUPKI_GOV_RU_SOAP_INDIVIDUAL_BASE_URL", DEFAULT_INDIVIDUAL_BASE_URL).strip()
            or DEFAULT_INDIVIDUAL_BASE_URL,
            individual_xsd_url=os.environ.get("ZAKUPKI_GOV_RU_SOAP_INDIVIDUAL_XSD_URL", DEFAULT_INDIVIDUAL_XSD_URL).strip()
            or DEFAULT_INDIVIDUAL_XSD_URL,
            individual_namespace=os.environ.get("ZAKUPKI_GOV_RU_SOAP_INDIVIDUAL_NAMESPACE", DEFAULT_INDIVIDUAL_NAMESPACE).strip()
            or DEFAULT_INDIVIDUAL_NAMESPACE,
            token_header_name=os.environ.get("ZAKUPKI_GOV_RU_SOAP_TOKEN_HEADER_NAME", DEFAULT_TOKEN_HEADER_NAME).strip()
            or DEFAULT_TOKEN_HEADER_NAME,
            mode=os.environ.get("ZAKUPKI_GOV_RU_SOAP_MODE", DEFAULT_SOAP_MODE).strip() or DEFAULT_SOAP_MODE,
            disable_proxy_for_eis=_read_bool("ZAKUPKI_GOV_RU_SOAP_DISABLE_PROXY_FOR_EIS", True),
            require_direct_ru_route=_read_bool("ZAKUPKI_GOV_RU_SOAP_REQUIRE_DIRECT_RU_ROUTE", True),
            allowed_hosts_raw=os.environ.get("ZAKUPKI_GOV_RU_SOAP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS).strip()
            or DEFAULT_ALLOWED_HOSTS,
            user_agent=os.environ.get("ZAKUPKI_GOV_RU_SOAP_USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT,
            content_type=os.environ.get("ZAKUPKI_GOV_RU_SOAP_CONTENT_TYPE", DEFAULT_CONTENT_TYPE).strip() or DEFAULT_CONTENT_TYPE,
            use_soap_action=_read_bool("ZAKUPKI_GOV_RU_SOAP_USE_SOAP_ACTION", True),
            soap_action_uri=os.environ.get("ZAKUPKI_GOV_RU_SOAP_SOAP_ACTION", DEFAULT_SOAP_ACTION_URI).strip()
            or DEFAULT_SOAP_ACTION_URI,
            search_action=os.environ.get("ZAKUPKI_GOV_RU_SOAP_SEARCH_ACTION", "searchProcurements").strip()
            or "searchProcurements",
            details_action=os.environ.get("ZAKUPKI_GOV_RU_SOAP_DETAILS_ACTION", "getProcurementDetails").strip()
            or "getProcurementDetails",
            attachments_action=os.environ.get("ZAKUPKI_GOV_RU_SOAP_ATTACHMENTS_ACTION", "listAttachments").strip()
            or "listAttachments",
            timeout_seconds=_read_int("ZAKUPKI_GOV_RU_SOAP_TIMEOUT_SECONDS", 30),
            max_results=_read_int("ZAKUPKI_GOV_RU_SOAP_MAX_RESULTS", 10),
            max_attachments=_read_int("ZAKUPKI_GOV_RU_SOAP_MAX_ATTACHMENTS", 20),
            max_download_mb=_read_int("ZAKUPKI_GOV_RU_SOAP_MAX_DOWNLOAD_MB", 200),
            trust_env_proxy=_read_bool("ZAKUPKI_GOV_RU_SOAP_TRUST_ENV_PROXY", False),
            debug=_read_bool("ZAKUPKI_GOV_RU_SOAP_DEBUG", False),
        )

    @property
    def token_configured(self) -> bool:
        return bool(self.active_token)

    @property
    def configured(self) -> bool:
        return self.enabled and self.token_configured

    @property
    def individual_mode(self) -> bool:
        return self.token_owner == "individual"

    @property
    def active_token(self) -> str:
        if self.document_export_token and self.document_export_token.strip().lower() not in PLACEHOLDER_TOKENS:
            return self.document_export_token
        if self.token and self.token.strip().lower() not in PLACEHOLDER_TOKENS:
            return self.token
        cached = _ACTIVE_TOKEN_CACHE.get(id(self))
        if cached is not None:
            return cached
        resolved = resolve_getdocsip_credential()
        if resolved.configured:
            _ACTIVE_TOKEN_CACHE[id(self)] = resolved.token
            return resolved.token
        resolved_legacy = resolve_getdocsip_credential(allow_legacy_fallback=True)
        if resolved_legacy.configured:
            _ACTIVE_TOKEN_CACHE[id(self)] = resolved_legacy.token
            return resolved_legacy.token
        _ACTIVE_TOKEN_CACHE[id(self)] = ""
        return ""

    @property
    def active_docs_endpoint(self) -> str:
        return self.individual_base_url if self.individual_mode else self.base_url

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return tuple(item.strip().lower() for item in self.allowed_hosts_raw.split(",") if item.strip())

    def safe_status(self) -> dict[str, Any]:
        reason = None
        if not self.token_configured:
            reason = "Источник ЕИС не настроен: добавьте токен сервиса ЕИС в .env.local"
        elif not self.enabled:
            reason = "Источник ЕИС не включён: установите ZAKUPKI_GOV_RU_SOAP_ENABLED=1"
        elif self.individual_mode:
            reason = None
        return {
            "source": "zakupki_gov_ru_getdocs_ip",
            "enabled": self.enabled,
            "configured": self.configured,
            "reason": reason,
            "token_owner": self.token_owner,
            "token_header_name": self.token_header_name,
            "mode": self.mode,
            "disable_proxy_for_eis": self.disable_proxy_for_eis,
            "require_direct_ru_route": self.require_direct_ru_route,
            "allowed_hosts": list(self.allowed_hosts),
            "user_agent": self.user_agent,
            "content_type": self.content_type,
            "use_soap_action": self.use_soap_action,
            "soap_action_uri": self.soap_action_uri,
            "individual_base_url_configured": bool(self.individual_base_url),
            "legacy_base_url_configured": bool(self.base_url),
            "individual_namespace": self.individual_namespace,
            "individual_xsd_url": self.individual_xsd_url,
            "active_docs_endpoint": self.active_docs_endpoint,
            "search_action": self.search_action,
            "details_action": self.details_action,
            "attachments_action": self.attachments_action,
            "timeout_seconds": self.timeout_seconds,
            "max_results": self.max_results,
            "max_attachments": self.max_attachments,
            "max_download_mb": self.max_download_mb,
            "trust_env_proxy": self.trust_env_proxy,
            "debug": self.debug,
            "legacy_mode_note": "services-vbs / legal entity mode / experimental",
        }


def is_zakupki_soap_configured(settings: ZakupkiSoapSettings | None = None) -> bool:
    return (settings or get_zakupki_soap_settings()).configured


@lru_cache(maxsize=1)
def get_zakupki_soap_settings() -> ZakupkiSoapSettings:
    return ZakupkiSoapSettings.from_env()


def clear_zakupki_soap_settings_cache() -> None:
    get_zakupki_soap_settings.cache_clear()
