from contextlib import contextmanager
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

class Database:
    def __init__(self, db_url=None):
        if db_url is None:
            user = os.getenv("POSTGRES_USER", "user")
            password = os.getenv("POSTGRES_PASSWORD", "password")
            host = os.getenv("POSTGRES_HOST", "localhost")
            port = os.getenv("POSTGRES_PORT", "5432")
            db_name = os.getenv("POSTGRES_DB", "agentic_devops")
            
            # Using psycopg3 adapter as specified in requirements.txt (psycopg[binary])
            db_url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db_name}"
            
        self.engine = create_engine(db_url)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

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