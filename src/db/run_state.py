from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from db.models import RunState
from orchestrator.state import (
    BlackboardState,
    blackboard_state_from_dict,
    blackboard_state_to_dict,
)


class RunStateStore:
    """Persists/reconstructs full BlackboardState snapshots in run_state.

    Mirrors EventLogger: only ever session.add()/merge() — commit ownership
    stays with the caller's db.session_scope().
    """

    @staticmethod
    def save(session: Session, run_id: str, state: BlackboardState) -> None:
        now = datetime.now(timezone.utc)
        record = session.get(RunState, run_id)
        state_json = blackboard_state_to_dict(state)
        if record:
            record.state_json = state_json
            record.version += 1
            record.updated_at = now
        else:
            session.add(RunState(
                run_id=run_id,
                version=1,
                state_json=state_json,
                updated_at=now,
            ))

    @staticmethod
    def load(session: Session, run_id: str) -> Optional[BlackboardState]:
        record = session.get(RunState, run_id)
        if record is None:
            return None
        try:
            return blackboard_state_from_dict(record.state_json)
        except (ValidationError, KeyError, TypeError) as e:
            print(f"[RunStateStore] WARNING: failed to reconstruct snapshot for run "
                  f"{run_id} (schema drift?): {e}. Falling back to a fresh state.")
            return None
