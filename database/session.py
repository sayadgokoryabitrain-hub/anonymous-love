from __future__ import annotations

import logging
import os

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}
            _ensure_sqlite_directory(url)
        _engine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine.sync_engine, "connect")
            def _sqlite_ready(dbapi_connection, _connection_record) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=8000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()
    return _engine


def _ensure_sqlite_directory(url: str) -> None:
    path = url.split("///", 1)[-1]
    if not path or path.startswith(":memory:"):
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _factory


async def init_database(factory: async_sessionmaker[AsyncSession] | None = None) -> None:
    from database.base import Base
    import database.models  # noqa: F401
    from services.seed import ensure_bootstrap_admin, seed_data

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_video_note_column)
        await conn.run_sync(_ensure_payment_columns)
    maker = factory or get_session_factory()
    async with maker() as session:
        await seed_data(session)
        await ensure_bootstrap_admin(session)
        await session.commit()
    logger.info("database ready")


def _ensure_video_note_column(connection) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(connection)
    if "verification_requests" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("verification_requests")}
    if "video_note_file_id" not in columns:
        connection.execute(text("ALTER TABLE verification_requests ADD COLUMN video_note_file_id VARCHAR(256)"))


def _ensure_payment_columns(connection) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(connection)
    if "payment_orders" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("payment_orders")}
    statements = {
        "provider_invoice_id": "ALTER TABLE payment_orders ADD COLUMN provider_invoice_id VARCHAR(64)",
        "quote_amount": "ALTER TABLE payment_orders ADD COLUMN quote_amount VARCHAR(32)",
        "quote_asset": "ALTER TABLE payment_orders ADD COLUMN quote_asset VARCHAR(16)",
        "pay_url": "ALTER TABLE payment_orders ADD COLUMN pay_url VARCHAR(512)",
    }
    for name, sql in statements.items():
        if name not in columns:
            connection.execute(text(sql))
