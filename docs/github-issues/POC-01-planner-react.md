# GitHub issues — POC-1: Planner ReAct agent

**Milestone goal:** A working **Planner** that uses a **ReAct loop** to explore the sandbox (read files, run allowlisted commands) and then outputs a structured **Plan + Microtasks**.

**Not in scope for POC-1:** Plan Reviewer, Coder, Tester, Reflector, semantic memory, GitOps, PR creation.

**Repo:** https://github.com/Kris0102/AgenticDevOps

## Create issues automatically

From the repo root (WSL), with [GitHub CLI](https://cli.github.com/) installed and authenticated (`gh auth login`):

```bash
bash scripts/create_github_issues_poc1.sh
```

Optional: create a milestone first:

```bash
gh api repos/Kris0102/AgenticDevOps/milestones -f title="POC-1 Planner ReAct" -f description="First POC: planner investigates sandbox and emits plan + microtasks"
```

## Issue list (order)

| # | Title | Depends on |
|---|--------|------------|
| 1 | POC-1: Run-scoped sandbox lifecycle (create once per run) | — |
| 2 | POC-1: Bootstrap — clone repo at base_sha into sandbox | #1 |
| 3 | POC-1: Planner tools — structured Tool Gateway only | #1 |
| 4 | POC-1: ReAct investigation loop (Planner explores codebase) | #2, #3 |
| 5 | POC-1: Structured synthesis — Plan + Microtasks output | #4 |
| 6 | POC-1: Minimal orchestrator graph (bootstrap → planner) | #1–#5 |
| 7 | POC-1: Demo run script + success criteria | #6 |

## Manual create

Copy each body from `docs/github-issues/bodies/` into GitHub **New issue**, or use:

```bash
gh issue create --title "..." --body-file docs/github-issues/bodies/01-sandbox-lifecycle.md --label "poc,planner"
```
