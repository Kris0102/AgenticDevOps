"""TestContext — the accumulator passed phase→phase through the TestPipeline.

Each TestPhase reads what earlier phases recorded and writes its own outputs onto the
same context, so the pipeline reads as a clear linear flow (detect → existing-suite →
synthesis → guard → execute → verdict). `to_result()` renders the final node-facing dict
that TesterRunner maps into evidence.checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents.Implementing.Tester.results import CheckResult
from orchestrator.state import BlackboardState, Microtask, Reflection
from providers import TokenUsage

ProjectType = str  # "python" | "javascript" | "unknown"


@dataclass
class TestContext:
    # ---- inputs (set at construction) ----
    state: BlackboardState
    microtask: Microtask
    run_id: str
    final_validation: bool
    rejection_note: Optional[str] = None
    reflections: List[Reflection] = field(default_factory=list)

    # ---- filled by DetectProjectPhase ----
    project_type: ProjectType = "unknown"
    skip: bool = False  # unsupported project — produce a skipped verdict, don't block the pipeline
    # JS test framework the repo actually uses ("jest" | "node:test" | "vitest" | "mocha"),
    # so Track B synthesizes AND runs tests in the right framework instead of always Jest
    # (a mismatch made correct code fail — the tester's #1 false-negative). None for Python.
    js_test_framework: Optional[str] = None

    # ---- Track A: deterministic checks (DeterministicChecksPhase) ----
    # Labelled suite/build/typecheck/lint results; gating subset folded into the verdict.
    track_a_checks: List[CheckResult] = field(default_factory=list)

    # ---- Track B: synthesized tests (Synthesis/Guard/Execute phases) ----
    synth_test_file: Optional[str] = None
    synth_test_content: str = ""
    guard_ok: bool = True
    guard_reason: str = ""
    synth_exec_check: Optional[CheckResult] = None
    # Role the synth test played in the final verdict (gated / advisory / excluded / not run) —
    # decided by VerdictPhase (single source of truth), carried into the test-synth artifact.
    synth_verdict_role: str = ""

    # ---- VerdictPhase outputs ----
    checks: List[CheckResult] = field(default_factory=list)  # canonical unit_tests (+ extras)
    summary: str = ""
    output_path: Optional[str] = None
    repo_diff: str = ""
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    # Existing-suite baseline = failing-test count on the pristine tree (GH #28/#72), persisted
    # to the blackboard; None until the first Tester run establishes it, then fixed.
    suite_baseline_failures: Optional[int] = None
    # This run's current existing-suite failing count (transient — set by DeterministicChecks,
    # read by Verdict to gate on the delta vs the baseline).
    suite_current_failures: int = 0

    def synth_artifact(self) -> Optional[Dict[str, Any]]:
        """Payload for persisting the LLM-synthesized test as its own dashboard artifact —
        emitted whenever synthesis RAN this iteration (`synth_test_file` set), even if it
        produced an empty/unusable file (recursion-limit degradation), so "the tester tried
        but produced nothing" is always visible. Returns None only when synthesis was skipped
        (a plain coder-retry reusing the existing test). Carries the test SOURCE + execution
        status so a human sees what was written and whether it ran/passed/was advisory/couldn't
        load (Stage 1/2 visibility)."""
        if not self.synth_test_file:
            return None
        if self.synth_exec_check is not None:
            status = str(self.synth_exec_check.status)
        elif not self.guard_ok:
            status = "rejected (trivial-guard)"
        else:
            status = "not run"
        return {
            "file": self.synth_test_file,
            "content": self.synth_test_content,
            "status": status,
            "verdict_role": self.synth_verdict_role,
            "guard_ok": self.guard_ok,
            "guard_reason": self.guard_reason,
        }

    def to_result(self) -> Dict[str, Any]:
        """The dict TesterRunner consumes. `check_results` feeds evidence.checks; the
        Tester verdict/routing is decided in TesterRunner from these checks."""
        return {
            "check_results": self.checks,
            "summary": self.summary,
            "final_validation": self.final_validation,
            "last_test_output_path": self.output_path or None,
            "repo_diff": self.repo_diff,
            # _INTERNAL_: the synthesized test written this run (source + status), persisted as
            # its own `test-synth` artifact by TesterRunner. Not a BlackboardState key.
            "synth_test": self.synth_artifact(),
            # Run-level existing-suite baseline (GH #28/#72) — carried back to the blackboard.
            "suite_baseline_failures": self.suite_baseline_failures,
            # Clear the rejection note after use so it doesn't leak to the next microtask.
            "test_rejection_note": None,
            # _INTERNAL_: consumed by TesterRunner (token accounting, ADR 0025), not a
            # BlackboardState key.
            "_token_usage": self.token_usage,
        }
