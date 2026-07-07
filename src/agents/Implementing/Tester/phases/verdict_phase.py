"""Phase 6 — combine the tracks into the canonical verdict the Reflector/node read.

`evidence.checks` is a fixed-field model and the Reflector decides pass/fail from
`evidence.checks.unit_tests`. So the OVERALL verdict (Track A suite/build/typecheck +
Track B synthesized tests) must be emitted as the `unit_tests` check — otherwise a Track A
regression would be invisible. The raw Track-A checks are also emitted (build/typecheck/
lint/integration_tests) for the audit trail.

Verdict rules (only PASSED/FAILED checks decide — SKIPPED/ERRORED do NOT):
- unsupported project (`ctx.skip`)      → SKIPPED (nothing to verify; passes downstream)
- any deciding check FAILED             → FAILED (a real code/build/type failure)
- any deciding check PASSED             → PASSED
- supported project, nothing decided    → FAILED "unverified" (do NOT silently pass; fix #2)

`lint` is advisory (excluded from gating); ERRORED (harness) checks are excluded from the
decision so a flaky synthesized-test import can't be blamed on the code (fix #1).
"""
from __future__ import annotations

from typing import List

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.results import CheckResult, CheckStatus
from agents.Implementing.Tester.tester_output import create_test_file_as_output
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger

_log = get_logger(Category.TESTER)


class VerdictPhase(TestPhase):
    name = "verdict"

    # Track-A labels that gate the verdict (correctness). `lint` is advisory (style).
    GATING_LABELS = {"integration_tests", "typecheck", "build"}

    def __init__(self, tools: TesterTools):
        self.tools = tools

    def run(self, ctx: TestContext) -> TestContext:
        unit = self._combined_unit_check(ctx)
        # Emit the canonical verdict + every raw Track-A check (separate evidence fields).
        ctx.checks = [unit, *ctx.track_a_checks]

        ctx.summary = self._summary(ctx, unit)
        ctx.repo_diff = self.tools.get_repo_diff()
        ctx.output_path = create_test_file_as_output(
            ctx.run_id, ctx.microtask, ctx.project_type,
            ctx.synth_test_file or "", ctx.synth_test_content, unit,
            rejection_note=ctx.rejection_note, reflections=ctx.reflections,
            token_usage=ctx.token_usage,
        ) or None
        _log.info("tester.verdict", run_id=ctx.run_id, status=str(unit.status),
                  final_validation=ctx.final_validation, output_path=ctx.output_path)
        return ctx

    def _combined_unit_check(self, ctx: TestContext) -> CheckResult:
        # Gating inputs: Track-A correctness checks (suite/typecheck/build) + Track-B exec.
        gating: List[CheckResult] = [c for c in ctx.track_a_checks if c.label in self.GATING_LABELS]
        if ctx.synth_exec_check:
            gating.append(ctx.synth_exec_check)
        deciding = [c for c in gating if c.decides_verdict]  # PASSED/FAILED only

        if ctx.skip:
            status, reason = CheckStatus.SKIPPED, "unsupported project type (not Python or JavaScript)"
        elif any(c.status == CheckStatus.FAILED for c in deciding):
            labels = ", ".join(c.label for c in deciding if c.status == CheckStatus.FAILED)
            status, reason = CheckStatus.FAILED, f"failed: {labels}"
        elif any(c.status == CheckStatus.PASSED for c in deciding):
            status, reason = CheckStatus.PASSED, "checks passed"
        else:
            # Supported project, but no check produced a real verdict — do NOT silently pass.
            status, reason = CheckStatus.FAILED, self._unverified_reason(ctx, gating)

        command = (ctx.synth_exec_check or (gating[0] if gating else None))
        return CheckResult(
            label="unit_tests",
            command=command.command if command else [],
            status=status,
            exit_code=0 if status in (CheckStatus.PASSED, CheckStatus.SKIPPED) else 1,
            duration_ms=sum(c.duration_ms for c in gating),
            stdout="\n\n".join(c.stdout for c in gating),
            stderr="\n\n".join(c.stderr for c in gating),
            artifact_text=self._artifact_text(ctx, status, reason, gating),
        )

    @staticmethod
    def _unverified_reason(ctx: TestContext, gating: List[CheckResult]) -> str:
        if ctx.synth_test_file and not ctx.guard_ok:
            return f"unverified — synthesized tests were trivial ({ctx.guard_reason}) and no other check decided"
        if ctx.synth_exec_check and ctx.synth_exec_check.status == CheckStatus.ERRORED:
            return "unverified — synthesized tests could not run (harness error) and no other check decided"
        return "unverified — no runnable verification (no suite, no build, nothing synthesized)"

    @staticmethod
    def _artifact_text(ctx: TestContext, status: CheckStatus, reason: str, subs: List[CheckResult]) -> str:
        lines = [f"VERDICT: {status} — {reason}", ""]
        if ctx.track_a_checks:
            for c in ctx.track_a_checks:
                gate = "" if c.label in VerdictPhase.GATING_LABELS else " (advisory)"
                lines.append(f"Track A · {c.label}{gate}: {c.status}")
        else:
            lines.append("Track A: not run")
        synth = str(ctx.synth_exec_check.status) if ctx.synth_exec_check else "not run"
        lines.append(f"Track B (synthesized):    {synth}")
        lines.append(f"Trivial guard:            {'ok — ' + ctx.guard_reason if ctx.guard_ok else 'REJECTED — ' + ctx.guard_reason}")
        for c in subs:
            lines += ["", f"--- {c.label} ({' '.join(c.command)}) ---", c.artifact_text]
        return "\n".join(lines)

    @staticmethod
    def _summary(ctx: TestContext, unit: CheckResult) -> str:
        scope = "final-microtask checks" if ctx.final_validation else "microtask checks"
        if unit.status == CheckStatus.FAILED:
            return f"{scope} FAILED."
        if unit.status == CheckStatus.SKIPPED:
            return f"{scope} skipped."
        return f"{scope} passed."
