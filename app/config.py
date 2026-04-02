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

# Resolve .env relative to this file (backend/app/config.py → backend/.env)
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    OPENAI_API_KEY: str | None = None
    FRONTEND_URL: str = "http://localhost:5173"
    AZURE_CONNECTION_STRING: str | None = None
    AZURE_RFQ_FILES_CONTAINER: str = "rfq-files"

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE))

    @property
    def frontend_url(self) -> str:
        return self.FRONTEND_URL.rstrip("/")

    @property
    def azure_connection_string(self) -> str:
        return (self.AZURE_CONNECTION_STRING or "").strip("\"' ")

    @property
    def azure_rfq_files_container(self) -> str:
        return (self.AZURE_RFQ_FILES_CONTAINER or "rfq-files").strip().lower()

    @property
    def async_db_url(self) -> URL:
        """
        Parse the raw DATABASE_URL and return a SQLAlchemy URL object with
        the asyncpg driver.  URL.create() accepts decoded strings, so
        percent-encoded passwords (e.g. St%24%400987 → St$@0987) work fine.
        """
        raw = self.DATABASE_URL
        # Strip surrounding quotes that Windows/pydantic_settings may leave
        raw = raw.strip("\"'")
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
