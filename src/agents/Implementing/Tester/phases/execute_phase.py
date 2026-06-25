"""Phase 5 — run the synthesized test file (pytest / jest).

Only runs when the guard passed: a trivial file is already handled, no point executing it.
A non-0/1 exit (pytest collection error, jest config error, gateway timeout) is classified
ERRORED — a harness problem, NOT a statement about the code — so a flaky synthesized-test
import doesn't get blamed on the Coder (fix: misattribution). The result becomes
`ctx.synth_exec_check`, combined by VerdictPhase.
"""
from __future__ import annotations

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.results import CheckStatus, make_check
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger

_log = get_logger(Category.TESTER)


class ExecutePhase(TestPhase):
    name = "execute_synth"

    def __init__(self, tools: TesterTools):
        self.tools = tools

    def should_run(self, ctx: TestContext) -> bool:
        return bool(ctx.synth_test_file) and ctx.guard_ok

    def run(self, ctx: TestContext) -> TestContext:
        if ctx.project_type == "javascript":
            cmd = ["npx", "jest", ctx.synth_test_file, "--no-coverage"]
        else:
            cmd = ["python", "-m", "pytest", "-q", ctx.synth_test_file]
        _log.info("tester.run_tests.start", run_id=ctx.run_id,
                  microtask_id=ctx.microtask.microtask_id, command=cmd)
        # exit codes other than 0 (pass) / 1 (real test failure) are harness errors —
        # collection failure, bad import, missing runtime, timeout — not a code verdict.
        check = make_check("unit_tests", cmd, self.tools.run(cmd, timeout_s=300),
                           error_exit_codes={2, 3, 4, 5})
        ctx.synth_exec_check = check
        if check.status == CheckStatus.ERRORED:
            _log.warning("tester.synth_harness_error", run_id=ctx.run_id,
                         exit_code=check.exit_code,
                         reason="synthesized tests could not run — harness error, not blaming code")
        else:
            _log.info("tester.run_tests.done", run_id=ctx.run_id,
                      status=str(check.status), exit_code=check.exit_code)
        return ctx
