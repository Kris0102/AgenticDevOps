import { useEffect, useState } from 'react'
import { api, type RunResponse } from '../api'


export function StatusBar({ run }: { run: RunResponse | null }) {
  const [nowDoing, setNowDoing] = useState('')
  const running = run?.run_state === 'running'
  const runId = run?.run_id

  // "now doing": the newest live-log line inline, so the current activity is visible
  // without scanning the log panel. Live runs only — the bus is ephemeral.
  useEffect(() => {
    setNowDoing('')
    if (!running || !runId) return
    let alive = true
    const tick = () =>
      api.getLogs(runId).then((logs) => {
        if (!alive || logs.length === 0) return
        const last = logs[logs.length - 1]
        setNowDoing(`${last.category ? `[${last.category}] ` : ''}${last.event}`)
      }).catch(() => {})
    tick()
    const t = setInterval(tick, 2000)
    return () => { alive = false; clearInterval(t) }
  }, [runId, running])

  if (!run) return <div className="statusbar">loading…</div>
  return (
    <div className={`statusbar state-${run.run_state}`}>
      <span className="badge">{run.run_state.replace('_', ' ')}</span>
      <code className="rid">{run.run_id}</code>
      {run.status && <span className="meta">status: {run.status}</span>}
      {run.run_state === 'paused_hitl' && <span className="gate">⏸ gate: {run.hitl_phase}</span>}
      {run.run_state === 'stalled' && <span className="gate">⚠ worker stopped — continue to resume</span>}
      {running && nowDoing && <span className="nowdoing" title="latest live-log event">▸ {nowDoing}</span>}
    </div>
  )
}
