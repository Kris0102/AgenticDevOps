"""Phase 1 — detect the project type from markers at the repo root.

JavaScript (package.json) takes priority over Python markers. An unsupported project
sets `ctx.skip` so the synthesis/execute phases no-op and the verdict is `skipped`
(the Reflector treats skipped as passed — nothing to fix).
"""
from __future__ import annotations
import enum



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
        _log.info("tester.project_type.detected", run_id=ctx.run_id,
                  project_type=ctx.project_type, microtask_id=ctx.microtask.microtask_id)
        return ctx

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
