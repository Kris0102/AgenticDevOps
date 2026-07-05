import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from observability import Category, get_logger
from orchestrator.state import ReviewFeedback

_log = get_logger(Category.REVIEWER)

if TYPE_CHECKING:
    from agents.Planning.Reviewer.reviewer import ReviewerOutput


def _format_planner_output(review_context: str) -> str:
    try:
        payload = json.loads(review_context)
    except Exception:
        return review_context

    if not isinstance(payload, dict):
        return review_context

    lines = []
    plan = payload.get("plan", {})
    microtasks = payload.get("microtasks", [])

    lines.append("PLAN")
    lines.append("-" * 40)
    lines.append(f"Summary: {plan.get('summary', '')}")
    lines.append(f"Architecture Notes: {plan.get('architecture_notes', '')}")
    lines.append("")

    data_contracts = plan.get("data_contracts") or []
    if data_contracts:
        lines.append("Data Contracts:")
        for contract in data_contracts:
            lines.append(f"  - {contract}")
        lines.append("")

    assumptions = plan.get("assumptions") or []
    if assumptions:
        lines.append("Assumptions:")
        for assumption in assumptions:
            lines.append(f"  - {assumption}")
        lines.append("")

    risks = plan.get("risks") or []
    if risks:
        lines.append("Risks:")
        for risk in risks:
            lines.append(f"  - {risk}")
        lines.append("")

    open_questions = plan.get("open_questions") or []
    if open_questions:
        lines.append("Open Questions:")
        for question in open_questions:
            lines.append(f"  - {question}")
        lines.append("")

    lines.append(f"MICROTASKS ({len(microtasks)} total)")
    lines.append("-" * 40)
    lines.append("")

    for index, task in enumerate(microtasks, start=1):
        lines.append(f"[{index}/{len(microtasks)}] {task.get('title', '')}")
        if task.get("microtask_id"):
            lines.append(f"  ID: {task['microtask_id']}")
        if task.get("dependencies"):
            lines.append(f"  Depends on: {', '.join(task['dependencies'])}")
        if task.get("target_files"):
            lines.append("  Target Files:")
            for target_file in task["target_files"]:
                lines.append(f"    - {target_file}")
        if task.get("description"):
            lines.append("  Description:")
            for line in str(task["description"]).splitlines():
                lines.append(f"    {line}")
        if task.get("approach"):
            lines.append("  Approach:")
            for line in str(task["approach"]).splitlines():
                lines.append(f"    {line}")
        if task.get("context_files"):
            lines.append("  Context Files:")
            for context_file in task["context_files"]:
                lines.append(f"    - {context_file}")
        if task.get("acceptance_criteria"):
            lines.append("  Acceptance Criteria:")
            for criterion in task["acceptance_criteria"]:
                # raw JSON dict after DB/checkpoint round-trip: {"text": ..., "examples": [...]}
                text = criterion["text"] if isinstance(criterion, dict) else str(criterion)
                lines.append(f"    - {text}")
        if task.get("test_strategy"):
            lines.append(f"  Test Strategy: {task['test_strategy']}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _output_folder(run_id: str, src_dir: Path) -> Path:
    short_hash = run_id[:8] if len(run_id) >= 8 else run_id
    output_dir = Path(src_dir, "output")
    if output_dir.exists():
        matches = sorted(output_dir.glob(f"*_{short_hash}"))
        if matches:
            return matches[0] / "planning"
    date_str = datetime.now().strftime('%Y-%m-%d_%H-%M')
    return output_dir / f"{date_str}_{short_hash}" / "planning"


def _next_iteration(folder: Path, glob_pattern: str) -> int:
    if not folder.exists():
        return 1
    return len(list(folder.glob(glob_pattern))) + 1


def create_review_file_as_output(
    objective_text: str,
    review_context: str,
    result: "ReviewerOutput",
    run_id: str,
    investigation_summary: Optional[str] = None,
    prior_feedback: Optional[List[ReviewFeedback]] = None,
    lessons_block: str = "",
) -> str:
    """Write the review to disk and return the absolute path."""
    try:
        src_dir = Path(__file__).resolve().parents[3]  # stable `src/` — not the launcher (uvicorn vs main.py)
        folder = _output_folder(run_id, src_dir)
        iteration = _next_iteration(folder, "reviewer_iteration_*.txt")
        file_path = folder / f"reviewer_iteration_{iteration}.txt"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        sep = "=" * 70

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"{sep}\n")
            f.write(f"  REVIEW OUTPUT  |  run_id: {run_id}\n")
            f.write(f"  Generated:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{sep}\n\n")

            f.write("REVIEW DECISION\n")
            f.write("-" * 40 + "\n")
            f.write(f"Decision: {result.decision.value}\n")
            f.write(f"Notes: {result.notes}\n")
            if getattr(result, "blockers", None):
                f.write("Blockers (fed to planner):\n")
                for b in result.blockers:
                    f.write(f"  - {b}\n")
            f.write("\n")

            if prior_feedback:
                f.write(f"{sep}\n")
                f.write(f"PRIOR REVIEW ROUNDS  ({len(prior_feedback)} total)\n")
                f.write(f"{sep}\n\n")
                for i, fb in enumerate(prior_feedback, 1):
                    f.write(f"[{i}] {fb.source.value} — {fb.decision.value}\n")
                    for line in fb.notes.splitlines():
                        f.write(f"    {line}\n")
                    f.write("\n")

            f.write(f"{sep}\n")
            f.write("OBJECTIVE\n")
            f.write(f"{sep}\n\n")
            f.write(objective_text + "\n\n")

            f.write(f"{sep}\n")
            f.write("PLANNER OUTPUT\n")
            f.write(f"{sep}\n\n")
            f.write(_format_planner_output(review_context) + "\n\n")

            f.write(f"{sep}\n")
            f.write("INVESTIGATION SUMMARY\n")
            f.write(f"{sep}\n\n")
            f.write((investigation_summary or "No reviewer investigation summary provided.") + "\n\n")

            f.write(f"{sep}\n")
            f.write("RETRIEVED LESSONS (injected into prompt)\n")
            f.write(f"{sep}\n\n")
            f.write((lessons_block or "No past-run lessons retrieved.") + "\n")

        _log.info("reviewer.review_file_written", path=str(file_path))
        return str(file_path)

    except Exception as error:
        _log.warning("reviewer.review_file_failed", error=str(error))
        return ""
