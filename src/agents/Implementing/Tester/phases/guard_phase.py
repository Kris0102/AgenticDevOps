"""Phase 4 — deterministic anti-trivial guard on the synthesized test file.

Keeps the synthesis track honest without an LLM: parse the written file and confirm it
actually asserts something. A trivial file (no assertions / all skipped / empty) sets
`guard_ok=False`, which VerdictPhase treats as "not verified" (a failed verdict) rather
than letting a do-nothing test rubber-stamp the Coder.
"""
from __future__ import annotations

from agents.Implementing.Tester import grounding
from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from observability import Category, get_logger

_log = get_logger(Category.TESTER)


class TrivialGuardPhase(TestPhase):
    name = "trivial_guard"

    def should_run(self, ctx: TestContext) -> bool:
        return bool(ctx.synth_test_file)

    def run(self, ctx: TestContext) -> TestContext:
        trivial, reason = grounding.is_trivial(ctx.project_type, ctx.synth_test_content)
        ctx.guard_ok = not trivial
        ctx.guard_reason = reason
        if trivial:
            _log.warning("tester.guard.rejected", run_id=ctx.run_id,
                         microtask_id=ctx.microtask.microtask_id, reason=reason)
        else:
            _log.info("tester.guard.passed", run_id=ctx.run_id, detail=reason)
        return ctx
