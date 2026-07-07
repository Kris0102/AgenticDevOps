// Typed axios client for the FastAPI control plane.
// Every interface here mirrors a Pydantic model in src/api/responses/responses.py — keep
// them in sync so the dashboard is end-to-end type-safe against the backend contract.

import axios, { AxiosError } from 'axios'

// ----------------------------------------------------------------- response models

export type RunState = 'running' | 'paused_hitl' | 'completed' | 'failed' | 'stalled'

export interface RunResponse {
  run_id: string
  run_state: RunState
  status?: string | null
  hitl_phase?: string | null
  hitl_approval_id?: string | null
  stalled_reason?: string | null   // transient-error / crash reason when run_state === 'stalled'
  stalled_kind?: 'token_budget' | 'transient' | string | null   // cause of the stall (ADR 0026)
  failure_reason?: string | null   // run.failed reason when run_state === 'failed'
}

// A run row for the previous-runs home list (GET /runs). Extra fields are the box's
// labels + sort keys. Mirrors responses.RunResponseWithDetails.
export interface RunResponseWithDetails extends RunResponse {
  repo_url: string
  repo_name: string
  base_sha?: string | null
  task_description?: string | null   // what the run was asked to do
  pushed_files?: number | null       // files shipped by the deploy push
  llm_provider?: string | null       // provider/model this run used
  llm_model?: string | null
  tokens_used_total: number          // running total across every agent (ADR 0025)
  max_total_tokens?: number | null   // this run's token budget, if one was set
  created_at: string   // ISO 8601
  updated_at: string   // ISO 8601
}

// ----------------------------------------------------------------- settings hub
export type ProviderType = 'openai_compatible' | 'anthropic'

export interface ProviderProfile {
  id: string
  label: string
  type: ProviderType
  base_url?: string | null
  api_key?: string | null            // masked ('••••••••') on read; leave as-is to keep stored key
  models: string[]
}

export interface LLMConfig {
  provider_id: string
  model: string
  temperature: number
  max_retries: number
}

export interface Knobs {
  planner_max_tool_steps: number; planner_timeout_seconds: number; planner_max_synthesis_attempts: number
  reviewer_max_tool_steps: number; reviewer_timeout_seconds: number
  coder_max_tool_steps: number; coder_timeout_seconds: number; coder_max_empty_diff_retries: number; coder_max_compile_retries: number
  tester_max_tool_steps: number; tester_timeout_seconds: number
  reflector_max_tool_steps: number; reflector_timeout_seconds: number; reflector_max_test_resynth: number
}

export interface RunDefaults {
  max_total_iterations: number
  max_microtask_attempts: number
  max_total_tokens?: number | null
  hitl_checkpoints: string[]
  teardown_on_terminal: boolean
}

export interface AppSettings {
  providers: ProviderProfile[]
  selected: LLMConfig
  knobs: Knobs
  defaults: RunDefaults
  github_token?: string | null   // masked on read; leave masked to keep stored token
}

export interface SchemaField {
  key: string
  label: string
  description: string
  type: string
  group: 'basic' | 'expert'
  nullable?: boolean   // int/float only: blank input means "clear to null", not 0/NaN
}

export interface ProviderTestResult {
  ok: boolean
  latency_ms?: number
  reason?: string | null
  kind?: string | null
}

// ----------------------------------------------------------------- repo registry (GH #41)
export type RepoValidationKind =
  | 'ok_public' | 'ok_private' | 'not_found' | 'no_access' | 'unreachable' | 'unexpected'

// Mirrors gitops.github_api.RepoValidationResult.
export interface RepoValidationResult {
  ok: boolean
  kind: RepoValidationKind
  reason: string
  default_branch?: string | null
  latency_ms?: number | null
}

// One registered repo. `token` is the mask ('••••••••') when a per-repo token is stored.
// Mirrors responses.RepoView.
export interface RepoView {
  repo_id: string
  repo_url: string
  label: string
  token?: string | null
  default_branch?: string | null
  push_branch_override?: string | null
  last_validated_at?: string | null
  last_validation_result?: RepoValidationResult | null
  created_at?: string | null
}

export interface RepoCreateRequest {
  repo_url: string
  label?: string | null
  token?: string | null
  push_branch_override?: string | null
}

export interface RepoUpdateRequest {
  label?: string | null
  token?: string | null                // mask = keep stored, '' = clear
  push_branch_override?: string | null // '' = clear, omitted = keep
}

// One per-repo lesson (ADR 0018). Mirrors responses.LessonView.
export interface LessonView {
  lesson_id: string
  run_id: string
  scope: string
  failure_type?: string | null
  summary: string
  guidance?: string | null
  created_at: string
}

export interface MicrotaskView {
  microtask_id: string
  title: string
  description: string
  approach: string
  acceptance_criteria: string[]
  target_files: string[]
  status: string
}

export interface PlanView {
  summary: string
  architecture_notes: string
  assumptions: string[]
  risks: string[]
  open_questions: string[]
  microtasks: MicrotaskView[]
}

export interface DiffData {
  microtask_id?: string | null
  summary: string
  base_sha: string
  diff_text: string
}

export interface HitlReviewContext {
  run_id: string
  approval_id: string
  phase: string
  current_microtask_id?: string | null
  prompt: string
  plan?: PlanView | null
  diff?: DiffData | null
}

export interface EventView {
  seq: number
  ts: string
  actor: string
  event_type: string
  payload: Record<string, unknown>
}

export interface LogView {
  seq: number
  ts?: string | null
  level?: string | null
  category?: string | null
  event: string
  fields: Record<string, unknown>
}

// Durable run-detail views (mirror responses.ArtifactView / ArtifactContent / MicrotaskRow).
export interface ArtifactView {
  artifact_id: string
  kind: string
  microtask_id?: string | null   // producing microtask (diff/test/reflection); null = run-level
  created_at: string
  size: number
  preview: string
}

export interface ArtifactContent extends ArtifactView {
  content_text: string
}

export interface MicrotaskRow {
  microtask_id: string
  idx: number
  title: string
  status: string
  attempts: number
  target_files: string[]
  files_touched: string[]
}

// ----------------------------------------------------------------- client

// Relative baseURL → served same-origin (or via the Vite dev proxy). The interceptor
// normalizes axios errors to a plain Error(message) so existing `catch (e) { alert(String(e)) }`
// call sites keep working, and the FastAPI `detail` string surfaces when present.
const http = axios.create({
  baseURL: '',
  headers: { 'content-type': 'application/json' },
})

http.interceptors.response.use(
  (res) => res,
  (err: AxiosError<{ detail?: string }>) => {
    const status = err.response?.status
    const detail = err.response?.data?.detail ?? err.message
    return Promise.reject(new Error(status ? `${status} ${detail}` : String(detail)))
  },
)

export const api = {
  createRun: (body: unknown) =>
    http.post<RunResponse>('/runs', body).then((r) => r.data),

  getRun: (id: string) => http.get<RunResponse>(`/runs/${id}`).then((r) => r.data),

  // All previous runs, newest first — powers the home list.
  getRunsDetails: () => http.get<RunResponseWithDetails[]>('/runs').then((r) => r.data),

  getHitl: (id: string) =>
    http.get<HitlReviewContext>(`/runs/${id}/hitl`).then((r) => r.data),

  getEvents: (id: string, after = 0) =>
    http.get<EventView[]>(`/runs/${id}/events`, { params: { after } }).then((r) => r.data),

  resume: (id: string, decision: 'approve' | 'reject', note?: string) =>
    http.post<RunResponse>(`/runs/${id}/resume`, { decision, note: note ?? null }).then((r) => r.data),

  // Re-drive a stalled run (worker died mid-graph, or a token-budget stop) from its
  // checkpoint. Not for HITL gates. Optional `maxTotalTokens` raises the checkpointed cap
  // before resuming (ADR 0026) — for a token-budget stall.
  continueRun: (id: string, maxTotalTokens?: number) =>
    http.post<RunResponse>(`/runs/${id}/continue`, maxTotalTokens != null ? { max_total_tokens: maxTotalTokens } : undefined).then((r) => r.data),

  // Human verdict on a stalled run: close the case as FAILED ('Mark as failed').
  failRun: (id: string, note?: string) =>
    http.post<RunResponse>(`/runs/${id}/fail`, { note: note || null }).then((r) => r.data),

  // Durable run detail: artifact browse list (no bodies), one artifact's body, microtask board.
  getArtifacts: (id: string) =>
    http.get<ArtifactView[]>(`/runs/${id}/artifacts`).then((r) => r.data),

  getArtifact: (id: string, artifactId: string) =>
    http.get<ArtifactContent>(`/runs/${id}/artifacts/${artifactId}`).then((r) => r.data),

  getMicrotasks: (id: string) =>
    http.get<MicrotaskRow[]>(`/runs/${id}/microtasks`).then((r) => r.data),

  getLessons: (id: string) =>
    http.get<LessonView[]>(`/runs/${id}/lessons`).then((r) => r.data),

  getLogs: (id: string, after = 0) =>
    http.get<LogView[]>(`/runs/${id}/logs`, { params: { after } }).then((r) => r.data),

  // Export is a plain download link (Content-Disposition names the file) — see exportUrl.
  exportUrl: (id: string) => `/runs/${id}/export`,

  // Restore a previously exported bundle (fresh DB / other machine). 409 if the run exists.
  importRun: (bundle: unknown) =>
    http.post<RunResponseWithDetails>('/runs/import', bundle).then((r) => r.data),

  // Settings hub (keys masked on read).
  getSettings: () => http.get<AppSettings>('/settings').then((r) => r.data),
  putSettings: (s: AppSettings) => http.put<AppSettings>('/settings', s).then((r) => r.data),
  getSettingsSchema: () => http.get<SchemaField[]>('/settings/schema').then((r) => r.data),
  testProvider: (providerId: string) =>
    http.post<ProviderTestResult>(`/settings/providers/${providerId}/test`).then((r) => r.data),
  testGithub: () =>
    http.post<ProviderTestResult>('/settings/github/test').then((r) => r.data),

  // Repo registry (GH #41). Tokens are masked on read; POST validates against GitHub
  // before storing (4xx = not created).
  listRepos: () =>
    http.get<RepoView[]>('/repos').then((r) => {
      // A misconfigured dev proxy answers with the SPA's index.html (HTTP 200) —
      // surface that as an error instead of crashing the page on a string.
      if (!Array.isArray(r.data)) throw new Error('unexpected /repos response — API not reachable (dev proxy?)')
      return r.data
    }),
  createRepo: (body: RepoCreateRequest) =>
    http.post<RepoView>('/repos', body).then((r) => r.data),
  updateRepo: (id: string, body: RepoUpdateRequest) =>
    http.put<RepoView>(`/repos/${id}`, body).then((r) => r.data),
  deleteRepo: (id: string) => http.delete(`/repos/${id}`).then(() => undefined),
  validateRepo: (id: string) =>
    http.post<RepoValidationResult>(`/repos/${id}/validate`).then((r) => r.data),
  getRepoBranches: (id: string) =>
    http.get<{ branches: string[] }>(`/repos/${id}/branches`).then((r) => r.data.branches),
}
