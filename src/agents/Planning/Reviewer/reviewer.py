import os
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

from agents.Planning.Reviewer.reviewer_support import (
    _default_max_tool_steps,
    _investigation_recursion_limit,
    _last_ai_text,
    _log_investigation_steps,
)

from agents.Planning.Reviewer.prompts.reviewer_prompt import build_investigation_prompt, build_review_prompt
from agents.Planning.Reviewer.prompts.reviewer_prompt import REVIEWER_SYSTEM_PROMPT
from agents.Planning.Reviewer.review_output import create_review_file_as_output
from agents.Planning.Reviewer.reviewer_tools import ReviewerTools
from observability import Category, get_logger
from orchestrator.state import (
    Artifact,
    BlackboardState,
    FeedbackDecision,
    FeedbackSource,
    Microtask,
    Plan,
    ReviewFeedback,
    RunStatus,
)
from tool_gateway.Roles import Roles

# Load environment variables from .env if present
load_dotenv()

_log = get_logger(Category.REVIEWER)


class ReviewerOutput(BaseModel):
    """The structured output expected from the Reviewer LLM."""

    decision: FeedbackDecision
    notes: str


def _build_objective_text(state: BlackboardState) -> str:
    objective = state.get("objective")
    if not objective:
        return "Objective: Review the current plan for clarity, scope, and feasibility."

    return (
        f"Objective: {objective.title}\n"
        f"{objective.description}\n"
        f"Goals: {', '.join(objective.definition_of_done)}"
    )


def _build_review_context(plan: Plan, microtasks: List[Microtask]) -> str:
    payload = {
        "plan": plan.model_dump(mode="json"),
        "microtasks": [task.model_dump(mode="json") for task in microtasks],
    }
    return json.dumps(payload, indent=2, ensure_ascii=True)


def _build_revision_context(plan: Plan) -> str:
    lines = [
        f"Prior plan summary: {plan.summary}",
        f"Prior architecture notes: {plan.architecture_notes}",
        "",
        "Prior review feedback:",
    ]
    for feedback in plan.review_feedback:
        lines.append(f"  [{feedback.source}] {feedback.decision.value}: {feedback.notes}")
    return "\n".join(lines)


class Reviewer:
    def __init__(self, run_id: str, tools: Optional[Sequence[BaseTool]] = None):
        """Initializes the Reviewer Agent.
        
        In this MAS architecture, environment actions (like semantic search, file reads)
        must be routed through the Tool Gateway.
        
        The reviewer stays read-only and uses a compact prompt that asks for a
        simple approve/reject decision with a given reason.
        """
        self.run_id = run_id
        self.tools = list(tools or [])
        self.max_tool_steps = _default_max_tool_steps()
        self.max_synthesis_attempts = int(os.getenv("PLANNER_MAX_SYNTHESIS_ATTEMPTS", "3"))
        self.name = Roles.REVIEWER.value
        self.timeout_seconds = int(os.getenv("REVIEWER_TIMEOUT_SECONDS", "60"))

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
                max_retries=int(os.getenv("LLM_RETRIES", "3")),
                timeout=self.timeout_seconds,
            )

            # Enforce structured output
            self.structured_llm = self.llm.with_structured_output(ReviewerOutput, method="json_mode")

            if self.tools:
                self.investigator_agent = create_agent(
                    self.llm,
                    tools=self.tools,
                    system_prompt=REVIEWER_SYSTEM_PROMPT,
                )
            else:
                self.investigator_agent = None
            
        
        except Exception as error:
            _log.error("reviewer.init_failed", error=str(error))
            raise

    def invoke(self, state: BlackboardState) -> Dict[str, Any]:
        """Review the current plan and return a decision plus updated feedback."""
        _log.debug("reviewer.invoke.start", run_id=self.run_id)
        
        # get plan from state, if missing return NEEDS_PLAN to go back to planner
        plan = state.get("plan")
        if plan is None:
            _log.warning("reviewer.no_plan", recovery="returning to planner")
            return {"status": RunStatus.NEEDS_PLAN}

        microtasks: List[Microtask] = list(state.get("microtasks") or [])
        
        # build context for review prompt
        objective_text = _build_objective_text(state)
        review_context = _build_review_context(plan, microtasks)
        revision_context = _build_revision_context(plan)

        investigation_summary = "No independent investigation performed (no tools provided)."
        if self.investigator_agent:
            _log.info("reviewer.investigating", tools=len(self.tools))
            investigation_prompt = build_investigation_prompt(objective_text, review_context)

            try:
                investigative_state = self.investigator_agent.invoke(
                    {"messages": [HumanMessage(content=investigation_prompt)]},
                    config={
                        "recursion_limit": _investigation_recursion_limit(
                            self.max_tool_steps
                        ),
                    },
                )
                messages = investigative_state.get("messages", [])
                _log_investigation_steps(messages)
                investigation_summary = _last_ai_text(messages) or (
                    "Investigation finished but produced no assistant summary."
                )
                tool_calls = sum(
                    1
                    for m in messages
                    if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
                )
                _log.info(
                    "reviewer.investigation_complete",
                    messages=len(messages),
                    tool_rounds=tool_calls,
                )
                _log.debug("reviewer.investigation_summary", summary=investigation_summary)

            except (APIConnectionError, APITimeoutError) as error:
                _log.error("reviewer.cluster_unreachable", phase="investigation", error=str(error))
                raise
            except Exception as error:
                _log.warning("reviewer.investigation_failed", error=str(error))
                investigation_summary = f"Investigation failed (continuing to review): {error}"

        _log.debug(
            "reviewer.invoke.context_read",
            run_id=self.run_id,
            microtasks=len(microtasks),
        )

        _log.info("reviewer.reviewing", microtasks=len(microtasks))

        # add the objective, planner's result and the reviewer's investigation summary to the final review prompt 
        prompt = build_review_prompt(objective_text, review_context, investigation_summary)

        try:
            raw = self.structured_llm.invoke([HumanMessage(content=prompt)])
            if isinstance(raw, ReviewerOutput):
                result = raw
            else:
                result = ReviewerOutput.model_validate(raw)
        except (APIConnectionError, APITimeoutError) as error:
            _log.error("reviewer.cluster_unreachable", error=str(error))
            raise
        except ValidationError as error:
            _log.error("reviewer.output_validation_failed", error=str(error))
            raise
        except Exception as error:
            _log.error("reviewer.review_failed", error=str(error))
            raise

        _log.debug(
            "reviewer.invoke.result",
            run_id=self.run_id,
            decision=result.decision.value,
            notes=result.notes,
        )

        feedback = ReviewFeedback(
            decision=result.decision,
            source=FeedbackSource.REVIEWER,
            notes=result.notes,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        review_file_path = create_review_file_as_output(
            objective_text=objective_text,
            review_context=review_context,
            result=result,
            run_id=self.run_id,
            investigation_summary=investigation_summary,
            prior_feedback=list(plan.review_feedback),
        )
        reviewed_plan = plan.model_copy(update={
            "review_feedback": list(plan.review_feedback) + [feedback],
        })
        next_status = RunStatus.NEEDS_CODE if result.decision == FeedbackDecision.APPROVE else RunStatus.NEEDS_PLAN

        _log.info(
            "reviewer.review_complete",
            decision=result.decision.value,
            next_status=next_status.value,
        )

        _log.debug("reviewer.invoke.end", run_id=self.run_id, next_status=next_status.value)

        return {
            "status": next_status,
            "plan": reviewed_plan,
            "artifacts_index": list(state.get("artifacts_index") or []) + ([Artifact(artifact_ref=review_file_path, kind="review_file", created_at=feedback.ts, summary=f"Review output for run {self.run_id}")] if review_file_path else []),
        }