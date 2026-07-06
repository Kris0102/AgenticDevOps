from typing import List

from sandbox.Tools import Tools
from tool_gateway.Roles import Roles


class RolePermissions:
    """Per-role policy: which gateway tools and which sandbox commands a role
    may use. Subclass per role and override `tools` / `allowed_commands`;
    `get_permissions()` looks instances up by `Roles`. Roles with no subclass
    get this base (empty = deny-by-default)."""

    role: Roles = Roles.HUMAN
    tools: List[Tools] = []
    allowed_commands: List[str] = []

    def can_use_tool(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def can_run_command(self, command: str) -> bool:
        return command in self.allowed_commands


class PlannerPermissions(RolePermissions):
    """Read-only investigation — inspects the repo, never edits."""

    role = Roles.PLANNER
    tools = [Tools.SANDBOX_RUN, Tools.FS_READ_FILE, Tools.SEMANTIC_SEARCH]
    allowed_commands = ["git", "python", "grep", "ls", "cat", "find","node"]


class CoderPermissions(RolePermissions):
    """Edits files + reads its own diff. No `pytest` — running the test suite
    is the Tester's job (CLAUDE.md known-bugs #8); without this the coder
    could self-report a pass the Tester never confirmed."""

    role = Roles.CODER
    tools = [Tools.FS_WRITE_FILE, Tools.FS_READ_FILE, Tools.REPO_DIFF, Tools.SANDBOX_RUN]
    allowed_commands = ["git", "python", "grep", "ls", "cat", "find", "mkdir", "rm", "touch", "node"]


class TesterPermissions(RolePermissions):
    """Runs the test suite and reads results. Includes write tools for writing tests and 'pytest' command permission,
    but the Tester should never edit existing code."""

    role = Roles.TESTER
    tools = [Tools.FS_WRITE_FILE, Tools.FS_READ_FILE, Tools.REPO_DIFF, Tools.SANDBOX_RUN]
    allowed_commands = ["git", "python", "pip", "pytest", "node", "npm", "npx", "apt-get", "grep", "ls", "cat", "find", "mkdir"]


class ReviewerPermissions(RolePermissions):
    """Plan/code review — read-only."""

    role = Roles.REVIEWER
    tools = [Tools.SANDBOX_RUN, Tools.FS_READ_FILE, Tools.SEMANTIC_SEARCH]
    allowed_commands = ["grep", "ls", "cat", "find"]


class ReflectorPermissions(RolePermissions):
    """Diagnoses failures by reading source files, test files, and diffs. No writes."""

    role = Roles.REFLECTOR
    tools = [Tools.FS_READ_FILE, Tools.REPO_DIFF, Tools.SANDBOX_RUN]
    allowed_commands = ["git", "grep", "ls", "cat", "find", "node"]


class OrchestratorPermissions(RolePermissions):
    """Never calls the gateway directly — mediates via agent roles (star
    topology). Empty allowlists are intentional."""

    role = Roles.ORCHESTRATOR


class BootstrapperPermissions(RolePermissions):
    """Sandbox lifecycle + clone go through DockerSandboxTools directly, not
    the gateway. Empty allowlists are intentional."""

    role = Roles.BOOTSTRAPPER


_REGISTRY = {
    cls.role: cls()
    for cls in (
        PlannerPermissions,
        CoderPermissions,
        TesterPermissions,
        ReviewerPermissions,
        ReflectorPermissions,
        OrchestratorPermissions,
        BootstrapperPermissions,
    )
}
_DEFAULT = RolePermissions()


def get_permissions(role: Roles) -> RolePermissions:
    """Deny-by-default: any role without an explicit subclass gets the empty base."""
    return _REGISTRY.get(role, _DEFAULT)
