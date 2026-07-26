m# src/gitops/ — The GitHub Boundary

**Role in the MAS:** the only place the system touches remote git/GitHub
(ADR 0012). Everything upstream of deploy treats the sandbox as the world; when a
run finishes and the human approves the deploy gate, `gitops/` ships the result.
It is driven by the orchestrator's terminal `deploy_push` node and deliberately
**bypasses the ToolGateway** — it is deterministic infrastructure, not an LLM agent.

## Files

| File | What it does |
|---|---|
| `gitops.py` | `GitOps` — clone (honors `base_ref`), stage/commit/push to the run branch, local export. GitHub token handling is centralized here and never logged |
| `github_api.py` | `GitHubRepoClient` — repo validation, live branch lists for the registry, and `create_pull_request` (the deploy auto-PR) |
| `pr_body.py` | Deterministic PR description built from run data (objective + microtasks + fileset) — **no LLM** (ADR 0027) |
| `commit_message.py` | Deterministic default commit message — the *single* source for both what the deploy node pushes and what the HITL gate prefills for approval; they diverged once (ADR 0035), never again |

## Deploy modes (ADR 0036)

Resolved **once at run creation** onto the blackboard — the deploy node never reads
live settings, so what was preflighted at run start is what ships:

| Mode | What happens on approve |
|---|---|
| `dry_run` | Nothing ships; `gitops.deploy_skipped` event. The human still sees the final diff at the gate |
| `push` | Stage/commit/push to `agent/run-<run_id>` (or a validated per-run/per-repo branch override) |
| `pr` | Push + a real GitHub PR with the deterministic body. PR failure *degrades* (run still succeeds with `pr_error`) — the code is already pushed |
| `local_only` | `git bundle` the sandbox commit and `git fetch` it into the user's local clone — no remote at all. Failure here FAILS the run, no silent degrade |

## Safety rails

- **Default-branch push is hard-refused at four layers.** The agent's work always
  lands on a run branch; merging is a human decision on GitHub.
- **Secret scanning gates the push** (ADR 0019): gitleaks runs over the tree before
  any push and **hard-blocks** on findings (`gitops.secret_blocked`, run → FAILED).
- **Deploy hygiene:** staging uses pathspec excludes (deps, secrets, the Tester's
  ephemeral `.agentic_tests/` never ship); bootstrap seeds a `.gitignore`.
- **Everything human-facing is deterministic:** commit message and PR body are
  templated from run data, so the human at the gate approves *exactly* what will be
  written — no LLM paraphrase in the loop.

## Interview sound bites

- "The MAS never has write access to your main branch — it ships to a run branch or
  a PR, and a gitleaks scan hard-blocks anything that smells like a secret."
- "One boundary class for GitHub means token handling, branch policy, and push
  hygiene live in exactly one place."
