import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List

from orchestrator.state import AcceptanceCriterion, Microtask, MicrotaskStatus
from Exceptions.BadMicroTaskExceptions import BadMicroTaskException

if TYPE_CHECKING:
    from agents.Planning.Planner.planner import PlannerOutput


# Tautological / meta acceptance criteria — content-free statements that give the Tester
# nothing to ground a real assertion on. "All tests in weather.test.js pass" grounds a test on
# "the tests pass" (circular); "it works", "no errors", "compiles" are the same class. These
# are the criteria behind the recurring value-grounding failures: with no observable behaviour
# and no example, the Tester falls back to the repo's existing tests — including any that carry
# a wrong self-computed value — and the microtask becomes unwinnable. Reject them so the
# Planner must state a concrete behaviour (ideally with an IOExample).
_TAUTOLOGICAL_RE = re.compile(
    r"\btests?\b.{0,40}\bpass(es|ing)?\b"                      # "all tests pass", "tests in X pass"
    r"|\bpass(es)?\s+(all\s+)?tests?\b"                        # "passes all tests"
    r"|\bit\s+works\b"
    r"|\bworks\s+(correctly|as\s+expected|properly|fine)\b"
    r"|\b(no|without)\s+errors?\b|\berror[-\s]free\b"
    r"|\b(compiles|builds|runs)\s+(successfully|correctly|without\s+errors?)\b"
    r"|\b(functions|behaves)\s+(correctly|as\s+expected)\b",
    re.IGNORECASE,
)


def _has_useful_example(c: AcceptanceCriterion) -> bool:
    """True only if a criterion carries at least one example with ACTUAL data — a non-empty
    `given` OR a non-null `expect`. An example of `given={}` + `expect=None` is schema-valid
    but useless (a weaker model dodging the criterion by emitting a hollow example object), so
    it does NOT count as grounding."""
    return any(bool(ex.given) or ex.expect is not None for ex in c.examples)


def _has_hollow_examples(c: AcceptanceCriterion) -> bool:
    """The model emitted example object(s) but left every one entirely empty (given={},
    expect=None). Worse than an empty list — it signals intent to exemplify with no data —
    and must be rejected so the planner either fills them or drops the examples list."""
    return bool(c.examples) and not _has_useful_example(c)


def _is_ungroundable(c: AcceptanceCriterion) -> bool:
    """A criterion the Tester can't turn into a real assertion: file/directory-existence
    checks, or tautological/meta statements (unless it carries a USEFUL IOExample, which
    gives the Tester something deterministic to assert). A hollow example does not count."""
    text = c.text.strip().lower()
    if _has_useful_example(c):
        return False
    if text.startswith(("file exists", "directory exists")):
        return True
    return bool(_TAUTOLOGICAL_RE.search(text))


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
    only_ungroundable_criteria: bool
    no_test_strategy: bool


def _check_microtask(task: Microtask) -> _MicrotaskFlags:
    ungroundable = [c for c in task.acceptance_criteria if _is_ungroundable(c)]
    empty_text = [c for c in task.acceptance_criteria if not c.text.strip()]
    return _MicrotaskFlags(
        # no criteria at all, OR a criterion with empty text — both leave the Tester nothing to ground on
        no_acceptance_criteria=(not task.acceptance_criteria) or bool(empty_text),
        no_target_files=not task.target_files,
        target_files_count=len(task.target_files),
        description_len=len(task.description),
        approach_len=len(task.approach),
        # every criterion is existence-only or tautological → the microtask has no single
        # groundable behaviour for the Tester to assert on.
        only_ungroundable_criteria=bool(task.acceptance_criteria) and len(ungroundable) == len(task.acceptance_criteria),
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
        if flags.only_ungroundable_criteria:
            issues.append("FAIL  acceptance_criteria are all tautological/existence-only "
                          "('all tests pass', 'file exists') — needs observable behaviour + example")
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


# The sandbox always clones the repo to /workspace/repo (bootstrapper). Any target/context
# path must resolve UNDER that root.
_REPO_ROOT = "/workspace/repo"


def _normalize_repo_path(path: str) -> str:
    """Re-root a planner-emitted file path to the real sandbox repo root.

    The synthesis contract asks for `/workspace/repo/...` absolute paths, but a weaker model
    drops the `repo` segment and emits `/workspace/astropy/modeling/separable.py` (run
    b6487b43). That path exists NOWHERE — the repo is at `/workspace/repo/astropy/...` — so
    the coder's pre-read comes back empty, its edit (if any) lands outside the git tree
    (invisible to `git diff`), and the tester's build check hits `[Errno 2]`. The coder then
    rationalises "file already correct, no change needed" and the microtask loops to BLOCKED
    on a path typo. Normalise every form to an absolute path under the repo root:
      /workspace/repo/x  → unchanged
      /workspace/x       → /workspace/repo/x   (the dropped-`repo` bug)
      /x  or  x          → /workspace/repo/x   (any other absolute/relative → repo-relative)
    """
    p = path.strip()
    if not p:
        return p
    if p == _REPO_ROOT or p.startswith(_REPO_ROOT + "/"):
        return p
    if p.startswith("/workspace/"):
        return _REPO_ROOT + "/" + p[len("/workspace/"):]
    return _REPO_ROOT + "/" + p.lstrip("/")


def _normalize_paths(paths: List[str]) -> List[str]:
    return [_normalize_repo_path(p) for p in paths]


# Imported to the planner.py
def _normalize_output(result: "PlannerOutput") -> "PlannerOutput":
    from agents.Planning.Planner.planner import PlannerOutput

    microtasks: List[Microtask] = []
    errors: List[str] = []

    for idx, task in enumerate(result.microtasks):
        # Re-root file paths BEFORE the quality checks read them — the coder, its pre-read,
        # and the tester's build check all consume these, so a dropped-`repo` typo must be
        # corrected here, at the single point microtasks are finalised.
        task = task.model_copy(update={
            "target_files": _normalize_paths(task.target_files),
            "context_files": _normalize_paths(task.context_files or []),
        })
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

        if flags.only_ungroundable_criteria:
            errors.append(
                f"{tid}: acceptance_criteria are all tautological or existence-only "
                "('all tests in X pass', 'file exists', 'it works') — the Tester can't ground a real "
                "assertion on these. State at least one OBSERVABLE BEHAVIOUR, and for any precise/computed "
                "value give a concrete example "
                "(e.g. {'text': \"findCity('London') returns the London entry\", "
                "'examples': [{'given': {'q': 'London'}, 'expect': {'name': 'London'}}]})"
            )

        hollow = [i + 1 for i, c in enumerate(task.acceptance_criteria) if _has_hollow_examples(c)]
        if hollow:
            errors.append(
                f"{tid}: acceptance_criteria {hollow} have EMPTY examples "
                "(given={} and expect=null) — a hollow example is worse than none. Either FILL each "
                "with real data (e.g. 'given': {'value': 18, 'unit': 'F'}, 'expect': 64.4) or set "
                "'examples': [] and describe the behaviour in `text`. Never emit an example object "
                "with no data."
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
