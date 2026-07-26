# src/tool_gateway/ — Code Explanation

Three files, 281 lines total. The permission boundary between agents and the world.

---

## Roles.py (14 lines)

A plain `enum.Enum` of every actor identity the system knows:
`PLANNER, REVIEWER, CODER, TESTER, ORCHESTRATOR, BOOTSTRAPPER, HUMAN, DEPLOYER, REFLECTOR`.

- String values (`"planner"`, …) — these strings are what lands in the `actor`
  column of audit events, so the enum is the single vocabulary for "who did this".
- Note `HUMAN` and `DEPLOYER` exist as identities even though neither calls the
  gateway — they appear as event actors (HITL decisions, deploy node events).

## permissions.py (98 lines)

**The policy definition.** One base class + one subclass per role + a registry.

### `RolePermissions` (base, lines 7–21)
```python
role: Roles = Roles.HUMAN
tools: List[Tools] = []
allowed_commands: List[str] = []
```
- Class-level attributes; subclasses override them. Empty lists on the base =
  **deny-by-default**: a role with no subclass can do nothing.
- `can_use_tool(tool_name)` → `tool_name in self.tools`. Works because `Tools` is a
  `str`-enum — a raw string compares equal to the enum member.
- `can_run_command(command)` → membership check on the **first argv element** only
  (the interpreter/binary); the gateway extracts `cmd[0]` before calling this.

### The subclasses (lines 24–78) — read as a policy table
| Class | tools | allowed_commands | Design intent |
|---|---|---|---|
| `PlannerPermissions` | RUN, READ, SEARCH | git, python, grep, ls, cat, find, node, cd | read-only investigation — no write tool at all |
| `CoderPermissions` | WRITE, READ, DIFF, RUN, SEARCH | git, python, grep, ls, cat, find, mkdir, rm, touch, node | can edit + see its own diff; **no `pytest`** — docstring says why: coder must not self-report a pass |
| `TesterPermissions` | WRITE, READ, DIFF, RUN, SEARCH | + pip, pytest, npm, npx, apt-get, mv | the only role that may run test suites; WRITE is for writing synth tests |
| `ReviewerPermissions` | RUN, READ, SEARCH | grep, ls, cat, find (no git, no python!) | strictest read-only — static inspection only |
| `ReflectorPermissions` | READ, DIFF, RUN, SEARCH | git, grep, ls, cat, find, node | reads evidence, never writes |
| `OrchestratorPermissions` | (empty) | (empty) | never calls the gateway — star topology means it *mediates*, doesn't act |
| `BootstrapperPermissions` | (empty) | (empty) | sandbox lifecycle/clone go straight to `DockerSandboxTools`, not through the gateway |

### Registry (lines 81–98)
```python
_REGISTRY = {cls.role: cls() for cls in (…all seven…)}
_DEFAULT = RolePermissions()
def get_permissions(role): return _REGISTRY.get(role, _DEFAULT)
```
Dict-comprehension builds one singleton instance per role, keyed by the enum.
Unknown role → `_DEFAULT` (the empty base) → everything denied. This function is
the ONLY lookup path the gateway uses.

## gateway.py (171 lines)

**The enforcement + audit engine.** One class, one public method.

### `ToolGateway.__init__` (lines 23–30)
Takes a live SQLAlchemy `Session` (borrowed — gateway never commits), the
`run_id`, a `DockerSandboxTools` instance, and optionally a `context_engine` +
`repo_url` (only wired for roles that may semantic-search). Holding the session
means every audit event joins the caller's transaction.

### `_log_event` (lines 32–41) — the dual-channel write
1. `EventLogger.log(...)` → durable Postgres `events` row (dashboard history).
2. `_log.info/warning(...)` → ephemeral structlog line (live SSE feed).
Denials log at WARNING, executions at INFO — one `emit = _log.warning if … else
_log.info` line picks the level. This is the two-telemetry-channels pattern in
miniature.

### `execute_tool(actor_role, tool_name, args)` (lines 43–162) — the whole story
Four numbered stages, in order:

**Stage 1 — RBAC (lines 46–59):** `get_permissions(role)`; if
`can_use_tool` fails → log `TOOL_DENIED` with `policy_rule="role_not_allowed"` and
return an `{"error": …}` dict. Note: it *returns* the error rather than raising —
inside a ReAct loop this becomes an observation the LLM can read and adapt to.

**Stage 2 — command allowlist (lines 61–80):** only for `SANDBOX_RUN`.
- `cmd` must be a **non-empty list** — array-form argv, never a shell string →
  structural defense against shell injection (there is no shell to inject into).
- `base_cmd = cmd[0]` checked via `can_run_command` → deny logs
  `policy_rule="sandbox_command_not_allowed"`.

**Stage 3 — dispatch (lines 82–144):** a `match tool_name:` over the `Tools` enum:
- `FS_WRITE_FILE` / `FS_READ_FILE` → sandbox file IO.
- `REPO_DIFF` → `sandbox_tools.repo_diff` (the coder's ground truth).
- `SANDBOX_RUN` → `sandbox_exec`, result carries exit_code/stdout/stderr/duration.
- `SEMANTIC_SEARCH` → `context_engine.search(query, repo_url, run_id, top_k)`;
  errors cleanly if no engine was injected.
- `sandbox_id` defaults to `f"sbox_{run_id}"` — the naming convention that ties a
  run to its container everywhere.
- Whole dispatch wrapped in `try/except` → any exception becomes
  `{"error": str(e)}` with `success=False` (again: observation, not crash).

**Stage 4 — mandatory logging (lines 146–160):** ALWAYS writes `TOOL_EXECUTED`
with duration_ms, success flag, truncated args (120 chars) and result (200 chars).
Every tool call in the system's history is reconstructable from these rows.

### `check_requested_path` (lines 165–170) — known dead code
Path-traversal guard that is **never called** (CLAUDE.md known-bug #5). The actual
protection lives in `sandbox_tools._validate_sandbox_path`. Honest exam answer if
asked: "defense exists at the sandbox layer; the gateway-level check is dead code
I'd wire or delete."

---

## Relationships

```
agents (planner_tools / coder_tools / …)      api/ (control plane)
        │  execute_tool(role, tool, args)          │ (bypasses gateway —
        ▼                                          ▼  human-driven, not an agent)
   ToolGateway ── get_permissions(role) ──► permissions.py (policy)
        │ match Tools enum (sandbox/Tools.py)
        ▼
   DockerSandboxTools (sandbox/sandbox_tools.py — mechanism)
        │
        ▼
   EventLogger → events table   +   structlog → RunLogBus → SSE
```

Separation to cite: **policy** (permissions.py) / **enforcement** (gateway.py) /
**mechanism** (sandbox_tools.py) — three files, three concerns.
