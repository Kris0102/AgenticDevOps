# 0024 — Repo registry: repos are first-class, validated, canonical (GH #41)

Status: accepted
Date: 2026-07-06

## Context
A repo was a raw URL pasted into the new-run form on every run. Consequences:
- The same PAT had to live globally or be unusable per repo; private repos with different
  tokens weren't representable.
- No validation until `git clone` failed mid-bootstrap — a typo'd URL or expired token
  cost a sandbox spin-up before failing.
- `Repo.base_ref` existed on the blackboard but was **dead**: the dashboard hardcoded
  `"main"` and `GitOps.clone_and_checkout` never read it. Every run worked off the
  default branch (plus optional `base_sha` pin).
- Lessons + code_chunks are keyed by **verbatim** `repo_url` (ADR 0018) — `.git` /
  trailing-slash spelling variants silently split one repo's cross-run memory.
- Every deploy pushed to `agent/run-<run_id>` with no way to choose a different target,
  and nothing structurally prevented a future caller from pushing to `main`.

## Decision
Repos become first-class registry entities (`repos` table, `db/repo_store.py`),
managed separately from run creation:

1. **Canonical URL keying.** `normalize_repo_url` (https-forced, SSH-form converted, no
   trailing `.git`/`/`, lowercase host) runs on add AND on the legacy raw-repo run path.
   The migration (`f6a7b8c9d0e1`) backfills existing `runs`/`lessons`/`code_chunks`
   rows, merging pre-registry memory under canonical keys.
2. **Token-resolution chain:** per-repo token (Fernet-encrypted at rest, masked on read —
   same machinery as provider keys, ADR 0021) → global Settings `github_token` (env
   fallback inside) → anonymous. Resolved via `RepoStore.resolve_token(repo_id)` at the
   two gitops seams (bootstrap clone, deploy push). Raw tokens never cross the API.
3. **Validate-then-create + run preflight.** `GET /repos/{owner}/{repo}` with the resolved
   token (in `gitops/github_api.py`, ADR 0012 boundary) gates both repo registration and
   `POST /runs` — mirror of the LLM preflight (ADR 0020): failure → 4xx, no run created,
   validation snapshot stored (status dot). GitHub's 404 is inherently ambiguous
   (not-found vs private-without-access); the reason text says so instead of pretending.
4. **`base_ref` gets real meaning.** Branch pickers (live `GET .../branches`, never
   DB-cached) feed `Repo.base_ref`; clone runs `git clone --branch <base_ref>`. The field
   became `Optional` — `None` = default branch — because honoring the previously-ignored
   hardcoded `"main"` would break `master`-default repos.
5. **Push-target override is per-repo only**, default stays `agent/run-<run_id>`. Pushing
   to the repo's **default branch is hard-refused** (no confirm flow): rejected at
   add/update, re-checked in the deploy node (routes through `RunFailureHandler`), and
   enforced last inside `GitOps.push_branch` so it holds regardless of caller.

Runs keep the `repo_url` snapshot (history self-contained) plus a nullable, FK-less
`repo_id`; deleting a registry entry never breaks past runs. The new-run form is a
registry picker only (no free-text escape hatch); `RunRequest` accepts exactly one of
`repo_id` (new) / `repo` (legacy, kept alive for CLI/scripts during the CLI deprecation).

## Rejected alternatives
- **Per-run push-target override** (issue asked "per repo / per run"): no current
  consumer — PR flow is still stubbed — so it's UI + state plumbing for nothing. Revisit
  with `create_pr`.
- **Confirm-flow for default-branch pushes**: over-engineered for a single-user localhost
  tool; a hard refusal is simpler and strictly safer.
- **Caching branch lists in the DB**: branches churn; only `default_branch` is stored.

## Consequences
- Old clients sending `base_ref: "main"` inside a raw `repo` object now get a real
  `--branch main` clone — loud failure on `master`-default repos (accepted: CLI is being
  deleted; the dashboard no longer sends it).
- `create_pr` (future) gets its natural inputs from the registry: head = push branch,
  base = the run's `base_ref`/default branch.
