from __future__ import annotations

import os

os.environ["ENVIRONMENT"] = "test"
os.environ["PAYMENT_MODE"] = "test"
os.environ["ADMIN_JWT_SECRET"] = "test-secret-key-for-jwt-please-32"
os.environ["IDENTITY_PEPPER"] = "test-pepper-value"
os.environ["BOT_TOKEN"] = "123456:TESTTOKEN"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from core.config import get_settings

get_settings.cache_clear()

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database.base import Base
import database.models  # noqa: F401
from services.seed import seed_data


@pytest.fixture
async def factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await seed_data(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest.fixture
async def session(factory):
    async with factory() as db:
        yield db
        await db.rollback()
