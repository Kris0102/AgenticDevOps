# 0030 — Per-agent model selection

Status: accepted
Date: 2026-07-09

## Context
A run resolved exactly ONE `LLMConfig` (provider_id + model + temperature/retries) and handed
it to **all five agents** (Planner, Reviewer, Coder, Tester, Reflector) — ADR 0020. Every
agent ran on the same provider+model.

That is the single biggest lever left for cost. The five agents do very different work at very
different value density: Planner/Reviewer/Tester mostly *investigate* the repo (input-heavy,
low reasoning bar — a cheap/fast model handles it); the Coder is the one agent where model
quality decides whether a microtask lands (wants the strongest model); the Reflector is
mid-tier. Forcing one model means either overpaying (everything strong) or undercutting the
Coder (everything cheap). On a paid API (e.g. Anthropic) tiering cuts per-run cost several-fold
with little quality loss, since the MAS already pays a "tax" of each agent re-investigating the
repo independently. See GH #71.

## Decision
Per-agent model selection, layered on the existing settings hub (ADR 0020).

- **`LLMConfig` is now a map of five per-agent selections** (`planner`/`reviewer`/`coder`/
  `tester`/`reflector`), each a `BaseLLMConfig` subclass carrying its own `provider_id` +
  `model` (+ temperature/max_retries defaults). `BaseLLMConfig` is abstract; only the named
  subclasses are instantiated.
- **`provider_id` stays a reference, not an inlined connection.** `RunConfig` carries
  `providers: Dict[str, ProviderProfile]` — only the DISTINCT providers actually referenced,
  one entry per id — plus `provider_for(llm)`. Each agent resolves its own provider. Three
  agents on one Anthropic key = **one** `ProviderProfile` (key encrypted/masked once, ADR
  0021), four selections pointing at it. This is the same normalization the repo registry uses
  (ADR 0024).
- **`role` is NOT a field on `ProviderProfile`.** A provider is a pure connection; which agent
  uses it is already stated by the selection that points at it. Putting `role` on the provider
  would be a dual source of truth AND would force a duplicate provider (duplicate key) per
  agent even when they share an endpoint. (An early draft added `role`; it was removed.)
- **Precedence unchanged** (ADR 0020): per-run override › global DB `selected` › env seed. The
  fallback lives in the node; agents take a resolved `RunConfig` as a mandatory arg.
- **Preflight probes every DISTINCT `(provider, model)` pair** (`Service.resolve_and_preflight`),
  deduped — a mis-keyed provider on ANY agent fails fast at `POST /runs`, before a run exists.
- **Denormalized `runs.llm_provider`/`llm_model`** (display-only, run list) now mirror the
  **Coder's** selection — the headline agent. Per-agent models live on the checkpointed
  `RunConfig`.
- **Backward compatibility:** `SettingsStore._migrate_blob` upgrades an older stored
  `AppSettings` JSON on read — a pre-#71 flat `selected` is fanned out to all five agents
  (behaviour unchanged: every agent on the same model); a per-agent blob written before the
  Reflector existed clones the Coder's selection into `reflector`; the removed provider `role`
  field is stripped. Idempotent, no Alembic migration (settings is one JSON row).
- **UI:** Settings renders a per-agent provider+model grid (cheap-for-investigation /
  strong-for-Coder guidance inline); New Run seeds a collapsible per-agent override from the
  global default and sends the full nested `llm`. Provider cards show "● in use" when any
  agent references them.

## Consequences
- Mixed providers in one run are supported (e.g. Coder on Anthropic, Planner on the cluster) —
  `RunConfig` resolves and carries each.
- Not in scope: switching a model mid-run (GH #64); per-agent temperature/max_retries in the UI
  (they stay at model defaults — the agents run deterministic at 0).
