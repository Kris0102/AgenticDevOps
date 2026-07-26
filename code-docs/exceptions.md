# src/Exceptions/ — Code Explanation

Five small files (~105 lines), but they ENCODE the failure taxonomy — each
exception class maps to one run outcome. Worth knowing cold.

---

## BadMicroTaskExceptions.py
```python
class BadMicroTaskException(Exception)
```
Raised by the Planner's deterministic quality gate (`quality.py`) when a
synthesized microtask fails validation (missing target files, tautological or
existence-only acceptance criteria…). **Caught inside the Planner itself** — the
message is fed back into the next synthesis prompt as corrective context, up to
`PLANNER_MAX_SYNTHESIS_ATTEMPTS`. Never escapes the agent → never a run outcome.

## RunExecutionError.py
```python
class RunExecutionError(Exception):
    def __init__(self, phase: str, reason: str)
```
The **terminal FAILED** path. Raised by an agent/node on an unrecoverable,
deterministic error. Node wrappers catch it and hand it to `RunFailureHandler`
(status → FAILED + audit events). Carries `phase` so the failure_reason names the
node. Message format `[{phase}] {reason}`.

## RetriableError.py — the interesting one
```python
class RetriableError(Exception):
    def __init__(self, reason: str, phase: str = "llm")
```
The **STALLED** path for transient infrastructure failures. Escapes to
`Service._drive`, which does NOT mark the run FAILED — it leaves it non-terminal
(reads as STALLED), checkpoint intact, re-drivable via `POST /continue`.

Two supporting pieces in the same file:

**`TRANSIENT_LLM_ERRORS` tuple** — the whitelist of safe-to-retry exception types,
from BOTH SDKs:
- openai: `APITimeoutError, APIConnectionError, RateLimitError, InternalServerError`
- anthropic: same four names, **different classes** — the comment warns that
  `isinstance` on the openai types silently misses Anthropic errors. This was a
  real bug class: same-named exceptions from two SDKs.

**`is_transient_llm_error(exc)`** — three-layer classifier:
1. `isinstance(exc, TRANSIENT_LLM_ERRORS)` — the typed check.
2. `getattr(exc, "status_code", None) in (429, 500, 502, 503, 504)` — catches
   errors that arrived **wrapped** (inside a LangGraph ReAct agent the original
   type can be lost).
3. Message sniff: `"overloaded"`, `"rate limit"`, `"code: 429"`, … — last resort.

**Why it matters (ADR 0023):** before this, the cluster's "GPU overloaded" 429
slipped through a generic `except Exception`, and the planner/reviewer "limped
forward on an empty investigation" — a no-progress loop caused by treating an
infra failure as a normal result. The design rule in the docstring: only
*genuinely transient* causes belong here — a deterministic failure that gets
whitelisted "looks retriable forever".

## RunStoppedError.py
```python
class RunStoppedError(RuntimeError):
    def __init__(self, run_id: str)
```
The **STOPPED** path (ADR 0033). Raised by `Orchestrator._check_stop` at the top
of a node when `run_control.is_stop_requested(run_id)` is true — aborts before the
node does any work. Escapes to `Service`, which finalizes the run as STOPPED.

## TokenBudgetExceededError.py
```python
class TokenBudgetExceededError(Exception):
    def __init__(self, phase: str, used: int, limit: int)
```
The **STALLED-with-topup** path (ADR 0025). Raised by
`orchestrator/token_budget.py` at the top of every agent node when
`tokens_used_total >= max_total_tokens`. The docstring explains the key design
decision: it is *deliberately NOT a subclass of `RunExecutionError`* — otherwise
the node's own `except RunExecutionError` would catch it and route to terminal
FAILED. By being a distinct type it escapes `graph.invoke()` to `Service._drive`,
which stalls the run; the human raises the budget via `/continue` (with
`additional_tokens`) or closes via `/fail`. Carries `used`/`limit` so the message
is exact: `Token budget exceeded: 51234/50000 tokens used`.

---

## The mapping to memorize

| Exception | Caught by | Run outcome |
|---|---|---|
| `BadMicroTaskException` | Planner itself (retry with feedback) | none — internal loop |
| `RunExecutionError` | node wrapper → `RunFailureHandler` | **FAILED** |
| `RetriableError` | `Service._drive` | **STALLED** (transient) |
| `TokenBudgetExceededError` | `Service._drive` | **STALLED** (token-budget, top-up required) |
| `RunStoppedError` | `Service` | **STOPPED** |

Exception *types* are the routing mechanism for the failure taxonomy — the class
hierarchy IS the design. (BLOCKED is the odd one out: not an exception at all, but
a reflector *decision* — the run ends through the normal failure path with the
conflict named.)
