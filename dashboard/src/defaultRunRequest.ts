// Builds a full RunRequest from the few fields a human cares about, filling the rest
// with the same defaults the CLI used (src/main.py _build_fresh_state). Keeps the
// "new run" form simple while satisfying the backend's required models.

export interface NewRunOpts {
  repoUrl: string
  baseSha?: string
  task: string
  hitlCheckpoints: string[] // subset of 'plan' | 'code' | 'test'
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
    repo: {
      repo_url: opts.repoUrl,
      default_branch: null,
      base_ref: 'main',
      base_sha: opts.baseSha || null,
      workspace_branch: 'agentic-devops-workspace',
      pr_url: null,
    },
    constraints: {
      time_budget_minutes: 60,
      cost_budget_usd: 10.0,
      max_total_iterations: 5,
      max_microtask_attempts: 3,
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
