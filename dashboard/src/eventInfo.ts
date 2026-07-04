// Turns raw events-table rows into human-readable timeline entries: a friendly label per
// event_type plus a one-line summary pulled from the payload fields that matter. This is
// what makes an old run's history readable — the payload is where the story lives.

import type { EventView } from './api'

const s = (v: unknown, max = 90): string => {
  if (v === null || v === undefined) return ''
  const str = String(v)
  return str.length > max ? `${str.slice(0, max)}…` : str
}

// event_type → short human label. Fallback: the raw type.
const LABELS: Record<string, string> = {
  'run.created': 'run created',
  'run.status_changed': 'status',
  'run.completed': 'run completed',
  'run.failed': 'run failed',
  'repo.cloned': 'repo cloned',
  'sandbox.created': 'sandbox up',
  'plan.drafted': 'plan drafted',
  'plan.reviewed': 'plan reviewed',
  'microtask.created': 'microtask created',
  'microtask.updated': 'microtask updated',
  'microtask.completed': 'microtask done',
  'microtask.failed': 'microtask failed',
  'reflection.created': 'diagnosis',
  'hitl.requested': 'gate — awaiting human',
  'hitl.decided': 'human decision',
  'tool.executed': 'tool',
  'tool.denied': 'tool denied',
  'gitops.pushed': 'pushed',
  'gitops.secret_blocked': 'push BLOCKED (secrets)',
  'semantic.baseline_ingested': 'code indexed',
  'semantic.workspace_synced': 'index synced',
  'test.unit_completed': 'tests ran',
  'check.lint_completed': 'lint ran',
}

// event_type → payload one-liner. Fallback: first few primitive key=value pairs.
const SUMMARIES: Record<string, (p: Record<string, unknown>) => string> = {
  'run.status_changed': (p) => `${s(p.from, 30)} → ${s(p.to, 30)}${p.reason ? ` — ${s(p.reason)}` : ''}`,
  'run.failed': (p) => s(p.reason ?? p.summary, 120),
  'run.completed': (p) => s(p.summary, 120),
  'repo.cloned': (p) => s(p.repo_url),
  'plan.drafted': (p) =>
    `${p.microtasks_count !== undefined ? `${s(p.microtasks_count)} microtasks` : ''}${p.plan_summary ? ` — ${s(p.plan_summary)}` : ''}`,
  'plan.reviewed': (p) => `${s(p.decision, 10)}${p.notes ? ` — ${s(p.notes)}` : ''}`,
  'microtask.completed': (p) => `${s(p.microtask_id, 24)}${p.summary ? ` — ${s(p.summary)}` : ''}`,
  'microtask.failed': (p) => `${s(p.microtask_id, 24)}${p.summary ? ` — ${s(p.summary)}` : ''}`,
  'reflection.created': (p) => `${s(p.failure_type, 30)}${p.microtask_id ? ` on ${s(p.microtask_id, 24)}` : ''}`,
  'hitl.requested': (p) => `${s(p.phase ?? p.type, 12)} gate`,
  'hitl.decided': (p) => `${s(p.decision, 10)}${p.note ? ` — ${s(p.note)}` : ''}`,
  'tool.executed': (p) => `${s(p.tool_name, 24)} ${s(p.args_summary, 60)}`,
  'tool.denied': (p) => `${s(p.tool_name, 24)}${p.reason ? ` — ${s(p.reason)}` : ''}`,
  'gitops.pushed': (p) => `${s(p.branch, 40)}${p.file_count !== undefined ? ` (${s(p.file_count)} files)` : ''}`,
  'gitops.secret_blocked': (p) => `${s(p.count ?? '?')} finding(s)`,
  'semantic.baseline_ingested': (p) => `${s(p.chunks)} chunks`,
}

const PRIMITIVE = (v: unknown) => ['string', 'number', 'boolean'].includes(typeof v)

export function eventLabel(e: EventView): string {
  return LABELS[e.event_type] ?? e.event_type
}

export function eventSummary(e: EventView): string {
  const fn = SUMMARIES[e.event_type]
  if (fn) return fn(e.payload ?? {})
  return Object.entries(e.payload ?? {})
    .filter(([, v]) => PRIMITIVE(v))
    .slice(0, 3)
    .map(([k, v]) => `${k}=${s(v, 40)}`)
    .join(' ')
}

// actor → the log-panel category palette (--cat-*). bootstrapper/human differ in name only.
const ACTOR_CAT: Record<string, string> = {
  bootstrapper: 'bootstrap',
  human: 'hitl',
}

export function actorCat(actor: string): string {
  return ACTOR_CAT[actor] ?? actor
}

// Events that mark something going wrong — tinted red in the timeline.
export const BAD_EVENTS = new Set(['run.failed', 'microtask.failed', 'tool.denied', 'gitops.secret_blocked'])
