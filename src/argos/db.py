"""Database engine, session helpers, and schema initialization."""

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, text
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


def init_db(database_url: Optional[str] = None) -> None:
    """Ensure tables exist idempotently."""
    engine = configure_db(database_url) if database_url else get_engine()
    Base.metadata.create_all(engine)


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
