import { useEffect, useRef } from 'react'
import { useLogStream } from '../hooks/useLogStream'

// Live structlog feed (SSE). Auto-scrolls to the newest line.
export function LogPanel({ runId, active }: { runId: string; active: boolean }) {
  const logs = useLogStream(runId, active)
  const endRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [logs.length])

  return (
    <div className="panel logs">
      <h3>live logs {active ? <span className="live-dot">●</span> : <span className="closed">(closed)</span>}</h3>
      <div className="logfeed">
        {logs.map((l) => (
          <div key={l.seq} className={`logline lvl-${l.level ?? 'info'}`}>
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
        <div ref={endRef} />
      </div>
    </div>
  )
}
