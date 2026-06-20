# 0012 — Stub GitOps deployer as a terminal HITL-gated node

Status: accepted
Date: 2026-06-19

## Context

The run produced verified code in the sandbox but had no way to get it out — GitOps
was "not started". We needed a first step: push the sandbox working tree to the remote
on a branch so a human can open a PR. `DockerSandboxTools.git_push` existed only as a
stub (remote-add + push, no staging/commit/identity).

## Decision

**All remote git lives in a dedicated `gitops.GitOps` class** (the GitHub boundary
from ARCHITECTURE §4.4) — `clone_and_checkout` (used by bootstrap), `push_branch`, and
the `create_pr` stub — with the token injected in one place (`_authenticated_url`).
`DockerSandboxTools` stays pure sandbox exec (keeps `repo_diff`, which is local
working-tree inspection for grounded verification, not a remote op). `GitOps.push_branch` sets a commit identity (a fresh container
has none, so `git commit` would fail), `git add -A`, commits (tolerating "nothing to
commit"), points an idempotent `auth-origin` remote (token injected only here), and
pushes `HEAD:refs/heads/agent/run-<run_id>` — the destination ref is fully-qualified
because HEAD is detached at base_sha, so git can't infer branch-vs-tag. No `git init` —
the sandbox repo always comes from `git clone` (bootstrap), so `.git` already exists;
initialization belongs to the clone step, not push. `GitOps.create_pr` is a stub
(returns None) until the GitHub App / token wiring lands — it gives PR creation an
obvious home next to push and keeps the token in one place.

**Deployer is a graph node, not a standalone call.** A push is outward and irreversible,
exactly what the checkpointed star topology + HITL policy exist to govern. Modelled as
the existing producer→hitl→actor pattern (mirrors plan_reviewer→hitl→coder), split into
two nodes:
- `deploy` — gate producer: emits `READY_TO_DEPLOY` via `HitlGate.apply(Phase.DEPLOY)`.
  When `deploy` is in `policy.hitl_checkpoints` the push pauses at the shared `hitl`
  node; otherwise it passes straight through.
- `deploy_push` — actor: `DeployRunner(state).invoke()` → `GitOps.push_branch` +
  `GitOps.create_pr` (stub), branch = `agent/run-<run_id>`.

Routing: `tester DONE → deploy → (hitl) → deploy_push → END`. Deploy reject →
`GateSpec.reject = DONE` → END (run finished, un-deployed); no loop-back phase, so
`_write_feedback` returns `{}` for `Phase.DEPLOY` (no Reflection/feedback seeded).

**Deployer bypasses the ToolGateway.** The gateway enforces a *per-LLM-agent* tool
allowlist + logs each variable tool call. Deploy is a fixed step, not an agent making
variable tool choices, so there is nothing to allowlist — it calls `DockerSandboxTools`
directly (same as the bootstrapper). To keep the audit trail it emits `gitops.pushed`
via `EventLogger` itself. Added `RunStatus.READY_TO_DEPLOY`, `EventType.GITOPS_PUSHED`,
`Category.DEPLOYER`.

## Alternatives rejected

- **Standalone `DeployRunner.invoke()` outside the graph.** Rejected: loses checkpoint
  resume, graph-level audit, and the HITL gate — the whole reason to govern an outward
  push.
- **Single re-entrant `deploy` node** (interrupt inside it, push on resume). Rejected:
  scatters `interrupt()` out of the single shared `hitl` node (ADR 0006). The
  producer/actor split keeps one interrupt site.
- **`git init` inside `git_push`.** Rejected: redundant on a cloned repo; conflates
  init (clone/bootstrap concern) with push.

## Consequences / follow-ups

- Token is injected into the `auth-origin` remote URL — it lands in the ephemeral
  sandbox `.git/config` only, and `sandbox_exec` doesn't log commands, so it stays out
  of logs. A push that errors *could* echo the URL on stderr; the named remote keeps the
  token off the `git push` line itself.
- `GitOps.create_pr` is a stub (no GitHub API yet); no webhook trigger, no CI relay,
  no merge, no `Run.status` "deployed" terminal beyond `DONE`. These are the next
  GitOps milestones and now have a home (`GitOps`) to land in.
- `deploy_push` doesn't re-verify the tree; it trusts the tester's `DONE`.
- A push failure still raises (crashes the run) rather than ending gracefully as FAILED.
