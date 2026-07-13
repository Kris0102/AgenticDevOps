from typing import List, Optional

from orchestrator.state import Microtask, Objective, Plan

TESTER_SYSTEM_PROMPT_PYTHON = """
You are the Test Writer agent in an autonomous DevOps pipeline.
Your job is to write pytest unit tests for a completed microtask, verifying each acceptance criterion.

You have access to the following tools:
- read_sandbox_file: Read any file in the repo (path relative to repo root, e.g. 'src/app.py').
- write_sandbox_file: Write a file into the repo (path relative to repo root).
- execute_in_sandbox: Run allowlisted commands (ls, find, grep, mkdir, etc.) to explore the repo.

HARD RULES:
1. Write tests to EXACTLY the path specified in the prompt — do not choose a different path.
2. Each acceptance criterion must have at least one corresponding test function.
3. Read the target source files before writing tests so you can import them correctly.
4. Use standard pytest conventions: test functions start with `test_`, use `assert` statements.
5. Do NOT modify any source files — your only write operation is the test file.
6. If the tests directory does not exist, create it first: execute_in_sandbox('mkdir -p tests').
7. After writing the test file, stop — do not execute the tests yourself.

CRITICAL — PYTHON IMPORTS ONLY, NO NODE/JS:
- Every import in the test file MUST be a valid Python import. There is no `import js`, `import node`,
  or any other JavaScript/non-Python construct — Python does not support those.
- Before importing a module, verify it exists: if it is a local module, confirm the .py file is present
  using execute_in_sandbox('find . -name "*.py"'). If it is a third-party package, verify it is listed
  in requirements.txt or requirements-dev.txt.
- If a target file is NOT a Python file (e.g. .js, .ts, .json, .yaml), do NOT attempt to run it via
  subprocess, node, or any external runtime. The sandbox has no JavaScript runtime.
  Write a placeholder test with `pytest.mark.skip` instead:

  import pytest

  @pytest.mark.skip(reason="Target is a non-Python file — not testable via pytest")
  def test_acceptance_criterion_placeholder():
      pass

- Never call subprocess with 'node', 'npm', 'npx', or any JavaScript tooling.
"""

TESTER_SYSTEM_PROMPT_JS = """
You are the Test Writer agent in an autonomous DevOps pipeline.
Your job is to write JavaScript unit tests for a completed microtask, verifying each
acceptance criterion, USING THE PROJECT'S OWN TEST FRAMEWORK (named in the task prompt below).

You have access to the following tools:
- read_sandbox_file: Read any file in the repo (path relative to repo root, e.g. 'src/app.js').
- write_sandbox_file: Write a file into the repo (path relative to repo root).
- execute_in_sandbox: Run allowlisted commands (ls, find, grep, mkdir, node, npm, etc.) to explore the repo.

HARD RULES:
1. Write tests to EXACTLY the path specified in the prompt — do not choose a different path.
2. Each acceptance criterion must have at least one corresponding test function.
3. Read the target source files before writing tests so you can require/import them correctly.
4. Use the EXACT test framework and assertion style named in the FRAMEWORK section of the
   task prompt — do NOT default to Jest. Writing Jest (`expect()`) for a `node:test` repo,
   or vice-versa, makes the test file fail to run even when the code is correct.
5. Do NOT modify any source files — your only write operation is the test file.
6. If the tests directory does not exist, create it first: execute_in_sandbox('mkdir -p tests').
7. After writing the test file, stop — do not execute the tests yourself.

CRITICAL — MODULE IMPORTS:
- Check package.json for "type": "module". If present, use ESM syntax (import/export). Otherwise use CommonJS (require/module.exports).
- For CommonJS: const { myFunc } = require('../src/myFile');
- For ESM:      import { myFunc } from '../src/myFile.js';
- Before requiring a module, verify the file exists using execute_in_sandbox('find . -name "*.js"').
- Do NOT attempt to import Python files (.py) or non-JS files.
- Never use pytest, unittest, or any Python testing library — this is a JavaScript project.

IF YOU ADD DEPENDENCIES:
- Prefer built-in Node/Jest APIs and the project's existing dependencies first.
- If the test truly needs a new npm package, install it immediately with execute_in_sandbox('npm install <package>'). Do not modify package.json manually.
- Before writing the test, verify the package is now listed in package.json and present in node_modules.
- If the package cannot be installed or verified, do not use it in the test.
- Do NOT install Python packages or use pip — this is a JavaScript project.
- Do NOT call subprocess with 'python', 'pip', or any Python tooling.
"""


def build_test_writing_prompt_python(
    objective: Optional[Objective],
    plan: Optional[Plan],
    microtask: Microtask,
    test_file: str,
    rejection_note: Optional[str] = None,
    reflections: Optional[List] = None,
) -> str:
    sections: List[str] = []

    if rejection_note:
        sections.append(
            f"HUMAN REVIEWER FEEDBACK (previous test attempt was rejected — address every point below):\n"
            f"{rejection_note}"
        )

    task_reflections = [
        r for r in (reflections or [])
        if getattr(r, "microtask_id", None) in (None, microtask.microtask_id)
    ]
    if task_reflections:
        lines = [f"PRIOR REFLECTOR FEEDBACK ({len(task_reflections)} round(s) — address any remaining issues):"]
        for i, r in enumerate(task_reflections, 1):
            lines.append(f"[{i}] Type: {r.failure_type}")
            lines.append(f"    Root Cause: {r.root_cause}")
            lines.append(f"    Fix Instructions: {r.fix_instructions}")
        sections.append("\n".join(lines))

    if objective:
        sections.append(
            f"OBJECTIVE:\n{objective.title}\n{objective.description}"
        )

    if plan:
        sections.append(
            f"PLAN SUMMARY (for context):\n{plan.summary}"
        )

    microtask_lines = [
        "MICROTASK TO TEST:",
        f"Title: {microtask.title}",
        f"Description: {microtask.description}",
        "Acceptance criteria (each must be covered by at least one test):",
    ]
    for criterion in microtask.acceptance_criteria:
        microtask_lines.append(f"  - {criterion.render()}")
    microtask_lines.append(
        "ASSERTION RULES (critical — wrong expected values fail correct code and waste the "
        "whole loop; these are HARD rules, not suggestions):\n"
        "  • If a criterion lists concrete examples (given → expect), assert EXACTLY those "
        "input→output pairs, copying the expected values verbatim. Never recompute, round, "
        "or 'improve' them.\n"
        "  • For any behaviour the criteria give NO concrete example for, assert STRUCTURE "
        "ONLY — that an error is thrown (its type/that it throws), the returned shape/keys, or "
        "a value's type. You MUST NOT assert a specific value, number, distance, count, or exact "
        "message string that you worked out yourself.\n"
        "  • NEVER invent a boundary input and compute its expected output yourself (e.g. do not "
        "hand-compute a Levenshtein distance to pick a 'distance-3' input, then assert the exact "
        "resulting message). If the Planner gave no example for a boundary/edge case, either skip "
        "the exact value and assert only that it errors / its shape, or omit that case — a "
        "self-computed expectation that is off by one rejects correct code."
    )
    microtask_lines.append(
        f"Target source files (read these to understand what to import and test): "
        f"{', '.join(microtask.target_files)}"
    )
    sections.append("\n".join(microtask_lines))

    sections.append(
        f"YOUR TASK:\n"
        f"Write pytest unit tests to `{test_file}` that verify every acceptance criterion above.\n\n"
        f"Steps:\n"
        f"1. Run execute_in_sandbox('find . -name \"*.py\" | head -40') to discover all Python files.\n"
        f"2. Run execute_in_sandbox('mkdir -p tests') to ensure the tests directory exists.\n"
        f"3. Read each target source file with read_sandbox_file to understand the Python API.\n"
        f"4. Determine the correct Python import path for each target file:\n"
        f"   - A file at `src/foo/bar.py` is imported as `from src.foo.bar import ...` or `from foo.bar import ...`.\n"
        f"   - NEVER import a .js, .ts, .json or any non-.py file — use subprocess for those.\n"
        f"5. Write the complete test file to `{test_file}` using write_sandbox_file.\n"
        f"   - Do NOT write to any other path.\n"
        f"   - Include one test function per acceptance criterion at minimum.\n"
        f"   - Every `import` statement must refer to a Python module you have confirmed exists."
        + ("\n6. IMPORTANT — this is a rewrite: the previous tests were rejected. "
           "Read the HUMAN REVIEWER FEEDBACK above and address every point." if rejection_note else "")
    )

    return "\n\n".join(sections)


# Per-framework marching orders — the tester used to hardcode Jest, which fails outright on a
# node:test / mocha repo (no `expect()`), so correct code got rejected. DetectProjectPhase
# picks the repo's framework; this block tells the writer its exact import + assertion style.
_JS_FRAMEWORK_GUIDE = {
    "node:test": (
        "FRAMEWORK: node:test — Node's BUILT-IN test runner — with node:assert. This repo does "
        "NOT use Jest; `expect()` does not exist here and will throw.\n"
        "  • Import runner + assert as node builtins:\n"
        "    ESM:  import { test, describe } from 'node:test';\n"
        "          import assert from 'node:assert/strict';\n"
        "    CJS:  const { test, describe } = require('node:test');\n"
        "          const assert = require('node:assert/strict');\n"
        "  • Assert with node:assert: assert.equal(actual, expected), assert.deepEqual(...), "
        "assert.throws(fn). NEVER use expect()/.toBe()/.toEqual().\n"
        "  • The runner is `node --test <file>` — no dependency install needed."
    ),
    "jest": (
        "FRAMEWORK: Jest. Use describe()/test()/it() and expect(...).toBe()/.toEqual()/.toThrow(). "
        "Jest injects these globals — do NOT import test/describe/expect."
    ),
    "vitest": (
        "FRAMEWORK: Vitest. Import the API explicitly:\n"
        "    import { describe, test, expect } from 'vitest';\n"
        "  • Assert with expect(...).toBe()/.toEqual()/.toThrow()."
    ),
    "mocha": (
        "FRAMEWORK: Mocha with node:assert. Use describe()/it() (Mocha globals — do not import them):\n"
        "    import assert from 'node:assert/strict';   // or const assert = require('node:assert/strict');\n"
        "  • Assert with node:assert (assert.equal/deepEqual/throws). Do NOT use expect()."
    ),
}


def _js_framework_guide(framework: Optional[str]) -> str:
    return _JS_FRAMEWORK_GUIDE.get(framework or "node:test", _JS_FRAMEWORK_GUIDE["node:test"])


def build_test_writing_prompt_js(
    objective: Optional[Objective],
    plan: Optional[Plan],
    microtask: Microtask,
    test_file: str,
    rejection_note: Optional[str] = None,
    reflections: Optional[List] = None,
    framework: Optional[str] = None,
) -> str:
    sections: List[str] = [_js_framework_guide(framework)]

    if rejection_note:
        sections.append(
            f"HUMAN REVIEWER FEEDBACK (previous test attempt was rejected — address every point below):\n"
            f"{rejection_note}"
        )

    task_reflections = [
        r for r in (reflections or [])
        if getattr(r, "microtask_id", None) in (None, microtask.microtask_id)
    ]
    if task_reflections:
        lines = [f"PRIOR REFLECTOR FEEDBACK ({len(task_reflections)} round(s) — address any remaining issues):"]
        for i, r in enumerate(task_reflections, 1):
            lines.append(f"[{i}] Type: {r.failure_type}")
            lines.append(f"    Root Cause: {r.root_cause}")
            lines.append(f"    Fix Instructions: {r.fix_instructions}")
        sections.append("\n".join(lines))

    if objective:
        sections.append(
            f"OBJECTIVE:\n{objective.title}\n{objective.description}"
        )

    if plan:
        sections.append(
            f"PLAN SUMMARY (for context):\n{plan.summary}"
        )

    microtask_lines = [
        "MICROTASK TO TEST:",
        f"Title: {microtask.title}",
        f"Description: {microtask.description}",
        "Acceptance criteria (each must be covered by at least one test):",
    ]
    for criterion in microtask.acceptance_criteria:
        microtask_lines.append(f"  - {criterion.render()}")
    microtask_lines.append(
        "ASSERTION RULES (critical — wrong expected values fail correct code and waste the "
        "whole loop; these are HARD rules, not suggestions):\n"
        "  • If a criterion lists concrete examples (given → expect), assert EXACTLY those "
        "input→output pairs, copying the expected values verbatim. Never recompute, round, "
        "or 'improve' them.\n"
        "  • For any behaviour the criteria give NO concrete example for, assert STRUCTURE "
        "ONLY — that an error is thrown (its type/that it throws), the returned shape/keys, or "
        "a value's type. You MUST NOT assert a specific value, number, distance, count, or exact "
        "message string that you worked out yourself.\n"
        "  • NEVER invent a boundary input and compute its expected output yourself (e.g. do not "
        "hand-compute a Levenshtein distance to pick a 'distance-3' input, then assert the exact "
        "resulting message). If the Planner gave no example for a boundary/edge case, either skip "
        "the exact value and assert only that it errors / its shape, or omit that case — a "
        "self-computed expectation that is off by one rejects correct code."
    )
    microtask_lines.append(
        f"Target source files (read these to understand what to require/import and test): "
        f"{', '.join(microtask.target_files)}"
    )
    sections.append("\n".join(microtask_lines))

    sections.append(
        f"YOUR TASK:\n"
        f"Write Jest unit tests to `{test_file}` that verify every acceptance criterion above.\n\n"
        f"Steps:\n"
        f"1. Run execute_in_sandbox('find . -name \"*.js\" | head -40') to discover all JS files.\n"
        f"2. Run execute_in_sandbox('cat package.json') to check whether \"type\": \"module\" is set "
        f"(determines ESM vs CommonJS).\n"
        f"3. Run execute_in_sandbox('mkdir -p tests') to ensure the tests directory exists.\n"
        f"4. Read each target source file with read_sandbox_file to understand the module's exports.\n"
        f"5. Determine the correct require/import path for each target file:\n"
        f"   - CommonJS: const {{ myFunc }} = require('../src/myFile');\n"
        f"   - ESM:      import {{ myFunc }} from '../src/myFile.js';\n"
        f"   - Paths must be relative from `{test_file}` to the target file.\n"
        f"6. Write the complete test file to `{test_file}` using write_sandbox_file.\n"
        f"   - Do NOT write to any other path.\n"
        f"   - Include one test() or it() call per acceptance criterion at minimum.\n"
        f"   - Use describe() blocks to group related tests.\n"
        f"   - Every require/import must refer to a JS module you have confirmed exists.\n"
        f"   - If a test needs a dependency that is not already declared, install it first with npm install and verify it appears in package.json before writing the test."
        + ("\n7. IMPORTANT — this is a rewrite: the previous tests were rejected. "
           "Read the HUMAN REVIEWER FEEDBACK above and address every point." if rejection_note else "")
    )

    return "\n\n".join(sections)
