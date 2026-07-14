import os
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, cast

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError

from langgraph.errors import GraphRecursionError

from Exceptions.RetriableError import RetriableError, is_transient_llm_error
from Exceptions.RunExecutionError import RunExecutionError
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

from agents.Planning.Reviewer.reviewer_support import (
    _investigation_recursion_limit,
    _last_ai_text,
    _log_investigation_steps,
)
from providers import TokenUsage, build_chat_llm, structured_output_method, sum_usage

from agents.Planning.Reviewer.prompts.reviewer_prompt import build_investigation_prompt, build_review_prompt
from db.lessons import LessonStore
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
    RunConfig,
    RunStatus,
)
from tool_gateway.Roles import Roles

# Load environment variables from .env if present
load_dotenv()

_log = get_logger(Category.REVIEWER)


class ReviewerOutput(BaseModel):
    """The structured output expected from the Reviewer LLM.

    `blockers` is the actionable channel: concrete plan defects (each "what's wrong +
    how to fix"), populated ONLY on reject. `notes` is a one-line verdict summary. We
    forward blockers to the planner rather than the free-text notes because the model
    tended to leak its investigation reasoning ("let me verify the codebase…") into a
    single notes blob — non-actionable text the planner can't address (GH #43).
    """

    decision: FeedbackDecision
    notes: str = ""
    blockers: List[str] = Field(default_factory=list)

    def to_feedback_notes(self) -> str:
        """What the planner should act on: the concrete blockers as a checklist, falling
        back to the summary when none were given (approve, or a model that skipped the
        structured list)."""
        if self.blockers:
            return "\n".join(f"- {b}" for b in self.blockers)
        return self.notes


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
    # Dump the plan with review_feedback trimmed to the LATEST verdict only: the reviewer
    # re-investigates the current plan from scratch every pass, so it just needs to check
    # against its most recent note ("was this addressed?"), not the whole stacked history.
    # Dumping all of it made the reviewer fixate on and re-assert the entire backlog with
    # "(UNRESOLVED)" tags each round (GH #43). Mirrors the planner-side trim.
    plan_view = plan.model_copy(update={"review_feedback": plan.review_feedback[-1:]})
    payload = {
        "plan": plan_view.model_dump(mode="json"),
        "microtasks": [task.model_dump(mode="json") for task in microtasks],
    }
    return json.dumps(payload, indent=2, ensure_ascii=True)


class Reviewer:
    def __init__(self, run_id: str, config: RunConfig, tools: Optional[Sequence[BaseTool]] = None):
        """Initializes the Reviewer Agent.
        
        In this MAS architecture, environment actions (like semantic search, file reads)
        must be routed through the Tool Gateway.
        
        The reviewer stays read-only and uses a compact prompt that asks for a
        simple approve/reject decision with a given reason.
        """
        self.run_id = run_id
        self.tools = list(tools or [])
        self.llm_config = config.llm.reviewer
        self.max_tool_steps = config.knobs.reviewer_max_tool_steps
        self.name = Roles.REVIEWER.value
        self.timeout_seconds = config.knobs.reviewer_timeout_seconds
        self.token_usage = TokenUsage()

        try:
            provider = config.provider_for(self.llm_config)
            self.llm = build_chat_llm(provider, self.llm_config,
                                      timeout_seconds=self.timeout_seconds)

            # Enforce structured output. include_raw=True keeps the underlying AIMessage
            # (and its usage_metadata) accessible for token accounting (ADR 0025). Method is
            # provider-aware: Anthropic → function_calling (json_mode there yields unparseable
            # JSON from cheaper models); cluster → json_mode.
            self.structured_llm = self.llm.with_structured_output(
                ReviewerOutput, method=structured_output_method(provider), include_raw=True
            )

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
        self.token_usage = TokenUsage()
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
                self.token_usage = self.token_usage + sum_usage(messages)
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

            except GraphRecursionError:
                # SOFT failure: investigation exhausted its tool-step budget. Review from the
                # plan itself; the objective + plan are still there. Mirrors the coder/planner
                # recursion recovery. Raise the reviewer tool-step budget if it recurs (ADR 0023).
                _log.warning("reviewer.investigation_recursion_limit",
                             recovery="reviewing from the plan")
                investigation_summary = (
                    "Investigation incomplete (tool-step budget exhausted); reviewing from the "
                    "plan. Raise the reviewer tool-step budget if this recurs."
                )
            except Exception as error:
                # Transient (429 "overloaded" / timeout / 5xx) → STALL: /continue re-investigates
                # once the cluster frees (ADR 0023).
                if is_transient_llm_error(error):
                    _log.error("reviewer.cluster_unreachable", phase="investigation", error=str(error))
                    raise RetriableError(f"reviewer investigation LLM call failed: {error}", phase="reviewer")
                # A GENUINE, unexpected investigation error → FAIL cleanly rather than review off
                # a broken investigation. RunExecutionError → reviewer_node → clean FAILED.
                _log.error("reviewer.investigation_error", error=str(error))
                raise RunExecutionError(phase="reviewer.investigation",
                                        reason=f"Investigation failed: {error}")

        _log.debug(
            "reviewer.invoke.context_read",
            run_id=self.run_id,
            microtasks=len(microtasks),
        )

        _log.info("reviewer.reviewing", microtasks=len(microtasks))

        # add the objective, planner's result and the reviewer's investigation summary to the final review prompt
        prompt = build_review_prompt(objective_text, review_context, investigation_summary)

        # Advisory: surface the top-k confirmed plan lessons from past runs on THIS repo
        # (ADR 0018). Semantic top-k, so it stays bounded no matter how many accumulate.
        # The human's plan-scope operator rules are pinned above them (ADR 0034).
        repo = state.get("repo")
        repo_url = repo.repo_url if repo else ""
        lessons = LessonStore.find_lessons(
            repo_url, objective_text,
            scope="plan", exclude_run_id=self.run_id,
        )
        lessons_block = LessonStore.render_block(
            lessons, LessonStore.find_operator_rules(repo_url, scope="plan")
        )
        if lessons_block:
            prompt = f"{lessons_block}\n\n{prompt}"

        try:
            # include_raw=True → invoke returns a {"raw","parsing_error","parsed"} dict at
            # runtime; the Runnable stub types it as the model, so cast to what it actually is.
            raw = cast(Dict[str, Any], self.structured_llm.invoke([HumanMessage(content=prompt)]))
            if raw.get("raw") is not None:
                self.token_usage = self.token_usage + sum_usage([raw["raw"]])
            if raw.get("parsing_error"):
                raise raw["parsing_error"]
            parsed = raw["parsed"]
            result = parsed if isinstance(parsed, ReviewerOutput) else ReviewerOutput.model_validate(parsed)
        except ValidationError as error:
            _log.error("reviewer.output_validation_failed", error=str(error))
            raise
        except Exception as error:
            if is_transient_llm_error(error):
                _log.error("reviewer.api_unreachable", error=str(error))
                raise RetriableError(f"reviewer LLM call failed: {error}", phase="reviewer")
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
            notes=result.to_feedback_notes(),
            ts=datetime.now(timezone.utc).isoformat(),
        )
        review_file_path = create_review_file_as_output(
            objective_text=objective_text,
            review_context=review_context,
            result=result,
            run_id=self.run_id,
            investigation_summary=investigation_summary,
            prior_feedback=list(plan.review_feedback),
            lessons_block=lessons_block,
            token_usage=self.token_usage,
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