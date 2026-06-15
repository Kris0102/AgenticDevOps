import shlex

from langchain_core.tools import BaseTool, StructuredTool

from tool_gateway.gateway import ToolGateway
from tool_gateway.permissions import get_permissions
from tool_gateway.Roles import Roles
from sandbox.Tools import Tools


# This can later be a MCP tool implementation for the Planner agent, exposing a safe interface to execute commands and read files from the Docker sandbox.
class PlannerTools:
    def __init__(self, gateway: ToolGateway):
        self.gateway = gateway
        self.role = Roles.PLANNER

    def build_tools(self) -> list[BaseTool]:
        execute_tool = StructuredTool.from_function(
            func=self.execute_in_sandbox,
            name="execute_in_sandbox",
            description=(
                "Run an allowlisted command inside the Docker sandbox at /workspace. "
                "Pass a single argv string with no shell, pipes, or redirects. "
                "Allowed bases: " + ", ".join(get_permissions(Roles.PLANNER).allowed_commands) + ". "
                "Examples: 'ls -la /workspace', 'grep -rn Planner src/'"
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
            description=(
                "Semantically search the indexed repository for code relevant to a "
                "natural-language query (e.g. 'where are HTTP routes defined', "
                "'authentication middleware'). Returns the most relevant functions/"
                "classes with their file paths. Prefer this over grepping or reading "
                "whole files to locate WHERE something lives, then read_sandbox_file "
                "for full detail."
            ),
        )

        return [execute_tool, read_tool, semantic_search_tool]

    def execute_in_sandbox(self, command: str) -> str:
        if not self.gateway:
            raise RuntimeError("Sandbox tools are not available. Docker initialization failed.")
    
        cmd_list = shlex.split(command)
        
        result = self.gateway.execute_tool(
            actor_role=self.role, 
            tool_name=Tools.SANDBOX_RUN, 
            args={"cmd": cmd_list}
        )
        if "error" in result:
            return result["error"]
        return f"Exit Code: {result.get('exit_code')}\nSTDOUT:\n{result.get('stdout')}\nSTDERR:\n{result.get('stderr')}"

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

        return result.get("content", "Empty file or could not read.")

    def semantic_search(self, query: str, top_k: int = 8) -> str:
        if not self.gateway:
            raise RuntimeError("Sandbox tools are not available. Docker initialization failed.")

        result = self.gateway.execute_tool(
            actor_role=self.role,
            tool_name=Tools.SEMANTIC_SEARCH,
            args={"query": query, "top_k": top_k},
        )
        if "error" in result:
            return result["error"]

        hits = result.get("hits", [])
        if not hits:
            return "No relevant code found in the semantic index for that query."

        lines = [f"Top {len(hits)} semantic matches:"]
        for h in hits:
            symbol = h.get("symbol") or h.get("chunk_type")
            lines.append(f"- {h['filepath']} :: {symbol} (score {h['score']})")
        return "\n".join(lines)
