from typing import ClassVar

from sandbox.Tools import Tools
from tool_gateway.Roles import Roles


class RolePermissions:
    """Per-role policy: which gateway tools and which sandbox commands a role
    may use. Subclass per role and override `tools` / `allowed_commands`;
    `get_permissions()` looks instances up by `Roles`. Roles with no subclass
    get this base (empty = deny-by-default)."""

    role: ClassVar[Roles] = Roles.HUMAN
    tools: ClassVar[list[Tools]] = []
    allowed_commands: ClassVar[list[str]] = []

    def can_use_tool(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def can_run_command(self, command: str) -> bool:
        return command in self.allowed_commands


class PlannerPermissions(RolePermissions):
    """Read-only investigation — inspects the repo, never edits."""

    role: ClassVar[Roles] = Roles.PLANNER
    tools: ClassVar[list[Tools]] = [Tools.SANDBOX_RUN, Tools.FS_READ_FILE, Tools.SEMANTIC_SEARCH]
    allowed_commands: ClassVar[list[str]] = ["git", "python", "grep", "ls", "cat", "find","node", "cd", "pwd"]


class CoderPermissions(RolePermissions):
    """Edits files + reads its own diff. No `pytest` — running the test suite
    is the Tester's job (CLAUDE.md known-bugs #8); without this the coder
    could self-report a pass the Tester never confirmed."""

    role: ClassVar[Roles] = Roles.CODER
    tools: ClassVar[list[Tools]] = [Tools.FS_WRITE_FILE, Tools.FS_READ_FILE, Tools.REPO_DIFF, Tools.SANDBOX_RUN, Tools.SEMANTIC_SEARCH]  
    allowed_commands: ClassVar[list[str]] = ["git", "python", "grep", "ls", "cat", "find", "mkdir", "rm", "touch", "node", "pwd", "sed"]  


class TesterPermissions(RolePermissions):
    """Runs the test suite and reads results. Includes write tools for writing tests and 'pytest' command permission,
    but the Tester should never edit existing code."""

    role: ClassVar[Roles] = Roles.TESTER
    tools: ClassVar[list[Tools]] = [Tools.FS_WRITE_FILE, Tools.FS_READ_FILE, Tools.REPO_DIFF, Tools.SANDBOX_RUN, Tools.SEMANTIC_SEARCH]
    allowed_commands: ClassVar[list[str]] = ["git", "python", "pip", "pytest", "node", "npm", "npx", "apt-get", "grep", "ls", "cat", "find", "mkdir", "mv", "pwd", "sed"]


class ReviewerPermissions(RolePermissions):
    """Plan/code review — read-only."""

    role: ClassVar[Roles] = Roles.REVIEWER
    tools: ClassVar[list[Tools]] = [Tools.SANDBOX_RUN, Tools.FS_READ_FILE, Tools.SEMANTIC_SEARCH]
    allowed_commands: ClassVar[list[str]] = ["grep", "ls", "cat", "find"]


class ReflectorPermissions(RolePermissions):
    """Diagnoses failures by reading source files, test files, and diffs. No writes."""

    role: ClassVar[Roles] = Roles.REFLECTOR
    tools: ClassVar[list[Tools]] = [Tools.FS_READ_FILE, Tools.REPO_DIFF, Tools.SANDBOX_RUN, Tools.SEMANTIC_SEARCH]
    allowed_commands: ClassVar[list[str]] = ["git", "grep", "ls", "cat", "find", "node"]


class OrchestratorPermissions(RolePermissions):
    """Never calls the gateway directly — mediates via agent roles (star
    topology). Empty allowlists are intentional."""

    role: ClassVar[Roles] = Roles.ORCHESTRATOR


class BootstrapperPermissions(RolePermissions):
    """Sandbox lifecycle + clone go through DockerSandboxTools directly, not
    the gateway. Empty allowlists are intentional."""

    role: ClassVar[Roles] = Roles.BOOTSTRAPPER


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
