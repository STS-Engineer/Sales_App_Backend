from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.async_db_url,
    echo=True,
    future=True,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_timeout=30,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

engine3 = create_async_engine(
    settings.async_db_url3,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_timeout=30,
)

AsyncSessionLocal3 = async_sessionmaker(
    engine3,
    class_=AsyncSession,
    expire_on_commit=False,
)


_db_url4 = settings.async_db_url4
engine4 = (
    create_async_engine(
        _db_url4,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_timeout=30,
    )
    if _db_url4 is not None
    else None
)
AsyncSessionLocal4 = (
    async_sessionmaker(engine4, class_=AsyncSession, expire_on_commit=False)
    if engine4 is not None
    else None
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session_maker() as session:
        yield session


async def get_db3() -> AsyncSession:
    async with AsyncSessionLocal3() as session:
        yield session


async def get_db4() -> AsyncSession:
    if AsyncSessionLocal4 is None:
        raise HTTPException(status_code=503, detail="KPI database (DATABASE_URL4) is not configured.")
    async with AsyncSessionLocal4() as session:
        yield session


async def get_db4_optional():
    if AsyncSessionLocal4 is None:
        yield None
        return
    async with AsyncSessionLocal4() as session:
        yield session
