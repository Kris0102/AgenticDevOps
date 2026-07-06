"""RepoStore — the repo registry (GH #41). DB is the source of truth for registered
repos; per-repo tokens are Fernet-encrypted at rest and masked on every read, the
same machinery as provider keys in SettingsStore.

`repo_url` is stored canonical (see `normalize_repo_url`) and unique — it is also
the cross-run memory key for lessons/code_chunks (ADR 0018), so one repo's memory
never splits across URL spellings.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel

from db.db import db
from db.models import RepoRecord
from db.secrets import decrypt, encrypt
from db.settings_store import SettingsStore
from gitops.github_api import RepoValidationResult
from observability import Category, get_logger

_log = get_logger(Category.DB)

# Shared with the service layer (mask-preservation checks on PUT /repos).
"""

  repo_store.py is a DTO layer over RepoRecord, which is the DB model. RepoStore handles encryption/decryption of tokens,
  normalization of repo URLs, and provides a clean interface for CRUD operations on the repo registry.
"""

MASK = "••••••••"
_SSH_RE = re.compile(r"^git@([^:]+):(.+)$")


def normalize_repo_url(url: str) -> str:
    """Canonical https form: SSH → https, http → https, no trailing `/` or `.git`,
    lowercase host. Raises ValueError when the URL has no owner/repo path."""
    url = url.strip()
    ssh = _SSH_RE.match(url)
    if ssh:
        url = f"https://{ssh.group(1)}/{ssh.group(2)}"
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"not a repository URL: {url!r}")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"repository URL needs an owner and a name: {url!r}")
    return f"https://{parsed.netloc.lower()}/{'/'.join(parts)}"


class DuplicateRepoError(Exception):
    """A repo with the same canonical URL is already registered."""


class RepoNotFoundError(Exception):
    """No registry row for the given repo_id."""

# THIS IS THE DTO CLASS for the RepoRecord model. It is used to return a masked view of the repo registry to the API layer.
class RepoEntry(BaseModel):
    """Masked registry view — `token` is the mask when a token is stored, else None.
    Raw tokens never leave the store; use `resolve_token` at the gitops seams."""
    repo_id: str
    repo_url: str
    label: str
    token: Optional[str] = None
    default_branch: Optional[str] = None
    push_branch_override: Optional[str] = None
    last_validated_at: Optional[datetime] = None
    last_validation_result: Optional[RepoValidationResult] = None
    created_at: Optional[datetime] = None

def _entry(row: RepoRecord) -> RepoEntry:
    return RepoEntry(
        repo_id=row.repo_id,
        repo_url=row.repo_url,
        label=row.label,
        token=MASK if row.token else None, # token thrown away on read; only the store can decrypt it, it is added in create/update
        default_branch=row.default_branch,
        push_branch_override=row.push_branch_override,
        last_validated_at=row.last_validated_at,
        last_validation_result=(RepoValidationResult.model_validate(row.last_validation_result)
                                if row.last_validation_result else None),
        created_at=row.created_at,
    )


class RepoStore:
    @staticmethod
    def list() -> List[RepoEntry]:
        with db.session_scope() as session:
            rows = session.query(RepoRecord).order_by(RepoRecord.created_at).all()
            return [_entry(r) for r in rows]

    @staticmethod
    def get(repo_id: str) -> RepoEntry:
        with db.session_scope() as session:
            row = session.get(RepoRecord, repo_id)
            if row is None:
                raise RepoNotFoundError(repo_id)
            return _entry(row)

    @staticmethod
    def find_by_url(canonical_url: str) -> Optional[RepoEntry]:
        with db.session_scope() as session:
            row = session.query(RepoRecord).filter(RepoRecord.repo_url == canonical_url).one_or_none()
            return _entry(row) if row else None

    @staticmethod
    def create(*, repo_url: str, label: Optional[str], token: Optional[str],
               push_branch_override: Optional[str],
               validation: RepoValidationResult) -> RepoEntry:
        """`repo_url` must already be canonical and validated by the caller."""
        with db.session_scope() as session:
            existing = session.query(RepoRecord).filter(RepoRecord.repo_url == repo_url).one_or_none()
            if existing is not None:
                raise DuplicateRepoError(repo_url)
            row = RepoRecord(
                repo_id=str(uuid.uuid4()),
                repo_url=repo_url,
                label=label or "/".join(repo_url.split("/")[-2:]),
                token=encrypt(token) if token else None,
                default_branch=validation.default_branch,
                push_branch_override=push_branch_override or None,
                last_validated_at=datetime.now(timezone.utc),
                last_validation_result=validation.model_dump(mode="json"),
                # set explicitly — the column default fires at flush, after _entry reads it
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
            _log.info("repo.registered", repo_url=repo_url, has_token=bool(token))
            return _entry(row)

    @staticmethod
    def update(repo_id: str, *, label: Optional[str] = None, token: Optional[str] = None,
               push_branch_override: Optional[str] = None) -> RepoEntry:
        """Mask-preservation (same rule as SettingsStore.update): token == mask or
        None keeps the stored value, '' clears it, anything else replaces it.
        push_branch_override: None keeps, '' clears."""
        with db.session_scope() as session:
            row = session.get(RepoRecord, repo_id)
            if row is None:
                raise RepoNotFoundError(repo_id)
            if label:
                row.label = label
            if token == "":
                row.token = None
            elif token and token != MASK:
                row.token = encrypt(token)
            if push_branch_override == "":
                row.push_branch_override = None
            elif push_branch_override is not None:
                row.push_branch_override = push_branch_override
            return _entry(row)

    @staticmethod
    def delete(repo_id: str) -> None:
        """Plain delete — runs keep their repo_url snapshot and a dangling repo_id."""
        with db.session_scope() as session:
            row = session.get(RepoRecord, repo_id)
            if row is None:
                raise RepoNotFoundError(repo_id)
            session.delete(row)
            _log.info("repo.deleted", repo_url=row.repo_url)

    @staticmethod
    def record_validation(repo_id: str, result: RepoValidationResult) -> None:
        """Persist the latest validation snapshot (+ refreshed default_branch)."""
        with db.session_scope() as session:
            row = session.get(RepoRecord, repo_id)
            if row is None:
                raise RepoNotFoundError(repo_id)
            row.last_validated_at = datetime.now(timezone.utc)
            row.last_validation_result = result.model_dump(mode="json")
            if result.default_branch:
                row.default_branch = result.default_branch

    @staticmethod
    def resolve_token(repo_id: Optional[str]) -> str:
        """Token chain for clone/push: per-repo → global Settings (env fallback
        inside) → '' anonymous. Never log the return value."""
        if repo_id:
            with db.session_scope() as session:
                row = session.get(RepoRecord, repo_id)
                if row is not None and row.token:
                    raw = decrypt(row.token)
                    if raw:
                        return raw
        return SettingsStore.github_token()
