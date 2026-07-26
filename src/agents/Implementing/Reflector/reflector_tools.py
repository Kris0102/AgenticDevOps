import shlex

from langchain_core.tools import BaseTool, StructuredTool

from agents.semantic_search import SEMANTIC_SEARCH_DESCRIPTION, run_semantic_search
from agents.tool_output import MAX_FILE_READ_CHARS, truncate_for_llm
from tool_gateway.gateway import ToolGateway
from tool_gateway.permissions import get_permissions
from tool_gateway.Roles import Roles
from sandbox.Tools import Tools

# Default only. The REAL path comes from the run's SandboxSpec via
# sandbox.factory.repo_root(state) and is injected by the node — a pre-provisioned image keeps
# the repo somewhere else entirely (e.g. /testbed), and hardcoding this made every command run
# in a nonexistent directory (exit 127, ADR 0039).
DEFAULT_REPO_ROOT = "/workspace/repo"


def to_sandbox_path(path: str, repo_root: str = DEFAULT_REPO_ROOT) -> str:
    """Normalize a repo-relative path to the absolute sandbox path."""
    if path.startswith("/"):
        return path
    return f"{repo_root}/{path}"


class ReflectorTools:
    """Read-only tool set for the Reflector. No writes, no test execution —
    the reflector investigates failure causes but never modifies anything."""

    def __init__(self, gateway: ToolGateway, repo_root: str = DEFAULT_REPO_ROOT):
        self.gateway = gateway
        self.role = Roles.REFLECTOR
        self.repo_root = repo_root

    def build_tools(self) -> list[BaseTool]:
        read_tool = StructuredTool.from_function(
            func=self.read_sandbox_file,
            name="read_sandbox_file",
            description=(
                "Read the contents of a file inside the Docker sandbox. "
                "Pass a path relative to the repo root (e.g. 'src/app.py'). "
                "Use this to read source files and test files before diagnosing failures."
            ),
        )

        diff_tool = StructuredTool.from_function(
            func=self.get_repo_diff,
            name="get_repo_diff",
            description=(
                "Return the git diff of all changes made by the Coder so far in the repo. "
                "Use to understand exactly what was changed and correlate with test failures."
            ),
        )

        execute_tool = StructuredTool.from_function(
            func=self.execute_in_sandbox,
            name="execute_in_sandbox",
            description=(
                "Run a read-only allowlisted command inside the repo at "
                f"{self.repo_root}. Pass a single argv string with no shell, pipes, or redirects. "
                "Allowed bases: " + ", ".join(get_permissions(Roles.REFLECTOR).allowed_commands) + ". "
                "Use for exploring repo structure (ls, find, grep, cat)."
            ),
        )

        semantic_search_tool = StructuredTool.from_function(
            func=self.semantic_search,
            name="semantic_search",
            description=SEMANTIC_SEARCH_DESCRIPTION,
        )

        return [read_tool, diff_tool, execute_tool, semantic_search_tool]

    def semantic_search(self, query: str, top_k: int = 8) -> str:
        return run_semantic_search(self.gateway, self.role, query, top_k)

    def read_sandbox_file(self, filepath: str) -> str:
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.FS_READ_FILE,
            args={"path": to_sandbox_path(filepath, self.repo_root)},
        )
        if "error" in result:
            return result["error"]
        return truncate_for_llm(
            result.get("content", "Empty file or could not read."), MAX_FILE_READ_CHARS
        )

    def get_repo_diff(self) -> str:
        self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": ["git", "add", "-N", "."], "cwd": self.repo_root},
        )
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.REPO_DIFF,
            args={},
        )
        if "error" in result:
            return result["error"]
        return truncate_for_llm(result.get("diff", ""))

    def execute_in_sandbox(self, command: str) -> str:
        cmd_list = shlex.split(command)
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": cmd_list, "cwd": self.repo_root},
        )
        if "error" in result:
            return result["error"]
        stdout = truncate_for_llm(result.get("stdout") or "")
        stderr = truncate_for_llm(result.get("stderr") or "")
        return f"Exit Code: {result.get('exit_code')}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
