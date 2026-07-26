# src/grafana/ — Cross-Run Metrics Dashboard

**Role in the MAS:** the analytics layer. While the app's own dashboard shows *one
run* live, Grafana answers the fleet-level questions — is the system getting
better, safer, cheaper? It reads **directly from the Postgres audit tables** the
MAS already writes (`runs`, `events`, `microtasks`, `lessons`): no extra metrics
pipeline, no instrumentation code. The audit trail *is* the metrics source.

## Files

| File | What it does |
|---|---|
| `Dockerfile` | Grafana image with provisioning + dashboards **baked in** — boots fully configured, zero manual clicking (reproducible demo). Anonymous Viewer enabled for one-click access — localhost only, not for exposure |
| `provisioning/datasources/postgres.yml` | Auto-connects the core Postgres datasource; credentials injected at runtime from the compose env via `$__env{…}` — no secrets in the file |
| `provisioning/dashboards/dashboards.yml` | Loads dashboards from `/etc/grafana/dashboards` (deliberately *not* under the data volume, so an image rebuild always wins over stale state) |
| `dashboards/agenticdevops.json` | The "AgenticDevOps — Cross-Run Metrics" dashboard |

## What the dashboard shows (panel highlights)

- **Delivery:** total runs, success rate, run-delivery funnel, branches pushed,
  PRs opened, code shipped (final diff per run), deploy rate.
- **Autonomy & safety:** autonomy ratio, gateway denials, secret pushes blocked,
  audit events logged, HITL approval rate, average human gate wait.
- **Learning & recovery:** lessons learned / operator rules, runs started with repo
  memory (with vs. without comparison), recovery rate after stall,
  self-correction rate, first-pass yield.
- **Economics:** cost per successful run, cost per delivered microtask, where the
  time goes per run.

Every panel is a SQL query over the audit schema — e.g. "secret pushes blocked"
counts `gitops.secret_blocked` events, "self-correction rate" is derived from
reflector → passing-tester sequences.

## Interview sound bites

- "Because every action already lands in the events table, our metrics layer is
  just SQL — Grafana provisioned over the same Postgres, no telemetry pipeline."
- "The panels are chosen to answer the MAS questions that matter: does it get
  better with memory, how often does it self-correct, and what does a shipped
  microtask cost."
