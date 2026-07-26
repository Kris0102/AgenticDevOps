# 0039 — Per-repo sandbox image, with a validated image contract

Status: accepted
Date: 2026-07-26

Extends ADR 0038 (per-run sandbox image) from benchmark-only to a first-class, user-facing
option. ADR 0038 made the container shape expressible; this ADR makes it *reachable* — and
adds the guard rail that a naive version of the feature would have needed anyway.

## Context

ADR 0038 fixed the Tester's environment for SWE-bench by pointing the sandbox at a
pre-provisioned per-instance image. It explicitly left a gap open:

> Not addressed: normal (non-benchmark) runs against arbitrary repos still have no dependency
> environment, so their Track A remains weak.

That gap is the same disease, not a lesser one. A user points the MAS at their Django project;
the sandbox is `python:3.14-slim` with no Django, no fixtures, no services. Track A finds their
pytest suite, runs it, collects import errors, and the verdict falls through to ADR 0037's
UNVERIFIED pass-through. *Grounded verification — the system's central claim — is weak for real
repos, not just benchmark ones.*

Meanwhile most serious projects already publish a CI or devcontainer image with exactly the
right toolchain. Reconstructing that environment ourselves (a bootstrap `pip install`) is both
harder and worse than letting the user point at the image that already exists.

## Decision

### 1. `sandbox_image` on the repo registry, not the New Run form

The image is a property of the **repo** ("this project needs Python 3.9 and postgres"), not of
an individual run. `RepoRecord.sandbox_image` (migration `c4d5e6f7a8b9`, nullable) follows the
precedent `push_branch_override` already sets: registry-level default, with a per-run override
available for experiments.

Resolution, in `Service.resolve_sandbox_spec`, mirroring `_resolve_push_target`:

1. `RunRequest.sandbox_spec` — a fully-specified shape from a scripted caller (the SWE-bench
   adapter). Passed through untouched; it knows its own workdir and PATH.
2. The registry's `sandbox_image` — **only the image changes.** `workdir`/`repo_dir` keep their
   defaults, because the repo is still cloned to `/workspace/repo` as usual. The image supplies
   the *environment*, not the code.
3. `None` → `SandboxSpec()` defaults (the generic `agentic-sandbox:0.1`).

The two shapes ADR 0038 identified — "clone into a custom image" and "the image already
contains the repo" — need no mode switch between them: bootstrap probes for
`<workspace_path>/.git` and adopts or clones accordingly.

### 2. The image contract is explicit, and checked by behaviour

The sandbox silently requires things that were free while the image was ours alone:

| Requirement | Why |
|---|---|
| `timeout` accepting `--kill-after` | `sandbox_exec` wraps **every** command in `timeout --kill-after=5` |
| `sh` | bootstrap seeds `.gitignore` via `sh -c` |
| `git` | clone, `repo.diff`, patch extraction |
| `sleep` | the container is held open with `sleep infinity` |
| a usable `workdir` | everything runs there |

`sandbox/preflight.py` validates these at **save** time (`ValueError` → 422, the existing
convention), pulling the image if absent. All violations are reported in one pass, each with
the reason it matters, so the user fixes the image once instead of discovering the next problem
on the next rejected run.

**Presence is not the contract; behaviour is.** busybox (Alpine) ships a `timeout` that exists
and then *rejects* `--kill-after` — verified: it prints usage and exits 1. A `command -v`
check therefore passes and **every subsequent sandbox command fails**, which is precisely the
baffling mid-run failure the preflight exists to prevent. The timeout check runs the real
invocation instead.

Two related findings, both verified rather than assumed:

- An image declaring its own `ENTRYPOINT` (e.g. `ENTRYPOINT ["python"]`) turns our keep-alive
  into `python sleep infinity`; the container **exits immediately** and every later exec fails
  against a dead container. `sandbox_create` and the preflight both pass `entrypoint=[]`. (ADR
  0038 removed this as a no-op — correct for the two images that existed then, wrong the moment
  an arbitrary image can be named.)
- `python:3.12-slim` — the obvious thing a user types — has **no git**. Rejection with a
  precise reason is the feature working, not a false positive.

### 3. Error handling: every failure is a value, and every failure is logged

The preflight never raises for an unusable image — it returns an `ImagePreflightResult`, and the
service turns a failing one into `ValueError` → 422. Docker exceptions are caught and translated
at the boundary, because the raw ones are unusable as user-facing text: docker-py stringifies as
`404 Client Error for http+docker://localhost/v1.55/images/create?…: Not Found ("<detail>")`, and
only `<detail>` means anything. `_docker_detail` extracts it.

| Situation | Detected as | Reported as |
|---|---|---|
| present locally | `images.get` succeeds | ok |
| absent locally, exists in registry | `NotFound` → `images.pull` succeeds | ok, `pulled=True` |
| no such repository, **or a bad tag on a real one** | `docker.errors.NotFound` | "not found — no such repository or tag, locally or in the registry" |
| malformed reference | `APIError` | "invalid reference format: repository name (Name) must be lowercase" |
| unauthorized / other registry | `APIError` | the registry's own message |
| no shell or coreutils (`hello-world`, scratch, distroless) | `APIError` at **start**, `executable file not found` | "`sleep` is missing … the sandbox needs a general-purpose base image" |
| starts, but missing `git` / broken `timeout` | contract probe | every violation, each with why it matters |

Four traps here were only found by probing each shape rather than reasoning about the code, and
each was a real defect:

1. **`ImageNotFound` is a subclass of `NotFound`.** A bad *tag* on a real repository raises plain
   `NotFound`, so catching only `ImageNotFound` sent bad tags down the generic handler and leaked
   a raw HTTP 404. Catch `NotFound`.
2. **"No usable binaries" is only detectable at container start**, as an opaque runc `APIError` —
   the `status != "running"` branch never runs, because `containers.run` itself raises.
3. **A marker match against already-truncated text silently never fires.** The
   `executable file not found` marker sits deep in a long runc trace, past the 200-char cut, so
   the translation has to match the full exception string.
4. **`containers.run` leaks a container when *start* fails.** It creates, then starts, and on a
   start failure raises without returning the handle — while the created container stays behind.
   Three orphans proved it. Use `create()` + `start()` so the handle is always bound before
   anything can fail and `finally` can always clean up.

Logging: every failure goes through one `_fail` helper that logs
`sandbox.image_preflight_failed` (WARNING) with the image, whether it was pulled, and the
problems/reason — so a rejection always leaves an operator-side trace, not just a 422 the user
sees. Successes log `sandbox.image_preflight_ok`; pulls log `sandbox.image_pulling`. There is
deliberately **no durable `events` row**: events are run-scoped and a repo save has no run.

### 4. Pull progress is streamed, because a blocking check reads as a hang

Validating means pulling, and a multi-GB pull takes minutes during which the save request just
blocks — visually identical to a crash. `GET /images/preflight/stream` runs the same check over
SSE (reusing the pattern `/runs/{id}/logs/stream` already established), and the dashboard's
**Check image** button watches it. By the time the user saves, the image is local and the
save-time check returns immediately. It is an **affordance, not a gate** — skipping it is fine,
the save still validates.

`stream_check` is the primitive; the blocking `check()` consumes it. One implementation, so the
streamed and blocking paths cannot drift.

Docker's pull events are per-layer and interleaved, which makes an honest aggregate percentage
harder than it looks. Four defects, each found by watching a real 400MB pull rather than by
reading the code:

1. **The percentage regressed** (`100% → 20% → 40% …`). A small first layer completes before
   later layers announce their size, so the *denominator grows*. Pinning finished layers — which
   an earlier comment claimed was sufficient — fixes the numerator and does nothing for this.
   Fix: withhold the number entirely until every announced layer has a size or has finished
   (report indeterminate, not a lie), plus a monotonic clamp so any remaining surprise shows as
   a stalled bar rather than a rewinding one.
2. **The stage label flapped** between `downloading` and `extracting` on nearly every frame,
   because it came from the *most recent* event while layers run concurrently. Fix:
   least-advanced-wins across per-layer stages — we are "downloading" until every layer has
   finished downloading.
3. **`Download complete` mapped to `downloading`**, which under least-advanced-wins pinned the
   overall verb to "downloading" forever, so `extracting` never appeared — defeating the entire
   purpose of the stage table. It maps to `extracting`.
4. **The tag was counted as a layer.** Docker's first line is
   `{"status": "Pulling from library/python", "id": "3.12"}` — that id is the *tag*, and it never
   reports a size or a completion, so the "still sizing" test stayed true forever and suppressed
   the percentage entirely. Only statuses in the stage table count as layer events.

Verified end to end on a cold 400MB pull:
`looking → downloading (0→100%) → extracting → pulled → starting → result`, **0 regressions**,
~100 frames (roughly one per percent — emitting every docker event would flood the stream).

The UI derives its label from the streamed verb rather than the phase, because during extraction
the phase is still `pulling` and a fixed "downloading" label sits at 100% looking stuck. While
Docker has reported no totals the bar is deliberately **indeterminate** (a sliding sliver), since
a bar pinned at 0% also reads as stuck.

### 5. The image a run used is shown on the run, like the repo is

`runs.sandbox_image` (migration `d5e6f7a8b9c0`, nullable) records the image the agents actually
ran in, surfaced as a badge on the run list and in the run overview beside model and deploy mode.

Denormalized onto the row rather than read from the checkpoint, for the same reason
`llm_model` and `deploy_mode` already are: `get_runs` renders the whole list from ONE SQL query,
and a `graph.get_state` per row is far too expensive for a list scan.

Written in bootstrap from the **resolved spec**, not from the registry — so the label stays
truthful if the repo's configured image is changed, or the repo deleted, after the run finished.
A run is a historical record; re-deriving its environment from current config would let that
record silently change.

NULL means "predates the column". Those runs did all use the generic image, but it was never
recorded, so the UI shows nothing rather than back-filling a claim; new runs always store the
real value, including the default (rendered "default sandbox"). Display helpers live in
`dashboard/src/imageLabel.ts`, shared by the registry page and the run views so one image reads
identically in both.

Why it earns space in the UI: this is the difference between a real Tester verdict and an
UNVERIFIED one. When a past run's results look wrong, "which environment did it run in" is the
first question, and it was previously unanswerable without digging into a checkpoint.

### 6. The repo root is per-run state for AGENTS too, not just the sandbox

ADR 0038 made the container's path configurable and stopped there. Every agent still hardcoded
`/workspace/repo`, so the first real benchmark run BLOCKED with exit 127 on every command and
reported itself as a broken environment. See the 2026-07-26 row in `docs/problems-log.md`.

`sandbox.factory.repo_root(state)` is now THE source of that path for agent code. It is
injected by each node into all five tools classes and into `Planner`/`Reviewer`; the four
prompt constants became builders taking `repo_root`; `quality._normalize_repo_path` re-roots to
the run's root; `DockerSandboxTools` carries `repo_dir` and exposes `workspace_path` so
`repo_diff` and the gateway derive their cwd instead of assuming `<workdir>/repo`.

**Nothing agent-side may hardcode the repo path again.** The `DEFAULT_REPO_ROOT` constants that
remain are defaults for callers with no state, not the value to use.

The planner and the reviewer must be given the SAME root — telling them different roots is
exactly the failure ADR 0022 fixed once already (the reviewer rejecting correct paths).

**The lesson about verification, recorded because it is the reusable part:** ADR 0038 was
verified at the sandbox layer — bootstrap, patch extraction, a direct `pytest` through
`DockerSandboxTools`. All three bypass the agent tool layer, which is where the constants
lived, so all three passed while the feature was broken end to end. A change to where the repo
lives must be verified by *running an agent*, not by exercising the layer beneath it.

### 7. Not re-validated at run start

The image is contract-checked when saved, not on every run start: a run must not pay a
container probe before it begins. A since-deleted image is re-pulled by `containers.run`; a
since-broken one fails in bootstrap with the sandbox error it deserves.

### 8. Rejected

- **Building images from a repo's Dockerfile.** Build context, caching, build args, and a whole
  new failure class, to produce something the project's CI usually already publishes.
- **Exposing `repo_dir` / `path_prepend` in the UI.** For the common case they are wrong to
  touch — the repo is still cloned to the usual place and the image's system interpreter is
  already correct. They exist for the pre-provisioned case, which is benchmark-shaped.
- **Allowlists, signature verification, registry credential management.** This is a self-hosted
  single-user tool that already executes arbitrary repo code and LLM-authored commands in the
  sandbox; a user-typed image name is not a meaningful escalation, and the user typing it is the
  authorization. The UI says images run with the sandbox's privileges; that is proportionate.
  Importing SaaS-scale supply-chain machinery here would be cost without a matching risk.

## Consequences

- Grounded verification becomes reachable for ordinary repos, not just benchmark instances: a
  user can give the Tester an environment where their suite actually runs.
- A bad image is a 422 naming the missing pieces, at the moment it is typed — not a run that
  fails four minutes in with an unexplained exec error.
- The registry path deliberately cannot express the pre-provisioned shape (it only sets
  `image`). A caller that needs `/testbed`-style layout uses `RunRequest.sandbox_spec`.
- `agentic-sandbox:0.1` remains the default, and the contract check documents what a
  replacement must provide. Anything we add to the sandbox's assumed toolchain must be added to
  `REQUIRED_BINARIES` or it becomes an undocumented, unenforced requirement again.
- Not addressed: images are pulled but never pruned. Private registries needing credentials also
  cannot work — `images.pull` is called without auth, so the registry's 403 is surfaced verbatim;
  public images and anything already local are unaffected.
