import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError
from pydantic import BaseModel, SecretStr, ValidationError

from agents.Planning.Planner.investigation_support import (
    _default_max_tool_steps,
    _investigation_recursion_limit,
    _last_ai_text,
)
from agents.Planning.Planner.plan_output import create_plan_file_as_output
from agents.Planning.Planner.prompts.investigator import (
    INVESTIGATOR_SYSTEM_PROMPT,
    build_investigation_prompt,
)
from agents.Planning.Planner.prompts.synthesis import build_synthesis_prompt
from agents.Planning.Planner.quality import _normalize_output
from Exceptions.BadMicroTaskExceptions import BadMicroTaskException
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


class PlannerOutput(BaseModel):
    """The structured output format expected from the Planner LLM."""

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

        # LLM Initialization
        api_key = os.getenv("CLUSTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "Missing API key. Set CLUSTER_API_KEY (preferred) or OPENAI_API_KEY before running."
            )

        try:
            self.llm = ChatOpenAI(
                model="google/gemma-4-31B-it",
                api_key=SecretStr(api_key),
                base_url="http://131.159.60.153:3000/inference",
                temperature=0.0,
                top_p=0.1,
                max_completion_tokens=8192,
                max_retries=0,
                timeout=60.0,
            )

            # Enforce structured output at the end of the pipeline
            self.structured_llm = self.llm.with_structured_output(PlannerOutput, method="json_mode")

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
            print(f"[Planner] Initialization failed: {e}")
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
            print("[Planner] Revision pass detected — will skip investigation and inject prior feedback.")

        # 1. Investigation Phase
        investigation_summary = "No investigation performed (no tools provided)."
        if is_revision:
            investigation_summary = (
                "Revision pass — investigation skipped. "
                "See REVISION PASS block in synthesis prompt for prior plan and rejection notes."
            )
        elif self.investigator_agent:
            print("[Planner] Investigating the codebase using tools...")
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
                investigation_summary = _last_ai_text(messages) or (
                    "Investigation finished but produced no assistant summary."
                )
                tool_calls = sum(
                    1
                    for m in messages
                    if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
                )
                print(
                    f"[Planner] Investigation complete "
                    f"({len(messages)} messages, {tool_calls} tool-call rounds)."
                )
                print(f"Investigation summary: {investigation_summary}")


            except Exception as e:
                print(f"[Planner] Investigation failed: {e}")
                investigation_summary = (
                    f"Investigation failed (continuing to synthesis): {e}"
                )

        # 2. Synthesis Phase (Structured Output) — retried with feedback if quality gate rejects
        print("[Planner] Synthesizing final structured Plan...")
        schema_json = PlannerOutput.model_json_schema()
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
                if isinstance(raw, PlannerOutput):
                    raw_result = raw
                else:
                    raw_result = PlannerOutput.model_validate(raw)
            except (APIConnectionError, APITimeoutError) as exception:
                print(
                    "[Planner] Failed to connect to the cluster. "
                    f"Ensure the VPN is connected! Error: {exception}"
                )
                raise
            except ValidationError as e:
                print(f"[Planner] Synthesis output failed validation: {e}")
                raise
            except Exception as e:
                print(f"[Planner] Error during synthesis: {e}")
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
                    print(
                        f"[Planner] Quality gate failed after {attempt} attempt(s) — giving up."
                    )
                    raise
                print(
                    f"[Planner] Attempt {attempt}/{self.max_synthesis_attempts} failed quality gate "
                    f"— retrying synthesis with validation feedback as context."
                )

        assert result is not None

        print("[Planner] Synthesis complete. Returning Plan and Microtasks.")
        print(f"Plan Summary: {result.plan.summary}")
        print("\nMicrotasks:")
        for idx, task in enumerate(result.microtasks):
            print(f"  [{idx + 1}] {task.title} (ID: {task.microtask_id})")
            print(f"      Description: {task.description}")
            if task.acceptance_criteria:
                print(f"      Acceptance Criteria: {', '.join(task.acceptance_criteria)}")

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
