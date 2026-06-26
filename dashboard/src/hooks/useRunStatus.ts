import { useEffect, useState } from 'react'
import { api, type RunResponse } from '../api'

// Polls GET /runs/{id} — the single source of run_state + hitl_phase the dashboard
// reacts to. ~1.5s is plenty for a localhost single-user tool.
export function useRunStatus(runId: string | null, intervalMs = 1500) {
  const [run, setRun] = useState<RunResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!runId) {
      setRun(null)
      return
    }
    let stopped = false
    const tick = async () => {
      try {
        const r = await api.getRun(runId)
        if (!stopped) {
          setRun(r)
          setError(null)
        }
      } catch (e) {
        if (!stopped) setError(String(e))
      }
    }
    tick()
    const handle = setInterval(tick, intervalMs)
    return () => {
      stopped = true
      clearInterval(handle)
    }
  }, [runId, intervalMs])

  return { run, error }
}
