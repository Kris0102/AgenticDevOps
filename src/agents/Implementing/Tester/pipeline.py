"""TestPipeline — orchestrates the Tester's phases.

Replaces the old monolithic `Tester` class: the verification flow is now an explicit,
ordered list of `TestPhase`s (detect → existing-suite → synthesis → guard → execute →
verdict), each its own class. The pipeline just builds the shared collaborators (the LLM
test-writer agents, the gateway-routed tools) and runs the phases, logging each one so a
run reads as visible phases. It produces the node-facing result dict; it does NOT decide
routing — TesterRunner derives the PASS/FAIL verdict from the emitted checks.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases import (
    DetectProjectPhase,
    DeterministicChecksPhase,
    ExecutePhase,
    SynthesisPhase,
    TestPhase,
    TrivialGuardPhase,
    VerdictPhase,
)
from agents.Implementing.Tester.prompt.tester_prompt import (
    TESTER_SYSTEM_PROMPT_JS,
    TESTER_SYSTEM_PROMPT_PYTHON,
)
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger
from langchain_core.language_models.chat_models import BaseChatModel

from orchestrator.state import BlackboardState, Microtask, RunConfig
from providers import build_chat_llm
from tool_gateway.Roles import Roles

load_dotenv()

_log = get_logger(Category.TESTER)


def _max_tool_steps() -> int:
    # A test-writer needs only a handful of tool calls (read targets → maybe one find →
    # write). Keep this tight so a wandering agent fails fast instead of burning the budget.
    try:
        return max(1, int(os.getenv("TESTER_MAX_TOOL_STEPS", "6")))
    except ValueError:
        return 6


class TestPipeline:
    """Builds the phase list once, runs it per microtask."""

    def __init__(self, run_id: str, tester_tools: TesterTools, config: RunConfig):
        self.run_id = run_id
        self.tools = tester_tools
        self.name = Roles.TESTER.value
        self.config = config
        recursion_limit = self.config.knobs.tester_max_tool_steps * 2 + 5

        llm = self._build_llm()
        python_agent = create_agent(llm, tools=self.tools.build_tools(), system_prompt=TESTER_SYSTEM_PROMPT_PYTHON)
        js_agent = create_agent(llm, tools=self.tools.build_tools(), system_prompt=TESTER_SYSTEM_PROMPT_JS)

        self.phases: List[TestPhase] = [
            DetectProjectPhase(self.tools),
            DeterministicChecksPhase(self.tools),
            SynthesisPhase(self.tools, python_agent, js_agent, recursion_limit),
            TrivialGuardPhase(),
            ExecutePhase(self.tools),
            VerdictPhase(self.tools),
        ]

    def run(self, state: BlackboardState, microtask: Microtask, final_validation: bool = False) -> Dict[str, Any]:
        ctx = TestContext(
            state=state,
            microtask=microtask,
            run_id=self.run_id,
            final_validation=final_validation,
            rejection_note=state.get("test_rejection_note"),
            reflections=list(state.get("reflections") or []),
        )
        _log.info("tester.pipeline.start", run_id=self.run_id,
                  microtask_id=microtask.microtask_id, final_validation=final_validation,
                  is_rewrite=bool(ctx.rejection_note))
        _log.info("tester.pipeline.phases", information = f"Run {state.get('run_id')} will execute the following phases: {[phase.name for phase in self.phases]}")
        for phase in self.phases:
            if phase.should_run(ctx):
                _log.info("tester.phase.enter", run_id=self.run_id, phase=phase.name)
                ctx = phase.run(ctx)
            else:
                _log.info("tester.phase.skip", run_id=self.run_id, phase=phase.name)
        _log.info("tester.pipeline.end", run_id=self.run_id, summary=ctx.summary)
        return ctx.to_result()

    def _build_llm(self) -> BaseChatModel:
        return build_chat_llm(self.config.provider_for(self.config.llm.tester), self.config.llm.tester,
                              timeout_seconds=self.config.knobs.tester_timeout_seconds)
