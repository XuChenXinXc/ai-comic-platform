from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, func, insert, select
from sqlalchemy.pool import StaticPool

from app.config import BACKEND_DIR, get_settings
from app.models.tables import api_provider, metadata


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def _ensure_sqlite_parent(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.endswith(":memory:"):
        return
    database_path = Path(database_url.removeprefix(prefix))
    database_path.parent.mkdir(parents=True, exist_ok=True)


def create_database_engine(database_url: str) -> Engine:
    _ensure_sqlite_parent(database_url)
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 5}
        if database_url.endswith(":memory:"):
            options["poolclass"] = StaticPool
    engine = create_engine(database_url, **options)
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)
    return engine


class Database:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_settings().database_url
        self.engine = create_database_engine(self.database_url)

    def create_all(self) -> None:
        metadata.create_all(self.engine)

    def seed_mock_providers(self) -> None:
        now = utc_now()
        provider_specs = (
            ("mock-text", "text", "Mock 文字", "mock"),
            ("mock-image", "image", "Mock 图片", "mock"),
            ("mock-video", "video", "Mock 视频", "mock"),
        )
        with self.engine.begin() as connection:
            for provider_id, kind, name, adapter in provider_specs:
                exists = connection.scalar(
                    select(func.count())
                    .select_from(api_provider)
                    .where(api_provider.c.id == provider_id)
                )
                if exists:
                    continue
                has_default = connection.scalar(
                    select(func.count())
                    .select_from(api_provider)
                    .where(
                        api_provider.c.kind == kind,
                        api_provider.c.is_default == 1,
                    )
                )
                connection.execute(
                    insert(api_provider).values(
                        id=provider_id,
                        kind=kind,
                        name=name,
                        adapter=adapter,
                        base_url="",
                        api_key_env="",
                        model=f"mock-{kind}",
                        params_json=json.dumps({}, ensure_ascii=False),
                        timeout_sec=120,
                        max_retries=0,
                        enabled=1,
                        is_default=0 if has_default else 1,
                        created_at=now,
                        updated_at=now,
                    )
                )

    def initialize(self, *, seed_mock_providers: bool = True) -> None:
        self.create_all()
        if seed_mock_providers:
            self.seed_mock_providers()

    def dispose(self) -> None:
        self.engine.dispose()


db = Database()


def get_database() -> Database:
    return db


def get_engine() -> Engine:
    return db.engine


def get_db() -> Database:
    return db


def migrate_schema(database: Database | None = None) -> Database:
    current = database or db
    alembic_config = Config()
    alembic_config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_config.set_main_option("prepend_sys_path", str(BACKEND_DIR))
    alembic_config.set_main_option("path_separator", "os")
    with current.engine.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            # SQLite 的 batch migration 需要重建被外键引用的表；
            # 约束在迁移事务外暂时关闭，完成后立即恢复。
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        try:
            alembic_config.attributes["connection"] = connection
            alembic_config.attributes["sqlite_foreign_keys_disabled"] = is_sqlite
            command.upgrade(alembic_config, "head")
        finally:
            if is_sqlite:
                # PRAGMA foreign_keys 在 SQLite 真实事务内是 no-op。迁移执行过 DML/DDL
                # 后失败时连接会停在未提交事务上，此时直接发 PRAGMA 会被静默忽略，
                # 外键保持关闭并随连接回到连接池被后续请求复用，因此先结束事务，
                # 然后把连接标为 invalid，避免被回收到连接池。
                if connection.in_transaction():
                    connection.rollback()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
            # 标记连接失效，下一次 engine.connect() 会从池里取新连接（或新建）。
            # 这样不会复用「外键刚被临时关过」的连接。
            connection.invalidate()
    return current


def init_db(
    *,
    seed_mock_providers: bool = True,
    database: Database | None = None,
) -> Database:
    current = migrate_schema(database)
    if seed_mock_providers:
        current.seed_mock_providers()
    return current
