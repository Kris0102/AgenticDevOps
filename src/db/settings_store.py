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
    CoderLLMConfig,
    Knobs,
    LLMConfig,
    PlannerLLMConfig,
    ProviderProfile,
    ProviderType,
    ReflectorLLMConfig,
    ReviewerLLMConfig,
    RunConfig,
    RunDefaults,
    TesterLLMConfig,
)

_log = get_logger(Category.ORCHESTRATOR)

_CLUSTER_PROVIDER_ID = "cluster"
_MASK = "••••••••"


def _env_seed() -> AppSettings:
    """Build the initial settings from env — the current cluster provider + the old knob
    defaults — so a fresh DB behaves exactly like the pre-settings deployment."""
    model = os.getenv("LLM_MODEL", "google/gemma-4-31B-it")
    providers = ProviderProfile.model_validate(dict(
            id=_CLUSTER_PROVIDER_ID,
            label="Cluster",
            type=ProviderType.OPENAI_COMPATIBLE,
            base_url=os.getenv("LLM_BASE_URL"),
            api_key=os.getenv("LLM_API_KEY"),
        ))
    return AppSettings(
        providers=[providers],
        selected=LLMConfig(
            planner=PlannerLLMConfig(provider_id=_CLUSTER_PROVIDER_ID, model=model),
            reviewer=ReviewerLLMConfig(provider_id=_CLUSTER_PROVIDER_ID, model=model),
            coder=CoderLLMConfig(provider_id=_CLUSTER_PROVIDER_ID, model=model),
            tester=TesterLLMConfig(provider_id=_CLUSTER_PROVIDER_ID, model=model),
            reflector=ReflectorLLMConfig(provider_id=_CLUSTER_PROVIDER_ID, model=model),
        ),
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


_AGENTS = ("planner", "reviewer", "coder", "tester", "reflector")


def _migrate_blob(raw: dict) -> dict:
    """Upgrade an older stored `AppSettings` JSON in place-of-read to the current shape (GH
    #71). The pre-#71 `selected` was ONE flat LLMConfig (`{provider_id, model, temperature,
    max_retries}`); it is now a per-agent map. Fan the old flat selection out to all five
    agents so an existing DB keeps working (behaviour unchanged — every agent on the same
    model). Also drops the removed `role` field off stored providers. Idempotent."""
    if not isinstance(raw, dict):
        return raw
    raw = dict(raw)
    selected = raw.get("selected")
    if isinstance(selected, dict) and "provider_id" in selected:
        base = {k: selected[k] for k in ("provider_id", "model", "temperature", "max_retries")
                if k in selected}
        raw["selected"] = {a: {**base, "name": a} for a in _AGENTS}
    elif isinstance(selected, dict) and "reflector" not in selected and "coder" in selected:
        # Migrated to per-agent before Reflector existed — clone the Coder's selection.
        raw["selected"] = {**selected, "reflector": {**selected["coder"], "name": "reflector"}}
    providers = raw.get("providers")
    if isinstance(providers, list):
        raw["providers"] = [{k: v for k, v in p.items() if k != "role"} if isinstance(p, dict) else p
                            for p in providers]
    return raw


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
            return _decrypt_secrets(AppSettings.model_validate(_migrate_blob(row.config)))

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
    Collects the DISTINCT ProviderProfiles referenced across the five per-agent selections
    (GH #71) — one entry per id, shared providers not duplicated — and validates every one
    exists. Raises on the first missing provider so the caller surfaces it before the run
    starts."""
    settings = SettingsStore.get()
    llm = override or settings.selected
    providers: dict[str, ProviderProfile] = {}
    for agent in llm.agents():
        if agent.provider_id in providers:
            continue
        provider = SettingsStore.provider(settings, agent.provider_id)
        if provider is None:
            raise ValueError(
                f"provider '{agent.provider_id}' selected for {agent.name} not found in settings")
        providers[agent.provider_id] = provider
    return RunConfig(llm=llm, providers=providers, knobs=knobs_override or settings.knobs)



def check_for_updated_models() -> LLMConfig :
    """Check the DB for any model changes during this run. Returns a new LLMConfig if
    any agent's model changed, else None. The caller can then prompt the user to confirm
    the change before starting the run."""
    settings = SettingsStore.get()
    return LLMConfig(
        planner=settings.selected.planner,
        reviewer=settings.selected.reviewer,
        coder=settings.selected.coder,
        tester=settings.selected.tester,
        reflector=settings.selected.reflector,
   )
    
    
    