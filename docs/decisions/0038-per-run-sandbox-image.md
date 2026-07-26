# 0038 — The sandbox image is per-run state, not a constant

Status: accepted
Date: 2026-07-26

Amends ADR 0016 (tester two-track grounding) and ADR 0037 (unverified pass-through) by
removing the condition that made them inert on benchmark runs. Diagnosed while preparing the
first full SWE-bench batch.

## Context — grounded verification was structurally impossible for SWE-bench

The system's founding claim is *grounded verification*: code is accepted only when
deterministic checks pass. On SWE-bench that claim was empty, and not because of a bug in the
Tester.

Every run — benchmark or dashboard — executed inside `agentic-sandbox:0.1`:
`python:3.14-slim` plus `git`, `node`, `gitleaks`, `pytest`. Nothing ever installed the target
repository's dependencies (the only dependency install in the tree is `npm install` for JS
subprojects in `deterministic_checks_phase`). So Track A would detect the repo's suite, run
it, and collect import errors.

That is not a missing-package problem. SWE-bench instances target **Python 3.9**
(`DEFAULT_DOCKER_SPECS`); our image ships 3.14. A bootstrap `pip install -e .` could not have
fixed it. The interpreter was wrong.

The consequence chained into ADR 0037: with Track A erroring and Track B unable to test
meaningfully, the verdict fell through to "supported project, nothing decided" → SKIPPED,
UNVERIFIED pass-through. Correct behaviour given the evidence, but it meant the Coder wrote
patches with **no real test feedback** and the Reflector diagnosed failures from a suite that
had never run.

Two container layers were being conflated, including in our own README:

| Layer | Image | Purpose |
|---|---|---|
| Generation (the MAS produces a patch) | `agentic-sandbox:0.1` | no repo deps, wrong Python |
| Grading (`swebench.harness.run_evaluation`) | `swebench/sweb.eval.x86_64.<instance>` | correct per-repo conda env |

Only the grader had a working environment. "We run the SWE image so there are no dependency
issues" was true of grading and false of generation.

The structural blocker: the image and workdir were **hardcoded string literals in ten
places** — `BootStrapper.IMAGE`, five node `_init_*` methods, the deploy bridge, four
`Service` sites. A per-run container shape could not be expressed at all.

## Decision

### 1. `SandboxSpec` on the blackboard; one factory

`SandboxSpec` (`image`, `workdir`, `repo_dir`, `path_prepend`, `network_mode`) is carried on
`BlackboardState` and settable via `RunRequest.sandbox_spec`. `sandbox.factory.sandbox_for(state)`
is the single construction site for `DockerSandboxTools`.

Defaults reproduce the historic values exactly (`agentic-sandbox:0.1`, `/workspace`, repo in
`repo/`), so a normal dashboard run is unchanged — verified by regression-testing the clone
path. Checkpoints written before the field existed load and fall back to the defaults.

The spec is read from state, never re-derived: a run's container shape is fixed at creation
and must survive a resume days later, with every node agreeing on it.

### 2. Pre-provisioned repos are DERIVED, not flagged

A benchmark run points at an image that already contains the repo checked out with its
dependency environment installed. Bootstrap decides whether to clone by **probing for
`<workspace_path>/.git` in the container**.

Rejected: a `pre_provisioned: bool` on the spec. The container already answers the question;
a flag would be a second source of truth that can disagree with reality.

Cloning over a provisioned image would be actively wrong, not merely wasteful: the bare
`base_commit` lacks the image's build fix, so the pre-installed environment would no longer
match the tree.

Adoption is verified — `base_sha` must be an **ancestor of** HEAD, not equal to it, because
SWE-bench images add one commit on top of `base_commit` (a `pyproject.toml` build fix). A
`base_sha` that isn't reachable means the image doesn't match the instance, and that fails the
run loudly rather than silently benchmarking the wrong tree.

### 3. `path_prepend`, not a shell wrapper

The instance's interpreter lives in a conda env. Rather than wrapping every command in
`source /opt/miniconda3/bin/activate && conda activate testbed`, the env's `bin` is prepended
to `PATH` at container creation, resolved against the **image's own declared PATH** (read from
the image config — a guessed PATH silently breaks every command).

Verified sufficient: `python -V` → 3.9.20, `import astropy` → 5.1.dev623, and
`pytest astropy/modeling/tests/test_separable.py` → **11 passed**, all through
`DockerSandboxTools`.

### 4. Image names come from swebench's resolver

The adapter calls `make_test_spec(instance, namespace="swebench").instance_image_key`. The
name embeds an arch, a tag, and a `__` → `_1776_` substitution that applies only to remote
images. Reimplementing it would silently drift from whatever the installed swebench version
grades against.

### 5. `Constraints.allowed_network` deleted

The field was set to `"none"` by every caller and read by nobody; the sandbox ran on Docker's
default bridge with **unrestricted egress**. It was also unenforceable as written — cloning
needs egress, so `"none"` would break any run that clones.

`SandboxSpec.network_mode` replaces it and is actually plumbed through to `containers.run`.
With a pre-provisioned image there is no clone, so `none` becomes genuinely available there
(`--sandbox-network`).

## Consequences

- The Tester executes real suites on benchmark runs. ADR 0037's UNVERIFIED pass-through stops
  being the default outcome and returns to being the exception it was designed for.
- Patch extraction is unchanged and was already correct: the harness applies our patch onto
  the same image state, so `git diff --cached HEAD` is the right base. `.gitignore` is now
  excluded from patches, and bootstrap no longer seeds it onto a pristine provisioned tree
  (that would dirty a tracked file and leak into every extracted diff).
- **Disk is the scaling limit.** Instance images are ~4GB each. ~40 fit in the current 927GB
  of free space; all 500 Verified instances do not. Prune between batches.
- Resolve rates from `--generic-sandbox` runs and image-backed runs are **not comparable** —
  the former have no verification signal. Comparison runs must say which they used.
- Egress is still open by default. Post-clone network disconnect is the real hardening and is
  **not** implemented.
- Not addressed: normal (non-benchmark) runs against arbitrary repos still have no dependency
  environment, so their Track A remains weak. The per-run spec makes a fix expressible; no
  fix is included here.
