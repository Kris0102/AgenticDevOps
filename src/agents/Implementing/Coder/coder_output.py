from datetime import datetime
from observability import Category, get_logger
from typing import Any, Dict, List, Optional
from pathlib import Path
from orchestrator.state import (
    Microtask,
)
from providers import TokenUsage
_log = get_logger(Category.CODER)

def create_code_file_log(
        run_id: str,
        microtask: Microtask,
        diff_text: str,
        files_touched: List[str],
        attempts_log: List[Dict[str, Any]],
        reflections: Optional[List] = None,
        lessons_block: str = "",
        token_usage: Optional[TokenUsage] = None,
    ) -> None:
        """Write a structured record of this microtask's coding attempts to a
        per-iteration file: coder_microtask_<n>_iteration_<m>.txt."""
        try:
            src_dir = Path(__file__).resolve().parents[3]  # stable `src/` — not the launcher (uvicorn vs main.py)
            _hash = run_id[:8] if len(run_id) >= 8 else run_id
            output_dir = Path(src_dir, "output")
            existing = sorted(output_dir.glob(f"*_{_hash}")) if output_dir.exists() else []
            if existing:
                folder = existing[0] / "implementing"
            else:
                _date = datetime.now().strftime('%Y-%m-%d_%H-%M')
                folder = output_dir / f"{_date}_{_hash}" / "implementing"
            mt_num = microtask.idx + 1
            existing_count = len(list(folder.glob(f"coder_microtask_{mt_num}_iteration_*.txt"))) if folder.exists() else 0
            iteration = existing_count + 1
            file_path = folder / f"coder_microtask_{mt_num}_iteration_{iteration}.txt"
            folder.mkdir(parents=True, exist_ok=True)

            sep = "=" * 70
            diff_produced = bool(diff_text and diff_text.strip())

            _log.debug("coder.writing_code_log", path=str(file_path))
            with open(file_path, "w", encoding="utf-8") as f:
                # ── Header ────────────────────────────────────────────────────
                f.write(f"{sep}\n")
                f.write(f"  CODER OUTPUT  |  run_id: {run_id}\n")
                f.write(f"  Generated:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                if token_usage:
                    f.write(f"  Tokens:       {token_usage.input_tokens} in / {token_usage.output_tokens} out / {token_usage.total_tokens} total\n")
                f.write(f"{sep}\n\n")

                # ── Outcome ───────────────────────────────────────────────────
                f.write("OUTCOME\n")
                f.write("-" * 40 + "\n")
                f.write(f"Status:        {'diff produced' if diff_produced else 'no diff produced'}\n")
                f.write(f"Attempts:      {len(attempts_log)}\n")
                f.write(f"Files Touched: {', '.join(files_touched) if files_touched else '(none)'}\n")
                f.write("\n")

                # ── Retrieved lessons injected into the coder prompt ──────────
                f.write(f"{sep}\n")
                f.write("RETRIEVED LESSONS (injected into prompt)\n")
                f.write(f"{sep}\n\n")
                f.write((lessons_block or "No past-run lessons retrieved.") + "\n\n")

                # ── Prior Reflections ─────────────────────────────────────────
                task_reflections = [
                    r for r in (reflections or [])
                    if getattr(r, "microtask_id", None) in (None, microtask.microtask_id)
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

                # ── Microtask ─────────────────────────────────────────────────
                f.write(f"{sep}\n")
                f.write("MICROTASK\n")
                f.write(f"{sep}\n\n")

                f.write(f"Title:  {microtask.title}\n")
                f.write(f"ID:     {microtask.microtask_id}\n\n")

                f.write("Target Files:\n")
                if microtask.target_files:
                    for tf in microtask.target_files:
                        f.write(f"  • {tf}\n")
                else:
                    f.write("  (none)\n")
                f.write("\n")

                f.write("Context Files:\n")
                if microtask.context_files:
                    for cf in microtask.context_files:
                        f.write(f"  • {cf}\n")
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

                # ── Final Diff ────────────────────────────────────────────────
                f.write(f"{sep}\n")
                f.write("FINAL DIFF\n")
                f.write(f"{sep}\n\n")

                if diff_produced:
                    f.write(diff_text)
                    if not diff_text.endswith("\n"):
                        f.write("\n")
                else:
                    f.write("(no diff produced)\n")
                f.write("\n")

                # ── Attempt Transcripts ───────────────────────────────────────
                if attempts_log:
                    f.write(f"{sep}\n")
                    f.write(f"ATTEMPT TRANSCRIPTS  ({len(attempts_log)} total)\n")
                    f.write(f"{sep}\n\n")

                    for entry in attempts_log:
                        attempt = entry.get("attempt", "?")
                        # "diff produced" alone hid the case this file exists to debug: a diff
                        # full of some OTHER microtask's files while this one's targets were
                        # never opened (run 80240f95). Name it.
                        untouched = entry.get("untouched_targets") or []
                        if entry.get("diff_empty"):
                            outcome = "no diff — retrying"
                        elif untouched:
                            outcome = f"diff produced, but {len(untouched)} target(s) untouched: " \
                                      + ", ".join(untouched)
                        else:
                            outcome = "diff produced"
                        f.write(f"{'─' * 70}\n")
                        f.write(f"ATTEMPT {attempt}  [{outcome}]\n")
                        f.write(f"{'─' * 70}\n\n")

                        f.write("PROMPT:\n")
                        prompt_text = entry.get("prompt", "(none)")
                        f.write(prompt_text)
                        if not prompt_text.endswith("\n"):
                            f.write("\n")
                        f.write("\n")

                        f.write("REACT TRANSCRIPT:\n")
                        transcript_text = entry.get("transcript", "(none)")
                        f.write(transcript_text)
                        if not transcript_text.endswith("\n"):
                            f.write("\n")
                        f.write("\n")

            _log.info("coder.output_file_written", path=str(file_path))

        except Exception as exc:
            _log.warning("coder.output_file_failed", error=str(exc))
            