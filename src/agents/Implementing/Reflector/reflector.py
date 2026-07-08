from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, ValidationError

from agents.Implementing.Reflector.prompt.reflector_prompt import (
    REFLECTOR_SYSTEM_PROMPT,
    build_diagnosis_prompt,
    build_investigation_prompt,
)
from agents.Implementing.Reflector.reflector_output import create_reflection_file_as_output
from agents.Implementing.Reflector.reflector_tools import ReflectorTools
from Exceptions.RetriableError import RetriableError, is_transient_llm_error
from db.lessons import LessonStore
from observability import Category, get_logger
from orchestrator.state import BlackboardState, Microtask, Reflection, RunConfig, RunStatus
from providers import TokenUsage, build_chat_llm, sum_usage
from tool_gateway.Roles import Roles

load_dotenv()
_log = get_logger(Category.REFLECTOR)


class ReflectionOutput(BaseModel):
    """Structured assessment produced by the reflector LLM.

    decision:
      "approve" — implementation + tests satisfy all acceptance criteria; advance.
      "improve" — something needs fixing; Coder/Tester must retry with the given instructions.
    """

    decision: Literal["approve", "improve", "blocked"] = Field(
        description=(
            "'approve' to advance to next microtask; 'improve' to send back for fixes; "
            "'blocked' when NO code change can succeed because two requirements/checks "
            "contradict each other (e.g. an existing-suite behaviour and a new acceptance "
            "criterion demand mutually exclusive outcomes for the same input) or your own "
            "prior diagnoses for this microtask oscillate (a fix that undoes the previous "
            "fix). 'blocked' stops the retry loop and surfaces the conflict to the human "
            "instead of burning the remaining attempts (GH #61)."
        )
    )
    fix_target: Literal["code", "test"] = Field(
        default="code",
        description=(
            "Only meaningful when decision='improve'. Judge against the ACCEPTANCE CRITERIA, "
            "not against what the test asserts. 'code' (default) = the IMPLEMENTATION violates "
            "the criteria → Coder retries. 'test' = the code satisfies the criteria but the "
            "SYNTHESIZED TEST is wrong — it asserts a value contradicting a criterion's "
            "given→expect example, or a value the Tester computed itself for an input the "
            "criteria give no example for (a hand-computed distance / rounded number / exact "
            "error string), or has a broken assertion/setup → the test is regenerated. A "
            "self-computed expected value the Planner never supplied is the classic wrong-test "
            "signal. Prefer 'code' when genuinely unsure."
        ),
    )
    failure_type: str = Field(
        default="",
        description="Category when decision=improve: assertion_error | missing_implementation | logic_error | type_error | quality_issue | other.",
    )
    root_cause: str = Field(
        default="",
        description="Precise explanation of what is wrong (empty on approve).",
    )
    fix_instructions: str = Field(
        default="",
        description="Step-by-step for Coder/Tester — which file, which function, what to change (empty on approve).",
    )
    quality_notes: str = Field(
        default="",
        description="Brief summary of what looks good (always filled).",
    )
    should_store_in_semantic_memory: bool = Field(
        default=False,
        description="True if this is a reusable anti-pattern worth caching.",
    )


def _max_tool_steps() -> int:
    raw = os.getenv("REFLECTOR_MAX_TOOL_STEPS", "8")
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def _recursion_limit(max_tool_steps: int) -> int:
    return max_tool_steps * 2 + 5


def _last_ai_text(messages: List) -> Optional[str]:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and isinstance(msg.content, str) and msg.content.strip():
            return msg.content.strip()
    return None


def _render_prior_diagnoses(reflections: List[Reflection], microtask_id: str,
                            max_each: int = 500) -> str:
    """The reflector's OWN earlier diagnoses for this microtask, rendered for the assess
    prompt (GH #61). Without this the reflector reasons blind to its past each attempt —
    the mechanical cause of the observed flip-flop (attempt 2 re-inserted exactly what
    attempt 1 removed). Empty string on the first attempt."""
    prior = [r for r in reflections if r.microtask_id == microtask_id]
    if not prior:
        return ""
    lines = []
    for i, r in enumerate(prior, 1):
        lines.append(
            f"[attempt {i}] failure_type: {r.failure_type}\n"
            f"  root cause: {r.root_cause[:max_each]}\n"
            f"  fix instructions given: {r.fix_instructions[:max_each]}"
        )
    return (
        "YOUR OWN PRIOR DIAGNOSES FOR THIS MICROTASK (earlier attempts):\n"
        + "\n".join(lines)
        + "\n\nCheck yourself against this history BEFORE deciding:\n"
        "- If your new fix would UNDO a fix you prescribed in an earlier attempt (or vice "
        "versa), you are OSCILLATING — the checks themselves likely conflict. Do NOT issue "
        "another opposite code fix; set decision=\"blocked\" and state the conflict.\n"
        "- If the same root cause keeps recurring despite the coder applying your fix, "
        "reconsider whether the fault is really in the code."
    )


def _tests_passed_from_evidence(state: BlackboardState) -> bool:
    """Derive test pass/fail from the evidence.checks written by the tester node."""
    evidence = state.get("evidence")
    if not evidence:
        return False
    checks = evidence.checks
    if not checks:
        return False
    unit = checks.unit_tests
    if unit is None:
        return False
    return unit.status in ("passed", "skipped")


class Reflector:
    """Two-phase Reflector that runs after EVERY coder+tester iteration.

    Phase 1 — Investigation: ReAct agent reads source files, test files, and diff.
    Phase 2 — Structured assessment: LLM produces ReflectionOutput with a decision:
      • approve  — implementation satisfies all acceptance criteria → advance pipeline.
      • improve  — something needs fixing → Coder/Tester retry with fix instructions.

    The result includes ``_reflector_approved`` (stripped by the orchestrator node)
    so the node can apply the correct HITL gate (TEST on approve, REFLECT on improve).
    """

    def __init__(self, run_id: str, reflector_tools: ReflectorTools, config: RunConfig):
        self.run_id = run_id
        self.reflector_tools = reflector_tools
        self.gateway = reflector_tools.gateway
        self.name = Roles.REFLECTOR.value
        self.config = config   # mandatory — the node supplies the fallback
        self.max_tool_steps = self.config.knobs.reflector_max_tool_steps
        self.token_usage = TokenUsage()

        self.llm = build_chat_llm(self.config.provider, self.config.llm,
                                  timeout_seconds=self.config.knobs.reflector_timeout_seconds)
        # include_raw=True keeps the underlying AIMessage (and its usage_metadata) accessible
        # for token accounting (ADR 0025).
        self.structured_llm = self.llm.with_structured_output(
            ReflectionOutput, method="json_mode", include_raw=True
        )
        self.investigator = create_agent(
            self.llm,
            tools=self.reflector_tools.build_tools(),
            system_prompt=REFLECTOR_SYSTEM_PROMPT,
        )

    def invoke(
        self,
        state: BlackboardState,
        microtask: Microtask,
        test_passed: bool,
        test_output: str = "",
    ) -> Dict[str, Any]:
        """`test_output` is the failing tester output, fetched by ReflectorRunner from the
        durable `unit_tests` artifact (blackboard carries only the ref) and passed in — the
        node owns DB access, the agent just reasons over the text."""
        self.token_usage = TokenUsage()
        rejection_note: Optional[str] = state.get("reflection_rejection_note")
        mode = "quality-review" if test_passed else "failure-diagnosis"
        _log.info(
            "reflector.invoke.start",
            run_id=self.run_id,
            microtask_id=microtask.microtask_id,
            microtask_title=microtask.title,
            mode=mode,
            is_re_reflection=bool(rejection_note),
        )

        diff = self.reflector_tools.get_repo_diff()

        # Phase 1: investigation
        investigation_summary = self._investigate(
            microtask, test_output, diff, test_passed, rejection_note
        )

        # Advisory: top-k confirmed code lessons from past runs on THIS repo to ground the
        # diagnosis (ADR 0018). Semantic top-k → bounded regardless of how many accumulate.
        repo = state.get("repo")
        lessons = LessonStore.find_lessons(
            repo.repo_url if repo else "",
            f"{microtask.title} {microtask.description} {' '.join(microtask.target_files)}",
            scope="code",
            exclude_run_id=self.run_id,
        )
        lessons_block = LessonStore.render_block(lessons)

        # The synthesized test the verdict may have failed on — so the code-vs-test call
        # can judge the actual assertion, not just the failure output. Empty on a
        # Track-A-only failure (the repo's own suite, which we don't regenerate).
        synth_test_source = self._read_synth_test(microtask.microtask_id)

        # This microtask's earlier diagnoses (GH #61) — injected into the assessment so
        # the reflector can recognize its own oscillation instead of flip-flopping.
        existing: List[Reflection] = list(state.get("reflections") or [])
        prior_diagnoses_block = _render_prior_diagnoses(existing, microtask.microtask_id)

        # Phase 2: structured assessment
        output = self._assess(
            microtask, test_output, diff, investigation_summary, test_passed, rejection_note,
            lessons_block, synth_test_source, prior_diagnoses_block,
        )
        approved = output.decision == "approve"
        blocked = output.decision == "blocked"

        reflection = Reflection(
            timestamp=datetime.now(timezone.utc).isoformat(),
            microtask_id=microtask.microtask_id,
            failure_type=output.failure_type or (
                "approved" if approved else ("requirements_conflict" if blocked else "unknown")
            ),
            root_cause=output.root_cause or (output.quality_notes if approved else ""),
            fix_instructions=output.fix_instructions,
            linked_artifacts=[],
            should_store_in_semantic_memory=output.should_store_in_semantic_memory,
            fix_target="escalate" if blocked else output.fix_target,
        )
        output_path = create_reflection_file_as_output(
            run_id=self.run_id,
            microtask=microtask,
            investigation_summary=investigation_summary,
            reflection=reflection,
            test_output=test_output,
            diff=diff,
            rejection_note=rejection_note,
            test_passed=test_passed,
            approved=approved,
            blocked=blocked,
            prior_reflections=existing,
            lessons_block=lessons_block,
            token_usage=self.token_usage,
        )
        _log.info(
            "reflector.invoke.end",
            run_id=self.run_id,
            microtask_id=microtask.microtask_id,
            mode=mode,
            decision=output.decision,
            output_path=output_path or None,
        )

        result: Dict[str, Any] = {
            "reflections": existing + [reflection],
            "reflection_rejection_note": None,   # clear after use
            "last_reflection_output_path": output_path or None,
            # _INTERNAL_: stripped by ReflectorRunner before reaching LangGraph state.
            "_reflector_approved": approved,
            # _INTERNAL_: "code" (coder retry) | "test" (regenerate the synthesized test).
            "_fix_target": output.fix_target,
            # _INTERNAL_: requirements contradict / oscillation — no code fix can succeed.
            # ReflectorRunner ends the run BLOCKED with the reason (GH #61).
            "_reflector_blocked": blocked,
        }

        if not approved and not blocked:
            result["status"] = RunStatus.NEEDS_CODE
        # When approved the node sets status (NEEDS_CODE or DONE) based on remaining tasks;
        # when blocked the node routes to the failure path with the conflict reason.

        return result

    # ------------------------------------------------------------------ #
    # Phase 1 — investigation                                              #
    # ------------------------------------------------------------------ #

    def _investigate(
        self,
        microtask: Microtask,
        test_output: str,
        diff: str,
        test_passed: bool,
        rejection_note: Optional[str],
    ) -> str:
        prompt = build_investigation_prompt(
            microtask=microtask,
            test_output=test_output,
            diff=diff,
            test_passed=test_passed,
            rejection_note=rejection_note,
        )
        try:
            result = self.investigator.invoke(
                {"messages": [HumanMessage(content=prompt)]},
                config={"recursion_limit": _recursion_limit(self.max_tool_steps)},
            )
            messages = result.get("messages", [])
            self.token_usage = self.token_usage + sum_usage(messages)
            tool_calls = sum(
                1 for m in messages if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
            )
            summary = _last_ai_text(messages) or "Investigation completed but produced no summary."
            _log.info(
                "reflector.investigation_complete",
                run_id=self.run_id,
                microtask_id=microtask.microtask_id,
                messages=len(messages),
                tool_rounds=tool_calls,
            )
            return summary
        except Exception as exc:
            # Transient (429 "overloaded" / timeout / 5xx) → STALL, don't diagnose off a
            # failed investigation (ADR 0023). /continue re-runs the reflector on recovery.
            if is_transient_llm_error(exc):
                _log.error("reflector.cluster_unreachable", phase="investigation", error=str(exc))
                raise RetriableError(f"reflector investigation LLM call failed: {exc}", phase="reflector")
            _log.warning("reflector.investigation_failed", error=str(exc))
            return f"Investigation failed: {exc}"

    # ------------------------------------------------------------------ #
    # Phase 2 — structured assessment                                      #
    # ------------------------------------------------------------------ #

    def _read_synth_test(self, microtask_id: str) -> str:
        """Best-effort read of the microtask's synthesized test file from the sandbox so the
        diagnosis can inspect the failing assertion. Path scheme mirrors the Tester's
        SynthesisPhase (`.agentic_tests/test_<id>.{test.js|py}`); we glob the extension since
        the reflector doesn't carry the project type. Any failure → "" (Track-A-only failure
        or no synth file); never let this crash the diagnosis."""
        safe = microtask_id.replace("-", "_")
        try:
            listing = self.reflector_tools.execute_in_sandbox(
                f"ls .agentic_tests/test_{safe}.* 2>/dev/null"
            )
            path = next((ln.strip() for ln in listing.splitlines() if ln.strip()), "")
            if not path:
                return ""
            return self.reflector_tools.read_sandbox_file(path) or ""
        except Exception as exc:
            _log.debug("reflector.synth_test_read_failed", microtask_id=microtask_id, error=str(exc))
            return ""

    def _assess(
        self,
        microtask: Microtask,
        test_output: str,
        diff: str,
        investigation_summary: str,
        test_passed: bool,
        rejection_note: Optional[str],
        lessons_block: str = "",
        synth_test_source: str = "",
        prior_diagnoses_block: str = "",
    ) -> ReflectionOutput:
        prompt = build_diagnosis_prompt(
            microtask=microtask,
            test_output=test_output,
            diff=diff,
            investigation_summary=investigation_summary,
            test_passed=test_passed,
            rejection_note=rejection_note,
            synth_test_source=synth_test_source,
            prior_diagnoses_block=prior_diagnoses_block,
        )
        if lessons_block:
            prompt = f"{lessons_block}\n\n{prompt}"
        try:
            raw = self.structured_llm.invoke([HumanMessage(content=prompt)])
            if raw.get("raw") is not None:
                self.token_usage = self.token_usage + sum_usage([raw["raw"]])
            if raw.get("parsing_error"):
                raise raw["parsing_error"]
            parsed = raw["parsed"]
            output = parsed if isinstance(parsed, ReflectionOutput) else ReflectionOutput.model_validate(parsed)
            # Tests failed → always improve regardless of what the LLM said.
            if not test_passed and output.decision == "approve":
                _log.warning(
                    "reflector.override_decision",
                    reason="tests failed but LLM returned approve — forcing improve",
                )
                output = output.model_copy(update={"decision": "improve"})
        except Exception as exc:
            # Transient (429 / timeout / 5xx) → STALL rather than emit a fallback diagnosis
            # that sends the coder to fix nothing (ADR 0023).
            if is_transient_llm_error(exc):
                _log.error("reflector.cluster_unreachable", phase="assessment", error=str(exc))
                raise RetriableError(f"reflector assessment LLM call failed: {exc}", phase="reflector")
            _log.warning("reflector.assessment_failed", error=str(exc))
            output = ReflectionOutput(
                decision="improve",
                failure_type="assessment_failed",
                root_cause=f"Structured assessment unavailable: {exc}",
                fix_instructions=f"Manual investigation required. Summary: {investigation_summary}",
                quality_notes="",
                should_store_in_semantic_memory=False,
            )
        return output
    
    


            
