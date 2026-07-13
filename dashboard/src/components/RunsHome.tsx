import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type RunResponseWithDetails, type RunState } from '../api'

// Previous-runs home: every run as a labelled box (repo, TASK, state, when, push size),
// filterable by text and sortable. Click a box to open that run. Import restores an
// exported bundle (agentic_run_<id>_<ts>.json) into this DB, then opens it.

type SortKey = 'created' | 'repo' | 'status'
const REFRESH_MS = 5000

const STATE_LABEL: Record<RunState, string> = {
  running: 'running',
  paused_hitl: 'paused',
  completed: 'completed',
  failed: 'failed',
  stalled: 'stalled',
  stopped: 'manually stopped',
}

function fmtWhen(iso: string): string {
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

function fmtAgo(iso: string): string {
  const t = new Date(iso).getTime()
  if (isNaN(t)) return ''
  const s = Math.round((Date.now() - t) / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

const LIVE_STATES: RunState[] = ['running', 'paused_hitl', 'stalled']
const DELETABLE_STATES = new Set<RunState>(['stalled', 'completed', 'failed'])

export function RunsHome({ activeRunId, onOpen }: { activeRunId: string | null; onOpen: (id: string) => void }) {
  const [runs, setRuns] = useState<RunResponseWithDetails[]>([])
  const [error, setError] = useState<string | null>(null)
  const [sortBy, setSortBy] = useState<SortKey>('created')
  const [asc, setAsc] = useState(false)
  const [filter, setFilter] = useState('')
  const fileRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      api
        .getRunsDetails()
        .then((r) => alive && (setRuns(r), setError(null)))
        .catch((e) => alive && setError(String(e)))
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])
  
  const deleteRun = async (runId: string) => {
    if (!confirm(`Are you sure you want to delete run ${runId}? This cannot be undone.`)) return
    try {
      await api.deleteRun(runId)
      setRuns((prev) => prev.filter((r) => r.run_id !== runId))
      if (activeRunId === runId) onOpen('') // close the deleted run if it was open
    } catch (e) {
      alert(`Failed to delete run ${runId}: ${String(e)}`)
    }
  }
  const onImportFile = async (file: File) => {
    try {
      const bundle = JSON.parse(await file.text())
      const restored = await api.importRun(bundle)
      onOpen(restored.run_id)
    } catch (e) {
      alert(`import failed: ${String(e)}`)
    }
  }

  const pulse = useMemo(() => {
    const by = (st: RunState) => runs.filter((r) => r.run_state === st).length
    const completed = by('completed')
    const failed = by('failed')
    const finished = completed + failed
    return {
      total: runs.length,
      active: runs.filter((r) => LIVE_STATES.includes(r.run_state)).length,
      completed,
      failed,
      // success rate over FINISHED runs only (in-flight runs don't count for/against)
      success: finished ? Math.round((completed / finished) * 100) : null,
    }
  }, [runs])

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase()
    const match = (r: RunResponseWithDetails) =>
      !q ||
      r.repo_name.toLowerCase().includes(q) ||
      (r.task_description ?? '').toLowerCase().includes(q) ||
      r.run_id.startsWith(q)
    const cmp = (a: RunResponseWithDetails, b: RunResponseWithDetails): number => {
      if (sortBy === 'repo') return a.repo_name.localeCompare(b.repo_name)
      if (sortBy === 'status') return a.run_state.localeCompare(b.run_state)
      return a.created_at.localeCompare(b.created_at) // ISO strings sort chronologically
    }
    const out = runs.filter(match).sort(cmp)
    return asc ? out : out.reverse()
  }, [runs, sortBy, asc, filter])

  return (
    <section className="runshome">
      {runs.length > 0 && (
        <div className="pulse" aria-label="run pulse">
          <div className="pstat"><span className="pv">{pulse.total}</span><span className="pl">runs</span></div>
          <div className={`pstat ${pulse.active ? 'accent' : ''}`}><span className="pv">{pulse.active}</span><span className="pl">active</span></div>
          <div className="pstat ok"><span className="pv">{pulse.completed}</span><span className="pl">completed</span></div>
          <div className={`pstat ${pulse.failed ? 'bad' : ''}`}><span className="pv">{pulse.failed}</span><span className="pl">failed</span></div>
          <div className="pstat"><span className="pv">{pulse.success === null ? '—' : `${pulse.success}%`}</span><span className="pl">success rate</span></div>
        </div>
      )}

      <div className="rh-head">
        <h3>previous runs <span className="rh-count">{runs.length}</span></h3>
        <div className="rh-sort">
          <input
            className="rh-filter"
            placeholder="filter repo / task…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <label>
            sort
            <select value={sortBy} onChange={(e) => setSortBy(e.target.value as SortKey)}>
              <option value="created">created</option>
              <option value="repo">repo</option>
              <option value="status">status</option>
            </select>
          </label>
          <button className="rh-dir" onClick={() => setAsc((v) => !v)} title="toggle direction">
            {asc ? '↑ asc' : '↓ desc'}
          </button>
          <button className="rh-import" onClick={() => fileRef.current?.click()} title="restore an exported run bundle">
            import
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="application/json"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) onImportFile(f)
              e.target.value = ''
            }}
          />
        </div>
      </div>

      {error && <div className="error">runs error: {error}</div>}
      {!error && runs.length === 0 && (
        <div className="rh-empty">
          <span className="rh-empty-mark" aria-hidden>◇</span>
          <p>No runs yet. Start one with <strong>＋ New run</strong>, or <strong>import</strong> an exported bundle.</p>
        </div>
      )}
      {!error && runs.length > 0 && visible.length === 0 && <div className="loading">no runs match “{filter}”</div>}

      <div className="rh-grid">
        {visible.map((run) => (
          <div
            key={run.run_id}
            className={`runbox state-${run.run_state} ${run.run_id === activeRunId ? 'active' : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => onOpen(run.run_id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onOpen(run.run_id)
              }
            }}
          >
            <div className="rb-top">
              <span className="rb-repo">{run.repo_name}</span>
              <span className="badge">
                {LIVE_STATES.includes(run.run_state) && run.run_state !== 'stalled' && <span className="livedot" aria-hidden />}
                {STATE_LABEL[run.run_state]}
              </span>
            </div>
            {run.task_description && (
              <div className="rb-task" title={run.task_description}>{run.task_description}</div>
            )}
            {run.run_state === 'failed' && run.failure_reason && (
              <div className="rb-fail" title={run.failure_reason}>✗ {run.failure_reason}</div>
            )}
            <div className="rb-meta">
              <span title={fmtWhen(run.created_at)}>{fmtAgo(run.created_at)}</span>
              {run.base_sha && <span className="rb-sha" title="base sha">@{run.base_sha.slice(0, 7)}</span>}
              {run.llm_model && <span className="rb-model" title={`model: ${run.llm_model}`}>⌁ {run.llm_model.split('/').pop()}</span>}
              {typeof run.pushed_files === 'number' && (
                <span className="rb-pushed" title="files shipped by the deploy push">⇡ {run.pushed_files} files</span>
              )}
            </div>
            <div className="rb-foot">
              <code className="rb-id" title={run.run_id}>{run.run_id.slice(0, 8)}</code>
              {run.run_name && <span className="rb-name" title={run.run_name}>{run.run_name}</span>}
              <button
                className="runbox-del"
                disabled={!DELETABLE_STATES.has(run.run_state)}
                onClick={(e) => { e.stopPropagation(); deleteRun(run.run_id) }}
                title={DELETABLE_STATES.has(run.run_state) ? 'delete this run' : 'delete becomes available after the run stalls, completes, or fails'}
              >
                delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
