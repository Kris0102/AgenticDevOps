import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from agents.Planning.Planner.quality import _quality_report
from observability import Category, get_logger

_log = get_logger(Category.PLANNER)

if TYPE_CHECKING:
    from agents.Planning.Planner.planner import PlannerOutput


def create_plan_file_as_output(investigation_summary: str, result: "PlannerOutput", run_id: str, revision_context: Optional[str]) -> str:
    """Write the plan to disk and return the absolute path."""
    try:
        initial_file_path = Path(os.path.abspath(sys.argv[0])).parent  # src folder
        file_path = Path(initial_file_path, "output", f"{run_id}", f"plan_output_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt")
        file_path.parent.mkdir(parents=True, exist_ok=True)

        sep = "=" * 70

        with open(file_path, "w") as f:
            # ── Header ────────────────────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write(f"  PLAN OUTPUT  |  run_id: {run_id}\n")
            f.write(f"  Generated:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{sep}\n\n")

            # ── Coder Readiness ───────────────────────────────────────────────
            f.write("CODER READINESS CHECK\n")
            f.write("-" * 40 + "\n")
            f.write(_quality_report(result) + "\n\n")
            if revision_context:
                f.write("REVISION CONTEXT \n")
                f.write("-" * 40 + "\n")
                f.write(revision_context + "\n\n")
            # ── Plan ─────────────────────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write("PLAN\n")
            f.write(f"{sep}\n\n")
            f.write(f"Summary:\n  {result.plan.summary}\n\n")
            f.write(f"Architecture Notes:\n  {result.plan.architecture_notes}\n\n")

            if result.plan.data_contracts:
                f.write("Data Contracts (shared types — all microtasks must honour these):\n")
                for contract in result.plan.data_contracts:
                    f.write(f"  • {contract}\n")
                f.write("\n")
            else:
                f.write("Data Contracts: NONE DEFINED\n\n")

            if result.plan.assumptions:
                f.write("Assumptions:\n")
                for a in result.plan.assumptions:
                    f.write(f"  • {a}\n")
                f.write("\n")

            if result.plan.risks:
                f.write("Risks:\n")
                for r in result.plan.risks:
                    f.write(f"  ⚠ {r}\n")
                f.write("\n")

            if result.plan.open_questions:
                f.write("Open Questions:\n")
                for q in result.plan.open_questions:
                    f.write(f"  ? {q}\n")
                f.write("\n")

            # ── Microtasks ────────────────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write(f"MICROTASKS  ({len(result.microtasks)} total)\n")
            f.write(f"{sep}\n\n")

            for idx, task in enumerate(result.microtasks):
                f.write(f"[{idx + 1}/{len(result.microtasks)}] {task.title}\n")
                f.write(f"    ID:     {task.microtask_id}\n")

                if task.dependencies:
                    f.write(f"    Depends on: {', '.join(task.dependencies)}\n")

                f.write(f"\n    Target Files:\n")
                if task.target_files:
                    for tf in task.target_files:
                        f.write(f"      • {tf}\n")
                else:
                    f.write("      !! MISSING — Coder has no file contract !!\n")

                f.write(f"\n    Description:\n")
                for line in task.description.splitlines():
                    f.write(f"      {line}\n")

                f.write(f"\n    Approach:\n")
                if task.approach:
                    for line in task.approach.splitlines():
                        f.write(f"      {line}\n")
                else:
                    f.write("      !! MISSING !!\n")

                f.write(f"\n    Context Files (read, not edit):\n")
                if task.context_files:
                    for cf in task.context_files:
                        f.write(f"      • {cf}\n")
                else:
                    f.write("      (none)\n")

                f.write(f"\n    Acceptance Criteria:\n")
                if task.acceptance_criteria:
                    for ac in task.acceptance_criteria:
                        f.write(f"      ✓ {ac}\n")
                else:
                    f.write("      !! MISSING !!\n")

                f.write(f"\n    Test Strategy:\n")
                f.write(f"      {task.test_strategy or '!! MISSING !!'}\n")

                f.write("\n" + "-" * 70 + "\n\n")

            # ── Investigation Summary ─────────────────────────────────────────
            f.write(f"{sep}\n")
            f.write("INVESTIGATION SUMMARY\n")
            f.write(f"{sep}\n\n")
            f.write(investigation_summary + "\n")

        _log.info("planner.plan_file_written", path=str(file_path))
        return str(file_path)

    except Exception as e:
        _log.warning("planner.plan_file_failed", error=str(e))
        return ""
