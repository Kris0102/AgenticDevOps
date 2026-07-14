"""Deterministic default commit message for the deploy push (ADR 0027).

Sibling of `pr_body.py`, same rules: built purely from checkpointed run data (objective,
microtask titles, changed fileset), no LLM — deploy is a deterministic terminal step and
the run data already holds the ground truth.

Lives here, not on DeployRunner, because TWO callers must produce the SAME string: the
deploy node (the message it actually pushes) and the deploy HITL gate (the message it
prefills into the human's editable box). When the gate prefilled something else, the human
submitted that back verbatim and the real default became unreachable — see ADR 0035.
"""
from typing import List, Optional

from orchestrator.state import Microtask, Objective

# Keep a huge run from producing an unreadable commit body.
_MAX_LISTED_FILES = 50

# Git convention: the subject is a short summary line, kept under ~72 chars so `git log
# --oneline` and GitHub don't truncate it mid-word.
_MAX_SUBJECT = 72


def build_commit_message(
    *,
    run_id: str,
    objective: Optional[Objective],
    microtasks: List[Microtask],
    files: List[str],
) -> str:
    """`<objective title>` subject + objective/microtask/file body, git-formatted
    (subject, blank line, body). Falls back to the run id as the subject when the run has
    no objective title to speak for it."""
    title = (objective.title.strip() if objective and objective.title else "")
    subject = (title or f"AgenticDevOps run {run_id}")[:_MAX_SUBJECT]

    body: List[str] = []
    if objective is not None and objective.description.strip():
        body.append(f"Objective:\n{objective.description.strip()}")

    if microtasks:
        lines = "\n".join(f"- {mt.title}" for mt in sorted(microtasks, key=lambda mt: mt.idx))
        body.append(f"Microtasks:\n{lines}")

    if files:
        listed = [f"- {path}" for path in files[:_MAX_LISTED_FILES]]
        overflow = len(files) - _MAX_LISTED_FILES
        if overflow > 0:
            listed.append(f"- …and {overflow} more")
        body.append("Files touched:\n" + "\n".join(listed))

    body.append(f"Run: {run_id}")
    return f"{subject}\n\n" + "\n\n".join(body)
