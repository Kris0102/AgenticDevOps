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
- supported project, nothing decided    → SKIPPED "unverified" (passes through UN-GATED,
  loudly labelled — see below)

`lint` is advisory (excluded from gating); ERRORED (harness) checks are excluded from the
decision so a flaky synthesized-test import can't be blamed on the code (fix #1).

Why "unverified" passes through instead of failing (run 27198e26): a FAIL here routes to
the Reflector with ZERO failing output to diagnose from — the failure is a property of the
HARNESS (nothing could run: e.g. .tsx targets under a pytest harness), not of the code, so
the Coder can never make it pass. The reflector then hallucinates a code defect, the coder
changes nothing, and the stagnation detector BLOCKs the microtask — every time, on correct
code. Passing through un-gated is the honest verdict: the reason string says exactly what
was NOT verified, the CODE/DEPLOY gates still show the human the real diff, and Track A
still gates whenever it produces any signal.
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
        # `ctx.suite_baseline_failures` (the pristine existing-suite failing COUNT) was
        # established by DeterministicChecksPhase (GH #28/#72); gating here is on the DELTA.
        unit = self._combined_unit_check(ctx)
        # Single source of truth for "what role did the synth test play in the verdict?" — the
        # verdict phase owns this decision; TestContext just carries it into the test-synth
        # artifact so the dashboard shows whether the test gated / was advisory / was excluded.
        ctx.synth_verdict_role = self._synth_role(ctx)
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
        # Existing-suite gating is on the DELTA vs the pristine baseline (GH #28/#72): a FAILING
        # `integration_tests` is inherited breakage ONLY while its failing count is at or below
        # the baseline — those failures were already in the repo. The moment the count exceeds
        # the baseline, the Coder introduced a NEW regression → gate. A PASSING existing suite
        # still counts; build/typecheck + the grounded synthesized tests always gate.
        baseline = ctx.suite_baseline_failures or 0
        new_regression = ctx.suite_current_failures > baseline

        def is_inherited_suite_failure(c: CheckResult) -> bool:
            return (c.label == "integration_tests"
                    and c.status == CheckStatus.FAILED
                    and not new_regression)

        # Gating inputs: Track-A correctness checks (suite/typecheck/build) + Track-B exec.
        gating: List[CheckResult] = [
            c for c in ctx.track_a_checks
            if c.label in self.GATING_LABELS and not is_inherited_suite_failure(c)
        ]
        # Track B (LLM-synthesized) GATES only when grounded (`_synth_gates`) — the Planner gave
        # concrete IOExamples the test asserts real values against. Otherwise it's advisory: a
        # PASS still counts as positive evidence, but a FAIL does NOT fail the microtask (an
        # ungrounded, prose-derived LLM test is the tester's main false-negative — it fails
        # correct code). Track A stays the primary gate. ERRORED never decides either way.
        if ctx.synth_exec_check and (self._synth_gates(ctx) or ctx.synth_exec_check.status == CheckStatus.PASSED):
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
            # Supported project, but no check produced a real verdict. NOT a failure: the
            # coder can't fix "nothing could run", and failing it loops reflector+coder to
            # BLOCKED on correct code (run 27198e26). SKIPPED passes downstream — loudly
            # labelled unverified, human still reviews the diff at the gates.
            status, reason = CheckStatus.SKIPPED, self._unverified_reason(ctx, gating)
            _log.warning("tester.verdict_unverified", run_id=ctx.run_id,
                         microtask_id=ctx.microtask.microtask_id, reason=reason)

        command = (ctx.synth_exec_check or (gating[0] if gating else None))
        return CheckResult(
            label="unit_tests",
            command=command.command if command else [],
            status=status,
            exit_code=0 if status in (CheckStatus.PASSED, CheckStatus.SKIPPED) else 1,
            duration_ms=sum(c.duration_ms for c in gating),
            stdout="\n\n".join(c.stdout for c in gating),
            stderr="\n\n".join(c.stderr for c in gating),
            artifact_text=self._artifact_text(ctx, status, reason),
        )

    def _synth_role(self, ctx: TestContext) -> str:
        """Human-readable role the synth test played in THIS verdict (mirrors the append logic
        in `_combined_unit_check`). Surfaced on the test-synth artifact so the dashboard shows
        the skip/advisory behaviour explicitly instead of a bare status."""
        c = ctx.synth_exec_check
        if not ctx.synth_test_file:
            return "not run — no test synthesized this iteration"
        if not ctx.guard_ok:
            return "excluded — trivial-guard rejected (did not gate; Track A decided)"
        if c is None:
            return "not run"
        if c.status == CheckStatus.ERRORED:
            return "excluded — could not load / harness error (did not gate; Track A decided)"
        if self._synth_gates(ctx):
            return f"gated the verdict ({c.status})"
        if c.status == CheckStatus.PASSED:
            return "counted as positive evidence (ungrounded — no IOExamples)"
        return "advisory — failure ignored (ungrounded, no IOExamples; Track A decided)"

    @staticmethod
    def _synth_gates(ctx: TestContext) -> bool:
        """Track B is trustworthy enough to GATE the verdict only when it's grounded: the
        Planner supplied ≥1 concrete IOExample (real expected values, not prose the LLM
        re-interprets) AND the trivial-guard passed. Otherwise the synth test is advisory —
        a PASS counts, a FAIL does not gate (Stage 2: don't let an ungrounded LLM test fail
        correct code, the tester's main run-killer)."""
        if not ctx.guard_ok:
            return False
        return any(c.examples for c in ctx.microtask.acceptance_criteria)

    @staticmethod
    def _unverified_reason(ctx: TestContext, gating: List[CheckResult]) -> str:
        tail = " — passing through UN-VERIFIED; review the diff at the gate"
        if ctx.synth_untestable_reason:
            return f"unverified — synthesis skipped: {ctx.synth_untestable_reason}; no other check decided{tail}"
        if ctx.synth_test_file and not ctx.guard_ok:
            return f"unverified — synthesized tests were rejected ({ctx.guard_reason}) and no other check decided{tail}"
        if ctx.synth_exec_check and ctx.synth_exec_check.status == CheckStatus.ERRORED:
            return f"unverified — synthesized tests could not run (harness error) and no other check decided{tail}"
        if (ctx.synth_exec_check and ctx.synth_exec_check.status == CheckStatus.FAILED
                and not VerdictPhase._synth_gates(ctx)):
            return ("unverified — the synthesized test failed but is advisory (no concrete "
                    f"IOExamples to gate on) and no other check decided{tail}")
        return f"unverified — no runnable verification (no suite, no build, nothing synthesized){tail}"

    @staticmethod
    def _artifact_text(ctx: TestContext, status: CheckStatus, reason: str) -> str:
        """The unit_tests verdict body: a SUMMARY (verdict + per-check one-liners) — NOT a
        re-dump of every sub-check. Each Track A check (integration_tests/build/typecheck/lint)
        has its OWN artifact that the dashboard + reflector read directly, so re-embedding them
        here made the verdict a near-duplicate of the suite artifact (user-reported). Only the
        Track B synth RUN output is embedded — it has no separate artifact of its own."""
        lines = [f"VERDICT: {status} — {reason}", ""]
        if ctx.track_a_checks:
            baseline = ctx.suite_baseline_failures or 0
            inherited_suite = ctx.suite_current_failures <= baseline
            for c in ctx.track_a_checks:
                if c.label == "integration_tests" and c.status == CheckStatus.FAILED and inherited_suite:
                    gate = (f" (advisory — {ctx.suite_current_failures} failing ≤ pristine baseline "
                            f"{baseline}, GH #72)")
                elif c.label in VerdictPhase.GATING_LABELS:
                    gate = ""
                else:
                    gate = " (advisory)"
                lines.append(f"Track A · {c.label}{gate}: {c.status}")
        else:
            lines.append("Track A: not run")
        if ctx.synth_exec_check:
            note = "" if VerdictPhase._synth_gates(ctx) else "  (advisory — no concrete IOExamples; a failure does not gate)"
            lines.append(f"Track B (synthesized):    {ctx.synth_exec_check.status}{note}")
        else:
            lines.append("Track B (synthesized):    not run")
        lines.append(f"Synth guard:              {'ok — ' + ctx.guard_reason if ctx.guard_ok else 'REJECTED — ' + ctx.guard_reason}")
        # Embed ONLY the Track B synth run (no artifact of its own). Track A checks are linked,
        # not duplicated — their full output lives in their own per-check artifacts.
        if ctx.synth_exec_check and ctx.synth_exec_check.artifact_text:
            lines += ["", f"--- synthesized test run ({' '.join(ctx.synth_exec_check.command)}) ---",
                      ctx.synth_exec_check.artifact_text]
        if ctx.track_a_checks:
            linked = " / ".join(f"test-{c.label}" for c in ctx.track_a_checks)
            lines += ["", f"Track A full output → see this microtask's own check artifacts: {linked}"]
        return "\n".join(lines)

    @staticmethod
    def _summary(ctx: TestContext, unit: CheckResult) -> str:
        scope = "final-microtask checks" if ctx.final_validation else "microtask checks"
        if unit.status == CheckStatus.FAILED:
            return f"{scope} FAILED."
        if unit.status == CheckStatus.SKIPPED:
            # "skipped" hides an important distinction the human needs at the gate:
            # unsupported repo (nothing ever runs) vs UNVERIFIED (this change specifically
            # could not be checked — look at the diff yourself).
            if ctx.skip:
                return f"{scope} skipped."
            return f"{scope} UNVERIFIED — passed through without any check deciding."
        return f"{scope} passed."
