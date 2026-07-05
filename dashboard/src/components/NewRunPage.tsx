import { useEffect, useState } from 'react'
import { api, type AppSettings } from '../api'
import { buildRunRequest } from '../defaultRunRequest'

const GATES = ['plan', 'code', 'test', 'deploy'] as const

// Per-run settings page: repo/task + a model/gates override pre-filled from the global
// settings. Starting the run preflights the provider server-side; a dead/mis-keyed provider
// is reported inline and no run is created.
export function NewRunPage({ onRun, onCancel }: { onRun: (id: string) => void; onCancel: () => void }) {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [repoUrl, setRepoUrl] = useState('')
  const [baseSha, setBaseSha] = useState('')
  const [task, setTask] = useState('')
  const [providerId, setProviderId] = useState('')
  const [model, setModel] = useState('')
  const [gates, setGates] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.getSettings().then((s) => {
      setSettings(s)
      setProviderId(s.selected.provider_id)
      setModel(s.selected.model)
      setGates(Object.fromEntries(GATES.map((g) => [g, s.defaults.hitl_checkpoints.includes(g)])))
    }).catch((e) => setError(String(e)))
  }, [])

  const provider = settings?.providers.find((p) => p.id === providerId)

  const start = async () => {
    if (!settings) return
    setBusy(true); setError(null)
    try {
      const req = buildRunRequest({
        repoUrl, baseSha, task,
        hitlCheckpoints: GATES.filter((g) => gates[g]),
        llm: { provider_id: providerId, model, temperature: settings.selected.temperature, max_retries: settings.selected.max_retries },
        maxTotalIterations: settings.defaults.max_total_iterations,
        maxMicrotaskAttempts: settings.defaults.max_microtask_attempts,
      })
      const r = await api.createRun(req)
      onRun(r.run_id)
    } catch (e) {
      // preflight failures come back as a 401/503 with the reason in the message
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="settingspage newrunpage">
      <header className="sp-head">
        <div className="sp-title">
          <button className="ghost sp-back" onClick={onCancel}>← back</button>
          <div>
            <h2>New run</h2>
            <p className="sp-sub">Repo + task, with an optional model / gate override for this run.</p>
          </div>
        </div>
      </header>

      <div className="panel">
        <div className="np-grid">
          <label className="fld"><span>repository</span>
            <input placeholder="https://github.com/owner/repo" value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} /></label>
          <label className="fld"><span>base sha <em>optional</em></span>
            <input placeholder="leave empty for a new repo" value={baseSha} onChange={(e) => setBaseSha(e.target.value)} /></label>
          <label className="fld span2"><span>task</span>
            <textarea placeholder="what should the agents implement?" value={task} onChange={(e) => setTask(e.target.value)} /></label>

          <label className="fld"><span>provider</span>
            <select value={providerId} onChange={(e) => setProviderId(e.target.value)}>
              {(settings?.providers ?? []).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select></label>
          <label className="fld"><span>model</span>
            <input list="np-models" value={model} onChange={(e) => setModel(e.target.value)} />
            <datalist id="np-models">{(provider?.models ?? []).map((m) => <option key={m} value={m} />)}</datalist>
          </label>

          <div className="fld span2"><span>human review gates</span>
            <div className="gatechips">
              {GATES.map((g) => (
                <label key={g} className={`chip ${gates[g] ? 'on' : ''}`}>
                  <input type="checkbox" checked={!!gates[g]} onChange={(e) => setGates({ ...gates, [g]: e.target.checked })} />{g}
                </label>
              ))}
            </div>
          </div>
        </div>

        {error && <div className="error">{error}</div>}

        <div className="np-actions">
          <button className="ghost" onClick={onCancel}>Cancel</button>
          <button className="primary" disabled={busy || !repoUrl.trim() || !task.trim() || !settings} onClick={start}>
            {busy ? 'preflighting…' : 'Start run'}
          </button>
        </div>
      </div>
    </section>
  )
}
