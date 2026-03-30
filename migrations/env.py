"""
Alembic async env.py — configured for asyncpg / SQLAlchemy 2.x.

Usage:
    cd backend/
    alembic revision --autogenerate -m "initial schema"
    alembic upgrade head
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
# Import all models so Alembic can detect them during autogenerate
import app.models  # noqa: F401
from app.database import Base

# ---------------------------------------------------------------------------
# Use settings.async_db_url — a SQLAlchemy URL object built via URL.create()
# with decoded credentials — so asyncpg never sees percent-encoded passwords.
# ---------------------------------------------------------------------------
ASYNC_URL = settings.async_db_url  # sqlalchemy.engine.URL object

# this is the Alembic Config object
config = context.config
# render_as_string(hide_password=False) is fine here; it's only used by the
# offline mode path and is not passed to asyncpg's DSN parser.
url_str = ASYNC_URL.render_as_string(hide_password=False)
config.set_main_option("sqlalchemy.url", url_str.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Pass the URL *object* (not a string) so SQLAlchemy builds the connection
    # internally and never hands a percent-encoded DSN to asyncpg.
    connectable = create_async_engine(ASYNC_URL)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
