"""GitHubRepoClient — read-only GitHub REST calls for the repo registry (GH #41).

Lives in the gitops boundary (ADR 0012): tokens never leave this package or gitops.py.
Result shape mirrors Service.test_github() ({ok, kind, reason, latency_ms}) so the API
can map kinds to HTTP statuses the same way the LLM preflight does.
"""
from __future__ import annotations

import enum
import time
from typing import List, Optional
from urllib.parse import urlparse

import requests
from pydantic import BaseModel

from observability import Category, get_logger

_log = get_logger(Category.DEPLOYER)

_API_BASE = "https://api.github.com"
_TIMEOUT_S = 5


class RepoValidationKind(str, enum.Enum):
    OK_PUBLIC = "ok_public"
    OK_PRIVATE = "ok_private"
    NOT_FOUND = "not_found"
    NO_ACCESS = "no_access"
    UNREACHABLE = "unreachable"
    UNEXPECTED = "unexpected"


class RepoValidationResult(BaseModel):
    ok: bool
    kind: RepoValidationKind
    reason: str
    default_branch: Optional[str] = None
    latency_ms: Optional[int] = None


class BranchListError(Exception):
    """Branch listing failed — carries the validation-style kind for HTTP mapping."""

    def __init__(self, kind: RepoValidationKind, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def parse_owner_repo(canonical_url: str) -> tuple[str, str]:
    """('owner', 'repo') from a canonical https URL. Raises ValueError otherwise."""
    parsed = urlparse(canonical_url)
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.scheme != "https" or not parsed.netloc or len(parts) < 2:
        raise ValueError(f"not a canonical https repo URL: {canonical_url!r}")
    return parts[0], parts[1]


class GitHubRepoClient:
    """Validate repos and list branches against the GitHub REST API. A non-empty
    token is sent as a Bearer header; empty = anonymous (public repos only)."""

    def _headers(self, token: str) -> dict:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def validate_repo(self, canonical_url: str, token: str) -> RepoValidationResult:
        """One GET /repos/{owner}/{repo} answers exists/private/access. GitHub returns
        404 for both 'does not exist' and 'private, no access' — the reason text
        carries that ambiguity instead of pretending to distinguish."""
        owner, repo = parse_owner_repo(canonical_url)
        started = time.monotonic()
        try:
            resp = requests.get(f"{_API_BASE}/repos/{owner}/{repo}",
                                headers=self._headers(token), timeout=_TIMEOUT_S)
        except requests.RequestException as exc:
            return RepoValidationResult(ok=False, kind=RepoValidationKind.UNREACHABLE,
                                        reason=f"GitHub unreachable: {exc.__class__.__name__}")
        latency_ms = int((time.monotonic() - started) * 1000)
        result = self._classify(resp, authed=bool(token), latency_ms=latency_ms)
        _log.info("gitops.repo_validated", repo=f"{owner}/{repo}", kind=str(result.kind),
                  ok=result.ok, latency_ms=latency_ms)
        return result

    def _classify(self, resp: requests.Response, *, authed: bool, latency_ms: int) -> RepoValidationResult:
        if resp.status_code == 200:
            body = resp.json()
            private = bool(body.get("private"))
            return RepoValidationResult(
                ok=True,
                kind=RepoValidationKind.OK_PRIVATE if private else RepoValidationKind.OK_PUBLIC,
                reason="private repo, token has access" if private else "public repo",
                default_branch=body.get("default_branch"),
                latency_ms=latency_ms,
            )
        if resp.status_code == 404:
            reason = ("repository not found, or the token lacks access to it" if authed
                      else "repository not found or private — add a token")
            return RepoValidationResult(ok=False, kind=RepoValidationKind.NOT_FOUND,
                                        reason=reason, latency_ms=latency_ms)
        if resp.status_code in (401, 403):
            return RepoValidationResult(ok=False, kind=RepoValidationKind.NO_ACCESS,
                                        reason=f"GitHub rejected the token (HTTP {resp.status_code})",
                                        latency_ms=latency_ms)
        return RepoValidationResult(ok=False, kind=RepoValidationKind.UNEXPECTED,
                                    reason=f"unexpected GitHub response (HTTP {resp.status_code})",
                                    latency_ms=latency_ms)

    def list_branches(self, canonical_url: str, token: str) -> List[str]:
        """Live branch names (first 100). Never cached in the DB — branches move."""
        owner, repo = parse_owner_repo(canonical_url)
        try:
            resp = requests.get(f"{_API_BASE}/repos/{owner}/{repo}/branches",
                                params={"per_page": 100},
                                headers=self._headers(token), timeout=_TIMEOUT_S)
        except requests.RequestException as exc:
            raise BranchListError(RepoValidationKind.UNREACHABLE,
                                  f"GitHub unreachable: {exc.__class__.__name__}")
        if resp.status_code == 200:
            return [b["name"] for b in resp.json()]
        if resp.status_code == 404:
            raise BranchListError(RepoValidationKind.NOT_FOUND,
                                  "repository not found, or the token lacks access to it")
        if resp.status_code in (401, 403):
            raise BranchListError(RepoValidationKind.NO_ACCESS,
                                  f"GitHub rejected the token (HTTP {resp.status_code})")
        raise BranchListError(RepoValidationKind.UNEXPECTED,
                              f"unexpected GitHub response (HTTP {resp.status_code})")
