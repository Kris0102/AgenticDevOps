import type { RunResponse } from '../api'

export function StatusBar({ run }: { run: RunResponse | null }) {
  if (!run) return <div className="statusbar">loading…</div>
  return (
    <div className={`statusbar state-${run.run_state}`}>
      <span className="badge">{run.run_state.replace('_', ' ')}</span>
      <code className="rid">{run.run_id}</code>
      {run.status && <span className="meta">status: {run.status}</span>}
      {run.run_state === 'paused_hitl' && <span className="gate">⏸ gate: {run.hitl_phase}</span>}
    </div>
  )
}
