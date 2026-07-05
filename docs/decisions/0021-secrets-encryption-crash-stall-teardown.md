# 0021 — Secrets encrypted at rest, crash→STALLED, settings-driven teardown

Date: 2026-07-05
Status: Accepted (implemented)

## Context
Three gaps surfaced in dashboard use: (1) the GitHub token still lived only in `.env`
while every other credential had moved to the settings hub (ADR 0020), and DB snapshots
(`scripts/snapshot_db.sh`) would have shipped raw keys once it moved; (2) an uncaught
crash in `_drive` wrote `Run.status=FAILED` while the checkpoint stayed non-terminal —
the runs LIST said FAILED, the run DETAIL said STALLED (with a continue bar), two views
disagreeing about the same run; (3) sandbox teardown was a global env flag read in an
odd place (planner node) and fired regardless of whether the run could still be continued.

## Decisions

### 1. Secrets encrypted at rest (Fernet), GitHub token in settings
`AppSettings.github_token` joins the provider `api_key`s: masked to the UI, stored as
`enc:<fernet-token>` in the `app_settings` row (`db/secrets.py`). Key resolution:
`SETTINGS_ENCRYPTION_KEY` env, else an auto-generated `.settings_key` file (0600,
gitignored). Legacy plaintext values pass through on read and are re-encrypted on the
next save; an undecryptable token degrades to None (re-enter in UI), never to using
ciphertext as a key. Threat model is honest: this protects **DB dumps, snapshots, and
pgAdmin browsing** — not an attacker who owns the machine (key sits beside the data).
That's the right trade-off for a localhost single-user tool whose snapshots do get
copied around. Consumers (`bootstrap_node` clone, `deploy_node` push) read
`SettingsStore.github_token()` (settings › env fallback). `POST /settings/github/test`
probes `GET /user` — auth vs unreachable vs no-token, mirroring the provider preflight.

### 2. Uncaught crash → STALLED, not FAILED (+ human give-up)
`_drive`'s catch-all now calls `_mark_stalled` (event with the crash reason) instead of
`_force_failed` (deleted). Rationale: a crash is OUR bug, the checkpoint is intact, and
the run is one fixed bug away from finishing — exactly what `/continue` exists for. This
also kills the list/detail disagreement: neither view sees a terminal status, both say
STALLED, and the continue bar shows the crash reason. Domain failures (iteration caps,
exhausted attempts) still go terminal through the nodes' `RunFailureHandler` paths.
FAILED runs now carry `failure_reason` (latest `run.failed` event) on both the detail
response and the list rows — red banner in the run view, reason line on the run box.

The counterpart to recoverable-by-default: **`POST /runs/{id}/fail`** ("Mark as failed"
next to Continue on the stalled bar). The human looks at the crash reason, judges the
run hopeless, and closes the case — `RunFailureHandler` (same bookkeeping as every other
failure: status, events, lesson retraction), optional note into the `run.failed` reason,
then `teardown_on_terminal` honored. Only valid on a STALLED run (409 otherwise). Since
the checkpoint stays non-terminal after a give-up, `_response` treats a DB row saying
FAILED as the verdict that wins over the orphaned checkpoint — and `/continue`'s guard
(which reads that response) therefore refuses resurrected zombies.

### 3. Teardown on terminal, settings-driven
`RunDefaults.teardown_on_terminal` (default false). Enforced in `Service._finalize`,
guarded on the CHECKPOINT status being DONE/FAILED — a stalled run always keeps its
container so `/continue` can pick the work back up. Best-effort: teardown failure logs a
warning, never masks the run outcome.

## Rejected
- **Encrypting with a passphrase prompt / OS keychain** — breaks headless start; over-built
  for localhost.
- **Keeping crash=FAILED + syncing the checkpoint to terminal** — throws away a
  recoverable run to make bookkeeping consistent; consistency the other way is strictly
  better here.
