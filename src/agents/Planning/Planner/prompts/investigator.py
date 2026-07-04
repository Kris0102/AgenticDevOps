from tool_gateway.permissions import PlannerPermissions

INVESTIGATOR_SYSTEM_PROMPT = (
    "You are the Lead Solutions Architect for an Agentic DevOps System. "
    f"Use only the provided structured tools {PlannerPermissions.tools} . "
    "The repository lives at /workspace inside the sandbox. "
    "Do not install packages, use network commands, or invoke a shell interpreter. "
    "Commands must be simple argv lists (e.g. ls, find, grep, cat) with no pipes or redirects. "
    "Start with: ls -la /workspace. "
    "If files exist, use find and grep to map structure or if the repository is huge use semantic search, then read_sandbox_file for key files. "
    "End with a concise summary: greenfield vs existing repo, concrete file paths, "
    "and what must change to satisfy the objective."
)


def build_investigation_prompt(objective_text: str, revision_context: str | None = None) -> str:
    base = (
        f"Perform a thorough investigation for the following objective:\n\n{objective_text}\n\n"
        "Use your tools to find relevant files, understand the architecture, and summarize "
        "exactly what needs to be changed and where. Return a detailed summary."
    )
    if not revision_context:
        return base
    # Revision investigation: a prior plan was REJECTED, usually because it MISREAD the repo
    # (wrong data shapes, field names, signatures). Re-ground on the real files and settle
    # each dispute from the source — don't trust the prior plan's assumptions.
    return (
        base
        + "\n\n== THIS IS A RE-INVESTIGATION — A PRIOR PLAN WAS REJECTED ==\n"
        "A reviewer (who read the actual repo) rejected the previous plan. Re-open the exact "
        "files involved and VERIFY THE GROUND TRUTH for every point below — quote the real "
        "data structures, field names, and function signatures as they actually are in the "
        "source, so the next plan cannot repeat the mistake:\n\n"
        f"{revision_context}"
    )
