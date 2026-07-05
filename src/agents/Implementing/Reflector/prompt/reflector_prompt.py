from typing import List, Optional

from orchestrator.state import Microtask

REFLECTOR_SYSTEM_PROMPT = """
You are the Reflector agent in an autonomous DevOps pipeline.
You run after EVERY coder+tester iteration — whether tests passed or failed — and produce a structured assessment that either advances the pipeline or sends it back for improvement.

You have access to read-only tools:
- read_sandbox_file: Read any file in the repo (path relative to repo root, e.g. 'src/app.py').
- get_repo_diff: Get the full git diff of all changes made by the Coder.
- execute_in_sandbox: Run read-only commands (ls, find, grep, cat, git) to explore the repo.

HARD RULES:
1. You are READ-ONLY. Never attempt to write files, modify code, or run tests.
2. Investigate THOROUGHLY before deciding — read the failing/passing test file, the source files, and the diff.
3. Your assessment must be specific: name the file, function/method, line of code.
4. If a re-reflection note was given (previous assessment rejected by human), address every point in that note.

WHEN TESTS FAILED (failure-diagnosis mode):
- Identify the EXACT discrepancy between test expectations and code reality.
- Produce precise fix instructions for the Coder covering: which file, which function, what to change.
- Always set decision = "improve".

WHEN TESTS PASSED (quality-review mode):
- Verify the implementation satisfies ALL acceptance criteria (not just what the tests happened to check).
- Check for obvious code quality issues: wrong edge-case handling, missing guard clauses, brittle tests.
- If everything looks genuinely correct → set decision = "approve".
- If you spot a real gap or problem → set decision = "improve" with specific fix instructions.
- Be reasonable: don't block for trivial style nits. Only improve if it matters for correctness or the acceptance criteria.

INVESTIGATION APPROACH:
1. Call get_repo_diff to see the Coder's changes.
2. Read the test file to understand what is being asserted.
3. Read each target source file to see the actual implementation.
4. Use execute_in_sandbox with grep/find to trace imports or structure as needed.
5. Compare implementation against the acceptance criteria.
6. Conclude: approve (advance) or improve (send back with specific instructions).
"""


def build_investigation_prompt(
    microtask: Microtask,
    test_output: str,
    diff: str,
    test_passed: bool,
    rejection_note: Optional[str] = None,
) -> str:
    sections: List[str] = []

    if rejection_note:
        sections.append(
            "RE-REFLECTION REQUESTED\n"
            "The previous assessment was rejected by the human reviewer. "
            "Address every point in the note below:\n"
            f"{rejection_note}"
        )

    mode = "QUALITY REVIEW" if test_passed else "FAILURE DIAGNOSIS"
    sections.append(
        f"MODE: {mode} (tests {'passed' if test_passed else 'FAILED'})\n\n"
        "MICROTASK:\n"
        f"Title:        {microtask.title}\n"
        f"Description:  {microtask.description}\n"
        f"Target files: {', '.join(microtask.target_files) or '(none declared)'}\n"
        f"Context files: {', '.join(microtask.context_files) or '(none)'}\n"
        "Acceptance criteria (the ground truth — not just what tests happen to check):\n"
        + "\n".join(f"  - {ac.render()}" for ac in microtask.acceptance_criteria)
    )

    if diff:
        trimmed = diff[:4000] + (" [truncated — use get_repo_diff for full diff]" if len(diff) > 4000 else "")
        sections.append(f"CODE CHANGES (git diff):\n{trimmed}")

    if test_output:
        trimmed = test_output[:4000] + (" [truncated]" if len(test_output) > 4000 else "")
        sections.append(f"TEST OUTPUT:\n{trimmed}")

    if test_passed:
        sections.append(
            "YOUR TASK (quality-review mode — tests PASSED):\n"
            "1. Call get_repo_diff to see exactly what the Coder changed.\n"
            "2. Read each target source file to verify the implementation.\n"
            "3. Read the test file to confirm it actually covers all acceptance criteria.\n"
            "4. Cross-check: does the implementation satisfy EVERY acceptance criterion above?\n"
            "5. Are there missing edge cases, incorrect logic, or tests that are too lenient?\n"
            "6. Write your ASSESSMENT SUMMARY clearly: what looks good, what (if anything) needs fixing.\n\n"
            "If everything is correct → your summary should conclude 'APPROVE'.\n"
            "If there is a real problem → your summary should describe it specifically and conclude 'IMPROVE'."
        )
    else:
        sections.append(
            "YOUR TASK (failure-diagnosis mode — tests FAILED):\n"
            "1. Call get_repo_diff to see all Coder changes.\n"
            "2. Read the test file to understand what is being asserted.\n"
            "3. Read each target source file to see the actual implementation.\n"
            "4. Use execute_in_sandbox('grep -r ...') or execute_in_sandbox('find . -name ...') as needed.\n"
            "5. Identify the EXACT mismatch between test expectations and code reality.\n"
            "6. Write your ASSESSMENT SUMMARY: root cause + specific fix instructions for the Coder.\n\n"
            "Always conclude with 'IMPROVE' (tests failed = something must be fixed)."
        )

    return "\n\n".join(sections)


def build_diagnosis_prompt(
    microtask: Microtask,
    test_output: str,
    diff: str,
    investigation_summary: str,
    test_passed: bool,
    rejection_note: Optional[str] = None,
    synth_test_source: str = "",
) -> str:
    sections: List[str] = []

    if rejection_note:
        sections.append(
            "RE-REFLECTION NOTE (human rejected previous assessment — incorporate this):\n"
            f"{rejection_note}"
        )

    sections.append(
        f"MODE: {'QUALITY REVIEW' if test_passed else 'FAILURE DIAGNOSIS'} "
        f"(tests {'passed' if test_passed else 'FAILED'})\n\n"
        "MICROTASK:\n"
        f"Title:        {microtask.title}\n"
        f"Description:  {microtask.description}\n"
        f"Target files: {', '.join(microtask.target_files) or '(none declared)'}"
    )

    # The spec the code+test must satisfy — the GROUND TRUTH for the code-vs-test call.
    # `render()` inlines any concrete IOExamples (given → expect); an assertion whose
    # expected value contradicts these examples (or was invented where the criteria give
    # none) points at a wrong TEST, not wrong code.
    if microtask.acceptance_criteria:
        criteria = "\n".join(f"  - {ac.render()}" for ac in microtask.acceptance_criteria)
        sections.append(
            "ACCEPTANCE CRITERIA (ground truth — the code is correct iff it satisfies THESE, "
            "not whatever a test happens to assert):\n" + criteria
        )

    if diff:
        trimmed = diff[:2000] + (" [truncated]" if len(diff) > 2000 else "")
        sections.append(f"CODE CHANGES:\n{trimmed}")

    if test_output:
        trimmed = test_output[:2000] + (" [truncated]" if len(test_output) > 2000 else "")
        sections.append(f"TEST OUTPUT:\n{trimmed}")

    # The synthesized test itself — so the reflector can judge the failing ASSERTION, not
    # just the failure output. Absent for a Track-A-only failure (repo's own suite).
    if synth_test_source:
        trimmed = synth_test_source[:2500] + (" [truncated]" if len(synth_test_source) > 2500 else "")
        sections.append(
            "SYNTHESIZED TEST UNDER SUSPICION (system-generated; may itself be wrong):\n" + trimmed
        )

    sections.append(f"INVESTIGATION / ASSESSMENT FINDINGS:\n{investigation_summary}")

    if test_passed:
        sections.append(
            "Based on the above, produce a structured quality assessment.\n\n"
            "Respond ONLY with valid JSON (no markdown fences, no extra keys):\n"
            "{\n"
            '  "decision": "approve" or "improve",\n'
            '  "failure_type": "<only needed when decision=improve: assertion_error | missing_implementation | logic_error | type_error | quality_issue | other; use empty string on approve>",\n'
            '  "root_cause": "<what is wrong — empty string when approving>",\n'
            '  "fix_instructions": "<step-by-step for Coder and/or Tester — empty string when approving>",\n'
            '  "quality_notes": "<brief summary of what looks good — always fill this>",\n'
            '  "should_store_in_semantic_memory": <true if this is a reusable anti-pattern, false otherwise>\n'
            "}"
        )
    else:
        sections.append(
            "Based on the above, produce a structured failure diagnosis.\n\n"
            "First decide WHO is at fault — compare the failing assertion against the ACCEPTANCE\n"
            "CRITERIA above (the ground truth), NOT against what the test happens to expect:\n"
            "  • fix_target = \"code\" (default) — the IMPLEMENTATION violates the acceptance\n"
            "    criteria. The test's expectation is consistent with the criteria; the code is wrong.\n"
            "  • fix_target = \"test\" — the code satisfies the criteria but the SYNTHESIZED TEST is\n"
            "    wrong. Strong signals the TEST is at fault:\n"
            "      - it asserts a concrete value/number/message that CONTRADICTS a criterion's\n"
            "        given→expect example, OR\n"
            "      - it asserts a specific value the Tester computed ITSELF for an input the\n"
            "        criteria give NO example for (e.g. a hand-computed distance / rounded number /\n"
            "        exact error string), and the code's behaviour actually matches the spec, OR\n"
            "      - broken assertion / bad setup / wrong import in the test.\n"
            "    A self-computed expected value the Planner never supplied is the classic wrong-test\n"
            "    signal — do not send the Coder to break spec-correct code to satisfy it.\n"
            "  Prefer \"code\" when genuinely unsure; the test-resynthesis budget is small.\n\n"
            "Respond ONLY with valid JSON (no markdown fences, no extra keys):\n"
            "{\n"
            '  "decision": "improve",\n'
            '  "fix_target": "code" or "test",\n'
            '  "failure_type": "<import_error | assertion_error | missing_implementation | logic_error | type_error | other>",\n'
            '  "root_cause": "<precise technical explanation — name file, function, line>",\n'
            '  "fix_instructions": "<for fix_target=code: what the Coder must change; for fix_target=test: what the test asserted wrongly and the correct expectation>",\n'
            '  "quality_notes": "",\n'
            '  "should_store_in_semantic_memory": <true if reusable anti-pattern>\n'
            "}"
        )

    return "\n\n".join(sections)
