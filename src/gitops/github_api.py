"""GitHubRepoClient — GitHub REST calls for the repo registry (GH #41) and the
deploy PR (ADR 0027). Read paths validate repos / list branches; the single write
path is create_pull_request, driven by GitOps.create_pr after a successful push.

Lives in the gitops boundary (ADR 0012): tokens never leave this package or gitops.py.
Result shape mirrors Service.test_github() ({ok, kind, reason, latency_ms}) so the API
can map kinds to HTTP statuses the same way the LLM preflight does.
"""
from __future__ import annotations

import enum
import time
import uuid
from typing import List, Optional
from urllib.parse import urlparse

import requests
from pydantic import BaseModel

from observability import Category, get_logger

_log = get_logger(Category.DEPLOYER)

_API_BASE = "https://api.github.com"
_TIMEOUT_S = 5
# PR creation is a write — slower than the preflight GETs the 5s budget is tuned for.
_WRITE_TIMEOUT_S = 15


class RepoValidationKind(str, enum.Enum):
    OK_PUBLIC = "ok_public"
    OK_PRIVATE = "ok_private"
    NOT_FOUND = "not_found"
    NO_ACCESS = "no_access"
    NO_PUSH_ACCESS = "no_push_access"
    NO_PR_ACCESS = "no_pr_access"
    UNREACHABLE = "unreachable"
    UNEXPECTED = "unexpected"


class RepoValidationResult(BaseModel):
    ok: bool
    kind: RepoValidationKind
    reason: str
    default_branch: Optional[str] = None
    latency_ms: Optional[int] = None
    can_push: Optional[bool] = None
    can_create_pr: Optional[bool] = None


class BranchListError(Exception):
    """Branch listing failed — carries the validation-style kind for HTTP mapping."""

    def __init__(self, kind: RepoValidationKind, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


class PrCreationError(Exception):
    """PR creation failed — carries a human-readable reason. Deliberately NOT fatal
    to the caller's run: the push already succeeded (ADR 0027). Never contains the token."""


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
        """GET /repos/{owner}/{repo} answers exists/private (GitHub returns 404 for
        both 'does not exist' and 'private, no access' — the reason text carries that
        ambiguity instead of pretending to distinguish). When authenticated, push and
        PR-create rights are independently confirmed with real (but side-effect-free)
        write probes — see `_check_push_access` / `_check_pr_access`.

        Push access is required and gates `ok` (no push, no usable repo, ever). PR
        access is reported (`can_create_pr`) but never affects `ok` here — whether a
        missing PR permission should block anything depends on
        Settings.defaults.auto_create_pr, which this module deliberately doesn't know
        about (ADR 0012, stays Settings-free); the caller (Service) decides."""
        owner, repo = parse_owner_repo(canonical_url)
        started = time.monotonic()
        try:
            resp = requests.get(f"{_API_BASE}/repos/{owner}/{repo}",
                                headers=self._headers(token), timeout=_TIMEOUT_S)
        except requests.RequestException as exc:
            return RepoValidationResult(ok=False, kind=RepoValidationKind.UNREACHABLE,
                                        reason=f"GitHub unreachable: {exc.__class__.__name__}")
        latency_ms = int((time.monotonic() - started) * 1000)
        result = self._classify(resp, authed=bool(token), latency_ms=latency_ms,
                                token=token, owner=owner, repo=repo)
        _log.info("gitops.repo_validated", repo=f"{owner}/{repo}", kind=str(result.kind),
                  ok=result.ok, can_push=result.can_push, can_create_pr=result.can_create_pr,
                  latency_ms=latency_ms)
        return result

    def _classify(self, resp: requests.Response, *, authed: bool, latency_ms: int,
                  token: str, owner: str, repo: str) -> RepoValidationResult:
        if resp.status_code == 200:
            body = resp.json()
            private = bool(body.get("private"))
            default_branch = body.get("default_branch")
            # Push access is a real write probe (`_check_push_access`), NOT the GET's
            # `permissions.push` — that reflects the authenticated USER's role, not a
            # fine-grained PAT's narrower granted scope (an admin holding a read-only
            # token still shows push=true there). Anonymous (no token) can't push to
            # anything, so it's treated as no-push directly (can_push=False), no probe.
            can_push = self._check_push_access(owner, repo, token) if authed else False

            if not can_push:
                # NO_PUSH_ACCESS is ok=False, but the repo IS readable (this is a 200) —
                # so it stays registrable (clone-only) and dry-runnable; only a DEPLOYING
                # run is blocked, gated downstream in Service.resolve_repo (ADR 0029).
                # More fundamental than PR access (every deploying run needs push), so we
                # skip the PR probe: one call saved, one unambiguous reason instead of two.
                return RepoValidationResult(
                    ok=False, kind=RepoValidationKind.NO_PUSH_ACCESS,
                    reason="token does not have push access to this repository" if authed else "Token not provided! Token required for repository write access unless you set run to 'dry run'.",
                    default_branch=default_branch, latency_ms=latency_ms, can_push=False,
                )

            can_create_pr = self._check_pr_access(owner, repo, token, default_branch=default_branch) if authed else None
            return RepoValidationResult(
                ok=True,
                kind=RepoValidationKind.OK_PRIVATE if private else RepoValidationKind.OK_PUBLIC,
                reason="private repo, token has access" if private else "public repo",
                default_branch=default_branch, latency_ms=latency_ms,
                can_push=can_push if authed else None, can_create_pr=can_create_pr,
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

    def _check_push_access(self, owner: str, repo: str, token: str) -> bool:
        """Does this token actually have write (push/Contents) access? Probes the
        real write path rather than trusting a GET: attempts to create a git ref
        pointing at a SHA that cannot possibly exist (40 zeros). GitHub authorizes a
        write BEFORE validating its payload, so the response cleanly distinguishes
        the two:
          - 403/404 → no write access — nothing is created
          - 422     → write access confirmed; rejected only because the SHA doesn't
                      exist — nothing is created
          - 201     → shouldn't happen (the SHA is bogus), but if GitHub ever did
                      create it, delete the ref immediately so the probe leaves zero
                      footprint
        Fails closed (False) on network errors."""
        probe_branch = f"agenticdevops-access-probe-{uuid.uuid4().hex[:10]}"
        try:
            resp = requests.post(
                f"{_API_BASE}/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{probe_branch}", "sha": "0" * 40},
                headers=self._headers(token), timeout=_TIMEOUT_S,
            )
        except requests.RequestException:
            return False
        if resp.status_code == 201:
            try:
                requests.delete(f"{_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{probe_branch}",
                                headers=self._headers(token), timeout=_TIMEOUT_S)
            except requests.RequestException:
                pass
            return True
        return resp.status_code == 422

    def _check_pr_access(self, owner: str, repo: str, token: str, *,
                         default_branch: Optional[str]) -> bool:
        """Does this token have write access to open pull requests? A plain GET on
        .../pulls only proves READ access (a read-only fine-grained PAT passes that
        too) — not enough to promise the deploy flow can actually open one. Probe the
        real write path instead: attempt to open a PR from a head branch that cannot
        exist. Same authorize-before-validate ordering as the push probe:
          - 403     → no write access — nothing is opened
          - 422     → write access confirmed; rejected only because the head branch
                      doesn't exist — nothing is opened
          - 201     → shouldn't happen (the branch doesn't exist), but if GitHub ever
                      did open it, close it immediately (PRs can't be deleted)
        Fails closed (False) on network errors, and when there's no default branch to
        target (brand-new/empty repo)."""
        if not default_branch:
            return False
        probe_branch = f"agenticdevops-access-probe-{uuid.uuid4().hex[:10]}"
        try:
            resp = requests.post(
                f"{_API_BASE}/repos/{owner}/{repo}/pulls",
                json={"title": "AgenticDevOps access probe (safe to ignore)",
                      "head": probe_branch, "base": default_branch,
                      "body": "Permission probe — the head branch does not exist, so this can never actually open."},
                headers=self._headers(token), timeout=_TIMEOUT_S,
            )
        except requests.RequestException:
            return False
        if resp.status_code == 201:
            pr_number = resp.json().get("number")
            try:
                requests.patch(f"{_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}",
                              json={"state": "closed"}, headers=self._headers(token),
                              timeout=_TIMEOUT_S)
            except requests.RequestException:
                pass
            return True
        return resp.status_code == 422

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

    def create_pull_request(self, canonical_url: str, token: str, *,
                            head: str, base: str, title: str, body: str) -> str:
        """Open a PR (head → base) and return its html_url. A 422 'already exists'
        resolves to the existing open PR's URL (idempotent re-deploys); every other
        failure raises PrCreationError — callers degrade, they don't fail the run."""
        owner, repo = parse_owner_repo(canonical_url)
        try:
            resp = requests.post(f"{_API_BASE}/repos/{owner}/{repo}/pulls",
                                 json={"title": title, "head": head, "base": base, "body": body},
                                 headers=self._headers(token), timeout=_WRITE_TIMEOUT_S)
        except requests.RequestException as exc:
            raise PrCreationError(f"GitHub unreachable: {exc.__class__.__name__}")
        if resp.status_code == 201:
            return resp.json()["html_url"]
        if resp.status_code == 422:
            reason = self._pr_422_reason(resp)
            if "already exists" in reason.lower():
                existing = self._find_open_pr(owner, repo, token, head=head, base=base)
                if existing:
                    return existing
            raise PrCreationError(f"GitHub refused the PR: {reason}")
        if resp.status_code in (401, 403):
            raise PrCreationError(
                f"GitHub rejected the token (HTTP {resp.status_code}) — the PR needs a token "
                "with pull-request permission")
        if resp.status_code == 404:
            raise PrCreationError("repository not found, or the token lacks access to it")
        raise PrCreationError(f"unexpected GitHub response (HTTP {resp.status_code})")

    def _pr_422_reason(self, resp: requests.Response) -> str:
        """Flatten GitHub's 422 body ('message' + per-field 'errors') into one line."""
        try:
            payload = resp.json()
        except ValueError:
            return "validation failed (unparseable 422 body)"
        parts = [payload.get("message") or ""]
        for err in payload.get("errors") or []:
            msg = err.get("message") if isinstance(err, dict) else str(err)
            if msg:
                parts.append(msg)
        return "; ".join(p for p in parts if p) or "validation failed"

    def _find_open_pr(self, owner: str, repo: str, token: str, *,
                      head: str, base: str) -> Optional[str]:
        """URL of the already-open PR for head → base, if GitHub reports one."""
        try:
            resp = requests.get(f"{_API_BASE}/repos/{owner}/{repo}/pulls",
                                params={"head": f"{owner}:{head}", "base": base, "state": "open"},
                                headers=self._headers(token), timeout=_TIMEOUT_S)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        pulls = resp.json()
        return pulls[0]["html_url"] if pulls else None
