"""structlog configuration: per-category levels, contextvars, console/JSON output.

Config (all env, fits the project's .env pattern):
  LOG_LEVEL=INFO              global default threshold
  LOG_LEVEL_TOOL=DEBUG        per-category override (LOG_LEVEL_<CATEGORY name>)
  LOG_LEVEL_SEMANTIC=INFO
  LOG_JSON=false              true → one JSON object per line (prod); false → pretty console
  LOG_COLORS=auto             auto (color when stdout is a TTY) | true | false. NO_COLOR env also honored.

`configure_logging()` is idempotent and should be called once at process start
(main.py). Modules just call `get_logger(Category.X)` — no setup needed; an
unconfigured logger still works with sane defaults.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Dict, Union

import structlog

from observability.categories import Category
from observability.log_bus import log_bus


def _bus_tee(logger, method_name, event_dict):
    """structlog processor: tee each surviving log line into the per-run live bus for
    the dashboard. Pass-through — returns event_dict unchanged. Placed AFTER the level
    filter (dropped lines don't reach the dashboard) and AFTER merge_contextvars (so
    run_id is present). Only lines bound to a run are tee'd."""
    run_id = event_dict.get("run_id")
    if run_id:
        log_bus.append(run_id, dict(event_dict))
    return event_dict

# ---- ANSI palette: one distinct color per category, plus per-level colors ----

_RESET = "\x1b[0m"

# SGR codes. Each category gets its own hue so channels are visually separable;
# levels recolor only the LEVEL token.
_CATEGORY_COLOR = {
    "tool": "36",          # cyan
    "semantic": "35",      # magenta
    "planner": "34",       # blue
    "reviewer": "94",      # bright blue
    "coder": "32",         # green
    "tester": "33",        # yellow
    "reflector": "95",     # bright magenta
    "hitl": "91",          # bright red
    "sandbox": "96",       # bright cyan
    "bootstrap": "92",     # bright green
    "orchestrator": "97",  # bright white
    "db": "90",            # grey
}
_LEVEL_COLOR = {
    "debug": "90",         # grey
    "info": "32",          # green
    "warning": "33",       # yellow
    "error": "31",         # red
    "critical": "1;31",    # bold red
}


def _paint(code: str, text: str, enabled: bool) -> str:
    return f"\x1b[{code}m{text}{_RESET}" if enabled and code else text


class ColoredConsoleRenderer:
    """Final processor for the console (non-JSON) path. Renders:
    `<dim ts> <level> [category] event key=val ...` with the level and the
    [category] token each colored from the palette above. Categories keep a
    stable color so you can eyeball which subsystem a line came from."""

    def __init__(self, colors: bool = True):
        self.colors = colors

    def __call__(self, logger, name, event_dict):
        ed = dict(event_dict)
        ts = ed.pop("timestamp", "")
        level = str(ed.pop("level", "info")).lower()
        category = ed.pop("category", None)
        event = str(ed.pop("event", ""))
        exc = ed.pop("exception", None)

        parts = []
        if ts:
            parts.append(_paint("2", ts, self.colors))                       # dim timestamp
        parts.append(_paint(_LEVEL_COLOR.get(level, "37"), f"{level.upper():<8}", self.colors))
        if category is not None:
            parts.append(_paint(_CATEGORY_COLOR.get(category, "37"), f"[{category}]", self.colors))
        parts.append(_paint("1", event, self.colors))                        # bold event name
        for key, value in ed.items():
            parts.append(f"{_paint('2', key, self.colors)}={value}")         # dim keys, plain values
        line = " ".join(parts)
        if exc:
            line += "\n" + exc
        return line


def _use_colors() -> bool:
    if os.getenv("NO_COLOR") is not None:
        return False
    setting = os.getenv("LOG_COLORS", "auto").strip().lower()
    if setting in ("true", "1", "yes"):
        return True
    if setting in ("false", "0", "no"):
        return False
    return sys.stdout.isatty()  # auto

_LEVEL_BY_NAME = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

_configured = False


def _level_int(name: str, default: int = logging.INFO) -> int:
    return _LEVEL_BY_NAME.get((name or "").strip().lower(), default)


def _per_category_levels(default_level: int) -> Dict[str, int]:
    """Build {category_value: level_int}, default from LOG_LEVEL, overridden by
    LOG_LEVEL_<CATEGORY> (e.g. LOG_LEVEL_TOOL)."""
    levels: Dict[str, int] = {}
    for cat in Category:
        env_val = os.getenv(f"LOG_LEVEL_{cat.name}")
        levels[cat.value] = _level_int(env_val, default_level) if env_val else default_level
    return levels


class _CategoryLevelFilter:
    """structlog processor: drop a line whose level is below the threshold
    configured for its `category`. Lines with no/unknown category use the
    global default. This is what makes per-category verbosity work."""

    def __init__(self, default_level: int, per_category: Dict[str, int]):
        self.default_level = default_level
        self.per_category = per_category

    def __call__(self, logger, method_name, event_dict):
        threshold = self.per_category.get(event_dict.get("category"), self.default_level)
        if _level_int(method_name, logging.INFO) < threshold:
            raise structlog.DropEvent
        return event_dict


def configure_logging(json_logs: Union[bool, None] = None, level: Union[str, None] = None) -> None:
    """Idempotent. Reads env unless args override. Sets up the structlog pipeline."""
    global _configured
    if _configured:
        return

    # Load .env BEFORE reading any LOG_* var. configure_logging() can fire at
    # import time (the first module-level get_logger() triggers it), which is
    # before any module's own load_dotenv() has run — without this, LOG_LEVEL
    # from .env is invisible and everything silently defaults to INFO.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    if json_logs is None:
        json_logs = os.getenv("LOG_JSON", "false").strip().lower() == "true"
    default_level = _level_int(level or os.getenv("LOG_LEVEL", "INFO"))
    per_category = _per_category_levels(default_level)

    # Cheap global pre-filter at the lowest threshold any category uses; the
    # _CategoryLevelFilter then does the fine-grained per-category drop.
    min_level = min([default_level, *per_category.values()])

    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else ColoredConsoleRenderer(colors=_use_colors())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,      # auto-inject bound run_id/microtask_id
            structlog.processors.add_log_level,
            _CategoryLevelFilter(default_level, per_category),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _bus_tee,                                       # tee enriched dict → live dashboard bus
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(category: Union[Category, str]):
    """Return a logger bound to a category. Configures with defaults on first use
    if configure_logging() wasn't called explicitly."""
    if not _configured:
        configure_logging()
    value = category.value if isinstance(category, Category) else str(category)
    return structlog.get_logger().bind(category=value)


# ---- run-scoped context (attached to every subsequent line on this thread) ----

def bind_run(run_id: str) -> None:
    structlog.contextvars.bind_contextvars(run_id=run_id)


def bind_microtask(microtask_id: str) -> None:
    structlog.contextvars.bind_contextvars(microtask_id=microtask_id)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
