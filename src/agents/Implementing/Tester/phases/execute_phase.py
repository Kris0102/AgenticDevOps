"""Phase 5 — run the synthesized test file with the repo's OWN runner (pytest for Python;
jest / vitest / mocha / node:test for JavaScript, per DetectProjectPhase).

Only runs when the guard passed: a trivial file is already handled, no point executing it.
A non-0/1 exit (pytest collection error, jest config error, gateway timeout) is classified
ERRORED — a harness problem, NOT a statement about the code — so a flaky synthesized-test
import doesn't get blamed on the Coder (fix: misattribution). The result becomes
`ctx.synth_exec_check`, combined by VerdictPhase.
"""
from __future__ import annotations

from typing import Optional

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.results import CheckStatus, make_check
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger

_log = get_logger(Category.TESTER)


# Browser/DOM globals that don't exist under a bare Node/pytest runner. A synth test that
# trips one of these never executed the code under test — it died IMPORTING a module that
# touches the DOM at load time (e.g. `ui.js` bootstrapping on import). That's an ENVIRONMENT
# problem (no jsdom), not a statement about the code, so it must NOT fail the microtask.
_BROWSER_GLOBALS = ("document", "window", "navigator", "localStorage", "sessionStorage",
                    "HTMLElement", "customElements", "alert")
# Module-resolution / parse failures — the test file (or something it imports) could not be
# loaded at all → harness error, the test never ran.
_MODULE_LOAD_MARKERS = ("Cannot find module", "Cannot find package", "ERR_MODULE_NOT_FOUND",
                        "ERR_REQUIRE_ESM", "SyntaxError:")


def _load_error_reason(output: str) -> Optional[str]:
    """Return a reason if `output` shows the synth test failed to LOAD (vs a real assertion
    failure). Scoped tightly to the known env/resolution classes so a genuine
    `ReferenceError: myVar is not defined` in the CODE stays a real FAILED verdict — only the
    well-known browser globals are treated as an environment (harness) error."""
    for marker in _MODULE_LOAD_MARKERS:
        if marker in output:
            return marker
    for g in _BROWSER_GLOBALS:
        if f"{g} is not defined" in output:
            return f"{g} is not defined (no DOM/browser env under the test runner)"
    return None


class ExecutePhase(TestPhase):
    name = "execute_synth"

    def __init__(self, tools: TesterTools):
        self.tools = tools

    def should_run(self, ctx: TestContext) -> bool:
        return bool(ctx.synth_test_file) and ctx.guard_ok

    @staticmethod
    def _js_command(ctx: TestContext) -> list[str]:
        """Run the synth test with the repo's OWN framework (DetectProjectPhase set it) —
        running node:test tests under `npx jest` (or vice-versa) was the tester's #1 false
        negative. All four runners target the single synth file explicitly so a repo's
        testMatch/config can never hide or over-collect it."""
        f = ctx.synth_test_file or ""
        framework = ctx.js_test_framework or "node:test"
        if framework == "jest":
            # --runTestsByPath: exact file, not a testMatch-filtered pattern.
            return ["npx", "jest", "--runTestsByPath", f, "--no-coverage"]
        if framework == "vitest":
            return ["npx", "vitest", "run", f]
        if framework == "mocha":
            return ["npx", "mocha", f]
        return ["node", "--test", f]   # node:test — built in, no dependency

    def run(self, ctx: TestContext) -> TestContext:
        if ctx.project_type == "javascript":
            cmd = self._js_command(ctx)
        else:
            cmd = ["python", "-m", "pytest", "-q", ctx.synth_test_file]
        _log.info("tester.run_tests.start", run_id=ctx.run_id,
                  microtask_id=ctx.microtask.microtask_id, command=cmd)
        # exit codes other than 0 (pass) / 1 (real test failure) are harness errors —
        # collection failure, bad import, missing runtime, watchdog timeout (124/137) — not a
        # code verdict. 124/137 = the sandbox watchdog killed a hung test (e.g. a browser module
        # with a live setInterval); that's a harness problem, so it must not fail the microtask.
        check = make_check("unit_tests", cmd, self.tools.run(cmd, timeout_s=60), # 60 seconds timeout is sufficient
                           error_exit_codes={2, 3, 4, 5, 124, 137})
        # Stage-1 reclassification: an exit-1 that is actually a LOAD failure (couldn't import
        # the module / no DOM env) is a harness error, not a code failure. make_check only sees
        # the exit code — a bad import under `node --test` exits 1, indistinguishable from a
        # real assertion failure without reading the text — so flip FAILED→ERRORED here. The
        # verdict then excludes it (ERRORED never decides) and Track A carries.
        if check.status == CheckStatus.FAILED:
            reason = _load_error_reason(f"{check.stdout}\n{check.stderr}")
            if reason:
                check.status = CheckStatus.ERRORED
                _log.warning("tester.synth_load_error", run_id=ctx.run_id, exit_code=check.exit_code,
                             reason=f"synth test could not load ({reason}) — harness error, not blaming code")
        ctx.synth_exec_check = check
        if check.status == CheckStatus.ERRORED:
            _log.warning("tester.synth_harness_error", run_id=ctx.run_id,
                         exit_code=check.exit_code,
                         reason="synthesized tests could not run — harness error, not blaming code")
        else:
            _log.info("tester.run_tests.done", run_id=ctx.run_id,
                      status=str(check.status), exit_code=check.exit_code)
        return ctx
