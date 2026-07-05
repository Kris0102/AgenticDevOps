# 0020 — Settings hub: UI-managed providers, model, and knobs

Date: 2026-07-04
Status: Accepted (implemented)

## Context
Model, endpoint, API key, and every tuning knob lived in `.env` and were duplicated across
the 5 agents (each built `ChatOpenAI(...)` inline — the provider-coupling TODO #6). Changing
anything meant editing a file and restarting, and there was no way to run a different
provider (e.g. Claude) — that needs its own endpoint + key.

## Decision
A **UI-managed settings hub**. The DB is the source of truth; `.env` demotes to
**bootstrap seed + fallback + headless override**.

- **Provider profiles.** `ProviderProfile { id, label, type (openai_compatible|anthropic),
  base_url, api_key, models }`. `providers/llm.py::build_chat_llm` dispatches by type
  (`ChatOpenAI` for the cluster / any OpenAI-shaped endpoint, `ChatAnthropic` for Claude) —
  the single LLM construction site, closing TODO #6.
- **Storage.** One JSON row (`app_settings`). `SettingsStore.get()` seeds from env on an
  empty table (existing deploys keep working). `AppSettings { providers, selected (LLMConfig),
  knobs (all former env knobs), defaults (iteration caps + gates) }`.
- **Secrets — mask, not encrypt (v1).** `get_public()` masks every api_key for the API; the
  raw key is used only by the factory. `update()` treats a still-masked key as "unchanged"
  so editing other fields never wipes a key. Keys are excluded from GET responses; encryption
  at rest (a `SETTINGS_ENCRYPTION_KEY` in env) is a documented harden-later step.
- **Resolution — once, at run creation.** `resolve_run_config(override) = per-run override ›
  global (DB) › env`. The effective `RunConfig` (llm + provider + knobs) is stored on the
  blackboard (`state["config"]`) and the provider/model persisted on the `Run` row. **Config
  is mandatory on every agent**; the *fallback* (`… or resolve_run_config()`) lives in the
  node, so an agent can never silently build off env.
- **Preflight healthcheck.** `health_check` (a 1-token probe) runs **synchronously in
  `POST /runs` before scheduling** — a dead/mis-keyed provider returns 503/401 and **no run
  is created** ("don't even start it"). The same probe backs the settings "Test connection"
  button. Complements ADR-0019's transient-stall: preflight blocks a doomed start;
  `RetriableError` recovers a mid-run hiccup.

## Consequences
- Switch providers/models and tune knobs from the browser; env stays only as seed/fallback.
- Two UI pages: a global settings hub (providers + Basic/Expert tiers, every field rendered
  from `GET /settings/schema` with an info tooltip) and a per-run page (pre-filled, override
  model/gates, inline preflight errors). Runs record + display the model they used.
- New dependency `langchain-anthropic`; migration `d4e5f6a7b8c9` (`app_settings` +
  `runs.llm_provider/llm_model`).

## Rejected / deferred
- **Encrypt keys at rest** — v1 is mask + exclude-from-snapshots; localhost single-user.
- **Per-agent model** (coder vs planner) — `RunConfig` is per-run, extensible to a map later.
- **`top_p`/`max_completion_tokens`** — dropped from the factory (incidental gemma tuning;
  irrelevant at temperature 0). Re-add as knobs if a model needs them.
