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

import re
from contextlib import contextmanager
from typing import Iterator, List, Optional

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.results import CheckResult, CheckStatus, make_check, skipped_check
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger

_log = get_logger(Category.TESTER)

# pytest exit codes: 0 = passed, 1 = failures, 5 = no tests collected.
_PYTEST_NO_TESTS = 5

# jest prints two "N failed" lines ("Test Suites:" and "Tests:") — prefer the test-level one;
# pytest's summary has a bare "N failed". Used to baseline + gate on the failure-count DELTA
# (GH #72) so a pre-existing red test doesn't hide later regressions.
_JEST_FAILED_RE = re.compile(r"Tests:.*?(\d+)\s+failed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")


def _parse_failing_count(stdout: str) -> int:
    if not stdout:
        return 0
    m = _JEST_FAILED_RE.search(stdout) or _FAILED_RE.search(stdout)
    return int(m.group(1)) if m else 0


class DeterministicChecksPhase(TestPhase):
    name = "deterministic_checks"

    def __init__(self, tools: TesterTools):
        self.tools = tools

    def should_run(self, ctx: TestContext) -> bool:
        return not ctx.skip

    def run(self, ctx: TestContext) -> TestContext:
        if ctx.project_type == "python":
            self._install_python_deps()
            self._establish_baseline(ctx, "python")   # pristine suite (first run) — deps present
            ctx.track_a_checks = self._compact([
                self._py_suite(),
                self._py_build(ctx),     # compile the microtask's target files (gating)
                self._py_typecheck(),    # mypy, if configured (gating)
                self._py_lint(),         # ruff, if configured (advisory)
            ])
        elif ctx.project_type == "javascript":
            self._install_js_deps()
            self._establish_baseline(ctx, "javascript")
            ctx.track_a_checks = self._compact([
                self._js_suite(),
                self._js_build(),        # npm run build, if a build script exists (gating)
                self._js_typecheck(),    # tsc --noEmit, if tsconfig.json (gating)
                self._js_lint(),         # eslint, if configured (advisory)
            ])
        # Target-scoped JS/TS SUBPROJECT checks — root detection is blind to e.g. a
        # dashboard/ inside a Python repo, which left .tsx microtasks with zero Track A
        # signal (run 27198e26: verdict fell through to 'unverified' every attempt).
        ctx.track_a_checks += self._subproject_js_checks(ctx)
        ctx.suite_current_failures = self._current_suite_failures(ctx)
        for c in ctx.track_a_checks:
            _log.info("tester.track_a.check", run_id=ctx.run_id, label=c.label, status=str(c.status))
        return ctx

    @staticmethod
    def _current_suite_failures(ctx: TestContext) -> int:
        """Failing-test count in THIS run's existing suite (GH #72). 0 unless the suite FAILED;
        a FAILED-but-unparseable suite is forced above the baseline so it still gates."""
        suite = next((c for c in ctx.track_a_checks if c.label == "integration_tests"), None)
        if not suite or suite.status != CheckStatus.FAILED:
            return 0
        n = _parse_failing_count(suite.stdout)
        return n if n > 0 else (ctx.suite_baseline_failures or 0) + 1

    # ---- existing-suite baseline (GH #28/#72) -------------------------------
    def _establish_baseline(self, ctx: TestContext, lang: str) -> None:
        """Set `ctx.suite_baseline_failures` — the failing-test COUNT of the repo's OWN suite,
        captured ONCE on the first Tester run and fixed for the rest of the run. Established on
        the PRISTINE tree (before any Coder change) so a green repo the Coder breaks is still
        gated (no false negative). Downstream, `integration_tests` gates only on the delta —
        NEW failures beyond this count (GH #72), so a pre-existing red test doesn't hide later
        regressions."""
        prev = ctx.state.get("suite_baseline_failures")
        if prev is not None:
            ctx.suite_baseline_failures = prev
            return
        ctx.suite_baseline_failures = self._pristine_suite_failures(lang)
        _log.info("tester.baseline.established", run_id=ctx.run_id,
                  suite_baseline_failures=ctx.suite_baseline_failures)

    def _pristine_suite_failures(self, lang: str) -> int:
        """Failing-test count of the repo's existing suite on the PRISTINE tree (HEAD): stash
        the Coder's uncommitted changes, run the suite, restore. Deps (node_modules / installed
        packages) are git-ignored so they survive the stash. On any git error, degrade to 0
        (gate normally) rather than a false 'inherited failures' that would hide regressions."""
        push = self.tools.run(["git", "stash", "push", "-u", "-m", "agentic-suite-baseline"], timeout_s=60)
        push_out = (push.get("stdout") or "") + (push.get("stderr") or "")
        if int(push.get("exit_code", 1)) != 0:
            _log.warning("tester.baseline.stash_failed", detail=push_out[:200])
            return 0
        stashed = "No local changes to save" not in push_out
        try:
            suite = self._py_suite() if lang == "python" else self._js_suite()
            return _parse_failing_count(suite.stdout) if suite.status == CheckStatus.FAILED else 0
        finally:
            if stashed:
                # Discard anything the pristine suite run touched (git-ignored deps are left
                # alone by checkout/clean), then restore the Coder's changes onto clean HEAD —
                # no conflict is possible since the tree is pristine before the pop.
                self.tools.run(["git", "checkout", "--", "."], timeout_s=30)
                self.tools.run(["git", "clean", "-fd", "--", "."], timeout_s=30)
                pop = self.tools.run(["git", "stash", "pop"], timeout_s=60)
                if int(pop.get("exit_code", 1)) != 0:
                    _log.error("tester.baseline.stash_pop_failed",
                               detail=(pop.get("stderr") or "")[:300])

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

    def _npm_has_script(self, name: str, pkg_dir: str = ".") -> bool:
        probe = self.tools.run(
            ["node", "-e",
             f"const s=(require('./{pkg_dir}/package.json').scripts||{{}}).{name}||'';"
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
    # The Tester's ephemeral synth dir (ADR 0019), and where we stash it while Track A runs.
    _AGENTIC_DIR = ".agentic_tests"
    _AGENTIC_HIDDEN = "/workspace/.agentic_tests_hidden"

    def _js_suite(self) -> CheckResult:
        """Run the repo's OWN suite (Track A) — and ONLY the repo's own tests.

        Bare `npm test` discovers our synthesized `.agentic_tests/` too (node --test /
        jest / mocha / vitest all glob the tree), so a PRIOR microtask's synth file would
        leak into THIS microtask's suite verdict and poison it (run 5e7f7c41: a stale
        test_MT_4 importing a browser module crashed `npm test` under node:test, failing an
        unrelated microtask). We hide the dir for the duration of the run — runner-agnostic,
        unlike the old jest-only `--testPathIgnorePatterns` (Python uses pytest `--ignore`).

        A repo with zero tests makes jest exit 1 ("No tests found") — 'nothing to verify',
        not a failure; map it to SKIPPED like the pytest side (exit 5), otherwise every first
        microtask on a testless repo fails Track A."""
        if not self._npm_has_script("test"):
            return skipped_check("integration_tests", "no real npm test script in package.json")
        cmd = ["npm", "test"]
        with self._agentic_tests_hidden():
            result = self.tools.run(cmd, timeout_s=300)
        blob = f"{result.get('stdout', '')} {result.get('stderr', '')}"
        if int(result.get("exit_code", 1)) == 1 and "No tests found" in blob:
            return skipped_check("integration_tests", "no discoverable tests in repo (jest: No tests found)")
        return make_check("integration_tests", cmd, result)

    @contextmanager
    def _agentic_tests_hidden(self) -> Iterator[None]:
        """Move `.agentic_tests/` out of the tree for the wrapped block, then restore it —
        so a Track-A suite run (any JS runner) can't discover our synthesized tests. No-op
        when the dir doesn't exist yet (first microtask). `mv` is gateway-allowlisted for the
        Tester. Best-effort restore in `finally` so a suite crash can't strand the synth files."""
        present = bool(
            self.tools.run(["find", self._AGENTIC_DIR, "-maxdepth", "0"], timeout_s=10)
            .get("stdout", "").strip()
        )
        if present:
            self.tools.run(["mv", self._AGENTIC_DIR, self._AGENTIC_HIDDEN], timeout_s=15)
        try:
            yield
        finally:
            if present:
                self.tools.run(["mv", self._AGENTIC_HIDDEN, self._AGENTIC_DIR], timeout_s=15)

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

    # ------------------------------------------------ JS/TS subproject checks (target-scoped)
    # Root detection sees one project type; a JS app nested in a Python repo (this repo's
    # own dashboard/) got NO deterministic check at all for its microtasks. These run the
    # subproject's own build (or tsc) for the directories the CURRENT microtask touches —
    # scoped to targets so an unrelated Python microtask never pays the npm cost.
    _JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")

    def _subproject_js_checks(self, ctx: TestContext) -> List[CheckResult]:
        dirs = sorted({
            d for f in (ctx.microtask.target_files or [])
            if f.endswith(self._JS_EXTS)
            for d in [self._nearest_pkg_dir(self._repo_relative(f))]
            if d and d != "."
        })
        checks: List[CheckResult] = []
        for d in dirs:
            self._install_subproject_deps(d)
            check = self._subproject_build_check(d)
            if check:
                checks.append(check)
        return checks

    @staticmethod
    def _repo_relative(path: str) -> str:
        """Planner/Coder paths are sometimes absolute sandbox paths — normalize to
        repo-root-relative so the ancestor walk works."""
        for prefix in ("/workspace/repo/", "workspace/repo/"):
            if path.startswith(prefix):
                return path[len(prefix):]
        return path.lstrip("/")

    def _nearest_pkg_dir(self, path: str) -> Optional[str]:
        """Closest ancestor directory of `path` (repo-relative) holding a package.json.
        Root ('.') is excluded by the caller — the root branches already cover it."""
        parts = path.split("/")[:-1]
        while parts:
            d = "/".join(parts)
            probe = self.tools.run(["find", f"{d}/package.json", "-maxdepth", "0"], timeout_s=10)
            if probe.get("stdout", "").strip():
                return d
            parts.pop()
        return None

    def _install_subproject_deps(self, d: str) -> None:
        """`npm install` once per subproject per run — node_modules survives in the
        run-lived sandbox across microtasks/retries, so presence is the cache marker."""
        probe = self.tools.run(["find", f"{d}/node_modules", "-maxdepth", "0"], timeout_s=10)
        if probe.get("stdout", "").strip():
            return
        _log.info("tester.subproject_deps_install", dir=d)
        self.tools.run(["npm", "--prefix", d, "install"], timeout_s=600)

    def _subproject_build_check(self, d: str) -> Optional[CheckResult]:
        """The subproject's own build script (gating `build` — usually tsc+bundler, the
        strongest signal it defines), else bare tsc when only a tsconfig exists. NB the
        label may coexist with a root `build` check on a mixed-target microtask — the
        verdict gates on the LIST, so both count; only the per-label artifact map keeps
        the last one."""
        if self._npm_has_script("build", pkg_dir=d):
            cmd = ["npm", "--prefix", d, "run", "build"]
            return make_check("build", cmd, self.tools.run(cmd, timeout_s=600))
        if self.tools.run(["find", f"{d}/tsconfig.json", "-maxdepth", "0"],
                          timeout_s=10).get("stdout", "").strip():
            cmd = ["npx", "--yes", "tsc", "--noEmit", "-p", d]
            return make_check("typecheck", cmd, self.tools.run(cmd, timeout_s=300))
        _log.info("tester.subproject_no_check", dir=d,
                  reason="no build script and no tsconfig.json — nothing deterministic to run")
        return None
