import os
from datetime import datetime, timezone
import sys
from typing import Any, Dict, List, Optional

from pathlib import Path
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError
from pydantic import SecretStr

from agents.Implementing.Coder.coder_support import format_react_transcript
from observability import Category, get_logger
from agents.Implementing.Coder.coder_tools import CoderTools
from agents.Implementing.Coder.prompt.coder_prompt import (
    CODER_SYSTEM_PROMPT,
    build_coder_prompt,
)
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.state import (
    Artifact,
    BlackboardState,
    Diff,
    Microtask,
    MicrotaskStatus,
    RunStatus,
)
from tool_gateway.Roles import Roles

# Load environment variables from .env if present
load_dotenv()

_log = get_logger(Category.CODER)


def _max_tool_steps() -> int:
    raw = os.getenv("CODER_MAX_TOOL_STEPS", "16")
    try:
        return max(1, int(raw))
    except ValueError:
        return 16


def _recursion_limit(max_tool_steps: int) -> int:
    # Each tool round is typically model + tools nodes (≈2 supersteps).
    return max_tool_steps * 2 + 5


def _files_from_diff(diff_text: str) -> List[str]:
    """Derive touched files from the actual git diff — never from LLM self-report."""
    files: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            # format: diff --git a/<path> b/<path>
            b_part = line.split(" b/", 1)
            if len(b_part) == 2:
                path = b_part[1].strip().strip('"')
                if path not in files:
                    files.append(path)
    return files


class Coder:
    """Implements exactly ONE microtask per invocation by editing files in the
    sandbox through gateway-routed tools.

    Single ReAct loop — no separate investigate phase like the Planner: the
    Planner needs one because its final artifact is structured JSON (json_mode
    can't interleave tool calls); the Coder's final artifact IS the file edits,
    so the tool calls are the work. Grounding comes from pre-fetched real file
    contents injected into the prompt, and the result is verified by a
    deterministic repo.diff call after the loop (empty diff → retry with
    feedback).
    """

    def __init__(self, run_id: str, coder_tools: CoderTools):
        self.run_id = run_id
        self.coder_tools = coder_tools
        self.name = Roles.CODER.value
        self.max_tool_steps = _max_tool_steps()
        self.max_empty_diff_retries = int(os.getenv("CODER_MAX_EMPTY_DIFF_RETRIES", "2"))
        self.timeout_seconds = int(os.getenv("CODER_TIMEOUT_SECONDS", "120"))

        # LLM Initialization
        api_key = os.getenv("CLUSTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "Missing API key. Set CLUSTER_API_KEY (preferred) or OPENAI_API_KEY before running."
            )

        try:
            self.llm = ChatOpenAI(
                model=os.getenv("LLM_MODEL", "google/gemma-4-31B-it"),
                api_key=SecretStr(api_key),
                base_url="http://131.159.60.153:3000/inference",
                temperature=0.0,
                top_p=0.1,
                max_completion_tokens=8192,
                max_retries=int(os.getenv("LLM_RETRIES", "3")),
                timeout=self.timeout_seconds,
            )
            self.coding_agent = create_agent(
                self.llm,
                tools=self.coder_tools.build_tools(),
                system_prompt=CODER_SYSTEM_PROMPT,
            )
        except Exception as e:
            _log.error("coder.init_failed", error=str(e))
            raise

    def invoke(
        self,
        state: BlackboardState,
        microtask: Microtask,
        file_contents: Dict[str, str],
    ) -> Dict[str, Any]:
        """Run the coding loop for one microtask and return the node-result dict."""
        diff_text, attempts_log = self._code_until_diff(state, microtask, file_contents)

        files_touched = _files_from_diff(diff_text)
        constraints = state.get("constraints")
        self._enforce_forbidden_paths(
            files_touched, constraints.forbidden_paths if constraints else None
        )

        _log.info("coder.diff_produced", microtask_id=microtask.microtask_id,
                  title=microtask.title, files_touched=files_touched)
        self.create_code_file_log(self.run_id, microtask, diff_text, files_touched, attempts_log,
                                  reflections=list(state.get("reflections") or []))
        return self._build_result(state, microtask, diff_text, files_touched)

    def _code_until_diff(
        self,
        state: BlackboardState,
        microtask: Microtask,
        file_contents: Dict[str, str],
    ) -> tuple[str, List[Dict[str, Any]]]:
        """ReAct attempts until repo.diff (deterministic, gateway-routed) is non-empty.

        Returns the winning diff plus a per-attempt log (prompt + full ReAct
        transcript + outcome) for create_code_file_log."""
        retry_feedback: Optional[str] = None
        attempts_log: List[Dict[str, Any]] = []
        for attempt in range(1, self.max_empty_diff_retries + 1):
            plan = state.get("plan")
            if not plan:
                raise RunExecutionError(
                    phase="code",
                    reason="Missing plan in state — cannot ground coder prompt without it.",
                )
            prompt = build_coder_prompt(
                objective=state.get("objective"),
                plan=plan,
                microtask=microtask,
                file_contents=file_contents,
                reflections=list(state.get("reflections") or []),
                forbidden_paths=(state.get("constraints").forbidden_paths
                                 if state.get("constraints") else None),
                retry_feedback=retry_feedback,
            )
            _log.info("coder.attempt", microtask_id=microtask.microtask_id,
                      attempt=attempt, max_attempts=self.max_empty_diff_retries)
            try:
                agent_result = self.coding_agent.invoke(
                    {"messages": [HumanMessage(content=prompt)]},
                    config={"recursion_limit": _recursion_limit(self.max_tool_steps)},
                )
            except (APIConnectionError, APITimeoutError) as exception:
                _log.error("coder.cluster_unreachable", phase="coding",
                           hint="ensure VPN connected", error=str(exception))
                raise

            transcript = format_react_transcript(agent_result.get("messages", []))
            diff_text = self.coder_tools.get_repo_diff()
            diff_empty = not diff_text.strip()
            attempts_log.append({
                "attempt": attempt,
                "prompt": prompt,
                "transcript": transcript,
                "diff_empty": diff_empty,
            })
            if not diff_empty:
                return diff_text, attempts_log

            retry_feedback = (
                "Your previous attempt produced an EMPTY git diff — no file in the "
                "repo actually changed. You must call write_sandbox_file with the "
                "complete new content of each target file."
            )
            _log.warning("coder.empty_diff_retry", microtask_id=microtask.microtask_id, attempt=attempt)

        raise RunExecutionError(
            phase="code",
            reason=(
                f"Coder produced no changes for microtask '{microtask.microtask_id}' "
                f"after {self.max_empty_diff_retries} attempt(s)."
            ),
        )

    @staticmethod
    def _enforce_forbidden_paths(
        files_touched: List[str], forbidden_paths: Optional[List[str]]
    ) -> None:
        for path in files_touched:
            for forbidden in forbidden_paths or []:
                if path.startswith(forbidden.rstrip("/")):
                    raise RunExecutionError(
                        phase="code",
                        reason=f"Coder modified forbidden path '{path}' (rule: '{forbidden}').",
                    )

    def _build_result(
        self,
        state: BlackboardState,
        microtask: Microtask,
        diff_text: str,
        files_touched: List[str],
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        attempts = microtask.attempts + 1
        diff_artifact_ref = f"art_{self.run_id}_diff_{microtask.microtask_id}_a{attempts}"
        summary = (
            f"Microtask '{microtask.title}' attempt {attempts}: "
            f"{len(files_touched)} file(s) changed ({', '.join(files_touched)})"
        )

        updated_microtasks = [
            mt.model_copy(update={
                "status": MicrotaskStatus.VERIFYING,
                "attempts": attempts,
                "files_touched": files_touched,
            }) if mt.microtask_id == microtask.microtask_id else mt
            for mt in state.get("microtasks", [])
        ]

        repo = state.get("repo")
        evidence = state.get("evidence")
        new_evidence = evidence.model_copy(update={
            "latest_diff": Diff(
                base_sha=(repo.base_sha if repo else None) or "",
                diff_artifact_ref=diff_artifact_ref,
                summary=summary,
            ),
        })

        diff_artifact = Artifact(
            artifact_ref=diff_artifact_ref,
            kind="diff",
            created_at=now,
            summary=summary,
        )

        return {
            "status": RunStatus.NEEDS_TEST,
            "microtasks": updated_microtasks,
            "current_microtask_id": microtask.microtask_id,
            "evidence": new_evidence,
            "artifacts_index": list(state.get("artifacts_index") or []) + [diff_artifact],
            # consumed by CoderRunner._persist_results, not a BlackboardState key
            "_diff_text": diff_text,
        }

    def create_code_file_log(
        self,
        run_id: str,
        microtask: Microtask,
        diff_text: str,
        files_touched: List[str],
        attempts_log: List[Dict[str, Any]],
        reflections: Optional[List] = None,
    ) -> None:
        """Write a structured record of this microtask's coding attempts to a
        per-iteration file: coder_microtask_<n>_iteration_<m>.txt."""
        try:
            src_dir = Path(os.path.abspath(sys.argv[0])).parent
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
                f.write(f"{sep}\n\n")

                # ── Outcome ───────────────────────────────────────────────────
                f.write("OUTCOME\n")
                f.write("-" * 40 + "\n")
                f.write(f"Status:        {'diff produced' if diff_produced else 'no diff produced'}\n")
                f.write(f"Attempts:      {len(attempts_log)}\n")
                f.write(f"Files Touched: {', '.join(files_touched) if files_touched else '(none)'}\n")
                f.write("\n")

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
                        f.write(f"  ✓ {ac}\n")
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
                        outcome = "no diff — retrying" if entry.get("diff_empty") else "diff produced"
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
            
