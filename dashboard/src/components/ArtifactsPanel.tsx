import { useEffect, useMemo, useState } from 'react'
import { Diff, Hunk, parseDiff, type FileData } from 'react-diff-view'
import { api, type ArtifactContent, type ArtifactView, type MicrotaskRow } from '../api'

// Durable artifact browser: everything the agents produced, fetched from the artifacts
// table — the deep-dive record for old runs. Artifacts are GROUPED under the microtask
// that produced them (run-level plan/review artifacts first), each with a human
// description of what it is — so a "diff" reads as "code changes for microtask 2".
// Bodies load on demand; `diff` artifacts render through the themed react-diff-view.

const KIND_INFO: Record<string, { icon: string; label: string; desc: string }> = {
  plan: { icon: '📋', label: 'plan', desc: "the planner's summary of this plan draft" },
  review: { icon: '🔍', label: 'plan review', desc: "the reviewer's verdict + notes on the plan" },
  diff: { icon: '±', label: 'code changes', desc: 'the diff the coder produced for this microtask' },
  reflection: { icon: '🩺', label: 'failure diagnosis', desc: "the reflector's root-cause analysis after a failed check" },
}

function kindInfo(kind: string): { icon: string; label: string; desc: string } {
  if (KIND_INFO[kind]) return KIND_INFO[kind]
  if (kind.startsWith('test-'))
    return { icon: '🧪', label: kind.slice(5).replace(/_/g, ' '), desc: 'check output the tester recorded for this microtask' }
  return { icon: '▪', label: kind, desc: '' }
}

function DiffBody({ text }: { text: string }) {
  let files: FileData[] = []
  try {
    files = parseDiff(text)
  } catch {
    files = []
  }
  if (files.length === 0) return <pre className="abody">{text}</pre>
  return (
    <div className="diffreview">
      {files.map((file, i) => (
        <div key={i} className="difffile">
          <div className="fhead">
            {file.oldPath === file.newPath ? file.newPath : `${file.oldPath} → ${file.newPath}`}
          </div>
          <Diff viewType="unified" diffType={file.type} hunks={file.hunks}>
            {(hunks) => hunks.map((hunk) => <Hunk key={hunk.content} hunk={hunk} />)}
          </Diff>
        </div>
      ))}
    </div>
  )
}

interface Group {
  key: string
  title: string    // section heading, e.g. "microtask 2 — Add findCity function"
  items: ArtifactView[]
}

export function ArtifactsPanel({ runId }: { runId: string }) {
  const [items, setItems] = useState<ArtifactView[]>([])
  const [tasks, setTasks] = useState<MicrotaskRow[]>([])
  const [open, setOpen] = useState<string | null>(null)
  const [bodies, setBodies] = useState<Record<string, ArtifactContent>>({})
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setItems([]); setTasks([]); setOpen(null); setBodies({})
    api.getArtifacts(runId).then(setItems).catch((e) => setError(String(e)))
    api.getMicrotasks(runId).then(setTasks).catch(() => {})
  }, [runId])

  // Run-level artifacts (plan, review) first, then one section per microtask in board order.
  const groups = useMemo<Group[]>(() => {
    const byTask = new Map<string, ArtifactView[]>()
    const runLevel: ArtifactView[] = []
    for (const a of items) {
      if (a.microtask_id) {
        const arr = byTask.get(a.microtask_id) ?? []
        arr.push(a)
        byTask.set(a.microtask_id, arr)
      } else runLevel.push(a)
    }
    const out: Group[] = []
    if (runLevel.length) out.push({ key: '_run', title: 'planning', items: runLevel })
    for (const t of tasks) {
      const arr = byTask.get(t.microtask_id)
      if (arr) {
        out.push({ key: t.microtask_id, title: `microtask ${t.idx + 1} — ${t.title}`, items: arr })
        byTask.delete(t.microtask_id)
      }
    }
    // artifacts pointing at microtasks no longer on the board (shouldn't happen, but never hide data)
    for (const [id, arr] of byTask) out.push({ key: id, title: id, items: arr })
    return out
  }, [items, tasks])

  const toggle = async (id: string) => {
    if (open === id) return setOpen(null)
    setOpen(id)
    if (!bodies[id]) {
      try {
        const content = await api.getArtifact(runId, id)
        setBodies((prev) => ({ ...prev, [id]: content }))
      } catch (e) {
        setError(String(e))
      }
    }
  }

  return (
    <div className="panel artifacts">
      <h3>artifacts <span className="rh-count">{items.length}</span></h3>
      {error && <div className="error">{error}</div>}
      {items.length === 0 && !error && <p className="empty-hint">nothing produced yet</p>}
      {groups.map((g) => (
        <div key={g.key} className="agroup">
          <div className="agroup-title">{g.title}</div>
          <div className="alist">
            {g.items.map((a) => {
              const info = kindInfo(a.kind)
              return (
                <div key={a.artifact_id} className="aitem">
                  <button className={`ahead ${open === a.artifact_id ? 'open' : ''}`} onClick={() => toggle(a.artifact_id)} title={info.desc}>
                    <span className="akind">{info.icon} {info.label}</span>
                    <span className="apreview">{a.preview}</span>
                    <span className="asize">{a.size > 1024 ? `${(a.size / 1024).toFixed(1)}kB` : `${a.size}B`}</span>
                    <span className="ats">{a.created_at.slice(11, 19)}</span>
                  </button>
                  {open === a.artifact_id && (
                    <>
                      {info.desc && <div className="adesc">{info.desc}</div>}
                      {bodies[a.artifact_id]
                        ? a.kind === 'diff'
                          ? <DiffBody text={bodies[a.artifact_id].content_text} />
                          : <pre className="abody">{bodies[a.artifact_id].content_text}</pre>
                        : <div className="loading">loading…</div>}
                    </>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}
