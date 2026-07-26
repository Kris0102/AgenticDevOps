# src/grafana/ — Code Explanation

No Python — a baked, provisioned Grafana image. Four files.

## Dockerfile
`FROM grafana/grafana:latest` plus:
- Fixed admin creds + signup off; **anonymous Viewer enabled** — one-click open
  for the localhost single-user demo (comment explicitly warns: do NOT expose
  port 3000 off-machine like this).
- `COPY provisioning /etc/grafana/provisioning` and
  `COPY dashboards /etc/grafana/dashboards` — everything loads at boot, zero
  manual clicking (reproducible for the graded demo).
- The placement comment is the subtle bit: `/etc/grafana` is deliberately NOT
  under the `grafana_data` volume — a volume mount over `/var/lib/grafana` would
  MASK baked-in dashboards there, so a rebuild couldn't update them. Shipping them
  in `/etc/grafana/dashboards` means **a rebuild always wins** over stale volume
  state.

## provisioning/datasources/postgres.yml
Postgres is a Grafana CORE datasource (no plugin). Auto-connects at boot:
- `uid: agenticdevops-pg` — stable uid the dashboard panels reference.
- `access: proxy` — the Grafana server talks to Postgres, not the browser.
- `url: postgres:5432` — the compose service name on the shared network.
- Credentials via `$__env{POSTGRES_USER}` etc. — Grafana's provisioning
  interpolation reads the container env, which docker-compose injects from the
  project `.env`. **The YAML holds no secrets.**

## provisioning/dashboards/dashboards.yml
File provider pointing at `/etc/grafana/dashboards` — loads every JSON there.

## dashboards/agenticdevops.json — "AgenticDevOps — Cross-Run Metrics"
~30 panels, all raw SQL over the audit schema (`runs`, `events`, `microtasks`,
`lessons`). Panel groups:
- **Headline:** total runs, success rate, avg run duration, avg microtask attempts.
- **Autonomy & safety:** autonomy ratio, gateway denials (`tool.denied` count),
  secret pushes blocked (`gitops.secret_blocked`), audit events logged, HITL
  approval rate, avg human gate wait.
- **Delivery:** run delivery funnel, branches pushed, PRs opened, code shipped
  (final diff per run), tests authored / checks executed, median duration,
  where-the-time-goes.
- **Learning & recovery:** lessons/operator rules counts, agent lessons per run,
  runs started with repo memory (with/without comparison), recovery rate after
  stall, first-pass yield, self-correction rate.
- **Economics:** cost per successful run, cost per delivered microtask — summing
  the `cost_usd` field that `token_budget.record_usage` stored on each
  `llm.usage` event (computed once in Python; the dashboard only aggregates).

Exam sentence: "The metrics layer is just SQL because the system already writes
every action to the events table — no instrumentation was added for Grafana; the
audit trail doubles as the analytics source."
