import { useEffect, useMemo, useState } from 'react'
import { api, type EventView, type MicrotaskRow } from '../api'

// Full microtask detail — the durable planner record for every microtask in the run,
// not just the compact board in RunOverview. One row per microtask (idx order); click
// to expand the complete record: description, approach, acceptance criteria (with any
// concrete I/O examples), target/context files, dependencies, test strategy.

const DONE = new Set(['COMPLETED', 'DONE'])
const BAD = new Set(['FAILED', 'BLOCKED'])

// Per-microtask token spend — Coder + Tester + Reflector. The Planner drafts the whole
// plan (all microtasks) before any microtask exists, so its tokens aren't attributable to
// a single microtask and are intentionally left out here (see TokensByAgent for the
// run-wide, per-agent breakdown including the Planner).
type MtTokens = { coder: number; tester: number; reflector: number; total: number }
const ZERO_TOKENS: MtTokens = { coder: 0, tester: 0, reflector: 0, total: 0 }

function fmtTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

function useMicrotaskTokens(runId: string): Record<string, MtTokens> {
  const [events, setEvents] = useState<EventView[]>([])

  useEffect(() => {
    let alive = true
    setEvents([])
    const load = async () => {
      let cursor = 0
      let acc: EventView[] = []
      try {
        for (;;) {
          const batch = await api.getEvents(runId, cursor)
          if (!alive || batch.length === 0) break
          cursor = batch[batch.length - 1].seq
          acc = [...acc, ...batch]
          if (batch.length < 200) break
        }
        if (alive) setEvents(acc)
      } catch {
        /* transient — leave prior totals in place */
      }
    }
    load()
    return () => { alive = false }
  }, [runId])

  return useMemo(() => {
    const byTask: Record<string, MtTokens> = {}
    for (const e of events) {
      if (e.event_type !== 'llm.usage') continue
      const agent = e.payload?.agent
      const microtaskId = e.payload?.microtask_id
      const total = e.payload?.total_tokens
      if (typeof microtaskId !== 'string' || typeof total !== 'number') continue
      if (agent !== 'coder' && agent !== 'tester' && agent !== 'reflector') continue
      const entry = byTask[microtaskId] ?? { ...ZERO_TOKENS }
      if (agent === 'coder') entry.coder += total
      else if (agent === 'tester') entry.tester += total
      else entry.reflector += total
      entry.total += total
      byTask[microtaskId] = entry
    }
    return byTask
  }, [events])
}

function FileList({ label, files, mono }: { label: string; files: string[]; mono?: boolean }) {
  if (files.length === 0) return null
  return (
    <div className="mtd-field">
      <span className="mtd-key">{label}</span>
      <ul className={`mtd-files ${mono ? 'mono' : ''}`}>
        {files.map((f) => <li key={f}>{f}</li>)}
      </ul>
    </div>
  )
}

function CriteriaList({ criteria }: { criteria: MicrotaskRow['acceptance_criteria'] }) {
  if (criteria.length === 0) return <p className="empty-hint">no acceptance criteria recorded</p>
  return (
    <ol className="mtd-criteria">
      {criteria.map((c, i) => (
        <li key={i}>
          <span className="mtd-crit-text">{c.text}</span>
          {c.examples.length > 0 && (
            <ul className="mtd-examples">
              {c.examples.map((ex, j) => (
                <li key={j} className="mtd-example">
                  given <code>{JSON.stringify(ex.given)}</code> → expect <code>{JSON.stringify(ex.expect)}</code>
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ol>
  )
}

function MicrotaskDetail({ t, tokens }: { t: MicrotaskRow; tokens: MtTokens }) {
  return (
    <div className="mtd">
      <div className="mtd-field">
        <span className="mtd-key">description</span>
        <p className="mtd-text">{t.description || '—'}</p>
      </div>
      {t.approach && (
        <div className="mtd-field">
          <span className="mtd-key">approach</span>
          <p className="mtd-text">{t.approach}</p>
        </div>
      )}
      <div className="mtd-field">
        <span className="mtd-key">acceptance criteria ({t.acceptance_criteria.length})</span>
        <CriteriaList criteria={t.acceptance_criteria} />
      </div>
      <FileList label={`target files (${t.target_files.length})`} files={t.target_files} mono />
      <FileList label={`context files (${t.context_files.length})`} files={t.context_files} mono />
      <FileList label={`files touched (${t.files_touched.length})`} files={t.files_touched} mono />
      {t.dependencies.length > 0 && (
        <div className="mtd-field">
          <span className="mtd-key">depends on</span>
          <div className="mtd-deps">
            {t.dependencies.map((d) => <span key={d} className="mtd-dep">{d}</span>)}
          </div>
        </div>
      )}
      {t.test_strategy && (
        <div className="mtd-field">
          <span className="mtd-key">test strategy</span>
          <p className="mtd-text">{t.test_strategy}</p>
        </div>
      )}
      <div className="mtd-field mtd-meta">
        <span className="mtd-key">attempts</span>
        <span className="mtd-text">{t.attempts}</span>
        <span className="mtd-key">created</span>
        <span className="mtd-text">{fmtTs(t.created_at)}</span>
        <span className="mtd-key">updated</span>
        <span className="mtd-text">{fmtTs(t.updated_at)}</span>
      </div>
      <div className="mtd-field mtd-meta">
        <span className="mtd-key">tokens (coder + tester + reflector)</span>
        <span className="mtd-text">{fmtTokens(tokens.total)}</span>
        <span className="mtd-key">coder</span>
        <span className="mtd-text">{fmtTokens(tokens.coder)}</span>
        <span className="mtd-key">tester</span>
        <span className="mtd-text">{fmtTokens(tokens.tester)}</span>
        <span className="mtd-key">reflector</span>
        <span className="mtd-text">{fmtTokens(tokens.reflector)}</span>
      </div>
    </div>
  )
}

export function MicrotasksPanel({ runId }: { runId: string }) {
  const [tasks, setTasks] = useState<MicrotaskRow[]>([])
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const tokensByTask = useMicrotaskTokens(runId)

  useEffect(() => {
    setTasks([]); setOpen(null); setError(null)
    api.getMicrotasks(runId).then(setTasks).catch((e) => setError(String(e)))
  }, [runId])

  return (
    <div className="panel microtasks-panel">
      <h3>microtasks <span className="rh-count">{tasks.length}</span></h3>
      {error && <div className="error">{error}</div>}
      {tasks.length === 0 && !error && <p className="empty-hint">no microtasks planned yet</p>}
      <div className="mtlist">
        {tasks.map((t) => {
          const cls = DONE.has(t.status) ? 'ok' : BAD.has(t.status) ? 'bad' : ''
          const isOpen = open === t.microtask_id
          const tokens = tokensByTask[t.microtask_id] ?? ZERO_TOKENS
          return (
            <div key={t.microtask_id} className={`mtitem ${cls}`}>
              <button
                className={`mthead ${isOpen ? 'open' : ''}`}
                onClick={() => setOpen(isOpen ? null : t.microtask_id)}
                aria-expanded={isOpen}
              >
                <span className="mtidx">{t.idx}</span>
                <span className="mttitle" title={t.title}>{t.title}</span>
                <span className="mtcrit-count" title="acceptance criteria">{t.acceptance_criteria.length} criteria</span>
                <span className="mtfiles" title={t.target_files.join('\n')}>{t.target_files.length} files</span>
                <span className="mttokens" title={`coder ${fmtTokens(tokens.coder)} + tester ${fmtTokens(tokens.tester)} + reflector ${fmtTokens(tokens.reflector)}`}>
                  {tokens.total > 0 ? `${fmtTokens(tokens.total)} tok` : '—'}
                </span>
                <span className={`mtattempts ${t.attempts > 1 ? 'multi' : ''}`}>
                  {t.attempts > 0 ? `${t.attempts} attempt${t.attempts > 1 ? 's' : ''}` : '—'}
                </span>
                <span className="mstatus">{t.status.toLowerCase()}</span>
                <span className="mtchevron" aria-hidden>{isOpen ? '▾' : '▸'}</span>
              </button>
              {isOpen && <MicrotaskDetail t={t} tokens={tokens} />}
            </div>
          )
        })}
      </div>
    </div>
  )
}
