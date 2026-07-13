// Builds a full RunRequest from the few fields a human cares about, filling the rest
// with the same defaults the CLI used (src/main.py _build_fresh_state). Keeps the
// "new run" form simple while satisfying the backend's required models.

import type { LLMConfig } from './api'

export interface NewRunOpts {
  repoId: string            // registered repo (GH #41) — the server resolves URL/token
  baseRef?: string | null   // base branch for the run; null = the repo's default branch
  baseSha?: string
  runName?: string | null  // name for the run OPTIONAL;
  task: string
  hitlCheckpoints: string[] // subset of 'plan' | 'code' | 'test' | 'deploy'
  llm?: LLMConfig | null                 // per-run model override (settings hub)
  maxPlannerReviewerIterations?: number
  maxMicrotaskAttempts?: number
  maxTotalTokens?: number | null
  commitMessage?: string | null          // deploy commit message; editable again at the deploy gate
  skipDeploy?: boolean                   // dry-run: end at DONE, no push/PR (no push token needed)
}

export function buildRunRequest(opts: NewRunOpts) {
  return {
    objective: {
      title: 'Dashboard-triggered run',
      description: opts.task,
      definition_of_done: ['Implements the task', 'Tests pass'],
      non_goals: [],
    },
    trigger: { type: 'dashboard', source_url: '', actor: 'user', raw_payload_ref: null },
    // Registry pick — the backend resolves the canonical URL, per-repo token, and
    // default branch from repo_id, and preflights the repo before creating the run.
    repo: null,
    repo_id: opts.repoId,
    run_name: opts.runName || null,
    base_ref: opts.baseRef ?? null,
    base_sha: opts.baseSha || null,
    llm: opts.llm ?? null,
    commit_message: opts.commitMessage ?? null,
    skip_deploy: opts.skipDeploy ?? false,
    constraints: {
      time_budget_minutes: 60,
      cost_budget_usd: 10.0,
      max_planner_reviewer_iterations: opts.maxPlannerReviewerIterations ?? 5,
      max_microtask_attempts: opts.maxMicrotaskAttempts ?? 3,
      max_total_tokens: opts.maxTotalTokens ?? null,
      allowed_languages: ['python', 'javascript'],
      forbidden_paths: ['src/secrets/'],
      allowed_network: 'none',
      risk_level: 'medium',
    },
    policy: {
      require_hitl_for_pr: true,
      require_hitl_for_merge: true,
      require_hitl_for_prod_deploy: true,
      security_scans_required: ['bandit', 'safety'],
      ci_required: true,
      protected_branches: ['main', 'master'],
    },
    hitl: {
      required: opts.hitlCheckpoints.length > 0,
      hitl_checkpoints: opts.hitlCheckpoints,
    },
  }
}
