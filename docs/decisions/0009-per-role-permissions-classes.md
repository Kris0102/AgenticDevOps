# 0009 — Per-role permission classes replace global ROLE_PERMISSIONS / ALLOWED_SANDBOX_CMDS

Status: accepted
Date: 2026-06-15

## Context

`tool_gateway/gateway.py` had two disconnected globals: `ROLE_PERMISSIONS` (role →
allowed tool names) and a single `ALLOWED_SANDBOX_CMDS` list shared by *every* role
that can call `sandbox.run`. Audit finding #1 (CLAUDE.md known-bugs #8) flagged this:
the Coder could technically run `pytest` via `sandbox.run` even though the prompt
forbids it — running tests is the Tester's job, and a coder that can run tests could
self-report a pass the Tester never confirmed (violates grounded verification).

Separately, only `PLANNER`/`CODER`/`TESTER`/`ORCHESTRATOR`/`BOOTSTRAPPER` had entries;
`REVIEWER`/`HUMAN` silently fell back to `.get(role, [])` (deny-all), which is correct
but undocumented.

## Decision

`tool_gateway/permissions.py` defines `RolePermissions` (base: empty `tools` +
`allowed_commands`, plus `can_use_tool`/`can_run_command` helpers) and one subclass
per role (`PlannerPermissions`, `CoderPermissions`, `TesterPermissions`,
`ReviewerPermissions`, `OrchestratorPermissions`, `BootstrapperPermissions`), each
bundling its tool allowlist AND sandbox command allowlist together. `get_permissions(role)`
looks up a `_REGISTRY` of instances, falling back to the empty base for any
unregistered role (deny-by-default, now explicit).

`gateway.py.execute_tool` calls `get_permissions(actor_role)` once and uses
`permissions.can_use_tool(tool_name)` / `permissions.can_run_command(base_cmd)` —
`ROLE_PERMISSIONS` and `ALLOWED_SANDBOX_CMDS` are removed.

**`CoderPermissions.allowed_commands` drops `pytest`** — closes the audit #8 gap at
the gateway level (deny, not just prompt instruction). `TesterPermissions` (new) gets
`pytest`/`npm`/`node` — the role that will actually run the test suite once the real
Tester lands.

`planner_tools.py` / `coder_tools.py` (tool descriptions shown to the LLM) now read
their allowed-commands list via `get_permissions(Roles.PLANNER/CODER).allowed_commands`
instead of importing the old shared `ALLOWED_SANDBOX_CMDS` from `gateway.py`.

## Alternatives considered

- **Keep two globals, just add a per-role cmd dict**: rejected — still two lookups per
  role with no shared interface; the class form gives both lists one home and a place
  to add role-specific behavior (e.g. future `can_write_path`) without another global.
- **Single `PERMISSIONS: Dict[Roles, RolePermissions]` dict of dataclasses instead of
  subclasses**: rejected — subclassing lets each role's docstring explain *why* its
  allowlist looks the way it does (e.g. "Coder has no pytest because...") right next
  to the data, which a dict-of-dataclasses would push into comments elsewhere.

## Consequences

- Adding a new role or tool: one subclass / one list edit in `permissions.py`, no
  `gateway.py` change (CLAUDE.md "Coding conventions" updated accordingly).
- Coder running `sandbox.run(["pytest", ...])` now returns a `tool.denied` event
  (`policy_rule=sandbox_command_not_allowed`) instead of merely being against the
  prompt — testable.
- `check_requested_path` (audit #5, dead code) is untouched — separate fix.
