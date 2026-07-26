# src/ Guide — one page per folder

Interview-ready explanations of every folder under `src/` and its function in the
multi-agent system (MAS). Read `orchestrator.md` first — it is the spine everything
else hangs off.

| File | Folder | One-liner |
|---|---|---|
| [patterns.md](patterns.md) | — | Cheat-sheet of every pattern used and why (star topology, actor–critic, gateway, HITL, gitleaks…) |
| [features.md](features.md) | — | Demo-worthy feature highlights (oscillation detection, test self-indictment, stall-don't-fail budget…) |
| [walkthrough.md](walkthrough.md) | — | Rehearsable ~10-min presentation script: pitch → diagram → demo → code tour → drill-down map |
| [oral-exam-prep.md](oral-exam-prep.md) | — | July 21/22 oral exam: YOUR commits vs Stefan's, 5 commit stories, likely questions, 15-min strategy |
| [opening-statement.md](opening-statement.md) | — | Spoken opening script: 60-second tiered system-design explanation with the WHY built in |
| [system-design.md](system-design.md) | — | Complete memorization guide: the whole design in 7 chunks with hooks + self-test |
| [orchestrator.md](orchestrator.md) | `src/orchestrator/` | LangGraph graph, blackboard state, routing, HITL gates, checkpointing — the MAS control plane |
| [agents.md](agents.md) | `src/agents/` | The five LLM agents: Planner, Reviewer, Coder, Tester, Reflector |
| [tool_gateway.md](tool_gateway.md) | `src/tool_gateway/` | Deny-by-default permission boundary every agent action passes through |
| [sandbox.md](sandbox.md) | `src/sandbox/` | Docker-per-run isolated execution environment — source of truth for code |
| [db.md](db.md) | `src/db/` | Postgres persistence: audit trail, microtasks, lessons memory, settings, repo registry |
| [context_engine.md](context_engine.md) | `src/context_engine/` | Semantic code memory — AST chunking + pgvector embeddings |
| [gitops.md](gitops.md) | `src/gitops/` | The GitHub boundary: branch/commit/push/PR, deploy modes, secret scanning |
| [api.md](api.md) | `src/api/` | FastAPI control plane — the dashboard backend that drives the graph |
| [providers.md](providers.md) | `src/providers/` | Pluggable LLM construction, token accounting, cost pricing |
| [observability.md](observability.md) | `src/observability/` | Structlog setup + in-memory log bus for live dashboard streaming |
| [grafana.md](grafana.md) | `src/grafana/` | Provisioned cross-run metrics dashboard straight off the Postgres audit tables |
