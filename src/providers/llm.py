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
from orchestrator.state import LLMConfig, ProviderProfile, ProviderType

_log = get_logger(Category.ORCHESTRATOR)


def build_chat_llm(
    provider: ProviderProfile,
    llm: LLMConfig,
    *,
    timeout_seconds: int = 120,
) -> BaseChatModel:
    """Construct the chat model for `provider`+`llm`. Never logs the api_key."""
    if provider.type == ProviderType.ANTHROPIC:
        return ChatAnthropic(
            model_name=llm.model,
            api_key=provider.api_key,  # type: ignore[arg-type]
            temperature=llm.temperature,
            max_retries=llm.max_retries,
            timeout=timeout_seconds,
            stop=None,
        )
    # openai_compatible (the cluster + anything OpenAI-shaped)
    if not provider.base_url:
        raise ValueError(f"provider '{provider.id}' is openai_compatible but has no base_url")
    return ChatOpenAI(
        model=llm.model,
        api_key=provider.api_key,  # type: ignore[arg-type]
        base_url=provider.base_url,
        temperature=llm.temperature,
        max_retries=llm.max_retries,
        timeout=timeout_seconds,
    )


@dataclass
class HealthResult:
    ok: bool
    latency_ms: int
    reason: Optional[str] = None      # None when ok; else a short human message
    kind: Optional[str] = None        # "auth" | "unreachable" | "timeout" | "config" when not ok


def health_check(provider: ProviderProfile, llm: LLMConfig, *, timeout_seconds: int = 5) -> HealthResult:
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
