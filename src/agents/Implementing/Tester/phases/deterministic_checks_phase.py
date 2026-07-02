"""Phase 2 (Track A) — the deterministic checks. The primary, LLM-free grounding.

Runs the repo's EXISTING test suite plus build/typecheck/lint, each verdict from an exit
code (ADR 0015/0016). Build/typecheck/lint are CONFIG-GATED: a check runs only when the
repo opted into that tool (a config file / npm script is present), so a repo without a
linter isn't punished for not having one. Build (typecheck) failures gate the verdict;
lint is advisory (recorded but non-gating — style shouldn't block acceptance).

`evidence.checks` has fixed fields (lint/typecheck/unit_tests/integration_tests/build), so
each produced check is labelled to match one of them; VerdictPhase folds the gating ones
into the canonical `unit_tests` verdict.
"""
from __future__ import annotations

from typing import List, Optional

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.results import CheckResult, make_check, skipped_check
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger

_log = get_logger(Category.TESTER)

# pytest exit codes: 0 = passed, 1 = failures, 5 = no tests collected.
_PYTEST_NO_TESTS = 5


class DeterministicChecksPhase(TestPhase):
    name = "deterministic_checks"

    def __init__(self, tools: TesterTools):
        self.tools = tools

    def should_run(self, ctx: TestContext) -> bool:
        return not ctx.skip

    def run(self, ctx: TestContext) -> TestContext:
        if ctx.project_type == "python":
            self._install_python_deps()
            ctx.track_a_checks = self._compact([
                self._py_suite(),
                self._py_build(ctx),     # compile the microtask's target files (gating)
                self._py_typecheck(),    # mypy, if configured (gating)
                self._py_lint(),         # ruff, if configured (advisory)
            ])
        elif ctx.project_type == "javascript":
            self._install_js_deps()
            ctx.track_a_checks = self._compact([
                self._js_suite(),
                self._js_build(),        # npm run build, if a build script exists (gating)
                self._js_typecheck(),    # tsc --noEmit, if tsconfig.json (gating)
                self._js_lint(),         # eslint, if configured (advisory)
            ])
        for c in ctx.track_a_checks:
            _log.info("tester.track_a.check", run_id=ctx.run_id, label=c.label, status=str(c.status))
        return ctx

    @staticmethod
    def _compact(items: List[Optional[CheckResult]]) -> List[CheckResult]:
        return [c for c in items if c]

    # ----------------------------------------------------------- detection helpers
    def _find(self, *patterns: str) -> bool:
        args = ["find", ".", "-maxdepth", "1"]
        for i, p in enumerate(patterns):
            if i:
                args.append("-o")
            args += ["-name", p]
        return bool(self.tools.run(args, timeout_s=10).get("stdout", "").strip())

    def _grep_fixed(self, token: str, path: str) -> bool:
        """True if the literal `token` appears in `path` (exit 0). Missing file → False."""
        return int(self.tools.run(["grep", "-qF", "--", token, path], timeout_s=10).get("exit_code", 1)) == 0

    def _npm_has_script(self, name: str) -> bool:
        probe = self.tools.run(
            ["node", "-e",
             f"const s=(require('./package.json').scripts||{{}}).{name}||'';"
             f"process.exit(s && !/no test specified/.test(s) ? 0 : 1)"],
            timeout_s=15)
        return int(probe.get("exit_code", 1)) == 0

    # ----------------------------------------------------------- python checks
    def _py_suite(self) -> CheckResult:
        # --ignore the Tester's own ephemeral synth dir so a stale synth file from a prior
        # iteration is never picked up by the repo's suite (Track A). ADR 0019.
        cmd = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--ignore=.agentic_tests"]
        result = self.tools.run(cmd, timeout_s=300)
        if int(result.get("exit_code", 1)) == _PYTEST_NO_TESTS:
            return skipped_check("integration_tests", "no existing pytest suite in repo")
        # exit 2/3/4 = collection/usage/internal error → harness (ERRORED), not a real failure.
        return make_check("integration_tests", cmd, result, error_exit_codes={2, 3, 4})

    def _py_build(self, ctx: TestContext) -> Optional[CheckResult]:
        """Compile the microtask's own Python target files — scoped to what the coder just
        wrote so pre-existing breakage elsewhere doesn't fail every microtask."""
        py_targets = [f for f in ctx.microtask.target_files if f.endswith(".py")]
        if not py_targets:
            return None
        cmd = ["python", "-m", "py_compile", *py_targets]
        return make_check("build", cmd, self.tools.run(cmd, timeout_s=60))

    def _py_typecheck(self) -> Optional[CheckResult]:
        if not (self._grep_fixed("[tool.mypy]", "pyproject.toml")
                or self._find("mypy.ini", ".mypy.ini")):
            return None
        self.tools.run(["pip", "install", "--quiet", "mypy"], timeout_s=120)
        cmd = ["python", "-m", "mypy", "."]  # via `python -m` — gateway allowlists `python`, not `mypy`
        return make_check("typecheck", cmd, self.tools.run(cmd, timeout_s=180))

    def _py_lint(self) -> Optional[CheckResult]:
        if not (self._grep_fixed("[tool.ruff]", "pyproject.toml")
                or self._find("ruff.toml", ".ruff.toml")):
            return None
        self.tools.run(["pip", "install", "--quiet", "ruff"], timeout_s=120)
        cmd = ["python", "-m", "ruff", "check", "."]  # via `python -m` — gateway allowlists `python`, not `ruff`
        return make_check("lint", cmd, self.tools.run(cmd, timeout_s=120))

    # Marker dir in the run-lived sandbox so dep install runs ONCE per run, not per
    # microtask/retry (the slow part — npm install / pip downloads). NOTE: if a later
    # microtask adds a dependency it won't be picked up until the sandbox is recreated.
    _DEPS_MARKER = "/workspace/.tester_deps_ready"

    def _deps_cached(self) -> bool:
        r = self.tools.run(["find", "/workspace", "-maxdepth", "1", "-name", ".tester_deps_ready"], timeout_s=10)
        return bool(r.get("stdout", "").strip())

    def _mark_deps_cached(self) -> None:
        self.tools.run(["mkdir", "-p", self._DEPS_MARKER], timeout_s=10)

    def _install_python_deps(self) -> None:
        if self._deps_cached():
            _log.info("tester.deps_cached", lang="python", reason="already installed this run")
            return
        self.tools.run(["pip", "install", "--quiet", "pytest"], timeout_s=120)
        for req in ("requirements.txt", "requirements-dev.txt", "requirements/test.txt"):
            r = self.tools.run(["pip", "install", "-r", req], timeout_s=120)
            if r.get("exit_code", 1) == 0:
                _log.info("tester.deps_installed", cmd=f"pip install -r {req}")
        if self._find("pyproject.toml", "setup.py", "setup.cfg"):
            self.tools.run(["pip", "install", "-e", "."], timeout_s=120)
        self._mark_deps_cached()

    # ----------------------------------------------------------- javascript checks
    def _js_suite(self) -> CheckResult:
        if not self._npm_has_script("test"):
            return skipped_check("integration_tests", "no real npm test script in package.json")
        cmd = ["npm", "test"]
        return make_check("integration_tests", cmd, self.tools.run(cmd, timeout_s=300))

    def _js_build(self) -> Optional[CheckResult]:
        if not self._npm_has_script("build"):
            return None
        cmd = ["npm", "run", "build"]
        return make_check("build", cmd, self.tools.run(cmd, timeout_s=300))

    def _js_typecheck(self) -> Optional[CheckResult]:
        if not self._find("tsconfig.json"):
            return None
        cmd = ["npx", "--yes", "tsc", "--noEmit"]
        return make_check("typecheck", cmd, self.tools.run(cmd, timeout_s=180))

    def _js_lint(self) -> Optional[CheckResult]:
        if not self._find(".eslintrc*", "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs"):
            return None
        cmd = ["npx", "--yes", "eslint", "."]
        return make_check("lint", cmd, self.tools.run(cmd, timeout_s=180))

    def _install_js_deps(self) -> None:
        if self._deps_cached():
            _log.info("tester.deps_cached", lang="javascript", reason="already installed this run")
            return
        if int(self.tools.run(["npm", "--version"], timeout_s=10).get("exit_code", 1)) != 0:
            _log.warning("tester.npm_not_found", reason="installing nodejs via apt-get (rebuild sandbox image)")
            self.tools.run(["apt-get", "update", "-qq"], timeout_s=60)
            self.tools.run(["apt-get", "install", "-y", "--no-install-recommends", "nodejs", "npm"], timeout_s=120)
        self.tools.run(["npm", "install"], timeout_s=180)
        self._mark_deps_cached()
