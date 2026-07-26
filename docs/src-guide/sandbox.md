# src/sandbox/ — Docker-Per-Run Execution Environment

**Role in the MAS:** the isolated workspace where the agents' code actually lives
and runs. Each run gets its **own Docker container**; during a run, the sandbox —
not the host, not GitHub — is the source of truth for the repository state. Agents
only reach it through the ToolGateway.

## Files

| File | What it does |
|---|---|
| `sandbox_tools.py` | `DockerSandboxTools` — create/exec/destroy containers, file read/write inside the container, and `repo_diff` (the deterministic `git diff` the Coder is verified against). Pure sandbox execution — **no remote git in here**; that's `gitops/`'s job. Every operation returns an `ExecResult` dataclass, never a raw string |
| `Tools.py` | The `Tools` enum — the identifiers the ToolGateway dispatches on (match/case). Registering a new tool starts here |
| `Dockerfile` | The base sandbox image (`agentic-sandbox:0.1`) — git, Python, Node (Node is baked in so the Tester's JS checks work), test tooling |

## Design decisions

- **DB-backed registry:** container IDs live in the `sandboxes` table, not an
  in-memory dict — the mapping survives process restarts, so a resumed or stalled
  run reattaches to its still-running container.
- **Lifecycle ownership:** the Bootstrapper (orchestrator) creates the sandbox
  before any agent runs; agent nodes must *never* call `sandbox_create()`. Teardown
  happens only when a run is truly terminal (DONE/FAILED — never STALLED, so a
  stalled run can be continued with its workspace intact).
- **Isolation as the safety story:** an agent's shell command can at worst trash its
  own container. Combined with the gateway's command allowlist, this bounds the
  blast radius of any single agent action.
- **Deterministic ground truth:** `repo_diff` is how the system knows what the Coder
  *actually did* (vs. what it claims), and the container's test/build exit codes are
  what the Tester's verdict is computed from.

## Interview sound bites

- "One container per run: reproducible, disposable, and the single source of truth
  for code while the run is live."
- "The sandbox plus the gateway allowlist is defense in depth — what an agent may do
  and where it may do it are enforced independently."
