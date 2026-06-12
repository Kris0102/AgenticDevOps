import inspect

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import Connection
from psycopg.rows import DictRow, dict_row

from db.db import db
from orchestrator import state as blackboard_state_module


class CheckpointerFactory:
    """Builds the LangGraph PostgresSaver — separate from SQLAlchemy's
    connection pool because PostgresSaver needs a raw psycopg connection
    with autocommit + dict_row, held open for the process lifetime (mirrors
    db.py's module-level `db = Database()` singleton)."""

    @staticmethod
    def build() -> PostgresSaver:
        conn = Connection[DictRow].connect(
            db.psycopg_connection_string(),
            autocommit=True,
            row_factory=dict_row,
        )
        return PostgresSaver(conn, serde=CheckpointerFactory._serializer())

    @staticmethod
    def _serializer() -> JsonPlusSerializer:
        """Explicitly allowlist every type defined in orchestrator.state for
        msgpack (de)serialization — derived from the module itself so the
        list can't drift as BlackboardState's Pydantic models evolve. Without
        this, LangGraph logs a 'deserializing unregistered type' deprecation
        warning per type and will refuse to deserialize them once
        LANGGRAPH_STRICT_MSGPACK becomes the default."""
        allowed_modules = [
            (blackboard_state_module.__name__, name)
            for name, obj in vars(blackboard_state_module).items()
            if inspect.isclass(obj) and obj.__module__ == blackboard_state_module.__name__
        ]
        return JsonPlusSerializer(allowed_msgpack_modules=allowed_modules)


checkpointer = CheckpointerFactory.build()


def setup_checkpointer() -> None:
    """Idempotent — creates checkpoints/checkpoint_blobs/checkpoint_writes/
    checkpoint_migrations tables if missing. Safe to call on every startup,
    mirrors 'always verify infra before running' (see BootStrapper)."""
    checkpointer.setup()
