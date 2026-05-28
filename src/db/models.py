from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime, JSON, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Run(Base):
    __tablename__ = "runs"
    
    run_id = Column(String, primary_key=True)
    repo_url = Column(String, nullable=False)
    base_sha = Column(String, nullable=False)
    status = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class RunState(Base):
    __tablename__ = "run_state"
    
    run_id = Column(String, primary_key=True)
    version = Column(Integer, default=1)
    state_json = Column(JSON, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class Event(Base):
    __tablename__ = "events"
    
    event_id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    ts = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    actor = Column(String, nullable=False)
    event_type = Column(String, nullable=False, index=True)
    payload_json = Column(JSON, nullable=False)

class Artifact(Base):
    __tablename__ = "artifacts"
    
    artifact_id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)
    content_text = Column(Text, nullable=True)
    sha256 = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
