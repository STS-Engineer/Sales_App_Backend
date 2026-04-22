import json
from types import SimpleNamespace

import pytest

import app.database_assembly as database_assembly
from app.config import Settings


def test_settings_async_db_url2_defaults_to_none():
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/rfq_db",
        DATABASE_URL3="postgresql+asyncpg://fx:secret@localhost:5432/ecb_rates",
        SECRET_KEY="test-secret",
    )

    assert settings.async_db_url2 is None


def test_settings_async_db_url2_parses_secondary_database_url():
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/rfq_db",
        DATABASE_URL2="postgresql+asyncpg://assembly:secret@localhost:5432/pl_assembly",
        DATABASE_URL3="postgresql+asyncpg://fx:secret@localhost:5432/ecb_rates",
        SECRET_KEY="test-secret",
    )

    parsed = settings.async_db_url2
    assert parsed is not None
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.database == "pl_assembly"
    assert parsed.username == "assembly"


def test_settings_async_db_url3_parses_third_database_url():
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/rfq_db",
        DATABASE_URL3="postgresql+asyncpg://fx:secret@localhost:5432/ecb_rates",
        SECRET_KEY="test-secret",
    )

    parsed = settings.async_db_url3
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.database == "ecb_rates"
    assert parsed.username == "fx"


@pytest.mark.asyncio
async def test_sync_rfq_to_assembly_returns_false_when_engine_missing(monkeypatch):
    monkeypatch.setattr(database_assembly, "assembly_engine", None)

    result = await database_assembly.sync_rfq_to_assembly(
        SimpleNamespace(rfq_id="rfq-1", rfq_data={"field": "value"})
    )

    assert result is False


class _FakeConnection:
    def __init__(self):
        self.statement = None
        self.params = None

    async def execute(self, statement, params):
        self.statement = statement
        self.params = params


class _FakeBeginContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self):
        self.connection = _FakeConnection()

    def begin(self):
        return _FakeBeginContext(self.connection)


class _FailingConnection:
    async def execute(self, statement, params):
        raise RuntimeError("secondary db unavailable")


class _FailingEngine:
    def begin(self):
        return _FakeBeginContext(_FailingConnection())


@pytest.mark.asyncio
async def test_sync_rfq_to_assembly_executes_upsert(monkeypatch):
    fake_engine = _FakeEngine()
    monkeypatch.setattr(database_assembly, "assembly_engine", fake_engine)

    rfq = SimpleNamespace(
        rfq_id="rfq-123",
        rfq_data={"systematic_rfq_id": "26001-ASS-00", "product_name": "Assembly"},
    )

    result = await database_assembly.sync_rfq_to_assembly(rfq)

    assert result is True
    assert fake_engine.connection.params["rfq_id"] == "rfq-123"
    assert fake_engine.connection.params["rfq_data"] == json.dumps(rfq.rfq_data)
    assert fake_engine.connection.params["created_at"] == fake_engine.connection.params["updated_at"]
    assert fake_engine.connection.params["created_at"].tzinfo is not None
    assert "INSERT INTO public.rfq" in str(fake_engine.connection.statement)
    assert "ON CONFLICT (rfq_id) DO UPDATE" in str(fake_engine.connection.statement)


@pytest.mark.asyncio
async def test_sync_rfq_to_assembly_logs_and_swallows_errors(monkeypatch, caplog):
    monkeypatch.setattr(database_assembly, "assembly_engine", _FailingEngine())

    with caplog.at_level("ERROR"):
        result = await database_assembly.sync_rfq_to_assembly(
            SimpleNamespace(rfq_id="rfq-456", rfq_data={"product_name": "Assembly"})
        )

    assert result is False
    assert "Assembly RFQ mirror failed for rfq_id=rfq-456" in caplog.text
