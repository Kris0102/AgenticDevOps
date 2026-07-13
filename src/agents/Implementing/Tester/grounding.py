"""Deterministic anti-trivial-test guard (ADR 0015/0016).

The Tester synthesizes tests with an LLM, so a generated file could be trivially passing
(no assertions, only `@skip`, empty bodies) and rubber-stamp the Coder's work. These are
PURE functions — no LLM, no I/O — that count real assertions so the guard verdict stays
deterministic even on the synthesis track. Python uses the `ast`; JavaScript uses regexes
(no JS parser in-process), which is good enough to catch the trivial cases.
"""
from __future__ import annotations

import ast
import json
import re
from typing import List, Set, Tuple

# Names that count as a real assertion when called (pytest/unittest idioms).
_PY_ASSERT_CALL_PREFIXES = ("assert",)  # assertEqual, assertTrue, assertRaises, ...
_PY_ASSERT_CALL_NAMES = {"raises", "warns", "approx"}  # pytest.raises(...), pytest.approx(...)


def count_python_assertions(source: str) -> int:
    """Count `assert` statements + assertion-style calls (self.assertX, pytest.raises…)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            count += 1
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name.startswith(_PY_ASSERT_CALL_PREFIXES) or name in _PY_ASSERT_CALL_NAMES:
                count += 1
    return count


def python_all_skipped(source: str) -> bool:
    """True if every test function is decorated with a skip marker (so nothing runs)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    test_fns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name.startswith("test")
    ]
    if not test_fns:
        return False
    return all(_has_skip_decorator(fn) for fn in test_fns)


def _has_skip_decorator(fn: ast.FunctionDef) -> bool:
    for dec in fn.decorator_list:
        if "skip" in ast.dump(dec).lower():
            return True
    return False


# Count assertion call sites: bare `assert(...)` / `expect(...)`, AND the dotted node:assert
# idiom `assert.equal(...)` / `assert.deepEqual(...)` / `assert.strict.equal(...)`. The old
# regex required `(` immediately after `assert`, so it scored every node:test file (which uses
# `assert.equal`, not `expect`) as ZERO assertions → trivial-guard rejected correct tests once
# synthesis was made framework-aware (run 7a64fb2b). Optional `.method` chain closes that gap.
_JS_ASSERT_RE = re.compile(r"\b(?:expect|assert)\s*(?:\.\s*\w+)*\s*\(")

_JS_IMPORT_FROM_RE = re.compile(r"(?m)^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]")
_JS_IMPORT_SIDE_EFFECT_RE = re.compile(r"(?m)^\s*import\s+['\"]([^'\"]+)['\"]")
_JS_EXPORT_FROM_RE = re.compile(r"(?m)^\s*export\s+.*?\s+from\s+['\"]([^'\"]+)['\"]")
_JS_REQUIRE_RE = re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)")
_JS_DYNAMIC_IMPORT_RE = re.compile(r"import\(\s*['\"]([^'\"]+)['\"]\s*\)")


def count_js_assertions(source: str) -> int:
    return len(_JS_ASSERT_RE.findall(source))


def js_import_specifiers(source: str) -> List[str]:
    """Return all static/dynamic JS module specifiers referenced in source."""
    specifiers: Set[str] = set()
    for regex in (
        _JS_IMPORT_FROM_RE,
        _JS_IMPORT_SIDE_EFFECT_RE,
        _JS_EXPORT_FROM_RE,
        _JS_REQUIRE_RE,
        _JS_DYNAMIC_IMPORT_RE,
    ):
        specifiers.update(match.group(1) for match in regex.finditer(source))
    return sorted(specifiers)


def js_package_name(specifier: str) -> str:
    """Map a JS import specifier to its package root name."""
    if specifier.startswith("@"):
        parts = specifier.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else specifier
    return specifier.split("/", 1)[0]


def js_dependency_specifiers(source: str) -> List[str]:
    """Return bare JS imports that should be checked against package.json/node_modules.

    Relative imports and Node builtins are excluded by the caller.
    """
    return [
        spec for spec in js_import_specifiers(source)
        if not spec.startswith((".", "/", "node:"))
    ]


def js_declared_dependency_names(package_json_text: str) -> Set[str]:
    """Return package roots declared in package.json dependency sections."""
    try:
        data = json.loads(package_json_text)
    except json.JSONDecodeError:
        return set()

    declared: Set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key) or {}
        if isinstance(section, dict):
            declared.update(section.keys())
    return declared


def is_trivial(project_type: str, source: str) -> Tuple[bool, str]:
    """Return (is_trivial, reason). A test file is trivial when it can pass without
    actually verifying anything — zero assertions, or all tests skipped."""
    if not source.strip():
        return True, "empty test file"
    if project_type == "python":
        if python_all_skipped(source):
            return True, "every test is @skip-decorated — nothing executes"
        n = count_python_assertions(source)
        if n == 0:
            return True, "no assertions found (no `assert` and no assertX/pytest.raises calls)"
        return False, f"{n} assertion(s)"
    if project_type == "javascript":
        n = count_js_assertions(source)
        if n == 0:
            return True, "no expect()/assert() calls found"
        return False, f"{n} assertion(s)"
    return False, "guard not applicable for project type"
