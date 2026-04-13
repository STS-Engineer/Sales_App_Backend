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
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    OPENAI_API_KEY: str | None = None
    FRONTEND_URL: str = "http://localhost:5173"
    FRONTEND_URLS: str | None = None
    AZURE_CONNECTION_STRING: str | None = None
    AZURE_RFQ_FILES_CONTAINER: str = "rfq-files"

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
    def azure_connection_string(self) -> str:
        return (self.AZURE_CONNECTION_STRING or "").strip("\"' ")

    @property
    def azure_rfq_files_container(self) -> str:
        return (self.AZURE_RFQ_FILES_CONTAINER or "rfq-files").strip().lower()

    @property
    def async_db_url(self) -> URL:
        raw = self.DATABASE_URL.strip("\"'")
        parsed = urlparse(raw)
        return URL.create(
            drivername="postgresql+asyncpg",
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            host=parsed.hostname,
            port=parsed.port or 5432,
            database=(parsed.path or "").lstrip("/"),
        )


settings = Settings()
