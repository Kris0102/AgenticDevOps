# src/sandbox/ — Code Explanation

Two Python files + Dockerfile, ~426 lines. The mechanism layer: actual Docker
operations. No policy here — the gateway already decided; this executes.

---

## Tools.py (9 lines)

```python
class Tools(str, enum.Enum):
    SANDBOX_RUN = "sandbox.run"
    FS_READ_FILE = "fs.read_file"
    FS_WRITE_FILE = "fs.write_file"
    REPO_DIFF = "repo.diff"
    SEMANTIC_SEARCH = "semantic.search"
```
- `str`-enum: members compare equal to their string values → the gateway can match
  incoming string tool names against enum members directly, and the same values
  serialize cleanly into event payloads.
- This enum is the system's complete tool vocabulary; adding a tool starts here.

## sandbox_tools.py (419 lines)

### Module-level: diff filtering (lines 42–68)
```python
_DIFF_EXCLUDE_PREFIXES = ("node_modules/", "vendor/", ".venv/", "__pycache__/")
_DIFF_EXCLUDE_FILES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml"})
```
`_filter_diff(diff)` walks the raw `git diff` output line by line, splitting it
into sections at each `diff --git a/... b/...` header. It parses the `b/` path from
the header and sets `skip` when the path is under an excluded prefix or is a lock
file; skipped sections are dropped, kept sections re-joined.
**Why:** an `npm install` side effect once flooded the diff with thousands of
node_modules lines — blowing the Reflector's and Coder's context windows. Generated
artifacts are never the coder's work, so they're not evidence.

### Types (lines 75–90)
- `ExecResult` dataclass: `exit_code, stdout, stderr, duration_ms` — the contract
  every exec returns (project convention: never raw strings).
- `SandboxCreateResult`: `sandbox_id, container_id`.
- `SandboxError(RuntimeError)`: the module's failure type.

### `DockerSandboxTools.__init__` (lines 110–125)
Config holder: image name, `workdir` (`/workspace`), optional cpu/mem/network
limits, base labels; `docker.from_env()` client. Resource limits are pass-throughs
to `containers.run` — the knobs exist even though the PoC doesn't set them.

### `sandbox_create(run_id)` (lines 136–200) — idempotent, 3-branch
Naming convention: `sandbox_id = f"sbox_{run_id}"` (also the container *name*).
1. **DB record exists** → try `containers.get(cid)`; if the container is alive,
   `start()` it if stopped, and return (**reconnect** — this is what makes resume
   after process restart work). If Docker says NotFound → delete the stale DB row
   and fall through.
2. **No DB record but a container with that name exists** (e.g. after a DB wipe) →
   reattach and `session.merge()` the registry row back. Without this branch,
   `containers.run` would 409 on the name conflict.
3. **Truly new** → `containers.run(image, name=sandbox_id,
   command=["sleep","infinity"], detach=True, labels={run_id…})` — the container
   idles forever; all work happens via `exec`. Then `mkdir -p /workspace` and
   `session.add(Sandbox(...))`.

**Exam point:** the DB (not an in-memory dict) is the registry — survives process
restarts; labels make containers findable/cleanable from the Docker side too.

### `sandbox_destroy(sandbox_id)` (lines 202–229)
Deletes the DB row, then **purges the run's workspace semantic chunks**
(`SemanticStore().delete_workspace(run_id)`, best-effort) — run-local memory must
not outlive the container — then `kill()` + `remove(force=True)`, each wrapped in
its own try/except (teardown must never crash a finalizer).

### `sandbox_exec(...)` (lines 267–310) — the watchdog
The core primitive. Key lines:
```python
wrapped = ["timeout", "--kill-after=5", str(timeout_s), *cmd]
res = container.exec_run(cmd=wrapped, workdir=cwd, environment=env, demux=True)
```
- **No shell**: `cmd` is argv-array; `demux=True` separates stdout/stderr.
- **The watchdog (comment block 283–289):** `docker exec` has NO kill-timeout — a
  synth test with a live `setInterval` or a server that `listen()`s used to freeze
  a run FOREVER. Coreutils `timeout` SIGTERMs at `timeout_s`, SIGKILLs 5s later.
- Exit 124 (timeout) / 137 (needed SIGKILL) get a self-describing note appended to
  stderr: `[sandbox watchdog] command exceeded Ns and was killed` — so downstream
  classifiers read it as a **harness error, never a code verdict** (a hung test
  must not count as a failing test).
- Output decoded with `errors="replace"` — hostile bytes can't crash the harness.

### `repo_diff(sandbox_id, repo_dir)` (lines 317–324)
Just `git diff` via `sandbox_exec`, piped through `_filter_diff`. The comment
marks the boundary: clone/branch/commit/push moved to `gitops.GitOps` (ADR 0012);
`repo_diff` stays because it's *local working-tree inspection* — the Coder's
grounded-verification input — not a remote operation.

### File IO (lines 331–395)
- `fs_read_file`: `cat` via exec; non-zero exit → `SandboxError`. Validated first.
- `fs_write_file`: **not** echo/heredoc (escaping hazards) — builds an in-memory
  tar (`tarfile` + `BytesIO`) and uses Docker's `put_archive`. Runs `mkdir -p
  dir_name` first because put_archive 404s on a missing directory (new-subdir
  microtasks like `tests/`).
- `fs_read_file_bytes`: `get_archive` → extract single member → raw bytes.
  Binary-safe counterpart for artifacts `cat` would mangle — exists for exactly
  one caller: GitOps local export pulling the **git bundle** (ADR 0036).
  Deliberately NOT gateway-exposed.

### `_validate_sandbox_path(path)` (lines 408–418)
Three checks: absolute path; no `..` component (split on `/`, so `..` embedded in
a filename is fine but a traversal component is not); must start with
`workdir + "/"`. This is the *actual* path-traversal defense (the gateway's
`check_requested_path` is dead code).

## Dockerfile
Base sandbox image `agentic-sandbox:0.1` — git, Python + pytest, Node/npm (added
when the Tester grew JS support). Rebuilt only when tooling changes:
`docker compose --profile donotstart build sandbox-image-builder`.

---

## Relationships

```
ToolGateway ──────────────► sandbox_exec / fs_* / repo_diff   (agent traffic, policy-checked)
BootStrapper ─────────────► sandbox_create / sandbox_exists    (lifecycle, pre-graph)
Service._finalize ────────► sandbox_destroy                    (teardown on terminal runs)
GitOps ───────────────────► sandbox_exec (git cmds), fs_read_file_bytes (bundle export)
Tester/Coder runners ─────► raw ExecResult (deterministic phases, no LLM truncation)
        registry: Sandbox row in Postgres  ←── every method resolves container via DB
```
