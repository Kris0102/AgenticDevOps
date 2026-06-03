
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os

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
        """Yields a database session"""
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()

db = Database()   