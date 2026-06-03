from tool_gateway.gateway import ToolGateway
from langchain_core.tools import StructuredTool
import shlex

class PlannerTools:
    def __init__(self, gateway: ToolGateway):
        self.gateway = gateway

    def build_tools(self):
        execute_tool = StructuredTool.from_function(
            func=self.execute_in_sandbox,
            name="execute_in_sandbox",
            description="Run a shell command like 'grep' or 'ls' inside the secure Docker Sandbox. Use this to search the codebase and understand the file structure. Example commands: 'ls -la', 'grep -rn \"class Planner\" src/'"
        )
        
        read_tool = StructuredTool.from_function(
            func=self.read_sandbox_file,
            name="read_sandbox_file",
            description="Read the contents of a specific file inside the secure Docker Sandbox. Use this to read specific module implementations and architectures."
        )
        
        return [execute_tool, read_tool]

    def execute_in_sandbox(self, command: str) -> str:
        if not self.gateway:
            raise RuntimeError("Sandbox tools are not available. Docker initialization failed.")
    
        cmd_list = shlex.split(command)
        
        result = self.gateway.execute_tool(
            actor_role="PLANNER", 
            tool_name="sandbox.run", 
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
            actor_role="PLANNER",
            tool_name="fs.read_file",
            args={"path": filepath}
        )
        if "error" in result:
            return result["error"]
        
        return result.get("content", "Empty file or could not read.")
