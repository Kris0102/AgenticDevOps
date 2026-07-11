import os
from datetime import datetime, timezone
import sys
from typing import Any, Dict, List, Optional

from pathlib import Path
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from openai import APIConnectionError, APITimeoutError

from Exceptions.RetriableError import RetriableError, is_transient_llm_error
from pydantic import SecretStr

from agents.Implementing.Coder.coder_output import create_code_file_log
from agents.Implementing.Coder.coder_support import format_react_transcript
from observability import Category, get_logger
from agents.Implementing.Coder.coder_tools import CoderTools
from agents.Implementing.Coder.prompt.coder_prompt import (
    CODER_SYSTEM_PROMPT,
    build_coder_prompt,
)
from db.lessons import LessonStore
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.state import (
    Artifact,
    BlackboardState,
    Diff,
    Microtask,
    MicrotaskStatus,
    RunConfig,
    RunStatus,
)
from providers import TokenUsage, build_chat_llm, sum_usage
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

    def __init__(self, run_id: str, coder_tools: CoderTools, config: RunConfig):
        self.run_id = run_id
        self.coder_tools = coder_tools
        self.name = Roles.CODER.value
        self.config = config
        self.llm_config = config.llm.coder
        knobs = self.config.knobs
        self.max_tool_steps = knobs.coder_max_tool_steps
        self.max_empty_diff_retries = knobs.coder_max_empty_diff_retries
        self.max_compile_retries = knobs.coder_max_compile_retries
        self.timeout_seconds = knobs.coder_timeout_seconds
        self.token_usage = TokenUsage()

        try:
            self.llm = build_chat_llm(self.config.provider_for(self.llm_config), self.llm_config,
                                      timeout_seconds=self.timeout_seconds)
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
        self.token_usage = TokenUsage()
        # Advisory: top-k confirmed code lessons from PAST runs on this repo (ADR 0018).
        # Computed once (constant across empty-diff retries), injected into the prompt and
        # recorded in the debug log. Excludes this run — same-run reflections already reach
        # the coder via state.reflections.
        repo = state.get("repo")
        lessons_query = f"{microtask.title} {microtask.description} {' '.join(microtask.target_files)}"
        lessons_block = LessonStore.render_block(LessonStore.find_lessons(
            repo.repo_url if repo else "", lessons_query,
            scope="code", exclude_run_id=state.get("run_id"),
        ))
        diff_text, messages, attempts_log = self._code_until_diff(
            state, microtask, file_contents, lessons_block)
        # Deterministic compile gate (warm): if the changes don't compile, feed the error
        # back into the SAME conversation and let the coder fix it in-context — no cold
        # re-prompt. On exhaustion, proceed: the Tester's build check is the backstop.
        diff_text = self._ensure_compiles(microtask, messages, attempts_log)

        files_touched = _files_from_diff(diff_text)
        constraints = state.get("constraints")
        self._enforce_forbidden_paths(
            files_touched, constraints.forbidden_paths if constraints else None
        )

        _log.info("coder.diff_produced", microtask_id=microtask.microtask_id,
                  title=microtask.title, files_touched=files_touched)
        create_code_file_log(self.run_id, microtask, diff_text, files_touched, attempts_log,
                                  reflections=list(state.get("reflections") or []),
                                  lessons_block=lessons_block,
                                  token_usage=self.token_usage)
        return self._build_result(state, microtask, diff_text, files_touched)

    def _code_until_diff(
        self,
        state: BlackboardState,
        microtask: Microtask,
        file_contents: Dict[str, str],
        lessons_block: str = "",
    ) -> tuple[str, List[Any], List[Dict[str, Any]]]:
        """ReAct attempts until repo.diff (deterministic, gateway-routed) is non-empty.

        Returns the winning diff, the winning attempt's message list (so the compile gate
        can continue the SAME conversation), plus a per-attempt log for create_code_file_log."""
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
            if lessons_block:
                prompt = f"{lessons_block}\n\n{prompt}"
            _log.info("coder.attempt", microtask_id=microtask.microtask_id,
                      attempt=attempt, max_attempts=self.max_empty_diff_retries)
            recursion_exceeded = False
            try:
                agent_result = self.coding_agent.invoke(
                    {"messages": [HumanMessage(content=prompt)]},
                    config={"recursion_limit": _recursion_limit(self.max_tool_steps)},
                )
            except GraphRecursionError:
                # Model hit the tool-call budget — likely stuck in a verify loop.
                # Check the actual diff: if the coder wrote files before looping,
                # the work is done and we can recover cleanly.
                _log.warning(
                    "coder.recursion_limit_exceeded",
                    microtask_id=microtask.microtask_id,
                    attempt=attempt,
                    limit=_recursion_limit(self.max_tool_steps),
                )
                agent_result = {"messages": []}
                recursion_exceeded = True
            except Exception as exception:
                if is_transient_llm_error(exception):
                    # Transient LLM failure (timeout / 429 / 5xx). The client already re-sent the
                    # SAME prompt up to max_retries before surfacing this; we now stall the run so
                    # the human can /continue, which re-drives the coder with the SAME prompt
                    # again. Logged loudly so the retry isn't a silent multi-minute hang.
                    _log.error(
                        "coder.llm_attempt_failed",
                        microtask_id=microtask.microtask_id,
                        attempt=attempt,
                        prompt_chars=len(prompt),
                        error=str(exception),
                        retry="same prompt is re-sent (client max_retries, then again on /continue)",
                    )
                    raise RetriableError(f"coder LLM call failed: {exception}", phase="coder")
                raise

            self.token_usage = self.token_usage + sum_usage(agent_result.get("messages", []))
            transcript = format_react_transcript(agent_result.get("messages", []))
            diff_text = self.coder_tools.get_repo_diff()
            diff_empty = not diff_text.strip()
            attempts_log.append({
                "attempt": attempt,
                "prompt": prompt,
                "transcript": transcript + ("\n[HIT RECURSION LIMIT]" if recursion_exceeded else ""),
                "diff_empty": diff_empty,
            })
            if not diff_empty:
                if recursion_exceeded:
                    _log.info(
                        "coder.diff_recovered_after_recursion_limit",
                        microtask_id=microtask.microtask_id,
                        attempt=attempt,
                    )
                return diff_text, agent_result.get("messages", []), attempts_log

            retry_feedback = (
                "Your previous attempt hit the tool call limit without writing any files. "
                "Be direct: call write_sandbox_file ONCE per target file with the complete "
                "new content, then stop — do not call get_repo_diff in a loop."
                if recursion_exceeded else
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

    # ------------------------------------------------------------------ #
    # Compile gate (deterministic, warm in-loop)                          #
    # ------------------------------------------------------------------ #

    def _ensure_compiles(
        self,
        microtask: Microtask,
        messages: List[Any],
        attempts_log: List[Dict[str, Any]],
    ) -> str:
        """Compile the microtask's target files. On failure, append the compiler error to
        the SAME message thread and re-run the coder so it fixes in-context (no cold
        re-prompt). Returns the final diff. On exhaustion, proceeds with a warning — the
        Tester's deterministic build check is the backstop, so we don't hard-fail here."""
        targets = self._compilable_targets(microtask.target_files)
        if not targets:
            return self.coder_tools.get_repo_diff()  # nothing compilable (e.g. .md/.json/.yaml)

        for attempt in range(1, self.max_compile_retries + 1):
            ok, report = self._compile(targets)
            if ok:
                _log.info("coder.compile_ok", microtask_id=microtask.microtask_id, attempt=attempt)
                return self.coder_tools.get_repo_diff()

            _log.warning("coder.compile_failed", microtask_id=microtask.microtask_id,
                         attempt=attempt, max_attempts=self.max_compile_retries)
            if attempt == self.max_compile_retries:
                break

            # Warm retry: continue the existing conversation with the compiler error.
            # We need to provide context for the coder to fix the error, so we append a new HumanMessage with the compiler error report to the existing messages list. The coder will then generate a new response that addresses the compilation issues.
            messages = list(messages) + [HumanMessage(content=(
                "Your changes do NOT compile. Fix the error(s) below by rewriting the "
                "affected target file(s) with write_sandbox_file (complete file content), "
                "then stop. Do not run tests.\n\n" + report
            ))]
            try:
                agent_result = self.coding_agent.invoke(
                    {"messages": messages},
                    config={"recursion_limit": _recursion_limit(self.max_tool_steps)},
                )
                self.token_usage = self.token_usage + sum_usage(agent_result.get("messages", []))
                messages = agent_result.get("messages", messages)
                attempts_log.append({
                    "attempt": f"compile-fix-{attempt}",
                    "prompt": report,
                    "transcript": format_react_transcript(agent_result.get("messages", [])),
                    "diff_empty": False,
                })
            except GraphRecursionError:
                _log.warning("coder.compile_fix_recursion_limit",
                             microtask_id=microtask.microtask_id, attempt=attempt)
            except Exception as exception:
                if is_transient_llm_error(exception):
                    _log.error("coder.cluster_unreachable", phase="compile-fix", error=str(exception))
                    raise RetriableError(f"coder compile-fix LLM call failed: {exception}", phase="coder")
                raise

        _log.warning("coder.compile_unresolved", microtask_id=microtask.microtask_id,
                     reason="handing to tester anyway — tester build check is the backstop")
        return self.coder_tools.get_repo_diff()

    @staticmethod
    def _compilable_targets(target_files: List[str]) -> Dict[str, List[str]]:
        """Bucket target files by the compiler that can check them. Files we can't
        statically compile (json/yaml/md/ts/...) are left out (the Tester still covers them)."""
        py = [f for f in target_files if f.endswith(".py")]
        js = [f for f in target_files if f.endswith((".js", ".mjs", ".cjs"))]
        buckets: Dict[str, List[str]] = {}
        if py:
            buckets["python"] = py
        if js:
            buckets["javascript"] = js
        return buckets

    def _compile(self, targets: Dict[str, List[str]]) -> tuple[bool, str]:
        """Run the scoped compile checks. Returns (ok, error_report)."""
        reports: List[str] = []
        if targets.get("python"):
            res = self.coder_tools.run(["python", "-m", "py_compile", *targets["python"]])
            if int(res.get("exit_code", 1)) != 0:
                reports.append("$ python -m py_compile\n" + (res.get("stderr") or res.get("stdout") or "").strip())
        for js_file in targets.get("javascript", []):
            res = self.coder_tools.run(["node", "--check", js_file])
            if int(res.get("exit_code", 1)) != 0:
                reports.append(f"$ node --check {js_file}\n" + (res.get("stderr") or res.get("stdout") or "").strip())
        return (not reports), "\n\n".join(reports)

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
            # New code → fresh test re-synthesis budget for the reflector→tester loop (GH #33).
            "test_resynth_count": 0,
            # consumed by CoderRunner._persist_results, not a BlackboardState key
            "_diff_text": diff_text,
        }

   
