import anthropic
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)


class RetriableError(Exception):
    """Raised for a TRANSIENT failure that is not the run's fault — an LLM API timeout,
    connection drop, rate-limit, or 5xx from the inference cluster. The run's state is
    intact; the same node just needs to run again.

    The `_drive` guard treats this differently from a real error: instead of marking the
    run FAILED, it leaves the run non-terminal so it reads STALLED (no worker driving it)
    and can be re-driven from its checkpoint via POST /runs/{id}/continue. Only whitelist
    genuinely transient, safe-to-retry causes here — anything deterministic must still FAIL,
    or a doomed run looks retriable forever. See ADR 0019 / continue feature.
    """

    def __init__(self, reason: str, phase: str = "llm"):
        self.phase = phase
        self.reason = reason
        super().__init__(f"[transient:{phase}] {reason}")


# Safe-to-retry inference failures: request timeout, connection drop, 429 rate-limit /
# "model overloaded", and 5xx from the cluster. Deterministic failures (bad structured
# output, recursion-budget exhaustion, a code bug) are NOT here — retrying them just loops.
TRANSIENT_LLM_ERRORS = (
    # openai SDK (the cluster / any OpenAI-compatible endpoint)
    APITimeoutError,
    APIConnectionError,
    RateLimitError,       # HTTP 429 — includes the cluster's "model overloaded" response
    InternalServerError,  # HTTP 5xx
    # anthropic SDK — SAME names, DIFFERENT classes; isinstance on the openai types misses
    # them (an Anthropic "Request timed out" also slips past the message sniff below).
    # Covers timeout, connection drop, 429 rate-limit, 5xx, and the 529 overloaded_error.
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


def is_transient_llm_error(exc: BaseException) -> bool:
    """True when `exc` is a transient, safe-to-retry inference failure — so the caller can
    raise RetriableError (→ STALL + /continue) instead of degrading past a failed LLM call.

    Catches the concrete openai types AND a 429/5xx that arrived WRAPPED — e.g. raised
    inside a LangGraph ReAct agent, where the original type may be lost — by sniffing the
    HTTP status and message. Critical for the cluster's "GPU overloaded" 429: without the
    fallback it slips through a generic `except Exception` and the planner/reviewer limp
    forward on an empty investigation (a no-progress loop). See ADR 0023.
    """
    if isinstance(exc, TRANSIENT_LLM_ERRORS):
        return True
    if getattr(exc, "status_code", None) in (429, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "overloaded", "code: 429", "rate limit", "code: 503",
        "service unavailable", "try again later",
    ))
