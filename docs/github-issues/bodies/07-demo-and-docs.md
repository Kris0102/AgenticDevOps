## Goal

Document and reproducibly demo POC-1: **Planner ReAct explores sandbox and produces plan + microtasks.**

## Tasks

- [ ] Add `docs/POC-01-planner-react.md` with prerequisites (Docker, Postgres, API keys)
- [ ] Document env vars: `CLUSTER_API_KEY`, `USE_TOOLS`, `DATABASE_URL`, etc.
- [ ] Optional: tiny fixture repo URL + SHA for CI/manual demo
- [ ] Add `Makefile` or `scripts/run_poc1.sh` one-liner
- [ ] Success checklist:
  - [ ] Sandbox created
  - [ ] Repo cloned
  - [ ] ≥2 `tool.executed` events in DB
  - [ ] Plan + microtasks in final state

## Acceptance criteria

A new developer can follow the doc and complete one successful POC run in &lt;30 minutes (given VPN/cluster access).

## Labels

`poc`, `documentation`
