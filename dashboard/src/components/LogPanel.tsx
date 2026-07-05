import { useEffect, useRef } from 'react'
import { useLogStream } from '../hooks/useLogStream'

// Live structlog feed (SSE). Stick-to-bottom: the FEED scrolls (never the page — no
// scrollIntoView, it walks every scrollable ancestor and yanks the viewport away from
// whatever the user is reading), and only while the user is already at the bottom.
// Scroll up to read → autoscroll disengages; scroll back down → re-engages.
export function LogPanel({ runId, active }: { runId: string; active: boolean }) {
  const logs = useLogStream(runId, active)
  const feedRef = useRef<HTMLDivElement | null>(null)
  const stickRef = useRef(true)

  const onScroll = () => {
    const el = feedRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }

  useEffect(() => {
    const el = feedRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [logs.length])

  return (
    <div className="panel logs">
      <h3>live logs {active ? <span className="live-dot">●</span> : <span className="closed">(closed)</span>}</h3>
      <div className="logfeed" ref={feedRef} onScroll={onScroll}>
        {logs.map((l) => (
          <div key={l.seq} className={`logline lvl-${l.level ?? 'info'} cat-${l.category ?? 'none'}`}>
            <span className="lts">{l.ts?.slice(11, 19) ?? ''}</span>
            <span className="lcat">[{l.category ?? '-'}]</span>
            <span className="lev">{l.event}</span>
            {Object.keys(l.fields).length > 0 && (
              <span className="lfields">
                {Object.entries(l.fields)
                  .map(([k, v]) => `${k}=${String(v)}`)
                  .join(' ')}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
