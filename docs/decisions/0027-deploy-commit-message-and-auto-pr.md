# 0027 — Deploy commit message (gate › per-run › default) + auto-create PR setting

Status: accepted
Date: 2026-07-08

## Context

The deploy push always committed as `AgenticDevOps run <id>` (hardcoded in
`DeployRunner.invoke`), and `GitOps.create_pr` was a stub that logged and returned
`None` (ADR 0012 left the GitHub write path for later). Two user-facing gaps:

1. The human had no way to name the commit
2. Runs ended with a pushed branch but no PR; opening one was a manual step every
   time, even though everything a PR needs (objective, microtasks, fileset) is
   already checkpointed run data.

Constraints in play: auditability (every deploy decision must land in the `events`
table), the star topology / node-return-dict state rule, and the principle that a
deploy whose push succeeded must never be retroactively failed by a cosmetic step.

## Decision

**Commit message — precedence: deploy-gate input › per-run field › default.**
- A new top-level blackboard key `deploy_commit_message` (same pattern as
  `test_rejection_note`: one producer, one consumer). Seeded from the optional
  `RunRequest.commit_message` at run creation (New Run form field).
- `HitlResume` gains `commit_message` — on APPROVE of the **deploy** gate,
  `HitlGate.resolve` routes it into state via the same feedback-merge mechanism
  `_write_feedback` uses on reject (`_approve_payload`, merged into the hitl node's
  return dict). The gate is the natural entry point: the human is already looking at
  the full cumulative diff and knows what the run actually changed.
- `DeployRunner` reads the field; blank/None → `AgenticDevOps run <id>`. The message
  is recorded in the `gitops.pushed` event payload; `GET /runs/{id}/hitl` surfaces
  the effective message so the dashboard prefills its editable input.

**Auto-PR — a global bool `RunDefaults.auto_create_pr` (default off), read live at
deploy time** (same pattern as `teardown_on_terminal`; UI toggle via the settings
schema, no DB migration — the settings row is one JSON blob).
- `GitOps.create_pr` is now real: `GitHubRepoClient.create_pull_request` POSTs
  `/repos/{owner}/{repo}/pulls` (Bearer token via the existing `resolve_token`
  chain; 15s write timeout vs. the 5s preflight GETs).
- **Signature fix**: `create_pr` takes the *actual pushed branch* + base instead of
  recomputing `branch_name(run_id)` — the old stub would have ignored a per-repo
  `push_branch_override` (latent bug, never fired because the stub returned early).
- PR base = `repo.base_ref or repo.default_branch or "main"`; PR title = first line
  of the commit message; PR body is **deterministic markdown** built by
  `gitops/pr_body.py` from checkpointed data (objective, microtask checklist,
  shipped fileset capped at 50, diff summary). No LLM call.
- **Failure semantics**: `PrCreationError` never fails the run — the push already
  landed. It degrades to a `deployer.pr_create_failed` log + `pr_error` in the
  `gitops.pushed` payload; success emits `gitops.pr_created` and carries the URL
  onto `Repo.pr_url` via the node return dict. A 422 "pull request already exists"
  resolves to the existing open PR's URL (idempotent re-deploys); an empty fileset
  skips the PR (a guaranteed "no commits between" 422 otherwise).

## Alternatives considered

- **Commit message as a global Settings template** — rejected. A commit message
  describes one run's change; a global value produces identical, meaningless
  messages across runs. (A prefix/template scheme could be added later if wanted.)
- **Stalling the run at deploy and carrying the message via `/continue`** —
  rejected. STALLED is the crash/token-budget *recovery* channel (ADR 0021/0025);
  abusing it conflates error handling with normal flow. The deploy HITL gate is
  already the pause point directly before the push, with the diff on screen — the
  approve payload is the natural carrier and needed zero new routes.
- **Per-repo commit/PR settings on `RepoRecord`** (next to `push_branch_override`)
  — deferred, not rejected. Global toggle + per-run message covers the single-user
  deployment (mirrors the ADR 0024 scope choice).
- **LLM-generated PR description** — rejected. Deploy is a deterministic terminal
  step (no LLM call after the tester's PASS today); an LLM body adds cost, latency,
  a new failure mode, and unverifiable prose to an audit surface whose ground truth
  (objective, microtasks, fileset) is already structured state.
- **Failing the run when PR creation fails** — rejected. The push is the deploy;
  the PR is a convenience on top. A missing PR is recoverable by hand in seconds,
  a FAILED run with a successfully pushed branch is confusing state.

## Consequences

- Easier: meaningful commit history on proposal branches; zero-click PRs; the
  dashboard shows `pr_url` (already plumbed through `gitops.pushed`) and the exact
  commit message per run in the event timeline.
- The GitHub boundary (ADR 0012) now has its first write path; `GitHubRepoClient`
  is no longer read-only — its docstring and CLAUDE.md were updated.
- New knob to know about: `defaults.auto_create_pr` needs a token with PR
  permission; with the toggle on and an anonymous/underscoped token, every deploy
  will log `pr_error` (by design, not a failure).
- Ungated runs (no `deploy` checkpoint) can still get a custom message only via the
  per-run field — the gate input obviously requires the gate.
