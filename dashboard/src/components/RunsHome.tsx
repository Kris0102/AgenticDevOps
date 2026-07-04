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
}

function fmtWhen(iso: string): string {
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

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

  const onImportFile = async (file: File) => {
    try {
      const bundle = JSON.parse(await file.text())
      const restored = await api.importRun(bundle)
      onOpen(restored.run_id)
    } catch (e) {
      alert(`import failed: ${String(e)}`)
    }
  }

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
      {!error && runs.length === 0 && <div className="loading">no runs yet</div>}

      <div className="rh-grid">
        {visible.map((run) => (
          <button
            key={run.run_id}
            className={`runbox state-${run.run_state} ${run.run_id === activeRunId ? 'active' : ''}`}
            onClick={() => onOpen(run.run_id)}
          >
            <div className="rb-top">
              <span className="rb-repo">{run.repo_name}</span>
              <span className="badge">{STATE_LABEL[run.run_state]}</span>
            </div>
            {run.task_description && (
              <div className="rb-task" title={run.task_description}>{run.task_description}</div>
            )}
            <div className="rb-meta">
              <span title="created">{fmtWhen(run.created_at)}</span>
              {run.base_sha && <span className="rb-sha" title="base sha">@{run.base_sha.slice(0, 7)}</span>}
              {typeof run.pushed_files === 'number' && (
                <span className="rb-pushed" title="files shipped by the deploy push">⇡ {run.pushed_files} files</span>
              )}
            </div>
            <code className="rb-id" title={run.run_id}>{run.run_id.slice(0, 8)}</code>
          </button>
        ))}
      </div>
    </section>
  )
}
