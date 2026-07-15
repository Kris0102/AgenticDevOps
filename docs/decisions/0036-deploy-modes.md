# 0036 — Per-run deploy modes: dry run, push, PR, local-only export

Status: accepted
Date: 2026-07-16

Amends ADR 0035 (deploy options per run), ADR 0029 (run-start rights), ADR 0027
(auto-PR), ADR 0012 (GitOps boundary).

## Context

What a run's deploy step does was spread over two booleans that composed badly:

- `RunRequest.skip_deploy` — dry-run, the tester/hitl DONE routers bypassed the deploy
  node entirely (no gate, no diff review at the end).
- `RunRequest.auto_create_pr` (tri-state, None → global default) — PR on top of the push.

That gave three reachable behaviours (dry / push / push+PR) encoded in 2×2 flag space,
with the invalid combination (`skip_deploy` + `auto_create_pr`) silently resolved by one
flag disabling the other in the UI. A fourth behaviour the operator actually wanted was
impossible to express: **get the result as a branch in the local clone on this machine,
without touching GitHub at all** — useful for reviewing/iterating on agent output in a
real editor before anything becomes visible on the remote, and for repos whose token has
no push rights.

The MAS deployment model makes local export natural: the API process and the user's
clones share a machine (self-hosted, single user — ADR 0013), so "local" is a plain
filesystem path, not a transfer problem.

## Decision

### One enum instead of two booleans

`DeployMode(str, Enum)` in `state.py`: `DRY_RUN | PUSH | PR | LOCAL_ONLY`.
`RunRequest.deploy_mode: Optional[DeployMode]` (None → global default) +
`RunRequest.local_export_path` replace `skip_deploy` + `auto_create_pr`. The blackboard
carries `deploy_mode` + `local_export_path`; `runs.deploy_mode` (nullable String,
migration `e8f9a0b1c2d3`) mirrors it as the run's **label** on the dashboard list.

The ADR 0035 invariant is kept: **one resolver** (`Service.resolve_deploy_mode`), two
consumers (run-start rights preflight + blackboard), resolved once at creation, never
re-read from Settings live. The global default stays the existing
`defaults.auto_create_pr` bool (True → PR, False → PUSH) rather than a new enum setting:
DRY_RUN and LOCAL_ONLY are per-run intents, never a sensible standing default, so a
bool that picks between the two remote modes covers the real choice without new
settings-schema machinery.

### Every mode passes the deploy gate

Previously `skip_deploy` bypassed the deploy node, so a dry run ended without the human
ever seeing the final diff. Now the tester's DONE always routes to `deploy`; the gate
(when configured) pauses for every mode, and the **mode decides what an approve does**:

| mode | rights preflight (ADR 0029) | approve does |
|---|---|---|
| `dry_run` | clone/read only | nothing ships; `gitops.deploy_skipped` event records the would-be fileset |
| `push` | `can_push` | stage → commit → push `agent/run-<id>` (or override) |
| `pr` | `can_push` + `can_create_pr` | push + GitHub PR (ADR 0027 semantics unchanged) |
| `local_only` | clone/read only | `git bundle` the sandbox commit → `git fetch` it into `local_export_path` as the run branch |

The gate UI (`HitlPanel`) now states the mode's consequence explicitly, and
`HitlReviewContext` carries `deploy_mode`/`local_export_path` for it.

### Local export mechanics

`GitOps.export_local_branch`: the shared `_stage_and_commit` (extracted from
`push_branch` — both paths build the identical commit), then `git bundle create` in the
sandbox, pulled out with the new `DockerSandboxTools.fs_read_file_bytes`
(`get_archive`, the binary-safe counterpart to `fs_write_file` — `cat` would mangle a
bundle), then host-side `git -C <clone> fetch <bundle> HEAD:refs/heads/<branch>` via
subprocess. A bundle+fetch transfers the REAL sandbox commit (identity, message,
parent), unlike re-applying a diff. `git fetch` natively refuses to move an existing or
checked-out ref, so the safety property holds even if the pre-checks race.

Validation is two-layered like `push_branch` (ADR 0035): at run creation
`Service._validated_local_export_path` 422s on not-a-dir / not-a-git-repo / an
explicitly-chosen branch that already exists; at deploy `export_local_branch` re-checks
and raises `LocalExportError` (subclasses `SandboxError`). An export failure **fails the
run** — the human chose this mode to receive a branch; degrading (the PR path's policy)
would be success theater. The secret scan (ADR 0019) runs for local exports too: a
bundle in a local clone is one `git push` away from being remote.

### Bug fixed en route

`orchestrator._deploy_push` stamped `"status": RunStatus.DONE` over the runner's return
unconditionally — a FAILED verdict from `RunFailureHandler.handle` (secret block,
forbidden branch) was overwritten to DONE in the checkpoint while the DB row said
FAILED. It now passes FAILED through. See problems-log 2026-07-16.

## Alternatives considered

- **Sandbox-internal branch ("commit but don't push")** — rejected: the sandbox is
  destroyed on terminal, so the branch would be stranded in a throwaway container;
  reject-at-gate + the diff artifact already covers "don't ship".
- **Per-repo `local_repo_path` in the registry** — deferred: a per-run field needs no
  migration/registry UI and matches how the mode is chosen (per run). Promote to the
  registry if repeated typing becomes annoying.
- **`docker cp`/volume-mount instead of bundle** — rejected: copying `.git` wholesale is
  fragile and the mount couples the sandbox to host paths; a bundle is git's own
  transport for exactly this.

## Consequences

- The dashboard run list shows each run's mode as a chip (user requirement: "each run
  should have a label — dry run, PR or local push only").
- Old runs have `runs.deploy_mode = NULL` → no label; old checkpoints carry the dead
  `skip_deploy`/`auto_create_pr` keys, which the TypedDict simply ignores (extra keys are
  dropped on read; DB wipes are routine here per the backcompat policy).
- `POST /runs` clients must send `deploy_mode`/`local_export_path` instead of
  `skip_deploy`/`auto_create_pr` — the CLI (`main.py`, being deleted per ADR 0013) was
  not updated.
