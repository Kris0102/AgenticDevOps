import shlex

from langchain_core.tools import BaseTool, StructuredTool

from agents.tool_output import (
    MAX_FILE_READ_CHARS,
    truncate_for_llm,
)
from agents.semantic_search import SEMANTIC_SEARCH_DESCRIPTION, run_semantic_search
from tool_gateway.gateway import ToolGateway
from tool_gateway.permissions import get_permissions
from tool_gateway.Roles import Roles
from sandbox.Tools import Tools

# Default only — the real path comes from the run's SandboxSpec via
# sandbox.factory.repo_root(state), injected by the node. Hardcoding it made every command run
# in a nonexistent directory on a pre-provisioned image (ADR 0039).
DEFAULT_REPO_ROOT = "/workspace/repo"



# This can later be a MCP tool implementation for the Reviewer agent, exposing a safe interface to execute commands and read files from the Docker sandbox.
class ReviewerTools:
    def __init__(self, gateway: ToolGateway, repo_root: str = DEFAULT_REPO_ROOT):
        self.gateway = gateway
        self.role = Roles.REVIEWER
        self.repo_root = repo_root

    def build_tools(self) -> list[BaseTool]:
        execute_tool = StructuredTool.from_function(
            func=self.execute_in_sandbox,
            name="execute_in_sandbox",
            description=(
                "Run an allowlisted command inside the Docker sandbox. The repo is at "
                f"{self.repo_root}. "
                "Pass a single argv string with no shell, pipes, or redirects. "
                "Allowed bases: " + ", ".join(get_permissions(Roles.REVIEWER).allowed_commands) + ". "
                f"Examples: 'ls -la {self.repo_root}', 'grep -rn Reviewer {self.repo_root}/src'"
            ),
        )
        
        read_tool = StructuredTool.from_function(
            func=self.read_sandbox_file,
            name="read_sandbox_file",
            description="Read the contents of a specific file inside the secure Docker Sandbox. Use this to read specific module implementations and architectures."
        )

        semantic_search_tool = StructuredTool.from_function(
            func=self.semantic_search,
            name="semantic_search",
            description=SEMANTIC_SEARCH_DESCRIPTION,
        )

        return [execute_tool, read_tool, semantic_search_tool]

    def execute_in_sandbox(self, command: str) -> str:
        if not self.gateway:
            raise RuntimeError("Sandbox tools are not available. Docker initialization failed.")
    
        # The command is LLM-authored — an unbalanced quote makes shlex raise
        # ("No closing quotation"). Catch it and hand the error back as an observation so
        # the model re-issues the command with fixed quoting, instead of the ValueError
        # propagating out and killing the whole investigation (→ run FAILED, empty patch).
        try:
            cmd_list = shlex.split(command)
        except ValueError as exc:
            return (f"Command parse error: {exc}. Re-issue the command with balanced "
                    f"quotes (or avoid quotes entirely).")

        result = self.gateway.execute_tool(
            actor_role=self.role, 
            tool_name=Tools.SANDBOX_RUN, 
            args={"cmd": cmd_list}
        )
        if "error" in result:
            return result["error"]
        stdout = truncate_for_llm(result.get("stdout") or "")
        stderr = truncate_for_llm(result.get("stderr") or "")
        return f"Exit Code: {result.get('exit_code')}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"

    def read_sandbox_file(self, filepath: str) -> str:
        if not self.gateway:
            raise RuntimeError("Sandbox tools are not available. Docker initialization failed.")

        # Note: check_requested_path should be inside gateway or we can omit it for now if not implemented.
        # Assuming execute_tool handles the policy checks:
        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.FS_READ_FILE,
            args={"path": filepath}
        )
        if "error" in result:
            return result["error"]

        return truncate_for_llm(
            result.get("content", "Empty file or could not read."), MAX_FILE_READ_CHARS
        )

    def semantic_search(self, query: str, top_k: int = 8) -> str:
        return run_semantic_search(self.gateway, self.role, query, top_k)
