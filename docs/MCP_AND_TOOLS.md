# MCP & Tooling Layer (Dedicated Spec + Developer Guide)

This document is the **canonical reference** for how we integrate **Model Context Protocol (MCP)** into our Multi‑Agent System (MAS).  
It defines:

- **What MCP is** (and what it is not)
- **How MCP servers/tools fit into our architecture**
- **How tools enable context retrieval (RAG) + safe actions**
- **Security, auditing, and HITL gating rules**
- **Implementation contracts** (tool schemas, naming, logging)
- **Developer guide** (how to run locally, how to add a new tool/server)

> **Scope:** This doc is the source of truth for the Tool Gateway + MCP servers.  
> Architecture.md should link here and only summarize MCP at a high level.

---

## 1) Definitions (No ambiguity)

### 1.1 What MCP is (strict definition)
**Model Context Protocol (MCP)** is a standard protocol that allows a model/agent runtime to:
- discover available **tools**
- call a tool with **structured arguments**
- receive **structured results/errors**

MCP is a **tool interface protocol**. It is not a planning framework, not a memory algorithm, and not a database.

### 1.2 What “MCP Server” means in this project
An **MCP Server** is a process/service that **executes tools** (deterministic code) and exposes them via MCP transport:
- **Local / stdio:** subprocess, good for sandbox-local tools
- **Remote / HTTP:** long-running service, good for shared services (DB/vector)

### 1.3 What “Tool Gateway” means in this project
The **Tool Gateway** is our central enforcement point that:
- connects to MCP servers (acts as the MCP client)
- enforces **tool allowlists** per agent role + run phase
- enforces security policies (path restrictions, command allowlists, network limits)
- logs every tool call into **Episodic Memory events**

> The Tool Gateway is the **policy perimeter**.  
> MCP servers are execution backends.

---

## 2) Why MCP (What problems it solves)

### 2.1 Context retrieval (RAG) without prompt bloat
Agents fetch only relevant context:
- code chunks from semantic memory
- events/logs from episodic memory
- diffs and artifacts

### 2.2 Capability boundaries (least privilege)
MCP enables “capabilities as tools”:
- Planner: retrieval-only
- Coder: read/write sandbox
- Tester: run allowlisted tests
- Deployer: deploy tools (rare; HITL-gated)

### 2.3 Security and secret containment
Credentials live in tool servers (or their environment), not in agent prompts:
- DB passwords remain inside memory servers
- GitHub App tokens remain inside GitOps service/server

### 2.4 Auditability + reproducibility
Every tool call can be logged uniformly:
- who called what tool
- with which args (redacted as needed)
- what artifacts were produced
- what context the model saw

---

## 3) Placement in our Architecture (Component wiring)

### 3.1 High-level flow
**Agent → Orchestrator → Tool Gateway → MCP Client → MCP Server → External resource**

Examples:
- `semantic.search` → pgvector/Postgres
- `events.tail` → Postgres
- `sandbox.run` → Docker sandbox
- `gitops.create_pr` (optional) → GitHub API

### 3.2 Responsibilities split (important)
**Orchestrator**
- state machine + blackboard writes
- decides which agent runs next
- decides which tools are allowed in this step

**Tool Gateway**
- connects to MCP servers
- enforces allowlists/policies
- writes `tool.executed/tool.denied` events

**MCP Servers**
- execute the tool logic deterministically
- own connections to DBs/services required for tool execution

---

## 4) Memory as Tools (Episodic vs Semantic)

### 4.1 Episodic Memory tools (Postgres)
Episodic memory holds:
- **Blackboard snapshot**
- **events** (append-only audit log)
- artifact references + microtask tracking

Exposed via tools such as:
- `runs.get_state(run_id)`
- `events.tail(run_id, n)`
- `events.query(run_id, type, since_seq, limit)`
- `artifacts.get(ref)`

**When used**
- reflection/debugging (fetch failing logs)
- resumption/replay (cursor + tail)
- HITL requests/decisions

### 4.2 Semantic Memory tools (Vector DB / pgvector)
Semantic memory holds:
- code chunks (baseline + workspace)
- validated reusable reflections/rules
- docs/SOPs

Exposed via tools such as:
- `semantic.search(query, namespace, top_k, filters)`
- `semantic.get(chunk_id)`
- `semantic.search_reflections(query, top_k, filters)`

**When used**
- planner orientation (find entrypoints, patterns)
- coder navigation (find where to implement)
- reflector assistance (locate code around stack trace + prior fixes)

---

## 5) Server Inventory (Recommended for MVP → Production)

### 5.1 MVP servers (recommended)
1) **mcp-semantic-memory**
   - connects to pgvector
   - read-only tools initially (`semantic.search`, `semantic.get`)
2) **mcp-episodic-memory**
   - connects to Postgres
   - read-only tools (`runs.get_state`, `events.tail`, `artifacts.get`)
3) **mcp-sandbox**
   - filesystem read/write inside sandbox root
   - allowlisted command runner (`sandbox.run`)

### 5.2 Optional later
4) **mcp-security**
   - wrappers for semgrep/gitleaks/osv
5) **mcp-gitops** (optional)
   - branch/commit/PR tools (or keep as separate GitOps service)

---

## 6) Tool Naming & Contract Rules (Spec)

### 6.1 Naming conventions
Use `<domain>.<verb>`:
- `semantic.search`
- `events.tail`
- `sandbox.run`
- `fs.read_file`

### 6.2 Tool input/output contract rules
All tools MUST:
- validate inputs (schema)
- bound outputs:
  - `top_k` max (e.g., 50)
  - max returned text length per chunk/log
- return references to large artifacts (not inline megabytes)
- never return secrets (redact)

### 6.3 Provenance rule (context traceability)
Tool results should include stable identifiers:
- `chunk_id` for semantic results
- `artifact_ref` for logs/diffs/scans

The Tool Gateway must log these identifiers in the event stream.

---

## 7) Security Model (Non-negotiables)

### 7.1 Tool allowlisting
Tool access is restricted by:
- `agent_role`
- `run.status` / phase
- policy risk flags (e.g., sensitive paths touched)

### 7.2 Sandbox tool restrictions
- no shell strings (`"sh -c ..."`) — command must be array-like (implementation choice)
- no pipes/redirects
- enforced timeouts
- restricted filesystem root (no `../../`)
- optional forbidden paths list (`.env`, `.git`, secrets dirs)

### 7.3 Memory server restrictions
- no unbounded event queries
- require `run_id` for event access
- enforce namespace filters for semantic queries

### 7.4 HITL gates for critical tools
Before enabling tools that can:
- merge PR
- deploy prod
- modify CI/IaC
- override policy

Orchestrator must:
- create `hitl.requested`
- wait in `NEEDS_HITL_APPROVAL`

---

## 8) Event Logging for Tool Calls (Spec)

Every tool call must produce an event:
- `tool.executed` OR `tool.denied`

**Required event payload keys**
- `tool_name`
- `server_name`
- `args_summary` (redacted)
- `duration_ms`
- `success`
- `result_refs` (chunk_ids / artifact_refs)

This makes runs auditable and allows “what did the model see?” reconstruction.

---

## 9) Flow: How MCP is used during a Run (Step-by-step)

### 9.1 Run bootstrap
1) GitOps triggers Orchestrator with task request
2) Orchestrator creates `run_id` + blackboard snapshot
3) Repo cloned into sandbox
4) Context Engine ingests baseline into semantic store

### 9.2 Planning step (Planner)
Tools enabled:
- `semantic.search` (baseline)
- `fs.read_file` (read-only)
- `repo.list_tree` (optional)

Planner retrieves entrypoints/build manifests/tests → emits plan.

### 9.3 Microtask execution (Coder + Tester)
Coder tools:
- `semantic.search` (baseline + workspace)
- `fs.read_file`, `fs.write_file`
- `repo.diff`

Tester tools:
- `sandbox.run` (allowlisted: lint/tests/build)
- `artifacts.put/get` (optional)

On failure:
Reflector tools:
- `events.tail`
- `artifacts.get(log_ref)`
- `semantic.search_reflections`

### 9.4 PR + CI
GitOps opens PR; CI runs.
Orchestrator consumes CI status events.
If CI fails → new microtask “fix CI” loop.

---

# Developer Guide

## 10) How to run locally (Suggested docker-compose layout)

### 10.1 Services you will run
- `postgres` (episodic + pgvector)
- `orchestrator`
- `mcp-semantic-memory`
- `mcp-episodic-memory`
- `mcp-sandbox`
- `context-engine`

> Start with `postgres + orchestrator + mcp-semantic-memory` first.

### 10.2 Configuration (environment variables)
Each MCP server should read its credentials from env / secrets:
- `DATABASE_URL=postgresql://...`
- `PGVECTOR_URL=...` (or same DB)
- `SANDBOX_ROOT=/workspace/repo`

Never pass credentials via tool arguments.

---

## 11) How to add a new MCP tool (Checklist)

1) Decide domain and name (e.g., `security.scan_semgrep`)
2) Define input schema (bounded, validated)
3) Define output schema (bounded, artifact refs)
4) Implement handler
5) Add policy rule:
   - which roles/phases may call it
6) Add event logging mapping:
   - ensure `tool.executed` includes `result_refs`
7) Add tests:
   - input validation
   - forbidden path/command tests
   - output truncation tests

---

## 12) Recommended first tools to implement (Minimal useful set)

### Semantic memory (read-only)
- `semantic.search`
- `semantic.get`

### Episodic memory (read-only)
- `runs.get_state`
- `events.tail`
- `artifacts.get`

### Sandbox
- `fs.read_file`
- `fs.write_file`
- `repo.diff`
- `sandbox.run` (allowlisted)

---

## Knowledge Check
Do you want MCP servers to run:
- **as HTTP services** (better for docker-compose/k8s), or
- **as local stdio subprocesses** (simpler for a single-machine prototype)?

Answering that determines how we write the first implementation skeletons.