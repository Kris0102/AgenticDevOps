# 0035 — Deploy options per run: push branch, auto-PR, and a reachable commit-message default

Status: accepted
Date: 2026-07-14

Amends ADR 0027 (deploy options), ADR 0012 (GitOps boundary) and ADR 0029 (run-start rights).

## Context

Two deploy defects, found together.

**1. The default commit message was unreachable.** ADR 0027 specified the precedence
"deploy-gate approve input › per-run `RunRequest.commit_message` › a default". A detailed
default was built from the run (`_create_detailed_commit_message`: objective title as
subject, objective/microtasks/changed files as body). In practice every gated run
committed `AgenticDevOps run <id>` instead.

The cause was not in the builder — it was in the gate. `Service._hitl_context` prefilled
the UI's editable commit-message box with:

```python
(state.get("deploy_commit_message") or "").strip() or f"AgenticDevOps run {run_id}"
```

That string is `GitOps.push_branch`'s **last-resort fallback**, not the default. The human
saw it in the box, approved, and `HitlGate` wrote it back into `deploy_commit_message` —
which made the deploy node's `state.get("deploy_commit_message")` non-empty, so the
detailed builder was never called. The prefill's own comment claimed it was "the message
the push WOULD use". It was a placeholder impersonating a default, and because the human
submits the box verbatim, the placeholder always won.

**2. The push target was not overridable per run.** ADR 0012 pushes to
`agent/run-<run_id>`; ADR 0024 added a per-repo `push_branch_override` in the registry.
There was no per-run control — a hotfix or a one-off target meant editing the registry
entry and remembering to put it back.

## Decision

### Commit message: one builder, two callers

`gitops/commit_message.py::build_commit_message` — deterministic, no LLM, sibling of
`pr_body.py` (ADR 0027's precedent). Called by **both** the deploy node (the message it
pushes) and `Service._deploy_message_preview` (the message the gate prefills). Since the
human submits the prefill verbatim, the only way the prefill can be honest is for it to be
the same function; anything else is a second source of truth for a string the first source
claims to own.

Precedence is unchanged from ADR 0027 — gate input › per-run › default — but the default
is now the built message rather than a stand-in, so approving the gate untouched commits
what the gate displayed.

Consequences:
- The preview needs the changed fileset, so it shells into the sandbox. `GET /runs/{id}/hitl`
  is fetched once on gate open (not polled), so this is one `docker exec` per gate. It
  degrades on failure — a dead sandbox drops the file list rather than breaking the gate a
  human is waiting on.
- The deploy node passes the fileset it already computed instead of re-shelling for it.
- The gate's `<input type="text">` became a `<textarea>`: the real default is a multi-line
  git message (subject, blank line, body), which a single-line input silently flattens.

### Push branch: `RunRequest.push_branch`, validated at creation

New per-run override, carried on the blackboard as `Repo.branch_to_push_to`. Precedence:

    RunRequest.push_branch  ›  registry push_branch_override  ›  agent/run-<run_id>

Validated in `Service._validated_push_branch` at **run creation** (`ValueError` → 422, no
run created) — rejecting the repo's default branch and malformed names. Validating here
rather than at deploy is the point: the deploy node is the last step of a completed run, so
a bad branch discovered there wastes the entire run.

`DeployRunner._resolve_push_target` re-checks and **degrades** rather than fails: a target
that equals the default branch falls back to `agent/run-<run_id>` with a warning. This can
only fire if a validated value went stale afterwards (e.g. the default branch was renamed
onto it), and by then the run is finished — landing the work on the always-safe run branch
beats throwing it away. The check sits outside the `repo_id` block so a per-run override on
an *unregistered* repo is guarded too; the first draft checked only the registry path.

Pushing to the default branch stays guarded at four layers (creation 422 → resolve degrade
→ `DeployRunner.invoke` refusal → `GitOps.push_branch` refusal). Redundant on purpose: it
is the one outcome that must never happen, so no layer trusts the one above it (ADR 0012).

### Auto-create-PR: per-run, resolved once at creation

`RunRequest.auto_create_pr` (`None` = follow the global `Settings.defaults.auto_create_pr`),
surfaced as a checkbox on the New Run form seeded from that global — same shape as the
per-run model/gate overrides.

`Service.resolve_auto_create_pr` is the single resolver, and it feeds **both** consumers:
the run-start PR-rights preflight (ADR 0029) and `BlackboardState.auto_create_pr`, which is
what the deploy node now reads. This changes the flag from "read live at deploy" (its
original design, still true of the sibling `teardown_on_terminal`) to resolve-at-creation,
because the two consumers must agree: run-start refuses a run whose token cannot open a PR,
so re-reading the global at deploy would let a mid-run settings flip attempt a PR the run
was never validated for — or preflight-reject a run that would not have opened one. Fixing
the value also makes a run reproducible across a settings change and across a stall→continue.

Turning it on makes PR rights a **run-start requirement**: the run is refused with 422
rather than discovering at deploy that the token cannot open a PR. Turning it off per-run is
equally supported (global on → this run pushes without a PR), and a dry-run needs neither
push nor PR rights.

## Alternatives considered

- **Keep the builder on `DeployRunner` and have the service instantiate one for the
  preview.** Works, rejected: it drags a `GitOps`/sandbox-owning object into a read path to
  reach one pure string function, and leaves the message's shape owned by a node.
- **Prefill the gate with an empty box.** Fixes the bug (an empty submit falls through to
  the default) but hides the message from the human at the exact moment they are approving
  it — a worse gate for the same fix.
- **Prefill without the file list** to avoid the sandbox call. Rejected: it reintroduces the
  original defect in miniature — the box would again show something other than what gets
  committed.
- **Fail the run when the push target resolves to the default branch.** Loud, and matches
  `invoke()`'s existing hard refusal — rejected for the *resolve* step because creation-time
  validation already catches the real cases, leaving only stale config, where discarding a
  finished run's work is a disproportionate response.
- **Deploy-gate override for the branch too** (symmetric with the commit message). Deferred,
  not rejected: the branch decides preflight push rights at run creation (ADR 0029), so
  letting it change at the gate would mean re-validating access mid-run.

## Consequences

- Runs now get readable git history by default — subject = the objective title.
- `RunRequest.push_branch` is per-run and does not touch the registry, so a hotfix target
  no longer requires editing (and reverting) the repo entry.
- The deploy gate cannot silently diverge from the push again: both read one function. A
  future third caller must use it too.
