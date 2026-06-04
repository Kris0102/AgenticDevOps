#!/usr/bin/env bash
# Create POC-1 GitHub issues for AgenticDevOps.
# Requires: gh auth login
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BODIES="${REPO_ROOT}/docs/github-issues/bodies"

if ! command -v gh >/dev/null 2>&1; then
  echo "Install GitHub CLI: https://cli.github.com/"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Run: gh auth login"
  exit 1
fi

cd "${REPO_ROOT}"

ensure_label() {
  local name="$1"
  local color="$2"
  gh label create "${name}" --color "${color}" --force 2>/dev/null || true
}

ensure_label "poc" "0E8A16"
ensure_label "planner" "1D76DB"
ensure_label "sandbox" "FBCA04"
ensure_label "orchestrator" "5319E7"
ensure_label "tool-gateway" "D93F0B"
ensure_label "react" "B60205"
ensure_label "langgraph" "006B75"
ensure_label "documentation" "0075CA"

create_issue() {
  local title="$1"
  local body_file="$2"
  shift 2
  echo "Creating: ${title}"
  gh issue create \
    --title "${title}" \
    --body-file "${body_file}" \
    "$@"
}

# Optional milestone (ignore if API fails)
MILESTONE_URL=$(gh api repos/:owner/:repo/milestones -f title="POC-1 Planner ReAct" -f state="open" -f description="Planner ReAct explores sandbox; emits Plan + Microtasks" --jq '.html_url' 2>/dev/null || true)

MILESTONE_ARG=()
if [[ -n "${MILESTONE_URL:-}" ]]; then
  MILESTONE_NUM=$(gh api repos/:owner/:repo/milestones --jq '.[] | select(.title=="POC-1 Planner ReAct") | .number' | head -1)
  if [[ -n "${MILESTONE_NUM}" ]]; then
    MILESTONE_ARG=(--milestone "POC-1 Planner ReAct")
  fi
fi

create_issue "POC-1: Run-scoped sandbox lifecycle (create once per run)" \
  "${BODIES}/01-sandbox-lifecycle.md" \
  --label "poc,sandbox,orchestrator" "${MILESTONE_ARG[@]}"

create_issue "POC-1: Bootstrap — clone repo at base_sha into sandbox" \
  "${BODIES}/02-bootstrap-clone.md" \
  --label "poc,sandbox,orchestrator" "${MILESTONE_ARG[@]}"

create_issue "POC-1: Planner tools — structured Tool Gateway only" \
  "${BODIES}/03-planner-tools-gateway.md" \
  --label "poc,planner,tool-gateway" "${MILESTONE_ARG[@]}"

create_issue "POC-1: ReAct investigation loop (Planner explores codebase)" \
  "${BODIES}/04-react-investigation.md" \
  --label "poc,planner,react" "${MILESTONE_ARG[@]}"

create_issue "POC-1: Structured synthesis — Plan + Microtasks output" \
  "${BODIES}/05-structured-synthesis.md" \
  --label "poc,planner" "${MILESTONE_ARG[@]}"

create_issue "POC-1: Minimal orchestrator graph (bootstrap → planner)" \
  "${BODIES}/06-minimal-orchestrator-graph.md" \
  --label "poc,orchestrator,langgraph" "${MILESTONE_ARG[@]}"

create_issue "POC-1: Demo run script + success criteria" \
  "${BODIES}/07-demo-and-docs.md" \
  --label "poc,documentation" "${MILESTONE_ARG[@]}"

echo ""
echo "Done. Issues: $(gh issue list --label poc --limit 20 --json url --jq '.[].url' | tr '\n' ' ')"
