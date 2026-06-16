from contextlib import contextmanager
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


load_dotenv()

class Database:
    def __init__(self, db_url=None):
        self.psycopg_conninfo = self._build_conninfo()
        if db_url is None:
            # Using psycopg3 adapter as specified in requirements.txt (psycopg[binary])
            db_url = f"postgresql+psycopg://{self.psycopg_conninfo}"

        self.engine = create_engine(db_url)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    @staticmethod
    def _build_conninfo() -> str:
        """user:password@host:port/db_name — shared by SQLAlchemy and raw psycopg connections."""
        user = os.getenv("POSTGRES_USER", "user")
        password = os.getenv("POSTGRES_PASSWORD", "password")
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        db_name = os.getenv("POSTGRES_DB", "agentic_devops")
        return f"{user}:{password}@{host}:{port}/{db_name}"

    def psycopg_connection_string(self) -> str:
        """Plain `postgresql://` DSN for raw psycopg connections (e.g. PostgresSaver)."""
        return f"postgresql://{self.psycopg_conninfo}"

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