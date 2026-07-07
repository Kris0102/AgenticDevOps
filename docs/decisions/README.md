# Architecture Decision Records (ADRs)

This folder is the project's **persistent memory of *why*** — the trail that survives
across coding-agent sessions and human context-switches. `CLAUDE.md` says *what is true
now* (current status, topology, schema); this folder says *why it's true* and *what we
already considered and rejected*. Read both — `CLAUDE.md` rots fast, ADRs are append-only
and dated, so they stay correct even when the code has moved on.

## When to add one

Any time a non-obvious architecture/implementation decision is made or revisited:
- Choosing or replacing a component (e.g. "why Postgres-JSON blackboard, not event-sourced")
- Resolving a spec-vs-code drift one way or the other
- Rejecting a tempting alternative (record *why*, so it isn't re-proposed and re-debated)
- Any audit finding from a `critical-architect` pass that gets actioned (or explicitly deferred)

Skip it for: routine bugfixes, refactors, anything `git log` already explains well.

## Format

One file per decision: `NNNN-short-title.md`, sequential, never renumbered or deleted
(superseded records stay — link forward/backward instead).

```markdown
# NNNN — Title

Status: proposed | accepted | superseded by NNNN | rejected
Date: YYYY-MM-DD

## Context
What prompted this. What constraints/properties of the MAS (auditability, safety,
reproducibility, HITL — see CLAUDE.md "Key architecture decisions") are in play.

## Decision
What we're doing, precisely enough that a future session can verify it's still true.

## Alternatives considered
What else we looked at and *why we didn't pick it* — this is the part that saves the
next session from re-litigating the same debate.

## Consequences
What this makes easier, harder, or newly possible. What it costs.
```

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-architecture-audit-2026-06-07.md) | Architecture audit findings & remediation order | accepted |
| [0002](0002-design-clarifications-execution-model.md) | Design clarifications: sequential execution, Reviewer scope, HITL placement, optional Auditor | accepted |
| [0003](0003-event-logger-centralization.md) | Centralize Event-row construction in `EventLogger`; close audit finding #3 (gateway mid-transaction commit) | accepted |
| [0004](0004-pr9-review-followup-deferrals.md) | PR #9 review follow-up: snapshot persistence deferrals (RunStateStore generalization, cost, merge_state) | accepted |
| [0005](0005-bootstrap-outside-checkpointed-graph.md) | Migrate to LangGraph PostgresSaver checkpointer; run Bootstrap outside the graph | accepted |
| [0006](0006-hitl-via-native-interrupt.md) | Mid-run HITL gates via native LangGraph `interrupt()` (plan/code/test, stage-dependent reject) | accepted |
| [0007](0007-investigation-stays-unstructured.md) | Investigation phase stays free-text; synthesis is the sole structured contract (rejected `response_format` on investigator) | accepted |
| [0008](0008-coder-single-react-loop-grounded.md) | Coder: single ReAct loop, pre-fetched grounding, deterministic diff verification, microtask attempt limit | accepted |
| [0009](0009-per-role-permissions-classes.md) | Per-role `RolePermissions` classes replace global `ROLE_PERMISSIONS`/`ALLOWED_SANDBOX_CMDS`; Coder loses `pytest` | accepted |
| [0010](0010-context-engine-pgvector-semantic-memory.md) | Context Engine: pgvector code semantic memory (baseline + workspace), local MiniLM embeddings, tree-sitter chunking, `semantic.search` Planner tool | accepted |
| [0011](0011-plan-reviewer-and-planning-loop-cap.md) | Plan Reviewer: two-phase investigate+review, structured approve/reject, planning-loop cap | accepted |
| [0012](0012-deploy-node-gitops-stub.md) | Deploy node + GitOps boundary: terminal deploy/deploy_push push sandbox to `agent/run-<id>`, HITL-gateable | accepted |
| [0013](0013-control-api-and-dashboard-backend.md) | Control API + dashboard backend: HITL over HTTP, review context (plan/diff), live logs via structlog→SSE, history via `events.seq`; right-sized to self-hosted single-user | accepted |
| [0025](0025-token-limits-and-usage-tracking.md) | Per-call token cap, token usage tracking (debug files/logs/events), and a global per-run token budget enforced FAILED in every node | accepted |
