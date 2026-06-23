from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, SecretStr, ValidationError

from agents.Implementing.Reflector.prompt.reflector_prompt import (
    REFLECTOR_SYSTEM_PROMPT,
    build_diagnosis_prompt,
    build_investigation_prompt,
)
from agents.Implementing.Reflector.reflector_output import create_reflection_file_as_output
from agents.Implementing.Reflector.reflector_tools import ReflectorTools
from observability import Category, get_logger
from orchestrator.state import BlackboardState, Microtask, Reflection, RunStatus
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


def _read_host_file(path: Optional[str]) -> str:
    """Read a file from the host filesystem (e.g. the tester output written to disk)."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        _log.warning("reflector.host_file_read_failed", path=path, error=str(exc))
        return ""


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

    def __init__(self, run_id: str, reflector_tools: ReflectorTools):
        self.run_id = run_id
        self.reflector_tools = reflector_tools
        self.gateway = reflector_tools.gateway
        self.name = Roles.REFLECTOR.value
        self.max_tool_steps = _max_tool_steps()

        api_key = os.getenv("CLUSTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Missing API key. Set CLUSTER_API_KEY or OPENAI_API_KEY.")

        self.llm = ChatOpenAI(
            model=os.getenv("LLM_MODEL", "google/gemma-4-31B-it"),
            api_key=SecretStr(api_key),
            base_url="http://131.159.60.153:3000/inference",
            temperature=0.0,
            top_p=0.1,
            max_completion_tokens=4096,
            max_retries=int(os.getenv("LLM_RETRIES", "3")),
            timeout=int(os.getenv("REFLECTOR_TIMEOUT_SECONDS", "120")),
        )
        self.structured_llm = self.llm.with_structured_output(ReflectionOutput, method="json_mode")
        self.investigator = create_agent(
            self.llm,
            tools=self.reflector_tools.build_tools(),
            system_prompt=REFLECTOR_SYSTEM_PROMPT,
        )

    def invoke(self, state: BlackboardState, microtask: Microtask, test_passed: bool) -> Dict[str, Any]:
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

        test_output = _read_host_file(state.get("last_test_output_path"))
        diff = self.reflector_tools.get_repo_diff()

        # Phase 1: investigation
        investigation_summary = self._investigate(
            microtask, test_output, diff, test_passed, rejection_note
        )

        # Phase 2: structured assessment
        output = self._assess(
            microtask, test_output, diff, investigation_summary, test_passed, rejection_note
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
    ) -> ReflectionOutput:
        prompt = build_diagnosis_prompt(
            microtask=microtask,
            test_output=test_output,
            diff=diff,
            investigation_summary=investigation_summary,
            test_passed=test_passed,
            rejection_note=rejection_note,
        )
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
