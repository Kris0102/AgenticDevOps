// Typed client for the FastAPI control plane. Shapes mirror src/api/responses/responses.py.

export type RunState = 'running' | 'paused_hitl' | 'completed' | 'failed' | 'stalled'

export interface RunResponse {
  run_id: string
  run_state: RunState
  status?: string | null
  hitl_phase?: string | null
  hitl_approval_id?: string | null
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

async function asJson<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
  return r.json() as Promise<T>
}

export const api = {
  createRun: (body: unknown) =>
    fetch('/runs', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }).then(asJson<RunResponse>),

  getRun: (id: string) => fetch(`/runs/${id}`).then(asJson<RunResponse>),

  getHitl: (id: string) => fetch(`/runs/${id}/hitl`).then(asJson<HitlReviewContext>),

  getEvents: (id: string, after = 0) =>
    fetch(`/runs/${id}/events?after=${after}`).then(asJson<EventView[]>),

  resume: (id: string, decision: 'approve' | 'reject', note?: string) =>
    fetch(`/runs/${id}/resume`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ decision, note: note ?? null }),
    }).then(asJson<RunResponse>),

  // Re-drive a stalled run (worker died mid-graph) from its checkpoint. Not for HITL gates.
  continueRun: (id: string) =>
    fetch(`/runs/${id}/continue`, { method: 'POST' }).then(asJson<RunResponse>),
}
