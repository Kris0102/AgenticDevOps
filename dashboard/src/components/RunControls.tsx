import { useState } from 'react'
import { api } from '../api'
import { buildRunRequest } from '../defaultRunRequest'

// plan/code = per-step review; deploy = one holistic review of the full diff before push
const GATES = ['plan', 'code', 'test', 'deploy'] as const

// New-run form. Visibility is owned by the header (App); this is the panel body. Sends a
// full RunRequest (defaults filled by buildRunRequest); the human supplies repo/task/gates.
export function RunControls({ onRun, onCancel }: { onRun: (id: string) => void; onCancel: () => void }) {
  const [repoUrl, setRepoUrl] = useState('')
  const [baseSha, setBaseSha] = useState('')
  const [task, setTask] = useState('')
  const [gates, setGates] = useState<Record<string, boolean>>({ plan: true, code: true, test: false })
  const [busy, setBusy] = useState(false)

  const start = async () => {
    setBusy(true)
    try {
      const hitlCheckpoints = GATES.filter((g) => gates[g])
      const r = await api.createRun(buildRunRequest({ repoUrl, baseSha, task, hitlCheckpoints }))
      onRun(r.run_id)
    } catch (e) {
      alert(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="controls newrun-panel">
      <div className="np-grid">
        <label className="fld">
          <span>repository</span>
          <input placeholder="https://github.com/owner/repo" value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
        </label>
        <label className="fld">
          <span>base sha <em>optional</em></span>
          <input placeholder="leave empty for a new repo" value={baseSha} onChange={(e) => setBaseSha(e.target.value)} />
        </label>
        <label className="fld span2">
          <span>task</span>
          <textarea placeholder="what should the agents implement?" value={task} onChange={(e) => setTask(e.target.value)} />
        </label>
        <div className="fld span2">
          <span>human review gates</span>
          <div className="gatechips">
            {GATES.map((g) => (
              <label key={g} className={`chip ${gates[g] ? 'on' : ''}`}>
                <input type="checkbox" checked={!!gates[g]} onChange={(e) => setGates({ ...gates, [g]: e.target.checked })} />
                {g}
              </label>
            ))}
          </div>
        </div>
      </div>
      <div className="np-actions">
        <button className="ghost" onClick={onCancel}>Cancel</button>
        <button className="primary" disabled={busy || !repoUrl.trim() || !task.trim()} onClick={start}>
          {busy ? 'starting…' : 'Start run'}
        </button>
      </div>
    </section>
  )
}
