import { useEffect, useState } from 'react'
import type { LogView } from '../api'

// Live logs over SSE (GET /runs/{id}/logs/stream). The backend tags frames `event: log`,
// so we listen for that. `active` gates the connection: once the run is terminal the
// caller flips it false so the EventSource closes instead of auto-reconnecting onto a
// drained, already-closed server stream.
export function useLogStream(runId: string | null, active: boolean) {
  const [logs, setLogs] = useState<LogView[]>([])

  useEffect(() => {
    if (!runId || !active) return
    setLogs([])
    const es = new EventSource(`/runs/${runId}/logs/stream`)
    es.addEventListener('log', (e) => {
      try {
        setLogs((prev) => [...prev, JSON.parse((e as MessageEvent).data) as LogView])
      } catch {
        /* ignore malformed frame */
      }
    })
    // EventSource auto-reconnects on transient errors; nothing to do here.
    return () => es.close()
  }, [runId, active])

  return logs
}
