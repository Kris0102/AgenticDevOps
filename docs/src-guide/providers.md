# src/providers/ — LLM Abstraction, Token Accounting, Cost

**Role in the MAS:** decouples the agents from any particular LLM vendor and makes
every token the system spends visible, budgeted, and priced. Agents receive a
configured chat model; they never construct one.

## Files

| File | What it does |
|---|---|
| `llm.py` | `build_chat_llm(provider, llm, timeout)` — **the single construction site** for LLM clients. Dispatches on provider type: `openai_compatible` (the local cluster or any OpenAI-shaped endpoint) → `ChatOpenAI`; `anthropic` → `ChatAnthropic`. Also `health_check` — the cheap preflight `POST /runs` uses to refuse starting on a dead/mis-keyed provider |
| `token_usage.py` | `TokenUsage` model + `sum_usage` — accumulates LangChain's standardized `usage_metadata` across every AIMessage in a ReAct trajectory. Tracks the prompt-cache split (cache-read / cache-creation as subsets of input) |
| `pricing.py` | Token → USD (GH #62). Cost is a **pure derivation** over durable `llm.usage` events — accurate live, after a clean finish, and after a crash. Unknown models (self-hosted cluster) price to `None` → the total is a flagged lower bound, never a wrong number |

## How it connects to the rest

- **Settings hub (ADR 0020):** provider/model/knobs are UI-managed in the DB;
  `.env` only seeds first boot. A run resolves ONE `RunConfig` at creation
  (per-run override → global DB → env seed) onto the blackboard — agents take
  `config` as a **mandatory argument**; the fallback lives at the node boundary.
  Mid-run settings changes can never change a running run's behavior.
- **Budget enforcement (ADR 0025):** every node records its real usage via
  `orchestrator/token_budget.py` into the checkpointed running total, the `runs`
  row, an `llm.usage` event, and the logs. Hitting `max_total_tokens` stalls the
  run (recoverable, human adds budget via `/continue`) rather than failing it.
- **Switching vendors is a settings change, not a code change** — that closed the
  original "provider coupling" architecture-audit TODO.

## Interview sound bites

- "One factory function owns LLM construction; five agents, zero vendor imports."
- "Every LLM call is metered into the audit trail — cost per run is derived from
  events, so it's correct even for a run that crashed halfway."
- "Config is resolved once at run creation and checkpointed — reproducibility over
  live-reload."
