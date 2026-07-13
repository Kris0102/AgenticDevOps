import { useEffect, useMemo, useState } from 'react'
import { api, type EventView, type MicrotaskRow, type PipelineProgress, type RunResponseWithDetails } from '../api'

// "What happened in this run" at a glance, built ONLY from durable tables (events +
// microtasks) — so it's just as rich for a run finished last week as for a live one.
// Consumed by App above the log/history panels.

function fmtDuration(ms: number): string {
  if (ms <= 0 || !isFinite(ms)) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

const DONE = new Set(['COMPLETED', 'DONE'])
// Statuses that mean the coder/tester/reflector loop is actively working this microtask
// (mirrors the backend's _IN_FLIGHT_MICROTASK_STATUSES) — the row to light up.
const IN_FLIGHT = new Set(['IN_PROGRESS', 'EXECUTING', 'VERIFYING'])

// Blackboard status → coarse phase for the Gantt strip.
function phaseOf(status: string): string {
  if (status.includes('BOOTSTRAP')) return 'setup'
  if (status.includes('PLAN')) return 'plan'
  if (status.includes('CODE')) return 'code'
  if (status.includes('TEST') || status.includes('REFLECT')) return 'test'
  if (status.includes('HITL')) return 'gate'
  if (status.includes('DEPLOY')) return 'deploy'
  return 'other'
}

interface PhaseSeg {
  phase: string
  ms: number
  pct: number
}

interface Interval {
  from: number
  to: number
}

// STALLED time shouldn't count toward "how long this run took" — the run wasn't being
// worked on. Pair each run.stalled with the next run.resumed to get the idle spans; a
// trailing unpaired run.stalled (still stalled right now) needs no special handling —
// there's no later event yet for it to inflate.
function stalledIntervals(events: EventView[]): Interval[] {
  const stalls = events.filter((e) => e.event_type === 'run.stalled')
  const resumes = events.filter((e) => e.event_type === 'run.resumed').map((e) => new Date(e.ts).getTime())
  const out: Interval[] = []
  for (const s of stalls) {
    const from = new Date(s.ts).getTime()
    const to = resumes.find((r) => r > from)
    if (to !== undefined) out.push({ from, to })
  }
  return out
}

function overlapMs(from: number, to: number, intervals: Interval[]): number {
  let ms = 0
  for (const iv of intervals) {
    ms += Math.max(0, Math.min(to, iv.to) - Math.max(from, iv.from))
  }
  return ms
}

// Consecutive run.status_changed events → proportional phase segments (where time went).
// Stalled spans are clipped out of whichever segment they fall in — idle time waiting for
// a human to hit "continue" isn't that phase's work.
function buildPhases(events: EventView[]): PhaseSeg[] {
  const changes = events.filter((e) => e.event_type === 'run.status_changed')
  if (changes.length === 0 || events.length < 2) return []
  const end = new Date(events[events.length - 1].ts).getTime()
  const stalls = stalledIntervals(events)
  const raw: { phase: string; ms: number }[] = []
  for (let i = 0; i < changes.length; i++) {
    const from = new Date(changes[i].ts).getTime()
    const to = i + 1 < changes.length ? new Date(changes[i + 1].ts).getTime() : end
    const phase = phaseOf(String(changes[i].payload?.to ?? ''))
    const ms = Math.max(0, to - from - overlapMs(from, to, stalls))
    if (ms === 0) continue
    // merge adjacent same-phase segments so the strip stays readable
    const last = raw[raw.length - 1]
    if (last && last.phase === phase) last.ms += ms
    else raw.push({ phase, ms })
  }
  const total = raw.reduce((acc, s) => acc + s.ms, 0)
  return total > 0 ? raw.map((s) => ({ ...s, pct: (s.ms / total) * 100 })) : []
}

export function RunOverview({ runId, live, capVersion, progress }: { runId: string; live: boolean; capVersion?: number; progress?: PipelineProgress | null }) {
  const [events, setEvents] = useState<EventView[]>([])
  const [tasks, setTasks] = useState<MicrotaskRow[]>([])
  const [info, setInfo] = useState<RunResponseWithDetails | null>(null)

  // Run labels (task, repo) — picked from the runs list; small, single-user deployment.
  // Also re-fetched when `capVersion` bumps (a token-budget cap raise just succeeded) so the
  // tokens-used tile's denominator reflects the new cap immediately, not on next remount.
  useEffect(() => {
    api.getRunsDetails()
      .then((rs) => setInfo(rs.find((r) => r.run_id === runId) ?? null))
      .catch(() => {})
  }, [runId, capVersion])

  useEffect(() => {
    let alive = true
    let cursor = 0
    let acc: EventView[] = []
    const load = async () => {
      try {
        // Page through everything new since the last tick (events are the source of truth).
        for (;;) {
          const batch = await api.getEvents(runId, cursor)
          if (!alive || batch.length === 0) break
          cursor = batch[batch.length - 1].seq
          acc = [...acc, ...batch]
          if (batch.length < 200) break
        }
        if (alive) setEvents(acc)
        const mts = await api.getMicrotasks(runId)
        if (alive) setTasks(mts)
      } catch {
        /* transient — next tick retries */
      }
    }
    setEvents([]); setTasks([]); acc = []
    load()
    if (!live) return () => { alive = false }
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [runId, live])

  const stats = useMemo(() => {
    const count = (type: string) => events.filter((e) => e.event_type === type).length
    const first = events[0]?.ts
    const last = events[events.length - 1]?.ts
    const rawDuration = first && last ? new Date(last).getTime() - new Date(first).getTime() : NaN
    const stalledMs = first && last
      ? overlapMs(new Date(first).getTime(), new Date(last).getTime(), stalledIntervals(events))
      : 0
    const duration = isFinite(rawDuration) ? rawDuration - stalledMs : rawDuration
    const pushed = events.find((e) => e.event_type === 'gitops.pushed')
    const toolMs = events
      .filter((e) => e.event_type === 'tool.executed')
      .reduce((acc, e) => acc + (typeof e.payload?.duration_ms === 'number' ? (e.payload.duration_ms as number) : 0), 0)
    const tokensUsed = events
      .filter((e) => e.event_type === 'llm.usage')
      .reduce((acc, e) => acc + (typeof e.payload?.total_tokens === 'number' ? (e.payload.total_tokens as number) : 0), 0)
    const capRaises = events.filter((e) => e.event_type === 'run.token_budget_raised')
    return {
      duration: fmtDuration(duration),
      planDrafts: count('plan.drafted'),
      reviews: count('plan.reviewed'),
      diagnoses: count('reflection.created'),
      failures: count('microtask.failed') + count('run.failed'),
      gates: count('hitl.decided'),
      tools: count('tool.executed'),
      toolTime: fmtDuration(toolMs),
      tokensUsed,
      capRaises,
      branch: pushed ? String(pushed.payload?.branch ?? '') : null,
      blocked: count('gitops.secret_blocked') > 0,
    }
  }, [events])

  // The cap in force at run start — the first raise's "old" value if it was ever
  // raised, otherwise whatever's on the run now (never raised → never changed).
  const initialCap = stats.capRaises.length > 0
    ? Number(stats.capRaises[0].payload?.old_max_total_tokens ?? 0)
    : info?.max_total_tokens ?? null

  const phases = useMemo(() => buildPhases(events), [events])

  const done = tasks.filter((t) => DONE.has(t.status)).length
  // The microtask being worked RIGHT NOW (coder/tester/reflector loop). Prefer the
  // checkpoint-derived progress idx (authoritative — it uses current_microtask_id), else fall
  // back to any in-flight status on the durable board. Only while live.
  const current = live
    ? (progress?.microtask ? tasks.find((t) => t.idx === progress.microtask!.idx) : undefined)
      ?? tasks.find((t) => IN_FLIGHT.has(t.status))
      ?? null
    : null
  // Attempt cap for the "now working on" denominator — from the checkpoint-derived
  // progress prop (ADR 0028), matched by idx in case it lags the durable board by a beat.
  const maxAttempts = current && progress?.microtask?.idx === current.idx ? progress.microtask.max_attempts : null
  const planDrafts = stats.planDrafts

  return (
    <div className="panel overview">
      <div className="ov-head">
        <h3>run overview</h3>
        <a className="export" href={api.exportUrl(runId)} download title="download this run's full record (events, artifacts, microtasks) as JSON">
          ⇓ export
        </a>
      </div>

      {info?.task_description && <p className="ov-task">{info.task_description}</p>}
      {info?.llm_model && <p className="ov-model">model: <code>{info.llm_model}</code>{info.llm_provider ? ` · ${info.llm_provider}` : ''}</p>}

      <div className="tiles">
        <div className="tile"><span className="tv">{tasks.length ? `${done}/${tasks.length}` : '—'}</span><span className="tl">microtasks done</span></div>
        <div className="tile"><span className="tv">{stats.duration}</span><span className="tl">duration</span></div>
        <div className="tile"><span className="tv">{planDrafts || stats.reviews}</span><span className="tl">plan attempts</span></div>
        <div className="tile"><span className="tv">{stats.gates}</span><span className="tl">human decisions</span></div>
        <div className={`tile ${stats.failures + stats.diagnoses > 0 ? 'warn' : ''}`}>
          <span className="tv">{stats.failures + stats.diagnoses}</span><span className="tl">failures fixed</span>
        </div>
        <div className="tile"><span className="tv">{stats.tools}</span><span className="tl">tool calls</span></div>
        <div className="tile"><span className="tv">{stats.toolTime}</span><span className="tl">tool time</span></div>
        <div className="tile">
          <span className="tv">{fmtTokens(stats.tokensUsed)}{info?.max_total_tokens ? ` / ${fmtTokens(info.max_total_tokens)}` : ''}</span>
          <span className="tl">tokens used</span>
        </div>
      </div>

      {phases.length > 0 && (
        <div className="gantt" title="where the run's time went">
          {phases.map((p, i) => (
            <div
              key={i}
              className={`gseg ph-${p.phase}`}
              style={{ width: `${Math.max(p.pct, 1.5)}%` }}
              title={`${p.phase} — ${fmtDuration(p.ms)}`}
            >
              {p.pct > 9 && <span>{p.phase}</span>}
            </div>
          ))}
        </div>
      )}

      {stats.blocked && <div className="error">⛔ push was blocked — secret findings in the working tree</div>}
      <div className="capraised">
        {initialCap ? `token cap at start: ${fmtTokens(initialCap)}` : 'no token cap set at start'}
      </div>
      {stats.capRaises.map((e) => (
        <div key={e.seq} className="capraised" title={new Date(e.ts).toLocaleString()}>
          ⬆ cap raised {fmtTokens(Number(e.payload?.old_max_total_tokens ?? 0))} → {fmtTokens(Number(e.payload?.new_max_total_tokens ?? 0))}
        </div>
      ))}
      {stats.branch && <div className="pushedto">pushed to <code>{stats.branch}</code></div>}

      {current && (
        <div className="nowworking">
          <span className="nw-pulse" aria-hidden>●</span>
          <span className="nw-label">working on</span>
          <span className="nw-idx">#{current.idx + 1}{tasks.length ? `/${tasks.length}` : ''}</span>
          <span className="nw-title" title={current.title}>{current.title}</span>
          <span className="nw-attempt">attempt {Math.max(current.attempts, 1)}{maxAttempts ? `/${maxAttempts}` : ''}</span>
        </div>
      )}

      {tasks.length > 0 && (
        <div className="mtboard">
          {tasks.map((t) => (
            <div
              key={t.microtask_id}
              className={`mtrow ${DONE.has(t.status) ? 'ok' : t.status === 'FAILED' ? 'bad' : ''} ${current?.microtask_id === t.microtask_id ? 'now' : ''}`}
            >
              <span className="mtidx">{t.idx + 1}</span>
              <span className="mttitle" title={t.title}>{t.title}</span>
              <span className="mtfiles" title={(t.files_touched.length ? t.files_touched : t.target_files).join('\n')}>
                {(t.files_touched.length ? t.files_touched : t.target_files).length} files
              </span>
              <span className={`mtattempts ${t.attempts > 1 ? 'multi' : ''}`}>
                {t.attempts > 0 ? `${t.attempts} attempt${t.attempts > 1 ? 's' : ''}` : '—'}
              </span>
              <span className="mstatus">{t.status.toLowerCase()}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
