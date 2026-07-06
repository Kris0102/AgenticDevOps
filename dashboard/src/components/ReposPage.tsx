import { useEffect, useState } from 'react'
import {
  api,
  type RepoCreateRequest,
  type RepoValidationResult,
  type RepoView,
} from '../api'

// Repo registry (GH #41). Adding a repo validates it against GitHub (with the given
// or global token) before it is stored — the new-run page only offers registered
// repos. Tokens are masked on read; leaving the field masked keeps the stored value.
//
// Visual language mirrors the run boxes: the card's left edge + a tinted pill carry
// the validation state, facts sit in a quiet uppercase-key / mono-value row.

function relTime(iso?: string | null): string | null {
  if (!iso) return null
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

function splitName(repoUrl: string, label: string): { owner: string; name: string } {
  const parts = repoUrl.split('/')
  const name = parts[parts.length - 1] ?? label
  const owner = parts[parts.length - 2] ?? ''
  return { owner, name }
}

function ValPill({ result }: { result?: RepoValidationResult | null }) {
  if (!result) return <span className="valpill"><span className="vdot" /> unvalidated</span>
  if (result.ok) {
    return (
      <span className="valpill ok">
        <span className="vdot" /> {result.kind === 'ok_private' ? 'private' : 'public'}
      </span>
    )
  }
  return <span className="valpill bad">✗ {result.kind.replace('_', ' ')}</span>
}

function RepoCard({ repo, onChanged, onDeleted }: {
  repo: RepoView
  onChanged: (r: RepoView) => void
  onDeleted: (id: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [label, setLabel] = useState(repo.label)
  const [token, setToken] = useState(repo.token ?? '')
  const [pushOverride, setPushOverride] = useState(repo.push_branch_override ?? '')
  const [reveal, setReveal] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { owner, name } = splitName(repo.repo_url, repo.label)
  const result = repo.last_validation_result
  const stateClass = result ? (result.ok ? 'val-ok' : 'val-bad') : 'val-none'
  const checked = relTime(repo.last_validated_at)

  const validate = async () => {
    setBusy('validating'); setError(null)
    try {
      const next = await api.validateRepo(repo.repo_id)
      onChanged({
        ...repo,
        last_validation_result: next,
        last_validated_at: new Date().toISOString(),
        default_branch: next.default_branch ?? repo.default_branch,
      })
    } catch (e) { setError(String(e)) } finally { setBusy(null) }
  }

  const save = async () => {
    setBusy('saving'); setError(null)
    try {
      const updated = await api.updateRepo(repo.repo_id, {
        label,
        token,                                   // mask kept = stored token unchanged
        push_branch_override: pushOverride,      // '' clears
      })
      onChanged(updated); setEditing(false)
    } catch (e) { setError(String(e)) } finally { setBusy(null) }
  }

  const remove = async () => {
    if (!window.confirm(`Remove ${repo.label} from the registry? Past runs keep their history.`)) return
    setBusy('deleting'); setError(null)
    try { await api.deleteRepo(repo.repo_id); onDeleted(repo.repo_id) }
    catch (e) { setError(String(e)); setBusy(null) }
  }

  return (
    <div className={`provcard repocard ${stateClass}`}>
      <div className="provcard-head">
        <span className="repoid">
          <span className="owner">{owner}/</span><span className="name">{name}</span>
        </span>
        <ValPill result={result} />
        <div className="provcard-actions">
          <button className="ghost sm" disabled={!!busy} onClick={validate}>
            {busy === 'validating' ? 'checking…' : 'Check access'}
          </button>
          <button className="ghost sm" disabled={!!busy} onClick={() => setEditing((v) => !v)}>
            {editing ? 'Close' : 'Edit'}
          </button>
          <button className="ghost sm danger" disabled={!!busy} onClick={remove}>Remove</button>
        </div>
      </div>

      <div className="repourl">{repo.repo_url}</div>

      <div className="repofacts">
        <span className="rfact"><span className="rk">branch</span>
          <span className="rv"><span className="glyph">⎇</span>{repo.default_branch ?? '—'}</span></span>
        <span className="rfact"><span className="rk">pushes to</span>
          {repo.push_branch_override
            ? <span className="rv">{repo.push_branch_override}</span>
            : <span className="rv quiet">agent/run-&lt;run_id&gt;</span>}</span>
        <span className="rfact"><span className="rk">token</span>
          <span className={`rv ${repo.token ? '' : 'quiet'}`}>{repo.token ? 'per-repo' : 'global'}</span></span>
        {checked && (
          <span className="rfact"><span className="rk">checked</span>
            <span className="rv quiet">{checked}</span></span>
        )}
      </div>

      {result && !result.ok && <div className="valreason">{result.reason}</div>}

      {editing && (
        <div className="repoedit np-grid">
          <label className="fld"><span>label</span>
            <input value={label} onChange={(e) => setLabel(e.target.value)} /></label>
          <label className="fld"><span>token <em>leave masked to keep · clear to use global</em></span>
            <div className="keyfield">
              <input type={reveal ? 'text' : 'password'} placeholder="no per-repo token"
                value={token} onChange={(e) => setToken(e.target.value)} />
              <button type="button" className="reveal" title={reveal ? 'hide' : 'show'}
                onClick={() => setReveal((v) => !v)}>{reveal ? '🙈' : '👁'}</button>
            </div>
          </label>
          <label className="fld"><span>push branch override <em>optional — never the default branch</em></span>
            <input placeholder={'agent/run-<run_id>'} value={pushOverride}
              onChange={(e) => setPushOverride(e.target.value)} /></label>
          <div className="np-actions span2">
            <button className="primary" disabled={!!busy} onClick={save}>
              {busy === 'saving' ? 'saving…' : 'Save changes'}
            </button>
          </div>
        </div>
      )}

      {error && <div className="error">{error}</div>}
    </div>
  )
}

export function ReposPage({ onClose }: { onClose: () => void }) {
  const [repos, setRepos] = useState<RepoView[] | null>(null)
  const [form, setForm] = useState<RepoCreateRequest>({ repo_url: '' })
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.listRepos().then(setRepos).catch((e) => setError(String(e)))
  }, [])

  const add = async () => {
    setAdding(true); setError(null)
    try {
      const created = await api.createRepo(form)
      setRepos((r) => [...(r ?? []), created])
      setForm({ repo_url: '' })
    } catch (e) {
      // validation failures (404/401/503) carry the GitHub-derived reason
      setError(String(e))
    } finally { setAdding(false) }
  }

  return (
    <section className="settingspage">
      <header className="sp-head">
        <div className="sp-title">
          <button className="ghost sp-back" onClick={onClose}>← back</button>
          <div>
            <h2>Repositories</h2>
            <p className="sp-sub">Repos the agents can run against — each is checked against GitHub before it can be used.</p>
          </div>
        </div>
      </header>

      <div className="sp-section">
        <div className="sp-section-head">
          <h3>Add repository</h3>
          <p>The URL is canonicalized; a private repo needs a token here or in Settings → GitHub.</p>
        </div>
        <div className="provcard repocard val-none">
          <div className="np-grid">
            <label className="fld span2 addrepo-url"><span>repository url</span>
              <input placeholder="https://github.com/owner/repo" value={form.repo_url}
                onChange={(e) => setForm({ ...form, repo_url: e.target.value })}
                onKeyDown={(e) => { if (e.key === 'Enter' && form.repo_url.trim() && !adding) add() }} /></label>
            <label className="fld"><span>label <em>optional — defaults to owner/repo</em></span>
              <input value={form.label ?? ''}
                onChange={(e) => setForm({ ...form, label: e.target.value || null })} /></label>
            <label className="fld"><span>token <em>optional — falls back to the global one</em></span>
              <input type="password" placeholder="per-repo access token" value={form.token ?? ''}
                onChange={(e) => setForm({ ...form, token: e.target.value || null })} /></label>
            <label className="fld span2"><span>push branch override <em>optional — never the default branch</em></span>
              <input placeholder={'agent/run-<run_id>'} value={form.push_branch_override ?? ''}
                onChange={(e) => setForm({ ...form, push_branch_override: e.target.value || null })} /></label>
          </div>
          {error && <div className="error">{error}</div>}
          <div className="np-actions">
            <button className="primary" disabled={adding || !form.repo_url.trim()} onClick={add}>
              {adding ? 'checking access…' : 'Check & add'}
            </button>
          </div>
        </div>
      </div>

      <div className="sp-section">
        <div className="sp-section-head">
          <h3>Registered</h3>
        </div>
        {repos === null && <div className="sp-loading">loading repositories…</div>}
        {repos?.length === 0 && (
          <div className="rh-empty">
            <span className="rh-empty-mark">⎇</span>
            <strong>No repositories yet</strong><br />
            Add one above — the new-run page picks from this list.
          </div>
        )}
        {(repos ?? []).map((r) => (
          <RepoCard key={r.repo_id} repo={r}
            onChanged={(next) => setRepos((rs) => (rs ?? []).map((x) => (x.repo_id === next.repo_id ? next : x)))}
            onDeleted={(id) => setRepos((rs) => (rs ?? []).filter((x) => x.repo_id !== id))} />
        ))}
      </div>
    </section>
  )
}
