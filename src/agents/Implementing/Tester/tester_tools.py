import shlex

from langchain_core.tools import BaseTool, StructuredTool

from agents.tool_output import (
    MAX_FILE_READ_CHARS,
    truncate_for_llm
)
from agents.semantic_search import SEMANTIC_SEARCH_DESCRIPTION, run_semantic_search
from tool_gateway.gateway import ToolGateway
from tool_gateway.permissions import get_permissions
from tool_gateway.Roles import Roles
from sandbox.Tools import Tools
from sandbox.sandbox_tools import SandboxError

# Default only. The REAL path comes from the run's SandboxSpec via
# sandbox.factory.repo_root(state) and is injected by the node — a pre-provisioned image keeps
# the repo somewhere else entirely (e.g. /testbed), and hardcoding this made every command run
# in a nonexistent directory (exit 127, ADR 0039).
DEFAULT_REPO_ROOT = "/workspace/repo"

# Gateway-error signatures that mean the sandbox itself is GONE (registry row / container
# missing), not that a command merely failed. These must NOT be swallowed as an empty
# command result — a dead sandbox that reads as "no output" makes detection find nothing →
# no Track A checks → the verdict falls through to "nothing decided → unverified PASS"
# (ADR 0037), laundering a destroyed sandbox into a green DONE (run b6487b43). Raise instead
# so it escapes the tester node and STALLS the run (recoverable), never a fake pass.
_SANDBOX_GONE_SIGNATURES = ("Unknown sandbox_id", "Unknown run_id", "No such container")


def _raise_if_sandbox_gone(result: dict) -> None:
    error = result.get("error")
    if error and any(sig in str(error) for sig in _SANDBOX_GONE_SIGNATURES):
        raise SandboxError(f"tester: sandbox unreachable — {error}")


def to_sandbox_path(path: str, repo_root: str = DEFAULT_REPO_ROOT) -> str:
    """Normalize a repo-relative path (how the Planner declares target_files)
    to the absolute sandbox path fs.read_file/fs.write_file require."""
    if path.startswith("/"):
        return path
    return f"{repo_root}/{path}"


# Like PlannerTools, this can later become an MCP tool implementation exposing a
# safe interface for the Tester to read/write files and run commands in the sandbox.
class TesterTools:
    def __init__(self, gateway: ToolGateway, repo_root: str = DEFAULT_REPO_ROOT):
        self.gateway = gateway
        self.role = Roles.TESTER
        self.repo_root = repo_root

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
                f"{self.repo_root}. Pass a single argv string with no shell, pipes, or "
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

        semantic_search_tool = StructuredTool.from_function(
            func=self.semantic_search,
            name="semantic_search",
            description=SEMANTIC_SEARCH_DESCRIPTION,
        )

        return [read_tool, write_tool, execute_tool, semantic_search_tool]

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
        return truncate_for_llm(result.get("content", "Empty file or could not read."),
                                 limit=MAX_FILE_READ_CHARS)

    def write_sandbox_file(self, filepath: str, content: str) -> str:
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.FS_WRITE_FILE,
            args={"path": to_sandbox_path(filepath, self.repo_root), "content": content},
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
            args={"cmd": ["git", "add", "-N", "."], "cwd": self.repo_root},
        )
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.REPO_DIFF,
            args={},
        )
        _raise_if_sandbox_gone(result)   # dead sandbox → stall, don't return "" as a clean diff
        if "error" in result:
            return result["error"]
        return result.get("diff", "")

    def run(self, argv: list[str], timeout_s: int = 300) -> dict:
        """Raw allowlisted sandbox command — returns the gateway result dict
        (exit_code/stdout/stderr/duration_ms). Used by the deterministic test phases
        (detect / install / run-suite); LLM-facing string wrappers are separate.

        Raises SandboxError if the sandbox is GONE (see `_raise_if_sandbox_gone`): the
        deterministic phases treat a `{'error': …}` dict as "empty output", so a destroyed
        sandbox would silently yield no checks and pass the microtask unverified. A dead
        sandbox must stall the run, not decide a verdict."""
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.SANDBOX_RUN,
            args={"cmd": argv, "cwd": self.repo_root, "timeout_s": timeout_s},
        )
        _raise_if_sandbox_gone(result)
        return result

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
