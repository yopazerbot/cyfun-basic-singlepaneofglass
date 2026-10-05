"""SQLite database setup. One file in DATA_DIR, WAL mode, foreign keys on.

Schema management is deliberately simple: tables are created when missing and columns
added in later versions are appended with ALTER TABLE at start. Columns are never
dropped or renamed.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
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


def _add_missing_columns() -> None:
    """Append columns that exist in the models but not yet in the database."""
    assert _engine is not None
    insp = inspect(_engine)
    with _engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=_engine.dialect)}'
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if default is not None:
                    if isinstance(default, bool):
                        ddl += f" DEFAULT {1 if default else 0}"
                    elif isinstance(default, (int, float)):
                        ddl += f" DEFAULT {default}"
                    elif isinstance(default, str):
                        ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                conn.execute(text(ddl))


def create_schema() -> None:
    from . import models

    assert _engine is not None
    _add_missing_columns()
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
