import { useEffect, useMemo, useState } from 'react'
import { api, type LessonCreateRequest, type LessonView, type RepoView } from '../api'
import { audience, isHumanReviewed, origin, originLabel, whyText } from '../lessonInfo'

// The lesson store (ADR 0034): everything a repo's cross-run memory holds, and the only
// place a human can correct it. Two kinds of row, deliberately separated:
//   - operator rules  — typed here, ALWAYS injected, framed to the agents as instructions
//   - learned lessons — written by the reviewer/reflector on confirmation (ADR 0018),
//                       retrieved semantically top-k, framed as advisory and possibly stale
// Every card explains WHY it exists; that narrative is derived (lessonInfo.ts), not stored.

const SCOPES = ['code', 'plan'] as const

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function LessonCard({ lesson, onOpenRun, onChanged }: {
  lesson: LessonView
  onOpenRun: (runId: string) => void
  onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({
    summary: lesson.summary, guidance: lesson.guidance ?? '', failure_type: lesson.failure_type ?? '',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const kind = origin(lesson)

  const save = async () => {
    setBusy(true); setError(null)
    try {
      await api.updateLesson(lesson.lesson_id, {
        summary: draft.summary, guidance: draft.guidance, failure_type: draft.failure_type,
      })
      setEditing(false)
      onChanged()
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  const remove = async () => {
    if (!confirm('Delete this lesson? It stops being sent to the agents immediately.')) return
    setBusy(true); setError(null)
    try {
      await api.deleteLesson(lesson.lesson_id)
      onChanged()
    } catch (e) { setError(String(e)); setBusy(false) }
  }

  return (
    <div className={`lscard kind-${kind}`}>
      <div className="lscard-head">
        <span className={`lspill scope-${lesson.scope}`}>{lesson.scope}</span>
        <span className="lsorigin">{originLabel(lesson)}</span>
        {lesson.run_id && (
          <button className="lslink" onClick={() => onOpenRun(lesson.run_id!)}
                  title="open the run that learned this">
            run {lesson.run_name || lesson.run_id.slice(0, 8)} ↗
          </button>
        )}
        {lesson.microtask_id && <span className="lsmt">{lesson.microtask_id}</span>}
        <span className="lsspacer" />
        <span className="lsdate">{fmtDate(lesson.created_at)}</span>
      </div>

      <p className="lswhy"><span className="lswhy-k">why</span>{whyText(lesson)}</p>

      {editing ? (
        <div className="lsedit">
          <label className="fld"><span>lesson</span>
            <textarea rows={2} value={draft.summary}
                      onChange={(e) => setDraft({ ...draft, summary: e.target.value })} />
          </label>
          <label className="fld"><span>how to apply (optional)</span>
            <textarea rows={2} value={draft.guidance}
                      onChange={(e) => setDraft({ ...draft, guidance: e.target.value })} />
          </label>
          <label className="fld"><span>tag (optional)</span>
            <input value={draft.failure_type}
                   onChange={(e) => setDraft({ ...draft, failure_type: e.target.value })} />
          </label>
        </div>
      ) : (
        <>
          <p className="lssummary">{lesson.summary}</p>
          {lesson.guidance && (
            <p className="lsguide"><span className="lsguide-k">{kind === 'human' ? 'how' : 'fix'}</span>{lesson.guidance}</p>
          )}
        </>
      )}

      <div className="lscard-foot">
        {lesson.failure_type && <span className="lstype">{lesson.failure_type}</span>}
        {isHumanReviewed(lesson) && (
          <span className="lsedited" title="a human reviewed this — it survives a failed run's retraction">
            edited by human
          </span>
        )}
        <span className="lsspacer" />
        {error && <span className="lserr">{error}</span>}
        {editing ? (
          <>
            <button className="ghost sm" disabled={busy} onClick={() => { setEditing(false); setError(null) }}>cancel</button>
            <button className="primary sm" disabled={busy || !draft.summary.trim()} onClick={save}>save</button>
          </>
        ) : (
          <>
            <button className="ghost sm" disabled={busy} onClick={() => setEditing(true)}>edit</button>
            <button className="ghost sm danger" disabled={busy} onClick={remove}>delete</button>
          </>
        )}
      </div>
    </div>
  )
}

export function LessonStorePage({ onClose, onOpenRun }: {
  onClose: () => void
  onOpenRun: (runId: string) => void
}) {
  const [repos, setRepos] = useState<RepoView[] | null>(null)
  const [repoId, setRepoId] = useState<string>('')
  const [lessons, setLessons] = useState<LessonView[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState<LessonCreateRequest>({ scope: 'code', summary: '', guidance: '' })
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    api.listRepos()
      .then((rs) => { setRepos(rs); if (rs.length && !repoId) setRepoId(rs[0].repo_id) })
      .catch((e) => setError(String(e)))
    // repoId intentionally omitted — this only seeds the initial selection
  }, [])

  const load = () => {
    if (!repoId) return
    api.getRepoLessons(repoId).then(setLessons).catch((e) => setError(String(e)))
  }
  useEffect(() => { setLessons(null); load() }, [repoId])

  const add = async () => {
    setAdding(true); setError(null)
    try {
      await api.createLesson(repoId, form)
      setForm({ scope: form.scope, summary: '', guidance: '' })
      load()
    } catch (e) { setError(String(e)) } finally { setAdding(false) }
  }

  // Rules first: they are instructions and always apply, so they read as the top of the
  // page rather than as one more entry in a list of maybe-stale notes.
  const [rules, learned] = useMemo(() => {
    const ls = lessons ?? []
    return [ls.filter((l) => origin(l) === 'human'), ls.filter((l) => origin(l) !== 'human')]
  }, [lessons])

  return (
    <section className="settingspage lessonstore">
      <header className="sp-head">
        <div className="sp-title">
          <button className="ghost sp-back" onClick={onClose}>← back</button>
          <div>
            <h2>Lesson store</h2>
            <p className="sp-sub">
              What the system knows about a repo. Agents write here when a failure diagnosis is
              confirmed; you can correct them or add rules of your own.
            </p>
          </div>
        </div>
        {repos !== null && repos.length > 0 && (
          <label className="fld lsrepo"><span>repository</span>
            <select value={repoId} onChange={(e) => setRepoId(e.target.value)}>
              {repos.map((r) => <option key={r.repo_id} value={r.repo_id}>{r.label}</option>)}
            </select>
          </label>
        )}
      </header>

      {error && <p className="sp-err">{error}</p>}
      {repos !== null && repos.length === 0 && (
        <p className="empty-hint">no repositories registered yet — add one under Repositories first.</p>
      )}

      {repoId && (
        <>
          <div className="sp-section">
            <div className="sp-section-head">
              <h3>Add an operator rule</h3>
              <p>
                A standing instruction, sent to the agents on every run of this repo and framed
                as a rule to follow — unlike learned lessons, it is never ranked away and never
                expires.
              </p>
            </div>
            <div className="provcard val-none">
              <div className="np-grid">
                <label className="fld"><span>applies to</span>
                  <select value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}>
                    {SCOPES.map((s) => <option key={s} value={s}>{s} — {audience(s)}</option>)}
                  </select>
                </label>
                <label className="fld span2"><span>rule</span>
                  <input placeholder="never use axios; this repo uses fetch"
                         value={form.summary}
                         onChange={(e) => setForm({ ...form, summary: e.target.value })} />
                </label>
                <label className="fld span2"><span>how to apply (optional)</span>
                  <input placeholder="call fetch() directly; do not add an HTTP dependency"
                         value={form.guidance ?? ''}
                         onChange={(e) => setForm({ ...form, guidance: e.target.value })} />
                </label>
              </div>
              <div className="np-actions">
                <button className="primary" disabled={adding || !form.summary.trim()} onClick={add}>
                  {adding ? 'adding…' : 'Add rule'}
                </button>
              </div>
            </div>
          </div>

          <div className="sp-section">
            <div className="sp-section-head">
              <h3>Operator rules <span className="rh-count">{rules.length}</span></h3>
              <p>Yours. Always sent to the agents.</p>
            </div>
            {rules.length === 0
              ? <p className="empty-hint">none yet — add one above and the next run will follow it.</p>
              : rules.map((l) => (
                  <LessonCard key={l.lesson_id} lesson={l} onOpenRun={onOpenRun} onChanged={load} />
                ))}
          </div>

          <div className="sp-section">
            <div className="sp-section-head">
              <h3>Learned lessons <span className="rh-count">{learned.length}</span></h3>
              <p>
                Written by the agents themselves, only once the diagnosis was confirmed by the
                outcome. Retrieved by similarity, so a run sees the few that match its task.
              </p>
            </div>
            {lessons === null && <p className="empty-hint">loading…</p>}
            {lessons !== null && learned.length === 0 && (
              <p className="empty-hint">
                nothing learned for this repo yet — lessons appear when a reviewer approves a
                revised plan, or a failed microtask finally passes.
              </p>
            )}
            {learned.map((l) => (
              <LessonCard key={l.lesson_id} lesson={l} onOpenRun={onOpenRun} onChanged={load} />
            ))}
          </div>
        </>
      )}
    </section>
  )
}
