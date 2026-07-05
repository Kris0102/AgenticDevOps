"""SettingsStore — the UI-managed settings hub (DB source of truth, env = seed/fallback).

The whole `AppSettings` lives as one JSON row (`app_settings`, id=1). `get()` returns it,
seeding from env on an empty table so existing deploys keep working. `get_public()` masks
every provider api_key for the API (raw keys never leave the backend). `resolve_run_config`
computes a run's effective config with precedence: per-run override › global (DB) › env.
"""
from __future__ import annotations

import os
from typing import Optional

from db.db import db
from db.models import AppSettings as AppSettingsRow
from db.secrets import decrypt, encrypt
from observability import Category, get_logger
from orchestrator.state import (
    AppSettings,
    Knobs,
    LLMConfig,
    ProviderProfile,
    ProviderType,
    RunConfig,
    RunDefaults,
)

_log = get_logger(Category.ORCHESTRATOR)

_CLUSTER_PROVIDER_ID = "cluster"
_MASK = "••••••••"


def _env_seed() -> AppSettings:
    """Build the initial settings from env — the current cluster provider + the old knob
    defaults — so a fresh DB behaves exactly like the pre-settings deployment."""
    model = os.getenv("LLM_MODEL", "google/gemma-4-31B-it")
    provider = ProviderProfile(
        id=_CLUSTER_PROVIDER_ID,
        label="Inference cluster",
        type=ProviderType.OPENAI_COMPATIBLE,
        base_url=os.getenv("LLM_BASE_URL", "http://131.159.60.153:3000/inference"),
        api_key=os.getenv("CLUSTER_API_KEY") or os.getenv("OPENAI_API_KEY"),
        models=[model],
    )
    return AppSettings(
        providers=[provider],
        selected=LLMConfig(provider_id=_CLUSTER_PROVIDER_ID, model=model,
                           max_retries=int(os.getenv("LLM_RETRIES", "3"))),
        knobs=Knobs(),          # field defaults already mirror the old env defaults
        defaults=RunDefaults(),
        github_token=os.getenv("GITHUB_TOKEN") or None,
    )


def _encrypt_secrets(s: AppSettings) -> AppSettings:
    """Secrets → `enc:` Fernet tokens for the stored JSON (idempotent)."""
    return s.model_copy(update={
        "providers": [p.model_copy(update={"api_key": encrypt(p.api_key)}) for p in s.providers],
        "github_token": encrypt(s.github_token),
    })


def _decrypt_secrets(s: AppSettings) -> AppSettings:
    """Stored `enc:` tokens → raw secrets (legacy plaintext passes through)."""
    return s.model_copy(update={
        "providers": [p.model_copy(update={"api_key": decrypt(p.api_key)}) for p in s.providers],
        "github_token": decrypt(s.github_token),
    })


class SettingsStore:
    @staticmethod
    def get() -> AppSettings:
        """Full settings WITH raw secrets — backend-internal only (factory, gitops,
        resolution). Seeds the row from env on first read; decrypts stored secrets."""
        with db.session_scope() as session:
            row = session.get(AppSettingsRow, 1)
            if row is None:
                seed = _env_seed()
                session.add(AppSettingsRow(id=1, config=_encrypt_secrets(seed).model_dump(mode="json")))
                _log.info("settings.seeded_from_env", providers=len(seed.providers))
                return seed
            return _decrypt_secrets(AppSettings.model_validate(row.config))

    @staticmethod
    def get_public() -> AppSettings:
        """Settings for the UI — every secret (provider api_key, github_token) masked."""
        s = SettingsStore.get()
        return s.model_copy(update={
            "providers": [
                p.model_copy(update={"api_key": _MASK if p.api_key else None})
                for p in s.providers
            ],
            "github_token": _MASK if s.github_token else None,
        })

    @staticmethod
    def update(new: AppSettings) -> AppSettings:
        """Persist a full settings object. Any secret that comes back as the mask (UI
        didn't retype it) keeps its stored value — editing other fields never wipes a
        key. Secrets are encrypted at rest. Returns the masked view."""
        current = SettingsStore.get()
        stored_keys = {p.id: p.api_key for p in current.providers}
        merged_providers = [
            p if (p.api_key and p.api_key != _MASK)
            else p.model_copy(update={"api_key": stored_keys.get(p.id)})
            for p in new.providers
        ]
        token = new.github_token if (new.github_token and new.github_token != _MASK) else (
            current.github_token if new.github_token == _MASK else new.github_token
        )
        merged = new.model_copy(update={"providers": merged_providers, "github_token": token})
        payload = _encrypt_secrets(merged).model_dump(mode="json")
        with db.session_scope() as session:
            row = session.get(AppSettingsRow, 1)
            if row is None:
                session.add(AppSettingsRow(id=1, config=payload))
            else:
                row.config = payload
        return SettingsStore.get_public()

    @staticmethod
    def github_token() -> str:
        """The raw GitHub token for gitops (clone/push). Settings first, env fallback,
        '' = anonymous. Never log this."""
        return SettingsStore.get().github_token or os.getenv("GITHUB_TOKEN", "")

    @staticmethod
    def provider(settings: AppSettings, provider_id: str) -> Optional[ProviderProfile]:
        return next((p for p in settings.providers if p.id == provider_id), None)


def resolve_run_config(override: Optional[LLMConfig] = None,
                       knobs_override: Optional[Knobs] = None) -> RunConfig:
    """The effective config a run executes under: per-run override › global (DB) › env seed.
    Resolves the selected LLMConfig to its ProviderProfile (carrying base_url/key). Raises
    if the selected provider is missing (caller surfaces it before starting the run)."""
    settings = SettingsStore.get()
    llm = override or settings.selected
    provider = SettingsStore.provider(settings, llm.provider_id)
    if provider is None:
        raise ValueError(f"selected provider '{llm.provider_id}' not found in settings")
    return RunConfig(llm=llm, provider=provider, knobs=knobs_override or settings.knobs)
