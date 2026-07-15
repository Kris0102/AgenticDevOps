# Agentic DevOps

Multi-agent system that autonomously plans, implements, tests, and proposes code
changes to an existing repo — with Docker-per-run sandboxing, deterministic
verification (lint/tests/build), human approval gates, and a full audit trail in
Postgres. Driven from a React dashboard over a FastAPI control API.

## Prerequisites

- Linux / WSL2 / macOS with bash (on Windows, run everything inside WSL2)
- Docker running (Docker Desktop or engine)
- Python 3.11 or 3.12 on your PATH (`python3`)
- Node.js + npm
- Git

## Quickstart

```bash
# 1. One-time environment setup — installs everything and restores the newest
#    DB snapshot from scripts/snapshots/ (run/event history, lessons, repo registry):
./setup.sh latest

# 2. Start the app (API :8000 + dashboard):
./run.sh

# 3. Open the dashboard:
#    http://localhost:5173
```

`setup.sh` creates a `.env` with working defaults if missing, builds a Python venv
and installs all requirements into it, installs the dashboard's npm packages,
starts the Docker services (Postgres, pgAdmin, Grafana), builds the sandbox base
image, and runs the DB migrations. Variants:

```bash
./setup.sh                                     # no snapshot — DB left as-is (migrations only)
./setup.sh scripts/snapshots/snapshot_X.sql    # restore a specific snapshot
```

If the database already contains data, setup asks before overwriting it.
`run.sh` refuses to start (and tells you what's missing) until setup has been run.

## First-time configuration — REQUIRED before the first run

The app starts without any secrets. **Runs will not start until you add a
provider API key**, and private repos / pushes / PRs also need a GitHub token.
All of this lives in the dashboard — `.env` is only a seed.

### 1. Add provider API keys (Settings → Providers)

Open the dashboard → **Settings** → **Providers**. Each provider is an endpoint +
API key. Fill in the **api key** field, then use **Test connection** to verify,
and **Save changes**.

Two typical providers:

**The cluster (or any OpenAI-compatible endpoint):**

| Field | Value |
|---|---|
| type | `openai_compatible` |
| base url | your cluster endpoint URL |
| api key | your cluster API key |
| suggested models | add each model name and press Enter — e.g. `google/gemma-4-31B-it` |

The **suggested models** list is what populates the model dropdowns on the New
Run page and in **Settings → Default model per agent** — add every cluster model
you want to be able to select (exact names as the endpoint expects them, e.g.
`google/gemma-4-31B-it`).

**Anthropic (Claude):**

| Field | Value |
|---|---|
| type | `anthropic` |
| api key | your Anthropic API key (`sk-ant-…`) |
| suggested models | e.g. `claude-sonnet-5`, `claude-opus-4-8` |

Use **+ add provider** for additional endpoints. Then pick which model each
agent uses under **Settings → Default model per agent** (cheap models for
investigation, a strong one for the Coder is a good split).

### 2. Add the GitHub token (Settings → GitHub)

**Settings → GitHub** → paste a GitHub personal access token → **Test
connection** → **Save changes**. Needed to clone private repos, push the agent's
branch, and open PRs. The token is encrypted at rest and masked in the UI.

### 3. Register your repos (Repos page)

Top bar → **Repos** → add the repository the agents should work on (it is
validated against GitHub before being stored). A repo entry can carry its own
token that overrides the global one. Registered repos appear in the New Run
page's picker, keyed to the per-repo memory (lessons, semantic index).

Then: **New run** → pick the repo, describe the objective, choose HITL gates
(plan / code / deploy) → run. The run view streams live logs and pauses at the
gates you selected for approve/reject.

## Services & ports

| URL | What |
|---|---|
| http://localhost:5173 | Dashboard (Vite dev server, proxies the API — no CORS setup) |
| http://localhost:8000 | Control API (FastAPI) |
| http://localhost:3000 | Grafana — cross-run metrics dashboard |
| http://localhost:8080 | pgAdmin |

## DB snapshots

| Script | Contents | Where | Shareable? |
|---|---|---|---|
| `./scripts/snapshot_db.sh [label]` | **Secret-free** dump — settings + run checkpoints excluded, tokens scrubbed and verified | `scripts/snapshots/` | Yes — this is what `setup.sh latest` restores |
| `./scripts/snapshot_full.sh [label]` | **Everything**, including encrypted secrets + the Fernet key file | `scripts/snapshot_secrets/` (gitignored) | **Never** — personal backup only |
| `./scripts/load_snapshot.sh <file>` | Restore either kind (drops all tables first, asks to confirm) | — | — |

After restoring a sanitized snapshot, settings re-seed from your local `.env`
and you enter your own API keys (step 1 above); the restored runs are view-only
history. A full restore also needs its bundled `.settings_key` copied back to
the project root.

## More

- `CLAUDE.md` — current architecture, graph topology, all environment variables
- `docs/ARCHITECTURE.md` — as-built architecture reference
- `docs/decisions/` — ADRs (why things are the way they are)
- `dashboard/README.md` — the UI's screens and mental model
