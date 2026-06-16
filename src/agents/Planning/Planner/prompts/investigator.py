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


def build_investigation_prompt(objective_text: str) -> str:
    return (
        f"Perform a thorough investigation for the following objective:\n\n{objective_text}\n\n"
        "Use your tools to find relevant files, understand the architecture, and summarize "
        "exactly what needs to be changed and where. Return a detailed summary."
    )
