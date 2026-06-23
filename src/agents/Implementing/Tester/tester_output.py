import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from observability import Category, get_logger
from orchestrator.state import Microtask, Reflection

_log = get_logger(Category.TESTER)


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


def create_test_file_as_output(
    run_id: str,
    microtask: Microtask,
    project_type: str,
    test_file: str,
    test_file_content: str,
    check_result: Dict[str, Any],
    rejection_note: Optional[str] = None,
    reflections: Optional[List[Reflection]] = None,
) -> str:
    """Write the tester output to disk and return the absolute path."""
    try:
        src_dir = Path(os.path.abspath(sys.argv[0])).parent
        folder = _output_folder(run_id, src_dir)
        mt_num = microtask.idx + 1
        iteration = _next_iteration(folder, f"tester_microtask_{mt_num}_iteration_*.txt")
        file_path = folder / f"tester_microtask_{mt_num}_iteration_{iteration}.txt"
        file_path.parent.mkdir(parents=True, exist_ok=True)

        sep = "=" * 70

        with open(file_path, "w", encoding="utf-8") as f:
            # ── Header ────────────────────────────────────────────────────────
            f.write(f"{sep}\n")
            label = "TESTER OUTPUT (REWRITE)" if rejection_note else "TESTER OUTPUT"
            f.write(f"  {label}  |  run_id: {run_id}\n")
            f.write(f"  Generated:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{sep}\n\n")

            # ── Verdict ───────────────────────────────────────────────────────
            f.write("TEST VERDICT\n")
            f.write("-" * 40 + "\n")
            f.write(f"Status:       {check_result.get('status', 'unknown')}\n")
            f.write(f"Project Type: {project_type}\n")
            if test_file:
                f.write(f"Test File:    {test_file}\n")
            if rejection_note:
                f.write(f"Rewrite:      yes (human feedback applied)\n")
            f.write("\n")

            # ── Human Feedback (only present on rewrites) ─────────────────────
            if rejection_note:
                f.write(f"{sep}\n")
                f.write("HUMAN REVIEWER FEEDBACK\n")
                f.write(f"{sep}\n\n")
                for line in rejection_note.splitlines():
                    f.write(f"  {line}\n")
                f.write("\n")

            # ── Prior Reflector Feedback ──────────────────────────────────────
            task_reflections = [
                r for r in (reflections or [])
                if r.microtask_id in (None, microtask.microtask_id)
            ]
            if task_reflections:
                f.write(f"{sep}\n")
                f.write(f"PRIOR REFLECTOR FEEDBACK  ({len(task_reflections)} total)\n")
                f.write(f"{sep}\n\n")
                for i, r in enumerate(task_reflections, 1):
                    f.write(f"[{i}] Type: {r.failure_type}\n")
                    f.write(f"    Root Cause: {r.root_cause}\n")
                    f.write("    Fix Instructions:\n")
                    for line in r.fix_instructions.splitlines():
                        f.write(f"      {line}\n")
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

            if microtask.approach:
                f.write("Approach:\n")
                for line in microtask.approach.splitlines():
                    f.write(f"  {line}\n")
                f.write("\n")

            f.write("Context Files:\n")
            if microtask.context_files:
                for cf in microtask.context_files:
                    f.write(f"  • {cf}\n")
            else:
                f.write("  (none)\n")
            f.write("\n")

            f.write("Acceptance Criteria:\n")
            if microtask.acceptance_criteria:
                for ac in microtask.acceptance_criteria:
                    f.write(f"  ✓ {ac}\n")
            else:
                f.write("  (none)\n")
            f.write("\n")

            f.write(f"Test Strategy:\n  {microtask.test_strategy or '(none)'}\n")

            f.write("\n" + "-" * 70 + "\n\n")

            # ── Written Tests ─────────────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write("WRITTEN TESTS\n")
            f.write(f"{sep}\n\n")

            if test_file_content:
                f.write(test_file_content)
                if not test_file_content.endswith("\n"):
                    f.write("\n")
            else:
                f.write("(no test file written)\n")
            f.write("\n")

            # ── Test Output ───────────────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write("TEST OUTPUT\n")
            f.write(f"{sep}\n\n")

            command = check_result.get("command", [])
            f.write(f"Command:   {' '.join(command) if command else '(none)'}\n")
            f.write(f"Exit Code: {check_result.get('exit_code', '-')}\n")
            f.write(f"Duration:  {check_result.get('duration_ms', 0)} ms\n\n")

            stdout = check_result.get("stdout", "")
            if stdout:
                f.write("STDOUT:\n")
                f.write(stdout)
                if not stdout.endswith("\n"):
                    f.write("\n")
                f.write("\n")

            stderr = check_result.get("stderr", "")
            if stderr:
                f.write("STDERR:\n")
                f.write(stderr)
                if not stderr.endswith("\n"):
                    f.write("\n")
                f.write("\n")

            if not stdout and not stderr:
                f.write("(no output)\n")

        _log.info("tester.output_file_written", path=str(file_path))
        return str(file_path)

    except Exception as e:
        _log.warning("tester.output_file_failed", error=str(e))
        return ""
