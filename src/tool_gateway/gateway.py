import time
from typing import Dict, Any
from sqlalchemy.orm import Session
import sys
import os
# Ensure we can import db.models when running within src module structure
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.event_log import EventLogger
from orchestrator.state import EventType
from sandbox.sandbox_tools import DockerSandboxTools
from sandbox.Tools import Tools
from tool_gateway.permissions import get_permissions
from tool_gateway.Roles import Roles


class ToolGateway:
    """
    Enforces deny-by-default execution policies and logs every invocation to Episodic Memory.
    """
    def __init__(self, db_session: Session, run_id: str, sandbox_tools: DockerSandboxTools):
        self.db = db_session
        self.run_id = run_id
        self.sandbox_tools = sandbox_tools

    def _log_event(self, actor: str, event_type: str, payload: Dict[str, Any]):
        EventLogger.log(self.db, self.run_id, actor, event_type, payload)

    def execute_tool(self, actor_role: Roles, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    
        actor_role_str = actor_role.value
        permissions = get_permissions(actor_role)
        # 1. Enforce Role-Based Access Control (Allowlist per Agent)
        if not permissions.can_use_tool(tool_name):
            self._log_event(
                actor=actor_role_str,
                event_type=EventType.TOOL_DENIED,
                payload={
                    "tool_name": tool_name,
                    "args_summary": str(args)[:120],  # simple redaction & truncation
                    "policy_rule": "role_not_allowed",
                    "reason": f"Role '{actor_role_str}' is not permitted to use tool '{tool_name}'"
                }
            )
            return {"error": f"Permission denied: {actor_role_str} cannot use {tool_name}"}

        # 2. Enforce Sandbox deterministic capabilities
        if tool_name == Tools.SANDBOX_RUN.value:
            cmd = args.get("cmd")
            # Enforce array-like shell command construction without shell injections
            if not isinstance(cmd, list) or not cmd:
                return {"error": "Permission denied: cmd must be a non-empty list of strings."}
            
            base_cmd = cmd[0]
            if not permissions.can_run_command(base_cmd):
                self._log_event(
                    actor=actor_role_str,
                    event_type=EventType.TOOL_DENIED,
                    payload={
                        "tool_name": tool_name,
                        "args_summary": str(args)[:120],
                        "policy_rule": "sandbox_command_not_allowed",
                        "reason": f"Interpreter '{base_cmd}' is not in the allowlist for role '{actor_role_str}'"
                    }
                )
                return {"error": f"Permission denied: Command '{base_cmd}' is not in the allowlist for role '{actor_role_str}'."}

        # 3. Execution Wrapper
        start_time = time.time()
        success = True
        result = {}
        
        try:
            # Route to actual sandbox.py / sandbox_tools.py inside the container instance
            # Default to the known sandbox mapped to this run_id
            sandbox_id = args.get("sandbox_id", f"sbox_{self.run_id}")
            
            match tool_name:
                case Tools.FS_WRITE_FILE:
                    self.sandbox_tools.fs_write_file(
                        sandbox_id=sandbox_id,
                        path=args.get("path",""),
                        content=args.get("content", "")
                    )
                    result = {"status": "wrote_file", "path": args.get("path")}
                case Tools.FS_READ_FILE:
                    content = self.sandbox_tools.fs_read_file(
                        sandbox_id=sandbox_id,
                        path=args.get("path","")
                    )
                    result = {"status": "read_file", "path": args.get("path"), "content": content}
                case Tools.REPO_DIFF:
                    diff_out = self.sandbox_tools.repo_diff(
                        sandbox_id=sandbox_id,
                        repo_dir=args.get("repo_dir", "repo")
                    )
                    result = {"status": "returned_diff", "diff": diff_out}
                case Tools.SANDBOX_RUN:
                    exec_result = self.sandbox_tools.sandbox_exec(
                        sandbox_id=sandbox_id,
                        cmd=args.get("cmd",""),
                        timeout_s=args.get("timeout_s", 60),
                        cwd=args.get("cwd", "/workspace")
                    )
                    result = {
                        "status": "ran_sandbox",
                        "exit_code": exec_result.exit_code,
                        "stdout": exec_result.stdout,
                        "stderr": exec_result.stderr,
                        "duration_ms": exec_result.duration_ms
                    }
                case _:
                    success = False
                    result = {"error": "Tool implemented but routing failed."}
                
        except Exception as e:
            success = False
            result = {"error": str(e)}

        duration_ms = int((time.time() - start_time) * 1000)

        # 4. Mandatory Execution Logging
        self._log_event(
            actor=actor_role_str,
            event_type=EventType.TOOL_EXECUTED,
            payload={
                "tool_name": tool_name,
                "args_summary": str(args)[:120],
                "duration_ms": duration_ms,
                "success": success,
                "result_refs": [],  # Would contain artifact IDs linking to heavy outputs
                "truncated_result": str(result)[:200]  # Simple redaction & truncation for logs
            }
        )

        return result
    
    
    def check_requested_path(self, path: str) -> bool:
        """Utility function to check if a requested file path is within allowed directories."""
        # For simplicity, we only allow reading files within the sandbox's workdir
        allowed_base = os.path.abspath(self.sandbox_tools.workdir)
        requested_path = os.path.abspath(os.path.join(allowed_base, path))
        return requested_path.startswith(allowed_base)
