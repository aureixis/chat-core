from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


database_url = get_settings().sqlalchemy_database_url
engine_options: dict = {"pool_pre_ping": True}
if database_url.startswith("sqlite"):
    engine_options.update(connect_args={"check_same_thread": False})
    # Only an in-memory database needs a shared connection. File databases must
    # give simultaneous visitor requests separate transactional connections.
    if make_url(database_url).database in {None, "", ":memory:"}:
        engine_options["poolclass"] = StaticPool
engine = create_engine(database_url, **engine_options)
if database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    database = SessionLocal()
    try:
        yield database
    finally:
        database.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    database = SessionLocal()
    try:
        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
