# src/tool_gateway/ — The Permission Boundary

**Role in the MAS:** the single chokepoint between agents and the environment.
Every environment action an LLM agent takes — file read/write, shell command, git
diff, semantic search — is a `ToolGateway` call. **Deny by default**: if a role's
allowlist doesn't explicitly permit the tool (and, for shell, the command), the call
is refused and a `tool.denied` audit event is written. Nothing an agent does is
invisible.

## Files

| File | What it does |
|---|---|
| `gateway.py` | `ToolGateway` — looks up the caller's `RolePermissions`, enforces the tool + sandbox-command allowlist, dispatches to `DockerSandboxTools`, and logs every call (`tool.executed` / `tool.denied`) via `EventLogger` |
| `permissions.py` | One `RolePermissions` subclass per role: `tools` (which `Tools` enum members) + `allowed_commands` (which sandbox binaries). E.g. `CoderPermissions` includes `node`/`python` for compile checks but *not* `pytest` |
| `Roles.py` | `AgentRole` enum — the identity each agent presents to the gateway |

## Why it exists (interview framing)

1. **Least privilege per agent.** Capability boundaries are enforced in code, not in
   prompts. A prompt says "don't run tests"; the gateway makes it impossible. The
   Coder being blocked from `pytest` is the canonical example — separation of duties
   between Coder and Tester is structural.
2. **Complete audit trail.** Every call lands in the `events` table with actor,
   tool, args, and outcome. This is what makes an autonomous system reviewable after
   the fact — you can replay exactly what each agent touched.
3. **One integration surface.** Agents don't know Docker exists. Swapping the
   sandbox backend (or adding MCP tools later) means changing the gateway's
   dispatch, not five agents.

## Design details worth mentioning

- **Commit-boundary discipline:** the gateway's event logging delegates to
  `EventLogger.log()`, which only ever calls `session.add()` — the *caller's*
  `session_scope()` owns the commit. The gateway once committed mid-transaction;
  that was audit finding #3 and is fixed.
- **Adding a tool** touches three places by convention: register it in
  `sandbox/Tools.py` (the enum the gateway dispatches on via match/case), grant it
  in the relevant `RolePermissions` subclass, and (for a new role) extend
  `Roles.py`.
- The API control plane (`src/api/`) deliberately **bypasses** the gateway — it is
  human-driven infrastructure, not an LLM agent. The gateway governs agents only.

## Interview sound bites

- "Deny-by-default, per-role allowlists, every call audited — the gateway is our
  answer to 'how do you trust an autonomous coder?'"
- "Security boundaries live in code, not prompts."
