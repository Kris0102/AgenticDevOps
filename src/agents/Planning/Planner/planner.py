import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError
from pydantic import BaseModel, Field, SecretStr, ValidationError

from agents.Planning.Planner.investigation_support import (
    _default_max_tool_steps,
    _investigation_recursion_limit,
    _last_ai_text,
    _log_investigation_steps,
)
from agents.Planning.Planner.plan_output import create_plan_file_as_output
from agents.Planning.Planner.prompts.investigator import (
    INVESTIGATOR_SYSTEM_PROMPT,
    build_investigation_prompt,
)
from agents.Planning.Planner.prompts.synthesis import build_synthesis_prompt
from agents.Planning.Planner.quality import _normalize_output
from Exceptions.BadMicroTaskExceptions import BadMicroTaskException
from observability import Category, get_logger
from orchestrator.state import (
    Artifact,
    BlackboardState,
    FeedbackSource,
    Microtask,
    Plan,
    RunStatus,
)
from tool_gateway.Roles import Roles

# Load environment variables from .env if present
load_dotenv()

_log = get_logger(Category.PLANNER)


def _build_revision_context(plan: Plan) -> str:
    """Format prior plan + all rejection notes for the revision prompt block."""
    lines = [
        f"Prior plan summary: {plan.summary}",
        f"Prior architecture notes: {plan.architecture_notes}",
        "",
        "Rejection notes (address every one):",
    ]
    for fb in plan.review_feedback:
        label = "Human reviewer" if fb.source == FeedbackSource.HUMAN else "Automated reviewer"
        lines.append(f"  [{label}] {fb.notes}")
    return "\n".join(lines)


# PlanDraft mirrors `Plan` minus the system-managed review-feedback channel (appended
# by reviewer/human, never authored by the LLM). It was kept out of the LLM output
# schema because the model echoed prompt labels (e.g. 'Automated reviewer') into that
# channel's `source`, breaking enum validation. The docstring is intentionally neutral
# so the field name never appears in the schema description the LLM reads.
class PlanDraft(BaseModel):
    """The implementation plan: summary, architecture, assumptions, risks, questions."""

    summary: str
    architecture_notes: str
    data_contracts: List[str] = Field(default_factory=list)
    assumptions: List[str]
    risks: List[str]
    open_questions: List[str]


# What the LLM returns via structured output (see PlanDraft note on the omitted channel).
class PlannerDraft(BaseModel):
    """A complete implementation plan plus its microtasks."""

    plan: PlanDraft
    microtasks: List[Microtask]


class PlannerOutput(BaseModel):
    """Internal planner result. `plan` is the full `Plan` (with the system-managed
    `review_feedback` channel); built from a `PlannerDraft` after the LLM responds."""

    plan: Plan
    microtasks: List[Microtask]


class Planner:
    def __init__(self, run_id: str, tools: Optional[Sequence[BaseTool]] = None):
        """
        Initializes the Planner Agent.
        In this MAS architecture, environment actions (like semantic search, file reads)
        must be routed through the Tool Gateway.
        """
        self.run_id = run_id
        self.tools = list(tools or [])
        self.max_tool_steps = _default_max_tool_steps()
        self.max_synthesis_attempts = int(os.getenv("PLANNER_MAX_SYNTHESIS_ATTEMPTS", "3"))
        self.name = Roles.PLANNER.value
        self.timeout_seconds = (int(os.getenv("PLANNER_TIMEOUT_SECONDS", "60")))

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
                max_retries= int(os.getenv("LLM_RETRIES", "3")),
                timeout=self.timeout_seconds,
            )

            # Enforce structured output at the end of the pipeline
            self.structured_llm = self.llm.with_structured_output(PlannerDraft, method="json_mode")

            # Setup the investigator ReAct agent if tools are available
            if self.tools:
                self.investigator_agent = create_agent(
                    self.llm,
                    tools=self.tools,
                    system_prompt=INVESTIGATOR_SYSTEM_PROMPT,
                )
            else:
                self.investigator_agent = None

        except Exception as e:
            _log.error("planner.init_failed", error=str(e))
            raise

    def invoke(self, state: BlackboardState) -> Dict[str, Any]:
        """Runs the planner logic to build the macro Plan and Microtasks."""
        objective = state.get("objective")
        obj_text = ""
        if objective:
            title = objective.title
            description = objective.description
            dod = objective.definition_of_done
            obj_text = f"Objective: {title}\n{description}\nGoals: {dod}"
        else:
            obj_text = "Objective: Analyze code and prepare next steps."

        # Detect revision pass: prior plan exists and carries human/reviewer rejection notes.
        # On a revision the repo is unchanged so re-investigation wastes tool budget.
        prior_plan = state.get("plan")
        is_revision = prior_plan is not None and bool(prior_plan.review_feedback)
        revision_context: Optional[str] = _build_revision_context(prior_plan) if is_revision and prior_plan else None
        if is_revision:
            _log.info("planner.revision_pass", reason="prior feedback present, skipping investigation")

        # 1. Investigation Phase
        investigation_summary = "No investigation performed (no tools provided)."
        if is_revision:
            investigation_summary = (
                "Revision pass — investigation skipped. "
                "See REVISION PASS block in synthesis prompt for prior plan and rejection notes."
            )
        elif self.investigator_agent:
            _log.info("planner.investigating", tools=len(self.tools))
            investigation_prompt = build_investigation_prompt(obj_text)

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
                _log.info("planner.investigation_complete",
                          messages=len(messages), tool_rounds=tool_calls)
                _log.debug("planner.investigation_summary", summary=investigation_summary)

            except (APIConnectionError, APITimeoutError) as exception:
                _log.error("planner.cluster_unreachable", phase="investigation",
                           hint="ensure VPN connected", error=str(exception))
                raise
            except Exception as e:
                _log.warning("planner.investigation_failed",
                             error=str(e), recovery="continuing to synthesis")
                investigation_summary = (
                    f"Investigation failed (continuing to synthesis): {e}"
                )
           

        # 2. Synthesis Phase (Structured Output) — retried with feedback if quality gate rejects
        _log.info("planner.synthesizing")
        schema_json = PlannerDraft.model_json_schema()
        previous_attempt_feedback: Optional[str] = None
        result: Optional[PlannerOutput] = None

        for attempt in range(1, self.max_synthesis_attempts + 1):
            synthesis_prompt = build_synthesis_prompt(
                obj_text, investigation_summary, schema_json,
                previous_attempt_feedback, revision_context,
            )
            # Since we use `.with_structured_output`, we can just invoke it with the prompt.
            try:
                raw = self.structured_llm.invoke(
                    [HumanMessage(content=synthesis_prompt)],
                )
                draft = raw if isinstance(raw, PlannerDraft) else PlannerDraft.model_validate(raw)
                # Lift the draft into a full PlannerOutput: the LLM never authors
                # review_feedback, so it starts empty and is carried forward below.
                raw_result = PlannerOutput(
                    plan=Plan(**draft.plan.model_dump(), review_feedback=[]),
                    microtasks=draft.microtasks,
                )
            except (APIConnectionError, APITimeoutError) as exception:
                _log.error("planner.cluster_unreachable", phase="synthesis",
                           hint="ensure VPN connected", error=str(exception))
                raise
            except ValidationError as e:
                _log.error("planner.synthesis_validation_failed", error=str(e))
                raise
            except Exception as e:
                _log.error("planner.synthesis_error", error=str(e))
                raise

            # Write output file from raw LLM result — always, even if quality checks fail below
            plan_file_path = create_plan_file_as_output(investigation_summary, raw_result, self.run_id, revision_context)

            # Quality validation — raises BadMicroTaskException with specific failures if microtasks are too vague
            try:
                result = _normalize_output(raw_result)
                # Carry forward accumulated review_feedback so multi-reject history is
                # preserved. LLM-generated plan starts with review_feedback=[] by default;
                # re-attaching prior entries means _write_feedback's next append sees the
                # full history, not just the most recent rejection.
                if prior_plan and prior_plan.review_feedback:
                    result = PlannerOutput(
                        plan=result.plan.model_copy(
                            update={"review_feedback": list(prior_plan.review_feedback)}
                        ),
                        microtasks=result.microtasks,
                    )
                break
            except BadMicroTaskException as exception:
                previous_attempt_feedback = str(exception)
                if attempt == self.max_synthesis_attempts:
                    _log.error("planner.quality_gate_giveup", attempts=attempt, error=str(exception))
                    raise
                _log.warning("planner.quality_gate_retry",
                             attempt=attempt, max_attempts=self.max_synthesis_attempts)

        assert result is not None

        _log.info("planner.synthesis_complete",
                  microtasks=len(result.microtasks), summary=result.plan.summary)
        for idx, task in enumerate(result.microtasks):
            _log.debug("planner.microtask", idx=idx + 1, id=task.microtask_id,
                       title=task.title, acceptance_criteria=[c.text for c in task.acceptance_criteria])

        now = datetime.now(timezone.utc).isoformat()
        plan_artifact = Artifact(
            artifact_ref=plan_file_path,
            kind="plan_file",
            created_at=now,
            summary=f"Plan output for run {self.run_id}",
        )
        existing_artifacts = list(state.get("artifacts_index") or [])

        return {
            "status": RunStatus.NEEDS_REVIEW,
            "plan": result.plan,
            "microtasks": result.microtasks,
            "artifacts_index": existing_artifacts + [plan_artifact],
        }
