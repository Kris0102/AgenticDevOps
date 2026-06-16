from contextlib import contextmanager
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, db_url=None):
        self.engine = create_engine(db_url or Database.sqlalchemy_url())
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    @staticmethod
    def _build_conninfo() -> str:
        """user:password@host:port/db_name from POSTGRES_* env vars. Dev defaults
        mirror deploy/docker-compose.yml so a no-.env dev box still reaches the
        default container."""
        user = os.getenv("POSTGRES_USER", "user")
        password = os.getenv("POSTGRES_PASSWORD", "password")
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        db_name = os.getenv("POSTGRES_DB", "agentic_devops")
        return f"{user}:{password}@{host}:{port}/{db_name}"

    @staticmethod
    def _base_url() -> str:
        """Driver-less DB URL — the SINGLE source of truth shared by the app
        (Database) AND Alembic (migrations/env.py). DATABASE_URL wins when set;
        otherwise built from POSTGRES_* parts. Returned as plain postgresql:// so
        each caller attaches the driver form it needs — SQLAlchemy wants +psycopg,
        raw psycopg wants none. Keeps app, Alembic, and compose from drifting onto
        different credentials (the cause of 'password authentication failed for
        user user'). Static so Alembic can read it WITHOUT instantiating Database
        (which would build a SQLAlchemy engine as a side effect)."""
        explicit = os.getenv("DATABASE_URL")
        if explicit:
            return explicit.replace("postgresql+psycopg://", "postgresql://", 1)
        return f"postgresql://{Database._build_conninfo()}"

    @staticmethod
    def sqlalchemy_url() -> str:
        """SQLAlchemy engine URL — forces the psycopg3 driver (psycopg[binary])."""
        return Database._base_url().replace("postgresql://", "postgresql+psycopg://", 1)

    @staticmethod
    def psycopg_dsn() -> str:
        """Plain DSN for raw psycopg connections (e.g. LangGraph PostgresSaver)."""
        return Database._base_url()

    def psycopg_connection_string(self) -> str:
        """Plain `postgresql://` DSN for raw psycopg connections (e.g. PostgresSaver)."""
        return Database.psycopg_dsn()

    def get_session(self):
        """Yields a database session (FastAPI-style dependency)."""
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def session_scope(self):
        """Context manager: commit on success, rollback on error, always close."""
        session: Session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


db = Database()   