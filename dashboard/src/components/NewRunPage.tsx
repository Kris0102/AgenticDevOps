import { useEffect, useState } from 'react'
import { api, type AppSettings, type RepoView } from '../api'
import { buildRunRequest } from '../defaultRunRequest'

const GATES = ['plan', 'code', 'test', 'deploy'] as const

// Per-run settings page: a registered-repo picker (GH #41) + task, with a model/gates
// override pre-filled from the global settings. Starting the run preflights the repo
// AND the provider server-side; a failure is reported inline and no run is created.
export function NewRunPage({ onRun, onCancel, onManageRepos }: {
  onRun: (id: string) => void
  onCancel: () => void
  onManageRepos: () => void
}) {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [repos, setRepos] = useState<RepoView[] | null>(null)
  const [repoId, setRepoId] = useState('')
  const [branches, setBranches] = useState<string[]>([])
  const [branchWarning, setBranchWarning] = useState<string | null>(null)
  const [baseRef, setBaseRef] = useState('')
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
    api.listRepos().then(setRepos).catch((e) => setError(String(e)))
  }, [])

  const provider = settings?.providers.find((p) => p.id === providerId)
  const repo = repos?.find((r) => r.repo_id === repoId)

  // Live branch list per selected repo; a fetch failure only degrades to "default
  // branch" (base_ref = null) with a warning — the run can still start.
  useEffect(() => {
    setBranches([]); setBranchWarning(null); setBaseRef('')
    if (!repoId) return
    let stale = false
    api.getRepoBranches(repoId)
      .then((names) => {
        if (stale) return
        setBranches(names)
        const def = repos?.find((r) => r.repo_id === repoId)?.default_branch
        setBaseRef(def && names.includes(def) ? def : names[0] ?? '')
      })
      .catch((e) => { if (!stale) setBranchWarning(`branch list unavailable (${e}) — using the default branch`) })
    return () => { stale = true }
  }, [repoId, repos])

  const start = async () => {
    if (!settings || !repoId) return
    setBusy(true); setError(null)
    try {
      const req = buildRunRequest({
        repoId,
        baseRef: baseRef || null,
        baseSha,
        task,
        hitlCheckpoints: GATES.filter((g) => gates[g]),
        llm: { provider_id: providerId, model, temperature: settings.selected.temperature, max_retries: settings.selected.max_retries },
        maxTotalIterations: settings.defaults.max_total_iterations,
        maxMicrotaskAttempts: settings.defaults.max_microtask_attempts,
        maxTotalTokens: settings.defaults.max_total_tokens,
      })
      const r = await api.createRun(req)
      onRun(r.run_id)
    } catch (e) {
      // repo/provider preflight failures come back as 4xx with the reason in the message
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
            <select value={repoId} onChange={(e) => setRepoId(e.target.value)}>
              <option value="" disabled>{repos === null ? 'loading…' : repos.length ? 'pick a repo…' : 'no repos registered'}</option>
              {(repos ?? []).map((r) => (
                <option key={r.repo_id} value={r.repo_id}>
                  {r.label}{r.last_validation_result?.kind === 'ok_private' ? ' · private' : ''}
                </option>
              ))}
            </select>
            {repos?.length === 0 && (
              <em className="np-hint">no repos yet — <button type="button" className="linklike" onClick={onManageRepos}>add one on the Repos page</button></em>
            )}
          </label>
          <label className="fld"><span>base branch</span>
            <select value={baseRef} onChange={(e) => setBaseRef(e.target.value)} disabled={!repoId || branches.length === 0}>
              {branches.length === 0 && <option value="">{repoId ? (branchWarning ? 'default branch' : 'loading…') : '—'}</option>}
              {branches.map((b) => (
                <option key={b} value={b}>{b}{b === repo?.default_branch ? ' (default)' : ''}</option>
              ))}
            </select>
            {branchWarning && <em className="np-hint">{branchWarning}</em>}
          </label>
          <label className="fld"><span>base sha <em>optional</em></span>
            <input placeholder="pin a commit on the base branch" value={baseSha} onChange={(e) => setBaseSha(e.target.value)} /></label>
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
          <button className="primary" disabled={busy || !repoId || !task.trim() || !settings} onClick={start}>
            {busy ? 'preflighting…' : 'Start run'}
          </button>
        </div>
      </div>
    </section>
  )
}
