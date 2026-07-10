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

    # ---- Track A: deterministic checks (DeterministicChecksPhase) ----
    # Labelled suite/build/typecheck/lint results; gating subset folded into the verdict.
    track_a_checks: List[CheckResult] = field(default_factory=list)

    # ---- Track B: synthesized tests (Synthesis/Guard/Execute phases) ----
    synth_test_file: Optional[str] = None
    synth_test_content: str = ""
    guard_ok: bool = True
    guard_reason: str = ""
    synth_exec_check: Optional[CheckResult] = None

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

    def to_result(self) -> Dict[str, Any]:
        """The dict TesterRunner consumes. `check_results` feeds evidence.checks; the
        Tester verdict/routing is decided in TesterRunner from these checks."""
        return {
            "check_results": self.checks,
            "summary": self.summary,
            "final_validation": self.final_validation,
            "last_test_output_path": self.output_path or None,
            "repo_diff": self.repo_diff,
            # Run-level existing-suite baseline (GH #28/#72) — carried back to the blackboard.
            "suite_baseline_failures": self.suite_baseline_failures,
            # Clear the rejection note after use so it doesn't leak to the next microtask.
            "test_rejection_note": None,
            # _INTERNAL_: consumed by TesterRunner (token accounting, ADR 0025), not a
            # BlackboardState key.
            "_token_usage": self.token_usage,
        }
