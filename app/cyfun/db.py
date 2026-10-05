"""SQLite database setup. One file in DATA_DIR, WAL mode, foreign keys on."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(db_url: str):
    global _engine, _SessionLocal
    _engine = create_engine(db_url, connect_args={"check_same_thread": False}, future=True)

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def create_schema() -> None:
    from . import models

    assert _engine is not None
    Base.metadata.create_all(_engine)
    with session() as s:
        if s.get(models.Organisation, 1) is None:
            s.add(models.Organisation(id=1, name=""))
            s.commit()


def session() -> Session:
    assert _SessionLocal is not None, "init_engine() not called"
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    db = session()
    try:
        yield db
    finally:
        db.close()
