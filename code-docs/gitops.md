# src/gitops/ — Code Explanation

Four files, ~769 lines. The GitHub boundary (ADR 0012): the ONLY package that talks
to remote git or the GitHub REST API, and the only place a GitHub token is used.
Not an agent — bypasses the ToolGateway; driven by the deploy nodes and the API.

---

## gitops.py (362 lines) — `GitOps`

### Class constants
- `BRANCH_PREFIX = "agent/run-"` → `branch_name(run_id)` = `agent/run-<id>`.
- `STAGE_EXCLUDES` (lines 43–51): pathspecs force-unstaged before commit —
  `**/node_modules/**`, `.env`, `.env.*`, `**/*.pem|key|p12`, `.agentic_tests`.
  The long comment explains a real git subtlety: `git add` with `:(exclude)`
  pathspecs *refuses outright* (exit 1) once an ignored dir like `.agentic_tests/`
  exists — so it's a **two-step**: `git add -A -- .` then
  `git reset -q -- <excludes>`. `:(glob)` makes `**` match zero+ leading dirs.

### `LocalExportError(SandboxError)` (line 14)
Subclasses SandboxError so the deploy node's existing gitops error handling
catches it without a new except branch — but it's distinct so the local_only
policy (fail, don't degrade) can key on it.

### `_authenticated_url(repo_url, token)` (60–66)
Injects the token into the HTTPS URL (`https://TOKEN@github.com/...`) so git never
prompts. Centralized here = token never appears anywhere else (and never in logs).

### `clone_and_checkout(...)` (68–110)
1. `git clone [--branch base_ref] <authed_url> repo` in `/workspace` — `base_ref`
   selects the branch (None = default branch).
2. If `base_sha` given → `git checkout <sha>` (**detached HEAD** — this matters
   later: everything the Coder does sits uncommitted on a detached HEAD until
   deploy). `base_sha=None` (brand-new repo) → clone only.
Non-zero exit at either step → `SandboxError` with git's stderr.

### `_stage_and_commit(...)` (179–207) — the shared commit builder
Called by BOTH `push_branch` and `export_local_branch` so the commit each ships is
built identically (ADR 0036 — one code path, no drift):
1. `git config user.email/name` — fresh containers have no identity; commit would
   fail without it.
2. `git add -A -- .` then `git reset -q -- STAGE_EXCLUDES` (the backstop to the
   bootstrap-seeded `.gitignore` — belt and braces from both directions).
3. `git commit -m <message>` with `allow_fail=True`; "nothing to commit" in the
   output → return `False` (caller logs and exits gracefully — idempotent
   re-runs). Any other failure raises.

### `push_branch(...)` (112–166)
- Resolves `branch` (param › `agent/run-<id>`), then the **defense line**: if
  `branch == forbidden_branch` (the repo's default branch, passed by the caller)
  → raise. Enforced *here*, not just in the API — "regardless of caller".
- `_stage_and_commit`; if nothing to commit, return early.
- Remote handling is re-run-safe: `git remote add auth-origin <authed>` with
  `allow_fail=True`; if it already exists → `set-url` instead.
- The push line: `git push auth-origin HEAD:refs/heads/<branch>` — **fully
  qualified ref** because HEAD is detached; a bare name would make git refuse
  (can't infer branch vs tag).
- Every git step goes through `_sandbox_git` (168–177), which appends each
  `ExecResult` to a `results` list → the caller persists the full command audit.

### `export_local_branch(...)` (209–283) — the local_only deploy (ADR 0036)
The no-remote path, step by step:
1. Same `_stage_and_commit` (identical commit as a push would make).
2. `git bundle create /workspace/agentic-export.bundle HEAD` inside the sandbox.
3. `fs_read_file_bytes` pulls the bundle out binary-safe (get_archive — `cat`
   would mangle it).
4. Host-side re-checks (run creation validated, but deploy may be hours later):
   target still a git repo; branch **must not already exist** → precise
   `LocalExportError` instead of a cryptic git message.
5. Bundle → `NamedTemporaryFile` on host → `git -C <local_repo> fetch <bundle>
   HEAD:refs/heads/<branch>` → temp file unlinked in `finally`.
Transfers the *real sandbox commit* (identity, message, parents) — not a
re-applied patch. Failure raises (run FAILS): "the human chose this mode to
receive a branch; silently not delivering one would look like success."
Host `subprocess` is justified in the docstring: API process and target clone
share the machine (self-hosted model).

### `scan_secrets(...)` (286–327) — the gitleaks gate (ADR 0019)
- `gitleaks detect --source . --no-git --no-banner --redact --exit-code 0
  --report-format json --report-path /tmp/...` — `--no-git` scans the working
  tree (content-based: catches a secret *hardcoded in source*, which filename
  ignores can't); `--exit-code 0` because findings are parsed from the report,
  not inferred from exit status; `--redact` keeps secret VALUES out of the report
  we then read.
- Non-zero exit = the scanner itself broke → raise. **A missing scanner must not
  silently allow a leak** (fail closed).
- Findings are returned redacted: `{rule, file, line}` only — never the secret.
  Caller (deploy node) hard-blocks on non-empty.

### `changed_files(...)` (329–338)
`git status --porcelain` → stripped paths (line[3:] drops the status columns).
The deploy gate's "what ships" preview. Best-effort: [] on error.

### `create_pr(...)` (340–361)
Thin delegation to `GitHubRepoClient.create_pull_request`. Takes the **actual
pushed branch** as a parameter (never recomputed from run_id — honors per-repo
overrides). Raises `PrCreationError` → caller degrades, never fails the run: the
push already landed (ADR 0027).

## github_api.py (303 lines) — `GitHubRepoClient`

REST client. Timeouts: 5s reads, 15s writes.

### `parse_owner_repo(url)` (65–71)
Strict: https scheme + netloc + ≥2 path segments, else ValueError. Canonical URL
in, `(owner, repo)` out.

### `validate_repo(...)` + `_classify(...)` (84–156)
`GET /repos/{owner}/{repo}` classified into `RepoValidationKind` (an enum of 8
outcomes: ok_public/ok_private/not_found/no_access/no_push_access/no_pr_access/
unreachable/unexpected). Notable honesty details:
- GitHub 404s BOTH "doesn't exist" and "private, no access" — the reason string
  carries the ambiguity instead of pretending to distinguish.
- **Push access is a real probe, not the GET's `permissions.push` field** — that
  field reflects the user's *role*, not a fine-grained PAT's narrower scope (an
  admin holding a read-only token still shows push=true).
- `NO_PUSH_ACCESS` is `ok=False` but the repo stays registrable — only a
  *deploying* run is blocked downstream (dry-run still fine, ADR 0029).
- Module knows nothing about Settings (whether missing PR access matters depends
  on config) — the caller decides. Boundary discipline.

### `_check_push_access` (158–187) — the clever probe
POST a git ref pointing at SHA `"0"*40` (cannot exist). GitHub **authorizes before
validating**, so:
- 403/404 → no write access (nothing created)
- **422 → write confirmed** — rejected only because the SHA is bogus
- 201 "can't happen", but if it did → delete the ref immediately (zero footprint)
Fails closed on network errors. `_check_pr_access` (189–225) is the same trick on
`/pulls` with a nonexistent head branch (201 → close the PR — PRs can't be
deleted).

### `list_branches` (227–246)
Live GET, first 100, **never DB-cached** ("branches move"). Errors carry the same
kind enum via `BranchListError` so the API maps them to HTTP statuses uniformly.

### `create_pull_request` (248–275)
POST `/pulls`. 201 → `html_url`. **422 "already exists" → look up the existing
open PR and return ITS url** (idempotent re-deploys). Other statuses →
`PrCreationError` with a flattened reason (`_pr_422_reason` joins GitHub's
`message` + per-field `errors` into one line).

## commit_message.py (54 lines) + pr_body.py (54 lines)

Twin deterministic builders — **no LLM** (ADR 0027): deploy is a deterministic
terminal step and the checkpointed run data already holds the ground truth.

- `build_commit_message`: git-formatted — subject (objective title, ≤72 chars, git
  one-line convention) + blank line + body (objective description, microtask list
  sorted by `idx`, files capped at 50 with "…and N more", `Run: <id>`).
  **Placement rationale (docstring):** TWO callers must produce the SAME string —
  the deploy node (what it pushes) and the HITL gate (what it prefills). When the
  gate prefilled something else, the human echoed it back verbatim and the real
  default became unreachable (ADR 0035). Single source = the bug class is gone.
- `build_pr_body`: markdown — Objective section, microtask **checklist**
  (`- [x]`/`- [ ]` from status), changed files (capped 50), optional diff summary,
  footer `Automated by AgenticDevOps — run <id> (branch → base)`.

---

## Relationships

```
bootstrap_node ──► clone_and_checkout                 (run start)
deploy_push node ─► scan_secrets → push_branch → create_pr    (push/pr modes)
                 └► export_local_branch                        (local_only mode)
hitl deploy gate ─► build_commit_message (prefill)  ┐ same string
deploy_push node ─► build_commit_message (push)     ┘
api/Service ──────► GitHubRepoClient.validate_repo / list_branches  (registry, preflight)
GitOps ───────────► DockerSandboxTools.sandbox_exec (git inside sandbox)
                    DockerSandboxTools.fs_read_file_bytes (bundle out)
```

Design sentence for the exam: "Everything GitHub-shaped is in one package; the
token enters exactly one function; the default branch is refused inside the
boundary itself; and the two things a human approves — commit message, PR body —
are deterministic templates, not LLM output."
