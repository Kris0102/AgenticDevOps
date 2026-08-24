"""Phase 1 — detect the project type from markers at the repo root.

JavaScript (package.json) takes priority over Python markers. An unsupported project
sets `ctx.skip` so the synthesis/execute phases no-op and the verdict is `skipped`
(the Reflector treats skipped as passed — nothing to fix).
"""
from __future__ import annotations
import enum
import json

from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger

_log = get_logger(Category.TESTER)

class SupportedProjectTypes(enum.Enum):
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    UNKNOWN = "unknown"

class DetectProjectPhase(TestPhase):
    name = "detect_project"

    def __init__(self, tools: TesterTools):
        self.tools = tools

    def run(self, ctx: TestContext) -> TestContext:
        ctx.project_type = self._detect()
        if ctx.project_type == SupportedProjectTypes.UNKNOWN.value:
            ctx.skip = True
        if ctx.project_type == SupportedProjectTypes.JAVASCRIPT.value:
            ctx.js_test_framework = self._detect_js_framework()
        _log.info("tester.project_type.detected", run_id=ctx.run_id,
                  project_type=ctx.project_type, js_framework=ctx.js_test_framework,
                  microtask_id=ctx.microtask.microtask_id)
        return ctx

    def _detect_js_framework(self) -> str:
        """Which JS test framework the repo uses, so Track B writes AND runs tests in it.
        `scripts.test` is ground truth (what the repo actually runs); devDependencies are the
        fallback. Defaults to node:test — Node's built-in runner needs no dependency, so it's
        the only safe default for a repo that declares no framework (Jest would need install)."""
        # read_file_or_none, not read_sandbox_file: the LLM-facing wrapper returns its error
        # TEXT on failure AND truncates at MAX_FILE_READ_CHARS. Either one makes json.loads
        # raise, and this silently degrades to node:test — running a jest repo's tests under
        # the wrong runner, the tester's #1 false negative. A big package.json must still parse.
        raw = self.tools.read_file_or_none("package.json")
        if raw is None:
            _log.warning("tester.detect.no_package_json", reason="defaulting to node:test")
            return "node:test"
        try:
            pkg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _log.warning("tester.detect.bad_package_json", reason="unparseable — defaulting to node:test")
            return "node:test"
        test_script = str((pkg.get("scripts") or {}).get("test", "")).lower()
        dep_names = {
            k.lower()
            for k in {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
        }
        # What the test script literally invokes wins over what's merely installed.
        for fw in ("vitest", "mocha", "jest"):
            if fw in test_script:
                return fw
        if "node --test" in test_script or "node:test" in test_script:
            return "node:test"
        for fw in ("vitest", "mocha", "jest"):
            if fw in dep_names:
                return fw
        return "node:test"

    def _detect(self) -> str:
        js = self.tools.run(
            ["find", ".", "-maxdepth", "1", "-name", "package.json"], timeout_s=10)
        if js.get("stdout", "").strip():
            return SupportedProjectTypes.JAVASCRIPT.value
        py = self.tools.run(
            ["find", ".", "-maxdepth", "1",
             "-name", "requirements.txt", "-o", "-name", "setup.py",
             "-o", "-name", "pyproject.toml", "-o", "-name", "setup.cfg"], timeout_s=10)
        if py.get("stdout", "").strip():
            return SupportedProjectTypes.PYTHON.value
        return SupportedProjectTypes.UNKNOWN.value
