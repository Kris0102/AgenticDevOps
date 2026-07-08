# 0029 — Require push access at run-start; add a dry-run (skip-deploy) mode

Status: accepted
Date: 2026-07-08

## Context
A completing run **always auto-pushes**: `tester DONE → deploy → deploy_push` (ADR 0012).
The deploy HITL gate (`deploy` in `hitl_checkpoints`) only inserts a human pause; with no
gate, `deploy_push` runs automatically. So the push is attempted on essentially every run
that reaches DONE.

Push needs a token with write access. But the token was effectively **optional**:
- A public repo clones anonymously, so a run could *start* and do plan/code/test with no token.
- `validate_repo` gated `ok` on push access **only when a token was present** — an anonymous
  request returned `ok=True, can_push=None` (never probed) and slipped through
  `resolve_repo` (which only raised on `not result.ok`).

Net: a token-less run would do all the agent work, then **fail at the final push**. The
optionality wasn't a designed "read-only mode" — there was no such mode — it was an
oversight that guaranteed a wasted run. Meanwhile the only legitimately push-free runs are
ones that fail/stall before DONE, or where a human turns the deploy gate on and rejects.

Two levers were on the table: force the deploy gate (a human pause on every run — kills
autonomous end-to-end), or require push up front and fail fast. Forcing the gate is only a
workaround for keeping the token optional; requiring push is the honest fix and keeps the
gate a genuine preference.

## Decision
1. **Push is required at run-start for a deploying run.** `Service._require_run_rights`
   requires `result.can_push is True` (not just `result.ok`) — this catches both the
   anonymous `None` and a token that can't push. Failure → 4xx at `POST /runs`, no run
   created, with an actionable reason ("add a token with push access, or start as a
   dry-run"). PR access is still required only when `defaults.auto_create_pr` is on.
2. **Dry-run / skip-deploy mode.** `RunRequest.skip_deploy` (default False) threads onto the
   blackboard; `_tester_router` and `_hitl_router` route `DONE → END` instead of `deploy`,
   so the run finishes at DONE with **no push, no PR**. A dry-run needs neither push nor PR
   rights — clone/read is enough, so `_require_run_rights` returns early and
   `_reject_if_unusable` accepts a clone-only (`NO_PUSH_ACCESS`) repo for it.
3. **Registration only needs read access.** `add_repo` accepts any *readable* repo
   (`_reject_if_unreadable`: `ok` OR `NO_PUSH_ACCESS`, both from a 200). A clone-only repo is
   registrable — usable for dry-runs, or pending a token — and shown orange "clone-only" in
   the dashboard (not a red failure). Not-found / no-read / unreachable still block. Push
   stays gated per-run, not at add time.
4. **The deploy gate stays optional.** Because push is guaranteed at start, auto-deploy is
   safe; the gate is a real user choice again, not a safety crutch. It is NOT forced.

`validate_repo` treats anonymous / push-lacking tokens as `NO_PUSH_ACCESS` (`ok=False`) via a
real write probe (`_check_push_access` — a side-effect-free ref-create against an impossible
SHA; `permissions.push` from the GET is not trusted, as it reflects account role, not a
fine-grained PAT's scope). `ok=False` there means "not push-usable," while the repo is still
readable — the split "readable vs push-usable" is resolved in `Service` (`_reject_if_unreadable`
for add, `_reject_if_unusable` + `_require_run_rights` for run-start).

## Alternatives considered
- **Force the deploy HITL gate on every run** so a no-push run stops cleanly at the gate
  instead of auto-failing the push. Rejected: removes fully-autonomous end-to-end runs (the
  point of the system) to work around keeping the token optional.
- **Keep push gated only inside `validate_repo`/`ok`** (so add + run both reject a token-less
  repo). Rejected: it makes a clone-only repo unregistrable, which breaks the dry-run flow
  for exactly the repos it targets. Registration is a read concern; push is a run concern.
- **Block every run on missing push, including non-deploying ones.** The current gate does
  block any *deploying* run; a dry-run is the explicit escape for read-only/exploration.
- **Leave it permissive (warn only).** The dashboard already warns "clone-only," but a run
  would still start and fail at the very end — the failure the user is trying to avoid.

## Consequences
- A token-less/underscoped repo fails at `POST /runs` with a clear reason, not after minutes
  of wasted agent work.
- Read-only / try-the-agent runs remain possible via `skip_deploy` (dashboard toggle, or the
  request field).
- The deploy gate is decoupled from safety — purely a review preference.
- `validate_repo`'s `ok` now means "push-usable," with readability recovered from the
  `NO_PUSH_ACCESS` kind; any future consumer must not read `ok` as "cloneable."
