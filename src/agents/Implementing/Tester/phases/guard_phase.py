"""Phase 4 — deterministic anti-trivial guard on the synthesized test file.

Keeps the synthesis track honest without an LLM: parse the written file and confirm it
actually asserts something and, for JavaScript tests, only imports packages that are
already declared and installed. A rejected file sets `guard_ok=False`, which VerdictPhase
 treats as "not verified" (a failed verdict) rather than letting a do-nothing or
undeclared-dependency test rubber-stamp the Coder.
"""
from __future__ import annotations

import json

from agents.Implementing.Tester import grounding
from agents.Implementing.Tester.context import TestContext
from agents.Implementing.Tester.phases.base import TestPhase
from observability import Category, get_logger

_log = get_logger(Category.TESTER)


class TrivialGuardPhase(TestPhase):
    name = "trivial_guard"

    def __init__(self, tools):
        self.tools = tools

    def should_run(self, ctx: TestContext) -> bool:
        return bool(ctx.synth_test_file)

    def run(self, ctx: TestContext) -> TestContext:
        dependency_failure = None
        if ctx.project_type == "javascript":
            dependency_failure = self._check_js_dependencies(ctx)

        if dependency_failure:
            ctx.guard_ok = False
            ctx.guard_reason = dependency_failure
            _log.warning("tester.guard.rejected", run_id=ctx.run_id,
                         microtask_id=ctx.microtask.microtask_id, reason=dependency_failure)
            return ctx

        trivial, reason = grounding.is_trivial(ctx.project_type, ctx.synth_test_content)
        ctx.guard_ok = not trivial
        ctx.guard_reason = reason
        if trivial:
            _log.warning("tester.guard.rejected", run_id=ctx.run_id,
                         microtask_id=ctx.microtask.microtask_id, reason=reason)
        else:
            _log.info("tester.guard.passed", run_id=ctx.run_id, detail=reason)
        return ctx

    def _check_js_dependencies(self, ctx: TestContext) -> str:
        """Reject generated JS tests that rely on undeclared or missing packages."""
        specs = grounding.js_dependency_specifiers(ctx.synth_test_content)
        if not specs:
            return ""

        package_json = self.tools.read_sandbox_file("package.json")
        declared = grounding.js_declared_dependency_names(package_json)
        checker = json.dumps(sorted(specs))
        result = self.tools.run([
            "node",
            "-e",
            (
                "const { builtinModules } = require('module');"
                "const specs = JSON.parse(process.argv[1]);"
                "const builtins = [];"
                "const missing = specs.filter((spec) => {"
                "  const normalized = spec.startsWith('node:') ? spec.slice(5) : spec;"
                "  if (builtinModules.includes(normalized)) { builtins.push(spec); return false; }"
                "  try { require.resolve(spec, { paths: [process.cwd()] }); return false; }"
                "  catch { return true; }"
                "});"
                "process.stdout.write(JSON.stringify({ builtins, missing }));"
                "if (missing.length) process.exit(1);"
            ),
            checker,
        ], timeout_s=30)

        payload = {}
        if result.get("stdout"):
            try:
                payload = json.loads(result["stdout"])
            except json.JSONDecodeError:
                payload = {}

        builtin_specs = set(payload.get("builtins") or [])
        dependency_specs = [spec for spec in specs if spec not in builtin_specs]
        missing_declared = sorted(
            spec for spec in dependency_specs
            if grounding.js_package_name(spec) not in declared
        )
        if missing_declared:
            return (
                "undeclared JS dependency import(s): "
                + ", ".join(missing_declared)
                + " — declare and install them before the test can be accepted"
            )

        if int(result.get("exit_code", 1)) != 0:
            missing_list = payload.get("missing") or []
            if missing_list:
                return (
                    "uninstalled JS dependency import(s): "
                    + ", ".join(sorted(missing_list))
                    + " — install them or remove the import before the test can be accepted"
                )
            return "uninstalled JS dependency import(s) — install packages before the test can be accepted"

        return ""
