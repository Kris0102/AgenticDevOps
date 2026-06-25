from dataclasses import dataclass
from typing import TYPE_CHECKING, List

from orchestrator.state import Microtask, MicrotaskStatus
from Exceptions.BadMicroTaskExceptions import BadMicroTaskException

if TYPE_CHECKING:
    from agents.Planning.Planner.planner import PlannerOutput


@dataclass
class _MicrotaskFlags:
    """Structural-soundness flags shared by `_quality_report` (informational) and
    `_normalize_output` (validating) — they judge the same conditions but report/raise
    with different wording, so only the condition checks are shared, not the messages."""
    no_acceptance_criteria: bool
    no_target_files: bool
    target_files_count: int
    description_len: int
    approach_len: int
    only_existence_criteria: bool
    no_test_strategy: bool


def _check_microtask(task: Microtask) -> _MicrotaskFlags:
    weak = [
        c for c in task.acceptance_criteria
        if c.text.strip().lower().startswith(("file exists", "directory exists"))
    ]
    empty_text = [c for c in task.acceptance_criteria if not c.text.strip()]
    return _MicrotaskFlags(
        # no criteria at all, OR a criterion with empty text — both leave the Tester nothing to ground on
        no_acceptance_criteria=(not task.acceptance_criteria) or bool(empty_text),
        no_target_files=not task.target_files,
        target_files_count=len(task.target_files),
        description_len=len(task.description),
        approach_len=len(task.approach),
        only_existence_criteria=bool(weak) and len(weak) == len(task.acceptance_criteria),
        no_test_strategy=not task.test_strategy,
    )


def _quality_report(result: "PlannerOutput") -> str:
    """Run quality checks per microtask and return a human-readable report. Does NOT raise."""
    lines = []
    all_pass = True
    for task in result.microtasks:
        flags = _check_microtask(task)
        issues = []

        if flags.no_acceptance_criteria:
            issues.append("FAIL  no acceptance_criteria")
        if flags.no_target_files:
            issues.append("FAIL  target_files is empty — Coder has no file contract")
        elif flags.target_files_count > 3:
            issues.append(f"FAIL  {flags.target_files_count} target_files — too broad (max 3), split this task")
        if flags.description_len < 80:
            issues.append(f"FAIL  description only {flags.description_len} chars — too vague (min 80)")
        if flags.approach_len < 40:
            issues.append(f"FAIL  approach only {flags.approach_len} chars — too vague (min 40), needs concrete technical detail")
        if flags.only_existence_criteria:
            issues.append("FAIL  acceptance_criteria only checks file existence — needs observable behaviour")
        if flags.no_test_strategy:
            issues.append("WARN  no test_strategy — Tester won't know how to verify this task")

        status = "PASS" if not issues else "FAIL"
        if status == "FAIL":
            all_pass = False
        lines.append(f"  [{status}] {task.title} (ID: {task.microtask_id})")
        for issue in issues:
            lines.append(f"         {issue}")

    summary = "ALL CHECKS PASSED — plan is coder-ready" if all_pass else "QUALITY ISSUES FOUND — plan will be rejected by validator"
    return summary + "\n" + "\n".join(lines)


def _normalize_output(result: "PlannerOutput") -> "PlannerOutput":
    from agents.Planning.Planner.planner import PlannerOutput

    microtasks: List[Microtask] = []
    errors: List[str] = []

    for idx, task in enumerate(result.microtasks):
        tid = f"'{task.title}' ({task.microtask_id})"
        flags = _check_microtask(task)

        if flags.no_acceptance_criteria:
            errors.append(f"{tid}: no acceptance_criteria")

        if flags.no_target_files:
            errors.append(f"{tid}: target_files is empty — Coder needs explicit file paths")

        if flags.target_files_count > 3:
            errors.append(
                f"{tid}: {flags.target_files_count} target_files — microtask too broad, split into ≤3 files each"
            )

        if flags.description_len < 80:
            errors.append(
                f"{tid}: description too short ({flags.description_len} chars) — must include file path, "
                "what to put there, and any relevant types/ports/URLs"
            )

        if flags.approach_len < 40:
            errors.append(
                f"{tid}: approach too short ({flags.approach_len} chars) — must spell out HOW: "
                "function/class signatures, data shapes, sequencing, or edge cases (not a restatement of description)"
            )

        if flags.only_existence_criteria:
            errors.append(
                f"{tid}: acceptance_criteria only checks file existence — must include observable behaviour "
                "(e.g. 'GET /api/results returns HTTP 200 with array of Result objects')"
            )

        microtasks.append(
            task.model_copy(update={"idx": idx, "status": MicrotaskStatus.PENDING})
        )

    if errors:
        raise BadMicroTaskException(
            "Planner output failed quality checks — retry with more specific microtasks:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    return PlannerOutput(plan=result.plan, microtasks=microtasks)
