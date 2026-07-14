import os
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
from pydantic import BaseModel, Field, SecretStr, ValidationError

from agents.Planning.Planner.investigation_support import (
    _investigation_recursion_limit,
    _last_ai_text,
    _log_investigation_steps,
)
from providers import TokenUsage, build_chat_llm, structured_output_method, sum_usage
from agents.Planning.Planner.plan_output import create_plan_file_as_output
from agents.Planning.Planner.prompts.investigator import (
    INVESTIGATOR_SYSTEM_PROMPT,
    build_investigation_prompt,
)
from agents.Planning.Planner.prompts.synthesis import build_synthesis_prompt
from agents.Planning.Planner.quality import _normalize_output
from db.lessons import LessonStore
from Exceptions.BadMicroTaskExceptions import BadMicroTaskException
from observability import Category, get_logger
from orchestrator.state import (
    Artifact,
    BlackboardState,
    FeedbackSource,
    Microtask,
    Plan,
    RunConfig,
    RunStatus,
)
from tool_gateway.Roles import Roles

# Load environment variables from .env if present
load_dotenv()

_log = get_logger(Category.PLANNER)


def _is_grammar_compile_error(exc: Exception) -> bool:
    """True for Anthropic's `json_schema` grammar-compile timeout (a 400 the constrained
    decoder raises when compiling a large/deeply-nested output schema into a sampling
    grammar exceeds its budget). Matched on the message — it's not its own exception type —
    so we can fall the ONE affected call back to `function_calling` (no grammar compile)
    rather than fail the whole synthesis. See planner __init__ + _invoke_structured."""
    return "grammar compilation timed out" in str(exc).lower()


def _build_revision_context(plan: Plan, microtasks: List[Microtask]) -> str:
    """Format the prior plan + its FULL microtasks + the CURRENT review for the revision
    prompt block.

    Only the LATEST review verdict is rendered — not the whole reject history. Each
    review is a complete, fresh re-assessment of the current plan, so it supersedes
    every earlier one; the latest note already lists exactly what is still wrong.
    Feeding all of them stacked (1,2,3,…) drowned the planner in duplicate, partly-
    stale "(UNRESOLVED)" notes and slowed convergence to a crawl (GH #43). The full
    history still lives on the blackboard for the audit trail — only the prompt
    rendering is trimmed.

    The PRIOR MICROTASKS are rendered in full detail. Without them the planner revised
    blind — it regenerated every microtask from scratch each pass, which (observed on a
    real run) dropped whole microtasks the reviewer then demanded back, and kept
    re-emitting the exact example strings the reviewer had flagged, looping the same
    rejection verbatim until the iteration cap. A reviewer note like "change
    'Hamburg' to 'hamburg' in M2 AC1" is only actionable if the planner can SEE M2 AC1.
    """
    lines = [
        f"Prior plan summary: {plan.summary}",
        f"Prior architecture notes: {plan.architecture_notes}",
        "",
        "PRIOR MICROTASKS (your previous output — revise these, do not regenerate blind):",
    ]
    for mt in microtasks:
        lines.append(f"  [{mt.microtask_id}] {mt.title}")
        lines.append(f"    description: {mt.description}")
        if mt.approach:
            lines.append(f"    approach: {mt.approach}")
        lines.append(f"    target_files: {mt.target_files}")
        if mt.context_files:
            lines.append(f"    context_files: {mt.context_files}")
        if mt.dependencies:
            lines.append(f"    dependencies: {mt.dependencies}")
        for i, c in enumerate(mt.acceptance_criteria, 1):
            lines.append(f"    AC {i}: {c.render()}")
    lines += [
        "",
        "Rejection notes (address every one):",
    ]
    latest = plan.review_feedback[-1] if plan.review_feedback else None
    if latest:
        label = "Human reviewer" if latest.source == FeedbackSource.HUMAN else "Automated reviewer"
        lines.append(f"  [{label}] {latest.notes}")
    lines += [
        "",
        "Apply the rejection notes as SURGICAL EDITS to the prior microtasks above: change "
        "exactly what each note flags, keep everything else (microtask count, ids, unflagged "
        "criteria and examples) UNCHANGED. Never drop a microtask the review did not tell you "
        "to drop.",
    ]
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
    def __init__(self, run_id: str, config: RunConfig, tools: Optional[Sequence[BaseTool]] = None,
                ):
        """
        Initializes the Planner Agent.
        In this MAS architecture, environment actions (like semantic search, file reads)
        must be routed through the Tool Gateway.
        """
        self.run_id = run_id
        self.tools = list(tools or [])
        # make run config mandatory. Fallback only in the nodes!
        self.llm_config = config.llm.planner 
        knobs = config.knobs
        self.max_tool_steps = knobs.planner_max_tool_steps
        self.max_synthesis_attempts = knobs.planner_max_synthesis_attempts
        self.name = Roles.PLANNER.value
        self.timeout_seconds = knobs.planner_timeout_seconds
        self.token_usage = TokenUsage()

        try:
            provider = config.provider_for(self.llm_config)
            self.llm = build_chat_llm(provider, self.llm_config,
                                      timeout_seconds=self.timeout_seconds)
            
            # Depending on provider the string to force structured output may differ. Anthropic uses `json_schema`, OpenAI uses `structured_output`.
            self._synthesis_method = structured_output_method(provider)
            self.structured_llm = self._build_structured_llm(self._synthesis_method)
            # Anthropic `json_schema` compiles the (large, deeply-nested) PlannerDraft schema
            # into a sampling grammar server-side; on a big plan that compile can time out
            # (400 "Grammar compilation timed out"). If it does, _invoke_structured swaps this
            # instance to `function_calling` (no grammar compile) ONCE and never looks back —
            # the flag guards against a swap loop. json_mode providers never hit this.
            self._grammar_fallback_used = False

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

    def _build_structured_llm(self, method: str):
        """Bind PlannerDraft as the structured-output schema under the given method
        (`json_schema` | `json_mode` | `function_calling`). include_raw=True so the invoke
        returns {"raw","parsing_error","parsed"} (raw carries token usage)."""
        return self.llm.with_structured_output(PlannerDraft, method=method, include_raw=True)

    def _invoke_structured(self, messages: List[Any]) -> Dict[str, Any]:
        """Invoke the structured-output LLM, with a ONE-TIME fallback: if Anthropic's
        `json_schema` grammar compile times out on our big schema, rebuild this instance's
        structured LLM with `function_calling` (which does no grammar compilation) and retry
        the same call once. The swap is permanent for the planner instance (`_grammar_fallback_used`)
        so later synthesis attempts don't re-hit the timeout. function_calling is looser
        (a required field can be omitted) — the existing `_normalize_output` quality gate +
        BadMicroTaskException retry already guard that."""
        try:
            return cast(Dict[str, Any], self.structured_llm.invoke(messages))
        except Exception as e:
            if self._grammar_fallback_used or not _is_grammar_compile_error(e):
                raise
            _log.warning(
                "planner.grammar_timeout_fallback",
                was=self._synthesis_method, now="function_calling",
            )
            self._grammar_fallback_used = True
            self._synthesis_method = "function_calling"
            self.structured_llm = self._build_structured_llm("function_calling")
            return cast(Dict[str, Any], self.structured_llm.invoke(messages))

    def invoke(self, state: BlackboardState) -> Dict[str, Any]:
        """Runs the planner logic to build the macro Plan and Microtasks."""
        self.token_usage = TokenUsage()
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
        prior_plan = state.get("plan")
        prior_microtasks: List[Microtask] = list(state.get("microtasks") or [])
        is_revision = prior_plan is not None and bool(prior_plan.review_feedback)
        revision_context: Optional[str] = (
            _build_revision_context(prior_plan, prior_microtasks) if is_revision and prior_plan else None
        )
        if is_revision:
            _log.info("planner.revision_pass", reason="prior feedback present — re-investigating to re-ground")

        # 1. Investigation Phase — ALWAYS re-investigate on a revision. Skipping it (to save
        # tool budget) was a false economy: a plan is usually rejected because it MISREAD the
        # repo, so the planner must re-read the real files rather than revise off its own
        # hallucinated prior plan. On a revision the investigation is TARGETED at the
        # reviewer's rejection notes (see build_investigation_prompt).
        investigation_summary = "No investigation performed (no tools provided)."
        if self.investigator_agent:
            _log.info("planner.investigating", tools=len(self.tools), revision=is_revision)
            investigation_prompt = build_investigation_prompt(obj_text, revision_context)

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
                _log.info("planner.investigation_complete",
                          messages=len(messages), tool_rounds=tool_calls)
                _log.debug("planner.investigation_summary", summary=investigation_summary)

            except GraphRecursionError:
                # SOFT failure: the investigation exhausted its tool-step budget (langgraph
                # drops the partial messages, so nothing to salvage). The model WAS working,
                # just slowly — plan from the objective; the reviewer + coder ground downstream.
                # Mirrors the coder's recursion recovery. Raise the planner tool-step budget if
                # this recurs. (ADR 0023 — recursion degrades, a real error fails.)
                _log.warning("planner.investigation_recursion_limit",
                             recovery="planning from the objective",
                             limit=_investigation_recursion_limit(self.max_tool_steps))
                investigation_summary = (
                    "Investigation incomplete (tool-step budget exhausted); planning from the "
                    "objective. Raise the planner tool-step budget if this recurs."
                )
            except Exception as e:
                # Transient (timeout / connection / 429 "overloaded" / 5xx) → STALL: /continue
                # re-runs the investigation once the cluster frees (ADR 0023).
                if is_transient_llm_error(e):
                    _log.error("planner.cluster_unreachable", phase="investigation", error=str(e))
                    raise RetriableError(f"planner investigation LLM call failed: {e}", phase="planner")
                # A GENUINE, unexpected investigation error (not transient, not a budget cap) →
                # FAIL cleanly. Synthesizing a plan off a broken investigation just wastes the
                # planner↔reviewer budget on a doomed plan; the coder likewise raises on a hard
                # error rather than limping. RunExecutionError → planner_node → clean FAILED.
                _log.error("planner.investigation_error", error=str(e))
                raise RunExecutionError(phase="planner.investigation",
                                        reason=f"Investigation failed: {e}")
           

        # What past runs on this repo learned about PLANNING it (ADR 0018), plus the human's
        # plan-scope operator rules (ADR 0034). The reviewer reads the same two sources before
        # judging a plan; the planner reads them before writing one, so a mistake this repo has
        # already made once is avoided rather than re-made and re-caught — the reject costs a
        # whole planner↔reviewer cycle (ADR 0034). Excludes this run: a revision already gets
        # the live verdict via `revision_context`, and re-injecting it here would double it.
        # Computed once — constant across the synthesis retries below.
        repo = state.get("repo")
        repo_url = repo.repo_url if repo else ""
        lessons_block = LessonStore.render_block(
            LessonStore.find_lessons(repo_url, obj_text, scope="plan", exclude_run_id=self.run_id),
            LessonStore.find_operator_rules(repo_url, scope="plan"),
        )

        # 2. Synthesis Phase (Structured Output) — retried with feedback if quality gate rejects
        _log.info("planner.synthesizing")
        schema_json = PlannerDraft.model_json_schema()
        previous_attempt_feedback: Optional[str] = None
        result: Optional[PlannerOutput] = None

        for attempt in range(1, self.max_synthesis_attempts + 1):
            synthesis_prompt = build_synthesis_prompt(
                obj_text, investigation_summary, schema_json,
                previous_attempt_feedback, revision_context, lessons_block,
            )
            # Since we use `.with_structured_output`, we can just invoke it with the prompt.
            try:
                # include_raw=True → invoke returns a {"raw","parsing_error","parsed"} dict at
                # runtime. _invoke_structured adds the one-time json_schema→function_calling
                # fallback if Anthropic's grammar compile times out on our schema.
                raw = self._invoke_structured([HumanMessage(content=synthesis_prompt)])
                if raw.get("raw") is not None:
                    self.token_usage = self.token_usage + sum_usage([raw["raw"]])
                if raw.get("parsing_error"):
                    raise raw["parsing_error"]
                parsed = raw["parsed"]
                draft = parsed if isinstance(parsed, PlannerDraft) else PlannerDraft.model_validate(parsed)
                # Lift the draft into a full PlannerOutput: the LLM never authors
                # review_feedback, so it starts empty and is carried forward below.
                raw_result = PlannerOutput(
                    plan=Plan(**draft.plan.model_dump(), review_feedback=[]),
                    microtasks=draft.microtasks,
                )
            except ValidationError as e:
                # Structured output is schema-CONSTRAINED server-side (Anthropic `json_schema`),
                # so a required field can't be omitted — a ValidationError here means a genuine
                # bug (schema/model mismatch), not a flaky generation. Crash cleanly; don't
                # retry the same deterministic failure.
                _log.error("planner.synthesis_validation_failed", error=str(e))
                raise
            except Exception as e:
                if is_transient_llm_error(e):
                    _log.error("planner.api_unreachable", phase="synthesis", error=str(e))
                    raise RetriableError(f"planner synthesis LLM call failed: {e}", phase="planner")
                _log.error("planner.synthesis_error", error=str(e))
                raise

            # Write output file from raw LLM result — always, even if quality checks fail below
            plan_file_path = create_plan_file_as_output(investigation_summary, raw_result, self.run_id, revision_context, self.token_usage, lessons_block)

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
