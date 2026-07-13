"""Phase 3 (Track B) — synthesize tests from the Planner's acceptance criteria.

The fallback grounding when the repo has no suite that covers the microtask: an LLM writes
a test file from `microtask.acceptance_criteria` (text + concrete examples) — an input
authored by the Planner, independent of the Coder, which is what keeps the tests from
being tautological. The written file is read back into the context for the guard + execute
phases. Output (verdict) is decided later; this phase only produces the file.
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.prompt.tester_prompt import (
    build_test_writing_prompt_js,
    build_test_writing_prompt_python,
)
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger
from orchestrator.state import RunConfig
from providers import sum_usage

_log = get_logger(Category.TESTER)


# Synth tests are VERIFICATION scaffolding, not a deliverable — write them to a git-ignored
# dir (seeded in bootstrap + pathspec-excluded at push) so they never reach the run branch.
# Depth 1 (like the old `tests/`) keeps `require('../x')` / relative imports resolving. ADR 0019.
SYNTH_TEST_DIR = ".agentic_tests"


def _test_file_path(microtask_id: str, project_type: str) -> str:
    safe = microtask_id.replace("-", "_")
    ext = "test.js" if project_type == "javascript" else "py"
    return f"{SYNTH_TEST_DIR}/test_{safe}.{ext}"


class SynthesisPhase(TestPhase):
    name = "synthesis"

    def __init__(self, tools: TesterTools, python_agent, js_agent, recursion_limit: int, config: RunConfig):
        self.tools = tools
        self.python_agent = python_agent
        self.js_agent = js_agent
        self.recursion_limit = recursion_limit
        self.llm_config = config.llm.tester  # for token accounting    

    def should_run(self, ctx: TestContext) -> bool:
        """Synthesize ONLY when needed — otherwise the test becomes a moving target the
        coder can never converge on (it satisfies vN, we regenerate vN+1, it fails, …).
        Write the test (a) the first time, or (b) when a rewrite was explicitly requested
        (reflector fix_target="test" / HITL test-reject → rejection_note set). On a normal
        coder-retry, reuse the existing test — it still runs as part of Track A."""
        if ctx.skip or ctx.project_type not in ("python", "javascript"):
            return False
        if ctx.rejection_note:
            return True                       # explicit rewrite requested
        return not self._test_exists(ctx)     # first time only

    def _test_exists(self, ctx: TestContext) -> bool:
        test_file = _test_file_path(ctx.microtask.microtask_id, ctx.project_type)
        r = self.tools.run(["find", test_file], timeout_s=10)
        return int(r.get("exit_code", 1)) == 0 and bool(r.get("stdout", "").strip())

    def run(self, ctx: TestContext) -> TestContext:
        test_file = _test_file_path(ctx.microtask.microtask_id, ctx.project_type)
        _log.info("tester.write_tests.start", run_id=ctx.run_id,
                  microtask_id=ctx.microtask.microtask_id, test_file=test_file,
                  lang=ctx.project_type, is_rewrite=bool(ctx.rejection_note))

        builder = build_test_writing_prompt_js if ctx.project_type == "javascript" else build_test_writing_prompt_python
        agent = self.js_agent if ctx.project_type == "javascript" else self.python_agent
        builder_kwargs = dict(
            objective=ctx.state.get("objective"),
            plan=ctx.state.get("plan"),
            microtask=ctx.microtask,
            test_file=test_file,
            rejection_note=ctx.rejection_note,
            reflections=ctx.reflections,
        )
        # JS synthesis targets the repo's detected framework (Python has one runner: pytest).
        if ctx.project_type == "javascript":
            builder_kwargs["framework"] = ctx.js_test_framework
        prompt = builder(**builder_kwargs)
        try:
            agent_result = agent.invoke(
                {"messages": [HumanMessage(content=prompt)]},
                config={"recursion_limit": self.recursion_limit},
            )
            ctx.token_usage = ctx.token_usage + sum_usage(agent_result.get("messages", []))
        except GraphRecursionError:
            # The test-writer looped past its tool-call budget. DON'T crash the run — use
            # whatever it wrote (if anything); the trivial-guard rejects an unusable file
            # and the verdict falls back to Track A / "unverified".
            _log.warning("tester.synth_recursion_limit", run_id=ctx.run_id,
                         microtask_id=ctx.microtask.microtask_id, limit=self.recursion_limit,
                         reason="test-writer hit its tool-call budget — degrading, not crashing")

        ctx.synth_test_file = test_file
        ctx.synth_test_content = self.tools.read_sandbox_file(test_file)
        _log.info("tester.write_tests.done", run_id=ctx.run_id,
                  microtask_id=ctx.microtask.microtask_id, test_file=test_file)
        return ctx
