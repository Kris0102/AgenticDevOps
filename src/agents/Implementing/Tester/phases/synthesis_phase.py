"""Phase 3 (Track B) — synthesize tests from the Planner's acceptance criteria.

The fallback grounding when the repo has no suite that covers the microtask: an LLM writes
a test file from `microtask.acceptance_criteria` (text + concrete examples) — an input
authored by the Planner, independent of the Coder, which is what keeps the tests from
being tautological. The written file is read back into the context for the guard + execute
phases. Output (verdict) is decided later; this phase only produces the file.
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.prompt.tester_prompt import (
    build_test_writing_prompt_js,
    build_test_writing_prompt_python,
)
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger

_log = get_logger(Category.TESTER)


def _test_file_path(microtask_id: str, project_type: str) -> str:
    safe = microtask_id.replace("-", "_")
    return f"tests/test_{safe}.test.js" if project_type == "javascript" else f"tests/test_{safe}.py"


class SynthesisPhase(TestPhase):
    name = "synthesis"

    def __init__(self, tools: TesterTools, python_agent, js_agent, recursion_limit: int):
        self.tools = tools
        self.python_agent = python_agent
        self.js_agent = js_agent
        self.recursion_limit = recursion_limit

    def should_run(self, ctx: TestContext) -> bool:
        # Track B is the fallback grounding — run on any supported project. (Refinable to
        # "only when the existing suite doesn't cover this microtask" — see ADR 0016.)
        return not ctx.skip and ctx.project_type in ("python", "javascript")

    def run(self, ctx: TestContext) -> TestContext:
        test_file = _test_file_path(ctx.microtask.microtask_id, ctx.project_type)
        _log.info("tester.write_tests.start", run_id=ctx.run_id,
                  microtask_id=ctx.microtask.microtask_id, test_file=test_file,
                  lang=ctx.project_type, is_rewrite=bool(ctx.rejection_note))

        builder = build_test_writing_prompt_js if ctx.project_type == "javascript" else build_test_writing_prompt_python
        agent = self.js_agent if ctx.project_type == "javascript" else self.python_agent
        prompt = builder(
            objective=ctx.state.get("objective"),
            plan=ctx.state.get("plan"),
            microtask=ctx.microtask,
            test_file=test_file,
            rejection_note=ctx.rejection_note,
            reflections=ctx.reflections,
        )
        agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"recursion_limit": self.recursion_limit},
        )

        ctx.synth_test_file = test_file
        ctx.synth_test_content = self.tools.read_sandbox_file(test_file)
        _log.info("tester.write_tests.done", run_id=ctx.run_id,
                  microtask_id=ctx.microtask.microtask_id, test_file=test_file)
        return ctx
