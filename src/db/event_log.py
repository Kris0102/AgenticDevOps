import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

from db.models import Event


class EventLogger:
    """Centralized Event-row construction. Every lifecycle/tool event goes through here.

    Callers own the session and its commit boundary — mirrors the Orchestrator's
    blackboard/commit ownership (CLAUDE.md). This class only ever calls session.add(),
    never session.commit()."""

    @staticmethod
    def log(session: Session, run_id: str, actor: str, event_type: str, payload: Dict[str, Any]) -> Event:
        event = Event(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            ts=datetime.now(timezone.utc),
            actor=actor,
            event_type=event_type,
            payload_json=payload,
        )
        session.add(event)
        return event
