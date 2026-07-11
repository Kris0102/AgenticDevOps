import inspect

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from db.db import db
from orchestrator import state as blackboard_state_module

# Pool sizing for the single-user dashboard: a handful of runs may execute concurrently,
# and each polling `graph.get_state` / driving `graph.invoke` borrows a connection for the
# duration of its op. 20 is comfortably above any realistic concurrent-run count here.
_CHECKPOINTER_POOL_MAX = 20


class CheckpointerFactory:
    """Builds the LangGraph PostgresSaver over a psycopg CONNECTION POOL — separate from
    SQLAlchemy's pool because PostgresSaver needs raw psycopg connections with autocommit +
    dict_row, held open for the process lifetime (mirrors db.py's `db = Database()` singleton).

    A pool, not one shared Connection: a lone psycopg connection is NOT thread-safe, and the
    graph is driven from multiple threads at once — several runs' background workers each call
    `graph.invoke`, and the API event loop calls `graph.get_state`/`update_state` to poll a run
    while its worker writes the next superstep. On one connection those collide ("another
    command already in progress") and corrupt the wire protocol. With a pool each op borrows its
    own connection; Postgres MVCC isolates the concurrent checkpoint reads/writes per row."""

    @staticmethod
    def build() -> PostgresSaver:
        pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            conninfo=db.psycopg_connection_string(),
            min_size=2,
            max_size=_CHECKPOINTER_POOL_MAX,
            open=False,
            # prepare_threshold=0 disables per-connection prepared statements, which otherwise
            # clash across pooled connections ("prepared statement ... already exists").
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        )
        pool.open()
        return PostgresSaver(pool, serde=CheckpointerFactory._serializer())

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
