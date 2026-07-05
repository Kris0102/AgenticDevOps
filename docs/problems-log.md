# Problems Log

A running register of every non-trivial problem we hit and how it was resolved — the
"what went wrong and what we did about it" companion to `docs/decisions/` (which records
*why* we chose an approach). Newest first. Each row links to the ADR / GH issue / commit
that carries the detail.

| Date | Problem | Root cause | Fix | Ref |
|------|---------|-----------|-----|-----|
| 2026-07-05 | Run FAILED — execution loop hit the microtask-attempt cap | Tester synthesized a spec-violating test (invented a boundary input, hand-miscalculated a Levenshtein distance, asserted a message correct code never produces); Reflector's diagnosis prompt lacked the acceptance criteria + test source so it misrouted `fix_target=code` and the coder burned all attempts | Defense in depth: planner nudge to supply IOExamples for precise-value/boundary cases; tester asserts structure-only when no example; reflector diagnosis injects criteria + failing test source and routes `fix_target=test` on a self-computed assertion | ADR 0022, GH #44 |
| 2026-07-05 | Run FAILED — planner↔reviewer loop hit `max_total_iterations` | (a) Planner received ALL prior rejections stacked (1..N) — duplicate/stale noise that slowed convergence; (b) reviewer's free-text `notes` leaked its investigation reasoning ("let me verify…") as a non-actionable blocker | Render only the latest review verdict to the planner (full history kept for audit); add structured `blockers` to `ReviewerOutput` forwarded instead of `notes`; prompt: findings-only, APPROVE if no concrete defect | ADR 0022, GH #43 |
| 2026-07-05 | Stalled run mislabelled — list said FAILED, detail said STALLED | Uncaught graph crash wrote `Run.status=FAILED` while the checkpoint stayed non-terminal, so the two views derived state from different sources | Uncaught crashes now mark STALLED (recoverable); human closes the case via `POST /runs/{id}/fail`; the DB FAILED row wins over the orphaned checkpoint in `_response` | ADR 0021 |
| 2026-07-05 | Secrets (provider keys, GitHub token) sat unencrypted in the settings row | v1 settings hub masked on read but stored plaintext; DB dumps/snapshots/pgAdmin would leak tokens | Fernet-encrypt secrets at rest (`db/secrets.py`), key from `SETTINGS_ENCRYPTION_KEY` env or auto-generated `.settings_key`; GitHub token moved into settings with a connection test | ADR 0021 |
| 2026-07-04 | Deploy crashed the run after 9/9 green microtasks | `git add -A -- . :(exclude)…` exits 1 once an ignored dir (`.agentic_tests/`) exists in the tree | Two-step stage: `git add -A -- .` then `git reset -q --` with `:(glob)` exclude patterns (also fixed a latent top-level `node_modules/` miss) | commit (feat-dashboard-enhacements) |
| 2026-07-04 | Dashboard showed interleaved microtasks ("5/9 done") | A re-planned draft left the superseded plan's rows in the DB | `planner_node` deletes rows not in the new plan; plan artifact versioned by iteration | commit |
| 2026-07-04 | Live logs looked like an error loop + page jumped on new lines | SSE ring-buffer replays on reconnect and lines were appended blind; `scrollIntoView` scrolled the whole document | Dedupe log frames by `seq`; container-scoped stick-to-bottom only when already at the bottom | commit |
| 2026-06→07 | Provider/model/knobs hard-coded in `.env`, duplicated across 5 agents | No UI to change them; no way to run a different provider (Claude) | Settings hub: `app_settings` DB row, provider factory (`providers/llm.py`), per-run `RunConfig`, preflight healthcheck | ADR 0020 |
| 2026-06 | (earlier) Deploy pushed `node_modules`/secrets; tester reasoning leaked into shipped tests | No `.gitignore` seeded; synth tests written into the repo tree | Bootstrap seeds `.gitignore`; synth tests in git-ignored `.agentic_tests/`; gitleaks hard-gate before push | ADR 0019, GH #38/#39 |
| 2026-06 | Cross-run failures kept recurring with no memory | No per-repo learning between runs | `lessons` table (pgvector), written on confirmation, retrieved top-k, injected advisory | ADR 0018 |

For anything before this log, see the numbered ADRs in `docs/decisions/` (0015–0017 cover the
tester/reflector architecture; 0001 is the first architecture audit).

## How to use this file
Add a row whenever a real problem is diagnosed and resolved (a failed run, a data bug, a
security gap) — one line, link the ADR/issue/commit that holds the evidence. Skip pure
refactors and anything `git log` already explains. This is the scannable index; the linked
ADR/issue is the depth.
