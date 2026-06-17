import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from observability import Category, get_logger

_log = get_logger(Category.REVIEWER)

if TYPE_CHECKING:
    from agents.Planning.Reviewer.reviewer import ReviewerOutput


def create_review_file_as_output(
    objective_text: str,
    review_context: str,
    result: "ReviewerOutput",
    run_id: str,
    revision_context: Optional[str] = None,
) -> str:
    """Write the review to disk and return the absolute path."""
    try:
        initial_file_path = Path(os.path.abspath(sys.argv[0])).parent  # src folder
        file_path = Path(initial_file_path, "output", f"{run_id}", f"review_output_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt")
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
            f.write(f"Notes: {result.notes}\n\n")

            if revision_context:
                f.write("REVISION CONTEXT\n")
                f.write("-" * 40 + "\n")
                f.write(revision_context + "\n\n")

            f.write(f"{sep}\n")
            f.write("OBJECTIVE\n")
            f.write(f"{sep}\n\n")
            f.write(objective_text + "\n\n")

            f.write(f"{sep}\n")
            f.write("REVIEWED PLANNER OUTPUT\n")
            f.write(f"{sep}\n\n")
            try:
                parsed = json.loads(review_context)
                f.write(json.dumps(parsed, indent=2, ensure_ascii=True) + "\n")
            except Exception:
                f.write(review_context + "\n")

        _log.info("reviewer.review_file_written", path=str(file_path))
        return str(file_path)

    except Exception as error:
        _log.warning("reviewer.review_file_failed", error=str(error))
        return ""