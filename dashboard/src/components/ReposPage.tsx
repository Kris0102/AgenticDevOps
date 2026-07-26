import { useEffect, useState } from 'react'
import { ImageCheck } from './ImageCheck'
import { shortImage } from '../imageLabel'
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

// Shown under the sandbox-image field on both the add and edit forms. The contract is the
// non-obvious part: the sandbox wraps every command in `timeout --kill-after`, so an image
// without GNU coreutils fails everything. The server checks and names what's missing.
const SANDBOX_IMAGE_HINT =
  'optional — an image with this project\'s toolchain, so tests actually run. ' +
  'Needs git, sh, sleep and GNU coreutils. Pulled and checked on save. ' +
  'Runs with the same privileges as the sandbox.'

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

// Traffic-light access feedback: green = push + PR access, orange = push only (no
// PR), red = not usable at all (push missing, or repo unreachable/not found/etc).
// One source of truth so "Check access" (existing repos) and "Check & add" (new
// repos) read identically.
type AccessCls = 'ok' | 'warn' | 'bad'

function accessState(result: RepoValidationResult): { cls: AccessCls; badge: string; note: string } {
  if (!result.ok) {
    // NO_PUSH_ACCESS came from a readable repo (200) — cloneable, usable for dry-runs
    // and registrable; not a hard failure. Warn (orange), don't reject (red).
    if (result.kind === 'no_push_access') {
      return {
        cls: 'warn',
        badge: 'clone-only',
        note: 'Readable, but no push access — usable for dry-runs only. Add a token with push access to deploy.',
      }
    }
    return { cls: 'bad', badge: result.kind.replace(/_/g, ' '), note: result.reason }
  }
  const kindLabel = result.kind === 'ok_private' ? 'private' : 'public'
  if (result.can_push == null) {
    // anonymous request (no token) — push/PR were never probed, and without a token
    // the deploy push + auto-PR can't run. Clone-only: warn rather than green-OK.
    return {
      cls: 'warn',
      badge: `${kindLabel} · clone-only`,
      note: 'No token — clone works, but the deploy push and auto-PR will fail. Add a token to enable deploying.',
    }
  }
  if (result.can_create_pr === false) {
    return {
      cls: 'warn',
      badge: `${kindLabel} · push only`,
      note: 'Push access confirmed, but the token has no pull-request access — auto-create-PR will fail until this is fixed.',
    }
  }
  return { cls: 'ok', badge: `${kindLabel} · full access`, note: 'Token has both push and pull-request access.' }
}

function ValPill({ result }: { result?: RepoValidationResult | null }) {
  if (!result) return <span className="valpill"><span className="vdot" /> unvalidated</span>
  const { cls, badge } = accessState(result)
  return (
    <span className={`valpill ${cls}`}>
      <span className="vdot" /> {cls === 'bad' ? `✗ ${badge}` : badge}
    </span>
  )
}

function ValNote({ result }: { result: RepoValidationResult }) {
  const { cls, note } = accessState(result)
  return <div className={`valnote valnote-${cls}`}>{note}</div>
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
  const [sandboxImage, setSandboxImage] = useState(repo.sandbox_image ?? '')
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
        sandbox_image: sandboxImage,             // '' clears; re-validated server-side
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
        <span className="rfact"><span className="rk">sandbox</span>
          {repo.sandbox_image
            ? <span className="rv" title={repo.sandbox_image}>{shortImage(repo.sandbox_image)}</span>
            : <span className="rv quiet">default</span>}</span>
        <span className="rfact"><span className="rk">token</span>
          <span className={`rv ${repo.token ? '' : 'quiet'}`}>{repo.token ? 'per-repo' : 'global'}</span></span>
        {checked && (
          <span className="rfact"><span className="rk">checked</span>
            <span className="rv quiet">{checked}</span></span>
        )}
      </div>

      {result && <ValNote result={result} />}

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
          <label className="fld span2"><span>sandbox image <em>{SANDBOX_IMAGE_HINT}</em></span>
            <input placeholder="agentic-sandbox:0.1 (default)" value={sandboxImage}
              onChange={(e) => setSandboxImage(e.target.value)} />
            <ImageCheck image={sandboxImage} /></label>
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
  // Feedback for the just-completed "Check & add" — the repo is already in the list
  // below by the time this renders, but the traffic-light + note right here confirms
  // what was actually checked without having to go find the new card.
  const [justAdded, setJustAdded] = useState<RepoValidationResult | null>(null)

  useEffect(() => {
    api.listRepos().then(setRepos).catch((e) => setError(String(e)))
  }, [])

  const setRepoUrl = (repo_url: string) => { setForm({ ...form, repo_url }); setJustAdded(null) }

  const add = async () => {
    setAdding(true); setError(null); setJustAdded(null)
    try {
      const created = await api.createRepo(form)
      setRepos((r) => [...(r ?? []), created])
      setForm({ repo_url: '' })
      setJustAdded(created.last_validation_result ?? null)
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
                onChange={(e) => setRepoUrl(e.target.value)}
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
            <label className="fld span2"><span>sandbox image <em>{SANDBOX_IMAGE_HINT}</em></span>
              <input placeholder="agentic-sandbox:0.1 (default)" value={form.sandbox_image ?? ''}
                onChange={(e) => setForm({ ...form, sandbox_image: e.target.value || null })} />
              <ImageCheck image={form.sandbox_image ?? ''} /></label>
          </div>
          {error && <div className="error">{error}</div>}
          {justAdded && (
            <div className="addrepo-result">
              <ValPill result={justAdded} />
              <ValNote result={justAdded} />
            </div>
          )}
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
