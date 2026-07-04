import { useEffect, useState } from 'react'
import { Diff, Hunk, parseDiff, type FileData } from 'react-diff-view'
import { api, type ArtifactContent, type ArtifactView } from '../api'

// Durable artifact browser: everything the agents produced (plan, reviews, diffs, test
// output, diagnoses) fetched from the artifacts table — the deep-dive record for old runs.
// Bodies load on demand; `diff` artifacts render through the themed react-diff-view.

const KIND_LABEL: Record<string, string> = {
  plan: '📋 plan',
  review: '🔍 review',
  diff: '± diff',
  reflection: '🩺 diagnosis',
}

function kindLabel(kind: string): string {
  if (KIND_LABEL[kind]) return KIND_LABEL[kind]
  if (kind.startsWith('test-')) return `🧪 ${kind.slice(5).replace(/_/g, ' ')}`
  return kind
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

export function ArtifactsPanel({ runId }: { runId: string }) {
  const [items, setItems] = useState<ArtifactView[]>([])
  const [open, setOpen] = useState<string | null>(null)
  const [bodies, setBodies] = useState<Record<string, ArtifactContent>>({})
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setItems([]); setOpen(null); setBodies({})
    api.getArtifacts(runId).then(setItems).catch((e) => setError(String(e)))
  }, [runId])

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

  if (items.length === 0 && !error) return null

  return (
    <div className="panel artifacts">
      <h3>artifacts <span className="rh-count">{items.length}</span></h3>
      {error && <div className="error">{error}</div>}
      <div className="alist">
        {items.map((a) => (
          <div key={a.artifact_id} className="aitem">
            <button className={`ahead ${open === a.artifact_id ? 'open' : ''}`} onClick={() => toggle(a.artifact_id)}>
              <span className="akind">{kindLabel(a.kind)}</span>
              <span className="apreview">{a.preview}</span>
              <span className="asize">{a.size > 1024 ? `${(a.size / 1024).toFixed(1)}kB` : `${a.size}B`}</span>
              <span className="ats">{a.created_at.slice(11, 19)}</span>
            </button>
            {open === a.artifact_id && (
              bodies[a.artifact_id]
                ? a.kind === 'diff'
                  ? <DiffBody text={bodies[a.artifact_id].content_text} />
                  : <pre className="abody">{bodies[a.artifact_id].content_text}</pre>
                : <div className="loading">loading…</div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
