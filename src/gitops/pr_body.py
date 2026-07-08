"""Deterministic PR description for the deploy auto-PR (ADR 0027).

Built purely from checkpointed run data — objective, microtasks, the pushed fileset —
so the description is reproducible and auditable. Deliberately NO LLM call: deploy is
a deterministic terminal step, and the run data already contains the ground truth.
"""
from typing import List, Optional

from orchestrator.state import Microtask, MicrotaskStatus, Objective

# Keep huge runs from producing an unreadable PR body.
_MAX_LISTED_FILES = 50

_DONE_STATUSES = {MicrotaskStatus.COMPLETED, MicrotaskStatus.DONE}


def build_pr_body(
    *,
    run_id: str,
    objective: Optional[Objective],
    microtasks: List[Microtask],
    files: List[str],
    diff_summary: Optional[str],
    branch: str,
    base: str,
) -> str:
    """Markdown PR body: objective, microtask checklist, shipped files, diff summary."""
    sections: List[str] = []

    if objective is not None:
        sections.append(f"## Objective\n**{objective.title}**\n\n{objective.description}")

    if microtasks:
        lines = [
            f"- [{'x' if mt.status in _DONE_STATUSES else ' '}] {mt.idx}. {mt.title}"
            for mt in sorted(microtasks, key=lambda mt: mt.idx)
        ]
        sections.append("## Microtasks\n" + "\n".join(lines))

    if files:
        listed = [f"- `{path}`" for path in files[:_MAX_LISTED_FILES]]
        overflow = len(files) - _MAX_LISTED_FILES
        if overflow > 0:
            listed.append(f"- …and {overflow} more")
        sections.append("## Changed files\n" + "\n".join(listed))
    else:
        sections.append("## Changed files\n_none reported_")

    if diff_summary:
        sections.append(f"## Diff summary\n{diff_summary}")

    sections.append(f"_Automated by AgenticDevOps — run {run_id} ({branch} → {base})._")
    return "\n\n".join(sections)
