"""
conftest.py — pytest fixtures for async FastAPI testing.

NOTE: tests require a running Postgres instance.
Set TEST_DATABASE_URL in your environment (or .env.test) before running.
"""
import asyncio
import os

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://user:password@localhost:5432/rfq_test",
)
os.environ.setdefault(
    "DATABASE_URL3",
    "postgresql://user:password@localhost:5432/rfq_fx_test",
)
os.environ.setdefault("SECRET_KEY", "test-secret")

from app.main import app
from app.database import Base, get_db, get_db3

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://user:password@localhost:5432/rfq_test",
)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine):
    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    """HTTP test client with the DB dependency overridden."""

    async def _override_get_db():
        yield db_session

    async def _override_get_db3():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_db3] = _override_get_db3
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
