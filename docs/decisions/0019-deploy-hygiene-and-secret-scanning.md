# 0019 — Deploy hygiene + secret scanning

Date: 2026-07-02
Status: Accepted (implemented)

## Context
Evaluating a real run (`Test_env` branch `agent/run-56880bd9-…`) showed the deploy push was
both noisy and unsafe:
- `GitOps.push_branch` did bare `git add -A`, and the repo had no `.gitignore`, so the branch
  got **2579 `node_modules/` files** + `package-lock.json` — 3 actual deliverables buried in
  vendored deps (#38).
- The Tester's Track-B synth tests were written **into the repo tree** (`tests/test_<mt>…`),
  so they were committed too — 5 overlapping per-microtask files duplicating the Coder's
  `__tests__/` (#37), one carrying a leaked reasoning comment (#39).
- Nothing scanned for secrets; a `.env` / private key in the sandbox would be pushed.

## Decision
The pushed branch must contain **only Coder deliverables**, and a secret **content scan**
hard-blocks a leaking push.

1. **Two-layer ignore.**
   - *Seed* a `.gitignore` at clone time (`bootstrap._ensure_gitignore`), idempotent (append
     only missing whole-line entries): node_modules, caches, build output, `.env*`, key
     material, and `.agentic_tests/`. This is the "a human would set this up" layer; deps are
     reinstalled by the Tester so they must never be committed.
   - *Backstop* at push: `GitOps.push_branch` stages with pathspec excludes
     (`git add -A -- . ':(exclude)**/node_modules/**' ':(exclude).env' … ':(exclude).agentic_tests/**'`),
     so a repo shipping its own incomplete `.gitignore` still can't leak deps/secrets.

2. **Tester synth tests are ephemeral, not deliverables.** They move from `tests/` to a
   git-ignored `.agentic_tests/` dir (depth-1 preserved so relative imports resolve), run as
   verification, saved as run artifacts, never committed. The Coder's `__tests__/` is the
   shipped suite; Track A already runs it (`npm test` / `pytest`), so the Tester still
   executes the Coder's tests — which resolves #37 without duplication. Track-separation is
   enforced on BOTH engines: `pytest` gets `--ignore=.agentic_tests`, and the jest path
   forwards `--testPathIgnorePatterns /node_modules/ .agentic_tests` (bare `jest` DOES
   discover dot dirs — observed leaking a stale synth file into Track A on run `ba3a937c`;
   the original "synthesis runs after Track A" assumption only held for the first
   iteration). Track B runs its file via `--runTestsByPath` (exact path, immune to the
   repo's own testMatch). Additionally, a repo with ZERO tests makes jest exit 1
   ("No tests found") — mapped to SKIPPED like pytest's exit 5, not a Track A failure.

3. **gitleaks hard-blocks a leaking push.** gitleaks is baked into the sandbox image;
   `GitOps.scan_secrets` runs it over the working tree BEFORE the push and returns REDACTED
   findings (rule/file/line, never the value). The deploy node blocks on ANY finding — and on
   a scan *failure* (gitleaks missing) — marking the run FAILED and emitting
   `gitops.secret_blocked`. Safe by default: a credential never ships, even with a human
   deploy-approval, even with no deploy gate configured. The push event also records the
   shipped fileset (`git status --porcelain`) for audit / the deploy-gate summary.

## Consequences
- Branches are reviewable (deliverables only); credentials can't leak via the push.
- New sandbox image dependency (gitleaks) → image rebuild required. Filename-based ignores +
  content scan are complementary (name vs content).
- "Runtime-needed ≠ version-controlled": the real `.env` lives in the sandbox/deploy env and
  is git-ignored; documenting required vars via a committed `.env.example` + a PR-body callout
  is a deferred follow-up (see below).

## Rejected / deferred
- **Derive the exact deliverable fileset from `repo.diff`/`target_files` as the primary stage
  set** (stronger than exclude-lists) — deferred; ignore + pathspec + scan covers the reported
  failures with less fragility.
- **Auto-emit `.env.example` + list required env vars in the PR body** — separate feature,
  filed as a follow-up.
- **Soft-warn on secret findings instead of hard-block** — rejected: a leaked credential is
  not a warning.
