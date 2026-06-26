"""Typed check records — the single currency of the Tester pipeline.

A `CheckResult` is what every check (suite/build/typecheck/lint/synthesized tests) produces.
Replaces the old `Dict[str, Any]` so attributes are known and the verdict logic reads
against a real type. `make_check` turns a raw sandbox-run result into one of these.

`CheckStatus` separates the verdict-deciding outcomes (PASSED/FAILED) from the ones that
must NOT decide a microtask's fate:
- SKIPPED — legitimately nothing to verify (no suite, unsupported project).
- ERRORED — the check ran but hit a harness/infra problem (collection error, timeout,
  gateway error) — NOT a statement about the code. Treating ERRORED as FAILED is the
  misattribution bug (a flaky synthesized-test import wrongly blaming the Coder).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Set


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"      # ran, real negative verdict (assertion failed, build broke)
    SKIPPED = "skipped"    # nothing to verify — not a failure
    ERRORED = "errored"    # ran but harness/infra error — does NOT decide the verdict

    def __str__(self) -> str:
        return self.value


@dataclass
class CheckResult:
    label: str                 # evidence slot: unit_tests/integration_tests/build/typecheck/lint
    command: List[str]
    status: CheckStatus
    exit_code: int
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    artifact_text: str = ""
    artifact_ref: str = ""     # filled by TesterRunner when the DB artifact is persisted

    @property
    def decides_verdict(self) -> bool:
        """Only PASSED/FAILED count toward a microtask's pass/fail. SKIPPED/ERRORED don't."""
        return self.status in (CheckStatus.PASSED, CheckStatus.FAILED)


def make_check(
    label: str,
    command: List[str],
    result: Dict[str, Any],
    *,
    error_exit_codes: Set[int] = set()
) -> CheckResult:
    """Normalize a raw gateway sandbox-run result into a typed CheckResult.

    exit 0 → PASSED. A gateway `error` key (denied/timeout/exception) or an exit code in
    `error_exit_codes` (e.g. pytest 2/3/4 = collection/usage error) → ERRORED (harness,
    not the code). Any other non-zero exit → FAILED (a real negative verdict).
    """
    exit_code = int(result.get("exit_code", 1))
    if "error" in result:
        status = CheckStatus.ERRORED
    elif exit_code == 0:
        status = CheckStatus.PASSED
    elif exit_code in error_exit_codes:
        status = CheckStatus.ERRORED
    else:
        status = CheckStatus.FAILED

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    parts = [f"exit_code: {exit_code}"]
    if stdout:
        parts.append(f"STDOUT:\n{stdout}")
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    if "error" in result:
        parts.append(f"GATEWAY ERROR: {result['error']}")
    return CheckResult(
        label=label,
        command=command,
        status=status,
        exit_code=exit_code,
        duration_ms=int(result.get("duration_ms", 0)),
        stdout=stdout,
        stderr=stderr,
        artifact_text="\n\n".join(parts),
    )


def skipped_check(label: str, reason: str) -> CheckResult:
    return CheckResult(
        label=label,
        command=[],
        status=CheckStatus.SKIPPED,
        exit_code=0,
        stderr=reason,
        artifact_text=f"skipped: {reason}",
    )
