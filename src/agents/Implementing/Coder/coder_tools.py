import shlex

from langchain_core.tools import BaseTool, StructuredTool

from agents.tool_output import (
    MAX_FILE_READ_CHARS,
    MAX_TOOL_OUTPUT_CHARS,
    truncate_for_llm,
)
from agents.semantic_search import SEMANTIC_SEARCH_DESCRIPTION, run_semantic_search
from tool_gateway.gateway import ToolGateway
from tool_gateway.permissions import get_permissions
from tool_gateway.Roles import Roles
from sandbox.Tools import Tools

# The repo is always cloned to <workdir>/repo by the bootstrapper.
REPO_ROOT = "/workspace/repo"


def to_sandbox_path(path: str) -> str:
    """Normalize a repo-relative path (how the Planner declares target_files)
    to the absolute sandbox path fs.read_file/fs.write_file require."""
    if path.startswith("/"):
        return path
    return f"{REPO_ROOT}/{path}"


# Like PlannerTools, this can later become an MCP tool implementation exposing a
# safe interface for the Coder to read/write files and run commands in the sandbox.
class CoderTools:
    def __init__(self, gateway: ToolGateway):
        self.gateway = gateway
        self.role = Roles.CODER

    def build_tools(self) -> list[BaseTool]:
        read_tool = StructuredTool.from_function(
            func=self.read_sandbox_file,
            name="read_sandbox_file",
            description=(
                "Read the contents of a file inside the Docker sandbox. "
                "Pass a path relative to the repo root (e.g. 'src/app.py'). "
                "Always read a file before referencing or modifying it."
            ),
        )

        write_tool = StructuredTool.from_function(
            func=self.write_sandbox_file,
            name="write_sandbox_file",
            description=(
                "Write a file inside the Docker sandbox. Pass a path relative to "
                "the repo root and the COMPLETE new file content — this replaces "
                "the whole file, so never pass a fragment or a diff."
            ),
        )

        diff_tool = StructuredTool.from_function(
            func=self.get_repo_diff_for_llm,
            name="get_repo_diff",
            description=(
                "Return the git diff of all changes made so far in the repo "
                "(including newly created files). Use to verify your edits."
            ),
        )

        execute_tool = StructuredTool.from_function(
            func=self.execute_in_sandbox,
            name="execute_in_sandbox",
            description=(
                "Run an allowlisted read-only command inside the repo at "
                f"{REPO_ROOT}. Pass a single argv string with no shell, pipes, or "
                "redirects. Allowed bases: " + ", ".join(get_permissions(Roles.CODER).allowed_commands) + ". "
                "Use for exploration only (grep/ls/find/cat) — do NOT run tests."
            ),
        )

        semantic_search_tool = StructuredTool.from_function(
            func=self.semantic_search,
            name="semantic_search",
            description=SEMANTIC_SEARCH_DESCRIPTION,
        )

        return [read_tool, write_tool, diff_tool, execute_tool, semantic_search_tool]

    def semantic_search(self, query: str, top_k: int = 8) -> str:
        return run_semantic_search(self.gateway, self.role, query, top_k)

    def read_sandbox_file(self, filepath: str) -> str:
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.FS_READ_FILE,
            args={"path": to_sandbox_path(filepath)},
        )
        if "error" in result:
            return result["error"]
        return truncate_for_llm(
            result.get("content", "Empty file or could not read."), MAX_FILE_READ_CHARS
        )

    def write_sandbox_file(self, filepath: str, content: str) -> str:
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.FS_WRITE_FILE,
            args={"path": to_sandbox_path(filepath), "content": content},
        )
        if "error" in result:
            return result["error"]
        return f"Wrote {filepath} ({len(content)} chars)."

    def get_repo_diff(self) -> str:
        # `git add -N .` registers untracked files as intent-to-add so newly
        # created files show up in `git diff` — without it the diff of a
        # create-only microtask is empty and looks like the Coder did nothing.
        self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["git", "add", "-N", "."], "cwd": REPO_ROOT},
        )
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.REPO_DIFF,
            args={},
        )
        if "error" in result:
            return result["error"]
        return result.get("diff", "")

    def get_repo_diff_for_llm(self) -> str:
        """LLM-facing diff wrapper — capped so a large diff doesn't bloat the ReAct loop
        (the model only needs to confirm its edits landed). The runner keeps calling the raw
        `get_repo_diff` for deterministic emptiness/verification."""
        return truncate_for_llm(self.get_repo_diff(), MAX_TOOL_OUTPUT_CHARS)

    def run(self, argv: list[str], timeout_s: int = 60) -> dict:
        """Raw allowlisted sandbox command — returns the gateway result dict
        (exit_code/stdout/stderr). Used by the deterministic compile gate; the
        LLM-facing string wrapper (execute_in_sandbox) is separate."""
        return self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": argv, "cwd": REPO_ROOT, "timeout_s": timeout_s},
        )

    def execute_in_sandbox(self, command: str) -> str:
        cmd_list = shlex.split(command)
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": cmd_list, "cwd": REPO_ROOT},
        )
        if "error" in result:
            return result["error"]
        stdout = truncate_for_llm(result.get("stdout") or "")
        stderr = truncate_for_llm(result.get("stderr") or "")
        return f"Exit Code: {result.get('exit_code')}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
