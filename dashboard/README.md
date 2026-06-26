# AgenticDevOps Dashboard

Web frontend for the MAS control plane (`src/api/`, ADR 0013). Single-user, localhost.
Triggers runs, shows live logs, and drives HITL gates (approve/reject with the plan or
green/red diff in view).

## Run

```bash
# 1. Start the control API (from the MAS repo root)
cd ../src && ../venv/bin/uvicorn api.app:app --port 8000

# 2. Start the dashboard (this dir)
npm install
npm run dev          # http://localhost:5173  (proxies /runs → :8000)
```

## What it does

| Piece | Source |
|---|---|
| status / HITL notify | poll `GET /runs/{id}` (`run_state`, `hitl_phase`) |
| live logs | SSE `GET /runs/{id}/logs/stream` (structlog feed) |
| gate review | `GET /runs/{id}/hitl` → plan + microtasks, or raw green/red diff |
| approve / reject | `POST /runs/{id}/resume` |
| history | poll `GET /runs/{id}/events?after=<seq>` (durable audit) |

## Stack
React + Vite + TypeScript · `EventSource` for SSE · `react-diff-view` for the diff.
No state library — local state + polling is enough for one user.

## Ship as one binary (later)
`npm run build` → `dist/` served by FastAPI `StaticFiles`, or wrap in Tauri for an exe.
