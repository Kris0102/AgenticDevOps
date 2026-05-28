# Agentic DevOps Project Guidelines

This project implements a Multi-Agent System (MAS) for DevOps relying on strict Control Plane / Data Plane separation, grounded verification, and deterministic sandboxing.

## Architecture & Boundaries
- **Control Plane**: Orchestrator (LangGraph), Agents (Planner, Coder, Tester, etc.).
- **Data Plane**: Sandbox (Docker), Episodic DB (Postgres).
- **Communication**: All agent-to-agent communication is mediated by the **Orchestrator** via the **Blackboard (Run State)**. No direct agent messaging.
- For full details, see `docs/ARCHITECTURE_preview.md` and `docs/Design_preview.md`.

## Code & Environment Source of Truth
- **Sandbox is the Source of Truth**: The only authoritative repository contents live in the Docker Sandbox filesystem. Semantic memory is STRICTLY for search/retrieval, never for reconstruction.
- **Episodic Memory**: The system of record for the run state machine, audit logs, and artifacts (Postgres). Agents DO NOT write directly to DBs; they write via Orchestrator events.
- See MVP details in `docs/instructions.md`.

## Environment Actions & Tools
- All environment actions MUST route through the **Tool Gateway** (via Model Context Protocol).
- Strict allowlists, denylists, scopes, and timeouts apply.
- Secrets are contained inside the tools/servers (not agent prompts). 
- Every tool call must be logged in the event log.
- For Tool Gateway & MCP usage contracts, refer to `docs/MCP_AND_TOOLS.md`.

## Build, Test & Hierarchical Verification
- Follow **Hierarchical Verification**: 
  - **Planning Loop**: Planner -> Plan Reviewer (critic).
  - **Execution Loop**: Coder -> Tester (execution) -> Reflector/Reviewer.
- **Required Grounding**: Code reviews and acceptance MUST be grounded by deterministic evidence (test, lint, and build logs).
- Purely probabilistic ("static" LLM) reviews are not sufficient.
