import { useState } from 'react'
import { api } from '../api'
import { buildRunRequest } from '../defaultRunRequest'

// plan/code = per-step review; deploy = one holistic review of the full diff before push
const GATES = ['plan', 'code', 'test', 'deploy'] as const

// Open an existing run by id, or trigger a new one. New-run sends a full RunRequest
// (defaults filled by buildRunRequest); the human only supplies repo/task/gates.
export function RunControls({ onRun }: { onRun: (id: string) => void }) {
  const [existing, setExisting] = useState('')
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
    <section className="controls">
  
      <details className="newrun">
        <summary>Start a new run</summary>
        <div className="grid">
          <input placeholder="repo url" value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
          <input
            placeholder="base sha (optional)"
            value={baseSha}
            onChange={(e) => setBaseSha(e.target.value)}
          />
          <textarea
            placeholder="task description"
            value={task}
            onChange={(e) => setTask(e.target.value)}
          />
          <div className="gates">
            {GATES.map((g) => (
              <label key={g}>
                <input
                  type="checkbox"
                  checked={!!gates[g]}
                  onChange={(e) => setGates({ ...gates, [g]: e.target.checked })}
                />{' '}
                {g} gate
              </label>
            ))}
          </div>
          <button disabled={busy || !repoUrl.trim() || !task.trim()} onClick={start}>
            {busy ? 'starting…' : 'Start run'}
          </button>
        </div>
      </details>
    </section>
  )
}
