import shlex

from langchain_core.tools import BaseTool, StructuredTool

from agents.tool_output import (
    MAX_FILE_READ_CHARS, 
    truncate_for_llm 
)
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
# safe interface for the Tester to read/write files and run commands in the sandbox.
class TesterTools:
    def __init__(self, gateway: ToolGateway):
        self.gateway = gateway
        self.role = Roles.TESTER

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

        # NOTE: get_repo_diff is intentionally NOT exposed to the test-writer LLM — it
        # doesn't need the diff to write tests, and exposing it invites a verify-loop that
        # burns the tool-call budget (the same trap the Coder hit). VerdictPhase still calls
        # the get_repo_diff() method directly.
        execute_tool = StructuredTool.from_function(
            func=self.execute_in_sandbox,
            name="execute_in_sandbox",
            description=(
                "Run an allowlisted command inside the repo at "
                f"{REPO_ROOT}. Pass a single argv string with no shell, pipes, or "
                "redirects. Allowed bases: " + ", ".join(get_permissions(Roles.TESTER).allowed_commands) + ". "
                "Use this for sandboxed checks, including tests."
            ),
        )

        write_tool = StructuredTool.from_function(
            func=self.write_sandbox_file,
            name="write_sandbox_file",
            description=(
                "Write content to a file inside the Docker sandbox. "
                "Pass a path relative to the repo root (e.g. 'tests/test_foo.py'). "
                "Use this to write the pytest test file."
            ),
        )

        return [read_tool, write_tool, execute_tool]

    def read_sandbox_file(self, filepath: str) -> str:
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.FS_READ_FILE,
            args={"path": to_sandbox_path(filepath)},
        )
        if "error" in result:
            return result["error"]
        return truncate_for_llm(result.get("content", "Empty file or could not read."),
                                 limit=MAX_FILE_READ_CHARS)

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

    def run(self, argv: list[str], timeout_s: int = 300) -> dict:
        """Raw allowlisted sandbox command — returns the gateway result dict
        (exit_code/stdout/stderr/duration_ms). Used by the deterministic test phases
        (detect / install / run-suite); LLM-facing string wrappers are separate."""
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
