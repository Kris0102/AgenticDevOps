"""The single place an LLM client is constructed (closes provider-coupling TODO #6).

`build_chat_llm` dispatches on the provider TYPE — an OpenAI-compatible endpoint (the
cluster, or any base_url) or Anthropic (Claude) — so switching providers is a settings
change, not a code change. `health_check` is a cheap preflight the API runs before starting
a run (and the settings "Test connection" button), so a dead/mis-keyed provider fails fast
instead of starting a doomed run. Base_url/api_key come from the resolved ProviderProfile;
model/temperature/retries/timeout from the run's LLMConfig + knobs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from observability import Category, get_logger
from orchestrator.state import BaseLLMConfig, ProviderProfile, ProviderType

_log = get_logger(Category.ORCHESTRATOR)


def _anthropic_thinking_param(model: str, enabled: bool) -> Optional[dict]:
    """The `thinking` value to send to ChatAnthropic for this model + the user's on/off toggle,
    per Anthropic's per-model rules (which differ — you can't omit-and-assume). Returns None to
    send NO thinking param.

      · Haiku 4.5 / pre-4.6 tiers — adaptive thinking unavailable; `adaptive`/`disabled` 400.
        → always None (the toggle is a no-op; the model's own default stands).
      · Opus 4.8 / 4.7 — thinking is OFF unless you EXPLICITLY send `{adaptive}`.
        → on: {adaptive}  off: {disabled}
      · Opus 4.6 / Sonnet 4.6 — off unless `{adaptive}`; `{disabled}` also accepted.
        → on: {adaptive}  off: {disabled}
      · Sonnet 5 — on by DEFAULT; `{disabled}` turns it off.
        → on: {adaptive}  off: {disabled}   (sending {adaptive} explicitly is harmless)
      · Fable 5 / Mythos 5 — always on; `{disabled}` is NOT supported (400).
        → on: {adaptive}  off: None (can't disable — leave on; documented)

    A model string we don't recognise is treated as non-adaptive → None (safe: the toggle is
    ignored and the model uses its own default, rather than risking a 400)."""
    m = model.lower()
    adaptive_capable = any(t in m for t in (
        "opus-4-6", "opus-4-7", "opus-4-8", "sonnet-5", "sonnet-4-6", "fable-5", "mythos-5"))
    if not adaptive_capable:
        return None
    if enabled:
        return {"type": "adaptive"}
    if "fable-5" in m or "mythos-5" in m:
        return None                      # thinking can't be disabled on these — leave on
    return {"type": "disabled"}


def structured_output_method(provider: ProviderProfile) -> str:
    """Which `with_structured_output` method reliably yields schema-valid JSON on this provider.

    Anthropic → `json_schema`: the API's real structured-outputs mode (`output_config.format`,
    GA on Haiku 4.5 / Sonnet 5 / Opus 4.8) — output is constrained to the schema SERVER-SIDE,
    so required fields can't be omitted. The alternatives both failed in practice: `json_mode`
    on Anthropic just prompts "output JSON" and parses (cheap models emit unparseable JSON →
    OutputParserException); `function_calling` lets the model omit required fields (Haiku
    dropped `microtasks` from PlannerDraft → ValidationError). Verified live with the real
    PlannerDraft schema. OpenAI-compatible endpoints (the cluster) → `json_mode`, which is what
    works there (their tool-calling support is uneven)."""
    return "json_schema" if provider.type == ProviderType.ANTHROPIC else "json_mode"


def build_chat_llm(
    provider: ProviderProfile,
    llm: BaseLLMConfig,
    *,
    timeout_seconds: int = 120,
) -> BaseChatModel:
    """Construct the chat model for `provider`+`llm`. Never logs the api_key."""
    if provider.type == ProviderType.ANTHROPIC:
        # Do NOT send `temperature`: the current Claude models (Sonnet 5, Opus 4.8/4.7, Fable 5)
        # REJECT it with a 400 ("`temperature` is deprecated for this model") — steer via
        # prompting instead. Omitting it is safe on the older models too (they use their
        # default). `temperature=None` keeps langchain from putting it in the request body.
        # Extended ("adaptive") thinking: the per-provider `thinking` toggle, mapped to the
        # right param PER MODEL (they differ — Opus 4.8/4.7 need an explicit `{adaptive}` to
        # turn ON, Sonnet 5 is on by default, Haiku has no adaptive mode, Fable 5 can't be
        # turned off). See _anthropic_thinking_param.
        return ChatAnthropic(
            model_name=llm.model,
            api_key=provider.api_key,  # type: ignore[arg-type]
            temperature=None,
            thinking=_anthropic_thinking_param(llm.model, provider.thinking),
            # Explicit output ceiling: langchain's unset default resolves to 4096, which
            # TRUNCATES a Coder writing a whole file in one write_sandbox_file call (cluster
            # runs have produced 17k-token outputs). 16k is the safe non-streaming maximum
            # (the SDK refuses much larger without streaming) and ample for one file.
            max_tokens_to_sample=16_000,   # field alias for max_tokens (pydantic alias)
            max_retries=provider.max_retries,
            timeout=timeout_seconds,
            stop=None,
            # Prompt caching (GH #62): merged into the request body as the TOP-LEVEL
            # `cache_control` param — the direct Anthropic API's automatic-caching form,
            # which caches the last cacheable block of every request. ReAct histories are
            # append-only, so each step re-reads the previous step's prefix at ~0.1x input
            # price (write 1.25x, 5-min TTL, breaks even after 2 calls; prefixes <4096
            # tokens silently don't cache). Verify via usage_metadata
            # input_token_details.cache_read (surfaced in llm.usage events).
            # Per-provider extra_request_params (GH #65) merge after — they may override.
            model_kwargs={"cache_control": {"type": "ephemeral"},
                          **provider.extra_request_params},
        )
    # openai_compatible (the cluster + anything OpenAI-shaped)
    if not provider.base_url:
        raise ValueError(f"provider '{provider.id}' is openai_compatible but has no base_url")
    return ChatOpenAI(
        model=llm.model,
        api_key=provider.api_key,  # type: ignore[arg-type]
        base_url=provider.base_url,
        temperature=llm.temperature,
        max_retries=provider.max_retries,
        timeout=timeout_seconds,
        # Vendor params (GH #65): prompt_cache_key, or anything the endpoint accepts.
        model_kwargs=dict(provider.extra_request_params),
    )


@dataclass
class HealthResult:
    ok: bool
    latency_ms: int
    reason: Optional[str] = None      # None when ok; else a short human message
    kind: Optional[str] = None        # "auth" | "unreachable" | "timeout" | "config" when not ok


def health_check(provider: ProviderProfile, llm: BaseLLMConfig, *, timeout_seconds: int = 5) -> HealthResult:
    """Preflight probe: one 1-token completion. Classifies failure so the UI can tell the
    user whether to fix the key (auth, permanent) or the endpoint is down (unreachable)."""
    start = time.time()
    try:
        client = build_chat_llm(provider, llm, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return HealthResult(False, 0, f"invalid provider config: {exc}", "config")
    try:
        client.invoke([HumanMessage(content="ping")])
        return HealthResult(True, int((time.time() - start) * 1000))
    except Exception as exc:
        return HealthResult(
            ok=False,
            latency_ms=int((time.time() - start) * 1000),
            reason=_short(exc),
            kind=_classify(exc),
        )


# One deterministic sentence (~15 tokens), repeated to pad the cache probe past every
# provider's minimum cacheable prefix (Anthropic 4096 tokens is the largest; OpenAI 1024).
_CACHE_PROBE_SENTENCE = "The quick brown fox jumps over the lazy dog near the riverbank at dawn. "


def probe_cache(provider: ProviderProfile, llm: BaseLLMConfig, *,
                timeout_seconds: int = 30) -> Optional[dict]:
    """Settings 'Test connection' cache probe (GH #65): send the SAME padded prompt
    twice and read the second response's cache_read. Caching is a provider/server
    property the client mostly can't switch on (OpenAI: automatic; self-hosted vLLM:
    server flag) — this makes it CHECKABLE from the dashboard instead of invisible.

    Returns {"active": bool, "read_tokens": int}, or None when the probe itself failed
    (never raises — a broken probe must not break the test button). Costs ~13k tokens
    per click; a deliberate spend on an explicit test action."""
    try:
        client = build_chat_llm(provider, llm, timeout_seconds=timeout_seconds)
        padding = _CACHE_PROBE_SENTENCE * 400   # ~6k tokens, byte-identical across calls
        messages = [HumanMessage(content=f"{padding}\nReply with the single word: ok")]
        client.invoke(messages)                  # warm — writes the cache where supported
        second = client.invoke(messages)         # measure
        details = (second.usage_metadata or {}).get("input_token_details") or {}
        read = int(details.get("cache_read") or 0)
        _log.info("provider.cache_probe", provider=provider.id, active=read > 0,
                  read_tokens=read)
        return {"active": read > 0, "read_tokens": read}
    except Exception as exc:
        _log.warning("provider.cache_probe_failed", provider=provider.id, error=_short(exc))
        return None


def _classify(exc: Exception) -> str:
    blob = f"{type(exc).__name__} {exc}".lower()
    if any(t in blob for t in ("auth", "api key", "apikey", "401", "403", "unauthor", "permission")):
        return "auth"
    if any(t in blob for t in ("timeout", "timed out")):
        return "timeout"
    if any(t in blob for t in ("connect", "unreachable", "refused", "resolve", "dns", "network")):
        return "unreachable"
    return "unreachable"


def _short(exc: Exception) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return msg[:160]
