import { useEffect, useRef, useState } from 'react'

// Live sandbox-image check with pull progress (ADR 0039).
//
// Why this exists: saving a repo validates its sandbox image, and validating means pulling it
// first. A multi-GB image takes minutes, during which the save request just blocks — visually
// identical to a hang. This runs the same check up-front over SSE so the user can watch it,
// and by the time they save, the image is local and the save returns immediately.
//
// It is an affordance, not a gate: skipping it is fine, the save still validates.

export interface PreflightFrame {
  phase: 'resolving' | 'pulling' | 'checking' | 'result'
  detail: string
  percent: number | null
  done: boolean
  ok?: boolean
  message?: string
  problems?: string[]
  pulled?: boolean
}

const PHASE_LABEL: Record<PreflightFrame['phase'], string> = {
  resolving: 'looking for image',
  pulling: 'downloading',
  checking: 'checking image',
  result: 'done',
}

// During `pulling` the phase alone is too coarse: download reaches 100% and then Docker spends
// another 10s+ decompressing, so a fixed "downloading" label sits at 100% and reads as stuck.
// The server puts its current verb first in `detail` ("extracting python:3.12 (409 MB)") —
// prefer that, and fall back to the phase label for every other phase.
function statusLabel(frame: PreflightFrame): string {
  if (frame.phase === 'pulling') {
    const verb = frame.detail.split(' ')[0]
    if (verb) return verb
  }
  return PHASE_LABEL[frame.phase]
}

export function ImageCheck({ image }: { image: string }) {
  const [frame, setFrame] = useState<PreflightFrame | null>(null)
  const [running, setRunning] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  // Close the stream if the component unmounts mid-pull (panel collapsed, page left).
  // The pull itself continues in Docker, which is what we want — the layers stay cached.
  useEffect(() => () => esRef.current?.close(), [])

  // A stale result under a since-edited image name would be actively misleading.
  useEffect(() => { setFrame(null) }, [image])

  const start = () => {
    const target = image.trim()
    if (!target || running) return
    esRef.current?.close()
    setFrame(null)
    setRunning(true)
    const es = new EventSource(`/images/preflight/stream?image=${encodeURIComponent(target)}`)
    esRef.current = es
    es.onmessage = (e) => {
      try {
        const next = JSON.parse(e.data) as PreflightFrame
        setFrame(next)
        if (next.done) { es.close(); setRunning(false) }
      } catch { /* ignore malformed frame */ }
    }
    // EventSource auto-reconnects, which would silently restart the whole check. The stream
    // is one-shot, so treat any error as terminal and say so rather than looping.
    es.onerror = () => {
      es.close()
      setRunning(false)
      setFrame((prev) => prev?.done ? prev : {
        phase: 'result', detail: 'connection lost', percent: null, done: true, ok: false,
        message: 'lost connection to the server while checking — is the API still running?',
      })
    }
  }

  const pct = frame?.percent
  const bar = running || frame?.done
  const cls = frame?.done ? (frame.ok ? 'imgchk ok' : 'imgchk bad') : 'imgchk'

  return (
    <div className={cls}>
      <div className="imgchk-head">
        <button type="button" className="ghost sm" disabled={!image.trim() || running}
          onClick={start}>
          {running ? 'checking…' : 'Check image'}
        </button>
        {bar && (
          <span className="imgchk-status">
            {frame?.done
              ? (frame.ok ? '✓ usable as a sandbox' : '✗ not usable')
              : `${frame ? statusLabel(frame) : 'starting'}${pct != null ? ` · ${pct}%` : '…'}`}
          </span>
        )}
      </div>

      {/* Indeterminate until Docker reports layer totals — a bar pinned at 0 reads as stuck. */}
      {running && (
        <div className="imgchk-bar">
          <div className={`imgchk-fill${pct == null ? ' indet' : ''}`}
            style={pct == null ? undefined : { width: `${pct}%` }} />
        </div>
      )}

      {running && frame && <div className="imgchk-detail">{frame.detail}</div>}

      {frame?.done && !frame.ok && (
        <div className="imgchk-problems">
          {frame.problems && frame.problems.length > 0
            ? <ul>{frame.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>
            : <div>{frame.message}</div>}
        </div>
      )}
    </div>
  )
}
