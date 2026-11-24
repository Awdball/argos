"""Database engine, session helpers, and schema initialization."""

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from argos.config import get_settings
from argos.models import Base

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker[Session]] = None


def configure_db(database_url: Optional[str] = None) -> Engine:
    """Create and bind the SQLAlchemy engine and session factory."""
    global _engine, _session_factory
    url = database_url or get_settings().database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    _engine = create_engine(url, **kwargs)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_engine() -> Engine:
    """Return the active SQLAlchemy engine, configuring defaults if needed."""
    if _engine is None:
        return configure_db()
    return _engine


def ensure_schema_migrations(engine: Engine) -> None:
    """Apply lightweight idempotent column migrations for existing tables."""
    inspector = inspect(engine)
    if "watch_items" not in inspector.get_table_names():
        return
    cols = {col["name"] for col in inspector.get_columns("watch_items")}
    binary_type = "BYTEA" if engine.dialect.name == "postgresql" else "BLOB"
    statements = []
    if "image_url" not in cols:
        statements.append("ALTER TABLE watch_items ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
    if "image_data" not in cols:
        statements.append(f"ALTER TABLE watch_items ADD COLUMN image_data {binary_type}")
    if "image_mime" not in cols:
        statements.append(
            "ALTER TABLE watch_items ADD COLUMN image_mime VARCHAR(64) NOT NULL DEFAULT ''"
        )
    if statements:
        with engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))


def init_db(database_url: Optional[str] = None) -> None:
    """Ensure tables and additive column migrations exist idempotently."""
    engine = configure_db(database_url) if database_url else get_engine()
    Base.metadata.create_all(engine)
    ensure_schema_migrations(engine)


@contextmanager
def get_session() -> Iterator[Session]:
    """Provide a transactional SQLAlchemy session scope."""
    if _session_factory is None:
        configure_db()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_db_health() -> bool:
    """Execute a lightweight connectivity probe against the database."""
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
