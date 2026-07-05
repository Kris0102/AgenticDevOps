import { useEffect, useRef, useState } from 'react'
import type { LogView } from '../api'

// Live logs over SSE (GET /runs/{id}/logs/stream). The backend tags frames `event: log`,
// so we listen for that. `active` gates the connection: once the run is terminal the
// caller flips it false so the EventSource closes instead of auto-reconnecting onto a
// drained, already-closed server stream.
//
// Frames are DEDUPED by seq: the server replays its whole ring buffer to every new
// connection, and EventSource auto-reconnects (plus StrictMode double-mounts in dev) —
// without the guard, one error line shows up N times and looks like a real loop.
export function useLogStream(runId: string | null, active: boolean) {
  const [logs, setLogs] = useState<LogView[]>([])
  const lastSeq = useRef(0)

  useEffect(() => {
    if (!runId || !active) return
    setLogs([])
    lastSeq.current = 0
    const es = new EventSource(`/runs/${runId}/logs/stream`)
    es.addEventListener('log', (e) => {
      try {
        const line = JSON.parse((e as MessageEvent).data) as LogView
        if (line.seq <= lastSeq.current) return // ring-buffer replay after reconnect
        lastSeq.current = line.seq
        setLogs((prev) => [...prev, line])
      } catch {
        /* ignore malformed frame */
      }
    })
    // EventSource auto-reconnects on transient errors; dedupe above absorbs the replay.
    return () => es.close()
  }, [runId, active])

  return logs
}
