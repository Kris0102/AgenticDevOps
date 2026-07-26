# src/providers/ — Code Explanation

Three files + `__init__`, ~355 lines. LLM construction, token accounting, USD
pricing. No agent logic — pure infrastructure the nodes consume.

---

## llm.py (199 lines)

### `build_chat_llm(provider, llm, timeout_seconds)` — THE construction site
Dispatches on `provider.type`:

**Anthropic branch → `ChatAnthropic`:**
- `temperature=None` — current Claude models 400 on an explicit temperature
  ("deprecated for this model"); `None` keeps langchain from sending the field at
  all. Steering happens via prompts instead.
- `thinking=_anthropic_thinking_param(...)` — see below.
- `max_tokens_to_sample=16_000` — langchain's unset default resolves to **4096**,
  which truncated a Coder writing a whole file in one tool call (real 17k-token
  outputs observed). 16k = safe non-streaming maximum.
- `model_kwargs={"cache_control": {"type": "ephemeral"}, **extra_request_params}`
  — **prompt caching**: caches the last cacheable block of every request. ReAct
  histories are append-only, so each step re-reads the previous prefix at ~0.1×
  input price (write 1.25×, 5-min TTL, breaks even after 2 calls; prefixes under
  4096 tokens silently don't cache). Per-provider `extra_request_params` merge
  AFTER so they can override.

**openai_compatible branch → `ChatOpenAI`:** requires `base_url` (raises
otherwise); passes temperature normally; `extra_request_params` for vendor knobs
(e.g. `prompt_cache_key`).

Never logs the api_key — the key exists only inside the constructed client.

### `_anthropic_thinking_param(model, enabled)` — per-model rules table
Encodes that Anthropic models differ in how "adaptive thinking" is toggled:
Haiku/pre-4.6 → no such mode, send nothing (a param would 400); Opus 4.6–4.8 +
Sonnet → `{adaptive}` to enable, `{disabled}` to disable; Fable/Mythos 5 → always
on, `{disabled}` unsupported → on→`{adaptive}`, off→None (can't disable —
documented, not hidden). Unknown model string → None (fail safe: ignore the
toggle rather than risk a 400).

### `structured_output_method(provider)` — hard-won empirics
Anthropic → `"json_schema"` (server-side schema-constrained output; required
fields cannot be omitted). The two alternatives FAILED in practice, and the
docstring names the failures: `json_mode` on Anthropic just prompts "output JSON"
→ cheap models emit unparseable JSON (OutputParserException); `function_calling`
lets a model omit required fields (Haiku dropped `microtasks` from PlannerDraft →
ValidationError). OpenAI-compatible → `"json_mode"` (their tool-calling support is
uneven). Every agent's `with_structured_output` call uses this function.

### `health_check(provider, llm, timeout=5)` — the preflight
One 1-token completion ("ping"). Returns `HealthResult(ok, latency_ms, reason,
kind)`; `kind` classified by `_classify` (message sniffing) into
`auth | timeout | unreachable | config` — so the UI can say "fix the key"
(permanent) vs "endpoint down" (transient). `POST /runs` refuses to start on a
failed preflight; the Settings "Test connection" button uses the same call.

### `probe_cache(provider, llm)` — cache checkability
Sends the SAME ~6k-token padded prompt twice (`_CACHE_PROBE_SENTENCE * 400` —
padding must exceed the 4096-token min cacheable prefix) and reads the second
response's `cache_read`. Returns `{"active": bool, "read_tokens": int}` or None on
probe failure (never raises — a broken probe must not break the test button).
~13k tokens per click, a deliberate spend on an explicit action. Why it exists:
caching is a *server-side* property the client can't switch on — this makes it
observable from the dashboard.

## token_usage.py (59 lines)

- `TokenUsage` (pydantic): `input/output/total_tokens` + `cache_read_tokens` /
  `cache_creation_tokens`. Cache fields are **SUBSETS of input_tokens** (LangChain
  already includes them) — informational, never added on top. `__add__` overloads
  `+` so usages accumulate naturally.
- `sum_usage(messages)`: iterates a trajectory, sums `usage_metadata` over every
  `AIMessage` (one per ReAct round); messages without usage silently skipped.
- The long comment carries a design decision worth quoting: per-model tallies are
  deliberately NOT accumulated onto RunConfig — nodes never return `config` in
  their result dict, so any mutation there is **dropped at the next checkpoint**
  (and empty after a crash) — "a dead, misleading second source". Cost is derived
  from durable events instead. (Same derive-don't-mirror principle as everywhere.)

## pricing.py (93 lines)

- `PRICE_TABLE`: bare Anthropic model ids → `ModelPrice(input, output)` USD per 1M
  tokens. Sonnet 5 priced at standard $3/$15 rather than its intro $2/$10:
  "a cost view that surprises high is a bug; one that surprises low after a price
  rise is worse." Cluster models intentionally absent.
- `cost_for(model, tokens…) -> Optional[Cost]`: model not in table → **None**
  (caller flags `has_unpriced`, total becomes a labeled lower bound — never a
  wrong number). Formula: uncached input = `input - cache_read - cache_creation`
  (subset semantics!), read at 0.1×, write at 1.25×, output at output rate.
- `Cost` breaks the total into input/output/cache_read/cache_write slices — the
  dashboard shows where money went.

---

## Relationships

```
SettingsStore (DB) ─► ProviderProfile + BaseLLMConfig (state.py models)
                                │
node runners ────────► build_chat_llm ──► ChatAnthropic / ChatOpenAI ──► agents
POST /runs preflight ─► health_check          Settings test button ─► health_check + probe_cache
agent trajectories ──► sum_usage ─► TokenUsage ─► orchestrator/token_budget.record_usage
                                                        │ (llm.usage events)
                                                        ▼
api/Service cost view ──────────────► pricing.cost_for  (pure derivation over events)
```

Exam sentence: "Five agents, zero vendor imports — providers is the only module
that knows ChatAnthropic from ChatOpenAI, and every empirical provider quirk
(temperature 400s, 4096 truncation, structured-output reliability, cache
minimums) is encoded here once, with the reason in a comment."
