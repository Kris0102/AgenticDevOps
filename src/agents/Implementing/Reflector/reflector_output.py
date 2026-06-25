import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from observability import Category, get_logger
from orchestrator.state import Microtask, Reflection

_log = get_logger(Category.REFLECTOR)


def _output_folder(run_id: str, src_dir: Path) -> Path:
    short_hash = run_id[:8] if len(run_id) >= 8 else run_id
    output_dir = Path(src_dir, "output")
    if output_dir.exists():
        matches = sorted(output_dir.glob(f"*_{short_hash}"))
        if matches:
            return matches[0] / "implementing"
    date_str = datetime.now().strftime('%Y-%m-%d_%H-%M')
    return output_dir / f"{date_str}_{short_hash}" / "implementing"


def _next_iteration(folder: Path, glob_pattern: str) -> int:
    if not folder.exists():
        return 1
    return len(list(folder.glob(glob_pattern))) + 1


def create_reflection_file_as_output(
    run_id: str,
    microtask: Microtask,
    investigation_summary: str,
    reflection: Reflection,
    test_output: str,
    diff: str,
    rejection_note: Optional[str] = None,
    test_passed: bool = False,
    approved: bool = False,
    prior_reflections: Optional[List[Reflection]] = None,
) -> str:
    """Write the reflector output to disk and return the absolute path."""
    try:
        src_dir = Path(__file__).resolve().parents[3]  # stable `src/` — not the launcher (uvicorn vs main.py)
        folder = _output_folder(run_id, src_dir)
        mt_num = microtask.idx + 1
        iteration = _next_iteration(folder, f"reflector_microtask_{mt_num}_iteration_*.txt")
        file_path = folder / f"reflector_microtask_{mt_num}_iteration_{iteration}.txt"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        sep = "=" * 70

        with open(file_path, "w", encoding="utf-8") as f:
            # ── Header ────────────────────────────────────────────────────────
            f.write(f"{sep}\n")
            label = "REFLECTOR OUTPUT (RE-REFLECTION)" if rejection_note else "REFLECTOR OUTPUT"
            f.write(f"  {label}  |  run_id: {run_id}\n")
            f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{sep}\n\n")

            # ── Diagnosis summary ─────────────────────────────────────────────
            mode = "QUALITY REVIEW" if test_passed else "FAILURE DIAGNOSIS"
            decision_label = "APPROVE" if approved else "IMPROVE"
            f.write(f"ASSESSMENT  [{mode}]\n")
            f.write("-" * 40 + "\n")
            f.write(f"Decision:      {decision_label}\n")
            f.write(f"Failure Type:  {reflection.failure_type}\n")
            f.write(f"Microtask ID:  {reflection.microtask_id or '(none)'}\n")
            if rejection_note:
                f.write("Re-reflection: yes (human feedback applied)\n")
            f.write("\n")

            # ── Prior reflections for this microtask ─────────────────────────
            task_reflections = [
                r for r in (prior_reflections or [])
                if r.microtask_id in (None, microtask.microtask_id)
            ]
            if task_reflections:
                f.write(f"{sep}\n")
                f.write(f"PRIOR REFLECTIONS  ({len(task_reflections)} total)\n")
                f.write(f"{sep}\n\n")
                for i, r in enumerate(task_reflections, 1):
                    f.write(f"[{i}] Type: {r.failure_type}\n")
                    f.write(f"    Root Cause: {r.root_cause}\n")
                    f.write("    Fix Instructions:\n")
                    for line in r.fix_instructions.splitlines():
                        f.write(f"      {line}\n")
                    f.write("\n")

            # ── Human feedback (re-reflection only) ───────────────────────────
            if rejection_note:
                f.write(f"{sep}\n")
                f.write("HUMAN REVIEWER FEEDBACK (REJECTED PREVIOUS DIAGNOSIS)\n")
                f.write(f"{sep}\n\n")
                for line in rejection_note.splitlines():
                    f.write(f"  {line}\n")
                f.write("\n")

            # ── Microtask ─────────────────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write("MICROTASK\n")
            f.write(f"{sep}\n\n")

            f.write(f"Title:  {microtask.title}\n")
            f.write(f"ID:     {microtask.microtask_id}\n")
            f.write(f"Status: {microtask.status.value if hasattr(microtask.status, 'value') else microtask.status}\n\n")

            f.write("Target Files:\n")
            if microtask.target_files:
                for tf in microtask.target_files:
                    f.write(f"  • {tf}\n")
            else:
                f.write("  (none)\n")
            f.write("\n")

            f.write("Description:\n")
            for line in microtask.description.splitlines():
                f.write(f"  {line}\n")
            f.write("\n")

            f.write("Acceptance Criteria:\n")
            if microtask.acceptance_criteria:
                for ac in microtask.acceptance_criteria:
                    f.write(f"  ✓ {ac.render()}\n")
            else:
                f.write("  (none)\n")
            f.write("\n")

            # ── Test output ───────────────────────────────────────────────────
            if test_output:
                f.write(f"{sep}\n")
                f.write("TEST OUTPUT (input to reflection)\n")
                f.write(f"{sep}\n\n")
                f.write(test_output[:5000])
                if len(test_output) > 5000:
                    f.write("\n[... truncated ...]\n")
                f.write("\n")

            # ── Code diff ─────────────────────────────────────────────────────
            if diff:
                f.write(f"{sep}\n")
                f.write("CODE CHANGES (git diff)\n")
                f.write(f"{sep}\n\n")
                f.write(diff[:3000])
                if len(diff) > 3000:
                    f.write("\n[... truncated ...]\n")
                f.write("\n")

            # ── Investigation summary ─────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write("INVESTIGATION SUMMARY\n")
            f.write(f"{sep}\n\n")
            f.write(investigation_summary)
            if not investigation_summary.endswith("\n"):
                f.write("\n")
            f.write("\n")

            # ── Structured reflection ─────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write("STRUCTURED REFLECTION\n")
            f.write(f"{sep}\n\n")

            f.write(f"Failure Type:               {reflection.failure_type}\n")
            f.write(f"Store in Semantic Memory:   {reflection.should_store_in_semantic_memory}\n\n")

            f.write("Root Cause:\n")
            for line in reflection.root_cause.splitlines():
                f.write(f"  {line}\n")
            f.write("\n")

            f.write("Fix Instructions (for the Coder):\n")
            for line in reflection.fix_instructions.splitlines():
                f.write(f"  {line}\n")
            f.write("\n")

        _log.info("reflector.output_file_written", path=str(file_path))
        return str(file_path)

    except Exception as exc:
        _log.warning("reflector.output_file_failed", error=str(exc))
        return ""
