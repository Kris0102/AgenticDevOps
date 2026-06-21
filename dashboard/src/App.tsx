import { useState } from 'react'
import { useRunStatus } from './hooks/useRunStatus'
import { RunControls } from './components/RunControls'
import { StatusBar } from './components/StatusBar'
import { LogPanel } from './components/LogPanel'
import { HitlPanel } from './components/HitlPanel'
import { EventTimeline } from './components/EventTimeline'

export default function App() {
  const [runId, setRunId] = useState<string | null>(null)
  const { run, error } = useRunStatus(runId)
  const terminal = run?.run_state === 'completed' || run?.run_state === 'failed'

  return (
    <div className="app">
      <header>
        <h1>AgenticDevOps</h1>
        <span className="tagline">control dashboard</span>
      </header>

      <RunControls onRun={setRunId} />

      {error && <div className="error">poll error: {error}</div>}

      {runId && (
        <>
          <StatusBar run={run} />

          {run?.run_state === 'paused_hitl' && (
            // remount per gate so each pause fetches fresh review context
            <HitlPanel
              key={run.hitl_approval_id ?? run.hitl_phase ?? 'gate'}
              runId={runId}
              phase={run.hitl_phase ?? ''}
            />
          )}

          <div className="cols">
            <LogPanel runId={runId} active={!terminal} />
            <EventTimeline runId={runId} stop={terminal} />
          </div>
        </>
      )}
    </div>
  )
}
