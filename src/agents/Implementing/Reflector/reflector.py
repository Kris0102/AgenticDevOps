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
from db.lessons import LessonStore
from observability import Category, get_logger
from orchestrator.state import BlackboardState, Microtask, Reflection, RunConfig, RunStatus
from providers import build_chat_llm
from tool_gateway.Roles import Roles

load_dotenv()
_log = get_logger(Category.REFLECTOR)


class ReflectionOutput(BaseModel):
    """Structured assessment produced by the reflector LLM.

    decision:
      "approve" — implementation + tests satisfy all acceptance criteria; advance.
      "improve" — something needs fixing; Coder/Tester must retry with the given instructions.
    """

    decision: Literal["approve", "improve"] = Field(
        description="'approve' to advance to next microtask; 'improve' to send back for fixes."
    )
    fix_target: Literal["code", "test"] = Field(
        default="code",
        description=(
            "Only meaningful when decision='improve'. 'code' = the IMPLEMENTATION is wrong "
            "(default) → Coder retries. 'test' = the implementation is correct but the "
            "SYNTHESIZED TEST is wrong (asserts a wrong expected value, bad assertion, "
            "broken setup) → the test is regenerated. Choose 'test' ONLY when you are "
            "confident the code satisfies the acceptance criteria and the test itself is at fault."
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

        self.llm = build_chat_llm(self.config.provider, self.config.llm,
                                  timeout_seconds=self.config.knobs.reflector_timeout_seconds)
        self.structured_llm = self.llm.with_structured_output(ReflectionOutput, method="json_mode")
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

        # Phase 2: structured assessment
        output = self._assess(
            microtask, test_output, diff, investigation_summary, test_passed, rejection_note,
            lessons_block,
        )
        approved = output.decision == "approve"

        reflection = Reflection(
            timestamp=datetime.now(timezone.utc).isoformat(),
            microtask_id=microtask.microtask_id,
            failure_type=output.failure_type or ("approved" if approved else "unknown"),
            root_cause=output.root_cause or (output.quality_notes if approved else ""),
            fix_instructions=output.fix_instructions,
            linked_artifacts=[],
            should_store_in_semantic_memory=output.should_store_in_semantic_memory,
            fix_target=output.fix_target,
        )

        existing: List[Reflection] = list(state.get("reflections") or [])
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
            prior_reflections=existing,
            lessons_block=lessons_block,
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
        }

        if not approved:
            result["status"] = RunStatus.NEEDS_CODE
        # When approved the node sets status (NEEDS_CODE or DONE) based on remaining tasks.

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
            _log.warning("reflector.investigation_failed", error=str(exc))
            return f"Investigation failed: {exc}"

    # ------------------------------------------------------------------ #
    # Phase 2 — structured assessment                                      #
    # ------------------------------------------------------------------ #

    def _assess(
        self,
        microtask: Microtask,
        test_output: str,
        diff: str,
        investigation_summary: str,
        test_passed: bool,
        rejection_note: Optional[str],
        lessons_block: str = "",
    ) -> ReflectionOutput:
        prompt = build_diagnosis_prompt(
            microtask=microtask,
            test_output=test_output,
            diff=diff,
            investigation_summary=investigation_summary,
            test_passed=test_passed,
            rejection_note=rejection_note,
        )
        if lessons_block:
            prompt = f"{lessons_block}\n\n{prompt}"
        try:
            raw = self.structured_llm.invoke([HumanMessage(content=prompt)])
            output = raw if isinstance(raw, ReflectionOutput) else ReflectionOutput.model_validate(raw)
            # Tests failed → always improve regardless of what the LLM said.
            if not test_passed and output.decision == "approve":
                _log.warning(
                    "reflector.override_decision",
                    reason="tests failed but LLM returned approve — forcing improve",
                )
                output = output.model_copy(update={"decision": "improve"})
        except (ValidationError, Exception) as exc:
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
    
    


            
