"""
Application settings.

DATABASE_URL in .env can be any of:
  postgresql://user:pass@host/db
  postgresql+asyncpg://user:pass@host/db
  (including Azure percent-encoded passwords like St%24%400987)

`settings.async_db_url` returns a properly constructed SQLAlchemy URL object
that bypasses the asyncpg DSN parser's intolerance of % chars in passwords.
"""
from pathlib import Path
from urllib.parse import urlparse, unquote

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_DEFAULT_FRONTEND_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://sales-management.azurewebsites.net",
)


class Settings(BaseSettings):
    DATABASE_URL: str
    DATABASE_URL2: str | None = None
    DATABASE_URL3: str
    DATABASE_URL4: str | None = None
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 60
    OPENAI_API_KEY: str | None = None
    FRONTEND_URL: str = "http://localhost:5173"
    FRONTEND_URLS: str | None = None
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USE_TLS: bool = False
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    FROM_EMAIL: str | None = None
    AZURE_CONNECTION_STRING: str | None = None
    AZURE_RFQ_FILES_CONTAINER: str = "rfq-files"
    CRON_TOKEN: str | None = None

    # Microsoft Graph / SharePoint integration
    AZURE_TENANT_ID: str | None = None
    AZURE_CLIENT_ID: str | None = None
    AZURE_CLIENT_SECRET: str | None = None
    SHAREPOINT_GROUP_NAME: str = "Product Development & Costing"
    SHAREPOINT_LIBRARY_NAME: str = "RFQ_Costing Files"
    SHAREPOINT_RFQ_ROOT_FOLDER: str = "RFQ"
    SHAREPOINT_SITE_ID: str | None = None
    SHAREPOINT_DRIVE_ID: str | None = None
    # Set to false to disable SharePoint sync without removing credentials
    SHAREPOINT_SYNC_ENABLED: bool = True
    # Set to true locally to surface Graph errors instead of swallowing them
    SHAREPOINT_SYNC_RAISE_ERRORS: bool = False

    # Workspace Agent GPT — AI pre-validation before sending the validator email.
    # Required to enable AI validation; submission proceeds without it if not set.
    AGENT_ACCESS_TOKEN: str | None = None
    WORKSPACE_AGENT_TRIGGER_ID: str | None = None
    WORKSPACE_AGENT_BASE_URL: str = "https://api.chatgpt.com/v1"
    AI_VALIDATION_CALLBACK_TOKEN: str | None = None

    # Public-facing backend URL used to build proxy URLs for external services.
    BACKEND_BASE_URL: str = "https://sales-app-backend.azurewebsites.net"

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE))

    @property
    def frontend_url(self) -> str:
        return self.FRONTEND_URL.rstrip("/")

    @property
    def frontend_urls(self) -> list[str]:
        candidates: list[str] = [*_DEFAULT_FRONTEND_ORIGINS, self.FRONTEND_URL]
        if self.FRONTEND_URLS:
            candidates.extend(part.strip() for part in self.FRONTEND_URLS.split(","))

        normalized: list[str] = []
        seen: set[str] = set()
        for origin in candidates:
            value = (origin or "").strip().rstrip("/")
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @property
    def smtp_host(self) -> str:
        return (self.SMTP_HOST or "").strip("\"' ")

    @property
    def smtp_port(self) -> int:
        return int(self.SMTP_PORT or 587)

    @property
    def smtp_use_tls(self) -> bool:
        return bool(self.SMTP_USE_TLS)

    @property
    def smtp_user(self) -> str:
        return (self.SMTP_USER or "").strip("\"' ")

    @property
    def smtp_password(self) -> str:
        return (self.SMTP_PASSWORD or "").strip("\"' ")

    @property
    def from_email(self) -> str:
        return (self.FROM_EMAIL or "").strip("\"' ")

    @property
    def azure_connection_string(self) -> str:
        return (self.AZURE_CONNECTION_STRING or "").strip("\"' ")

    @property
    def azure_rfq_files_container(self) -> str:
        return (self.AZURE_RFQ_FILES_CONTAINER or "rfq-files").strip().lower()

    # --- SharePoint / Microsoft Graph accessors ---

    @property
    def azure_tenant_id(self) -> str:
        return (self.AZURE_TENANT_ID or "").strip()

    @property
    def azure_client_id(self) -> str:
        return (self.AZURE_CLIENT_ID or "").strip()

    @property
    def azure_client_secret(self) -> str:
        return (self.AZURE_CLIENT_SECRET or "").strip()

    @property
    def sharepoint_group_name(self) -> str:
        return (self.SHAREPOINT_GROUP_NAME or "").strip()

    @property
    def sharepoint_library_name(self) -> str:
        return (self.SHAREPOINT_LIBRARY_NAME or "").strip()

    @property
    def sharepoint_rfq_root_folder(self) -> str:
        return (self.SHAREPOINT_RFQ_ROOT_FOLDER or "RFQ").strip()

    @property
    def sharepoint_site_id(self) -> str:
        return (self.SHAREPOINT_SITE_ID or "").strip()

    @property
    def sharepoint_drive_id(self) -> str:
        return (self.SHAREPOINT_DRIVE_ID or "").strip()

    @property
    def sharepoint_sync_enabled(self) -> bool:
        return bool(self.SHAREPOINT_SYNC_ENABLED)

    @property
    def sharepoint_sync_raise_errors(self) -> bool:
        return bool(self.SHAREPOINT_SYNC_RAISE_ERRORS)

    @property
    def agent_access_token(self) -> str:
        return (self.AGENT_ACCESS_TOKEN or "").strip("\"' ")

    @property
    def workspace_agent_trigger_id(self) -> str:
        return (self.WORKSPACE_AGENT_TRIGGER_ID or "").strip("\"' ")

    @property
    def workspace_agent_base_url(self) -> str:
        base_url = (self.WORKSPACE_AGENT_BASE_URL or "https://api.chatgpt.com/v1").strip("\"' ")
        return base_url.rstrip("/")

    @property
    def workspace_agent_endpoint(self) -> str:
        trigger_id = self.workspace_agent_trigger_id
        if not trigger_id:
            return ""
        return f"{self.workspace_agent_base_url}/workspace_agents/{trigger_id}/trigger"

    @property
    def ai_validation_callback_token(self) -> str:
        return (self.AI_VALIDATION_CALLBACK_TOKEN or "").strip("\"' ")

    @property
    def backend_base_url(self) -> str:
        return (self.BACKEND_BASE_URL or "https://sales-app-backend.azurewebsites.net").strip().rstrip("/")

    @staticmethod
    def _build_async_db_url(raw_url: str | None) -> URL | None:
        raw = str(raw_url or "").strip("\"' ")
        if not raw:
            return None
        parsed = urlparse(raw)
        return URL.create(
            drivername="postgresql+asyncpg",
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            host=parsed.hostname,
            port=parsed.port or 5432,
            database=(parsed.path or "").lstrip("/"),
        )

    @property
    def async_db_url(self) -> URL:
        url = self._build_async_db_url(self.DATABASE_URL)
        if url is None:
            raise ValueError("DATABASE_URL is not configured.")
        return url

    @property
    def async_db_url2(self) -> URL | None:
        return self._build_async_db_url(self.DATABASE_URL2)

    @property
    def async_db_url4(self) -> URL | None:
        return self._build_async_db_url(self.DATABASE_URL4)

    @property
    def async_db_url3(self) -> URL:
        url = self._build_async_db_url(self.DATABASE_URL3)
        if url is None:
            raise ValueError("DATABASE_URL3 is not configured.")
        return url


settings = Settings()
