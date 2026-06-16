"""Observability: structured, category-scoped operational logging.

Ephemeral by design — logs go to the console (dev) or JSON (prod) for live
debugging and tailing. They are NOT the dashboard data source; durable
audit/analytics live in the Postgres `events` table via db.event_log.EventLogger.

Usage:
    from observability import get_logger, Category
    log = get_logger(Category.SEMANTIC)
    log.info("semantic.search", query=q, hits=len(hits), top_score=hits[0]["score"])

Run-scoped context (run_id / microtask_id) is bound once and auto-attached to
every line — see bind_run / bind_microtask.
"""
from observability.categories import Category
from observability.logging_config import (
    bind_microtask,
    bind_run,
    clear_context,
    configure_logging,
    get_logger,
)

__all__ = [
    "Category",
    "configure_logging",
    "get_logger",
    "bind_run",
    "bind_microtask",
    "clear_context",
]
