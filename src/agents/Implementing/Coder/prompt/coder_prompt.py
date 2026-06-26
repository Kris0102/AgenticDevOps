from typing import Dict, List, Optional

from orchestrator.state import Microtask, Objective, Plan, Reflection

COMPILE_AFTER_CHANGES_PROMPT = (
    "After your edits, run the build/compile command for the project (e.g. `python -m compileall .` or `npm run build`) to verify your changes don't break the build. "
    "If the build fails, fix the errors and re-run the build until it succeeds."
    "Do NOT run tests (pytest, python -m unittest, ...) — the Tester agent does that. Do NOT commit, push, or run any git command that mutates history."
)

CODER_SYSTEM_PROMPT = (
    "You are the Coder Agent in an autonomous DevOps pipeline. Your job is to "
    "implement exactly ONE microtask by editing files in the repository at "
    "/workspace/repo inside a sandbox.\n"
    "\n"
    "HARD RULES:\n"
    "1. Edit ONLY the microtask's target files (plus a file they directly import "
    "if strictly unavoidable). Never touch forbidden paths.\n"
    "2. NEVER invent file contents. The real contents of every target and context "
    "file are provided in the task message. If you need any other file, read it "
    "with read_sandbox_file BEFORE referencing or changing it.\n"
    "3. write_sandbox_file replaces the ENTIRE file — always pass the complete "
    "new file content, never a fragment, placeholder, or diff.\n"
    "4. Make the minimal change that satisfies every acceptance criterion. "
    "Follow the conventions visible in the provided context files.\n"
    "5. Do NOT run tests (pytest, python -m unittest, ...) — the Tester agent "
    "does that. Do NOT commit, push, or run any git command that mutates history.\n"
    "6. After your edits, call get_repo_diff and check the diff matches your "
    "intent: only intended files, no accidental deletions.\n"
    "\n"
    "When done, reply with a short summary: the files you changed and what/why "
    "(1-3 sentences). Do not paste full file contents in the summary."
)

_MISSING_FILE_SENTINEL = "<<does not exist yet — you will create it>>"


def _file_block(path: str, content: str) -> str:
    return f"--- FILE: {path} ---\n{content}\n--- END FILE: {path} ---"


def build_coder_prompt(
    objective: Optional[Objective],
    plan: Plan,
    microtask: Microtask,
    file_contents: Dict[str, str],
    reflections: List[Reflection],
    forbidden_paths: Optional[List[str]],
    retry_feedback: Optional[str] = None,
) -> str:
    """Assemble the full grounding context for one coding attempt. Everything the
    Coder must not hallucinate (objective, plan contracts, real file contents,
    reflector feedback) is injected here rather than left to optional tool calls."""
    sections: List[str] = []

    if objective:
        sections.append(
            "OBJECTIVE:\n"
            f"{objective.title}\n{objective.description}\n"
            f"Definition of done: {'; '.join(objective.definition_of_done)}"
        )

    plan_lines = [
        "OVERALL PLAN (for context — implement ONLY the microtask below):",
        f"Summary: {plan.summary}",
        f"Architecture notes: {plan.architecture_notes}",
    ]
    if plan.data_contracts:
        plan_lines.append(
            "Data contracts every microtask must honour:\n  - "
            + "\n  - ".join(plan.data_contracts)
        )
    sections.append("\n".join(plan_lines))

    task_lines = [
        "YOUR MICROTASK:",
        f"Title: {microtask.title}",
        f"Description (WHAT/WHERE): {microtask.description}",
        f"Approach (HOW): {microtask.approach}",
        "Acceptance criteria:\n  - " + "\n  - ".join(c.render() for c in microtask.acceptance_criteria),
        f"Target files (the ONLY files you should edit/create): {microtask.target_files}",
        f"Context files (read-only, for conventions): {microtask.context_files}",
    ]
    if microtask.test_strategy:
        task_lines.append(
            f"Test strategy (how the Tester will verify — do NOT run it yourself): "
            f"{microtask.test_strategy}"
        )
    sections.append("\n".join(task_lines))

    if forbidden_paths:
        sections.append("FORBIDDEN PATHS (never read or write):\n  - " + "\n  - ".join(forbidden_paths))

    if file_contents:
        blocks = [_file_block(path, content) for path, content in file_contents.items()]
        sections.append(
            "CURRENT REAL FILE CONTENTS (ground truth from the sandbox — trust "
            "these over any assumption):\n\n" + "\n\n".join(blocks)
        )

    task_reflections = [
        r for r in reflections if r.microtask_id in (None, microtask.microtask_id)
    ]
    if task_reflections:
        lines = ["FEEDBACK FROM PREVIOUS FAILED ATTEMPTS (address every point):"]
        for r in task_reflections:
            lines.append(f"  - [{r.failure_type}] root cause: {r.root_cause} | fix: {r.fix_instructions}")
        sections.append("\n".join(lines))

    if retry_feedback:
        sections.append(f"RETRY FEEDBACK FROM THE ORCHESTRATOR:\n{retry_feedback}")

    sections.append(
        "Implement the microtask now: write the changed files with "
        "write_sandbox_file, verify with get_repo_diff, then reply with your "
        "short summary."
    )

    return "\n\n".join(sections)
