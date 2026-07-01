import { useState } from 'react'
import { api } from './api'
import { useRunStatus } from './hooks/useRunStatus'
import { RunControls } from './components/RunControls'
import { StatusBar } from './components/StatusBar'
import { LogPanel } from './components/LogPanel'
import { HitlPanel } from './components/HitlPanel'
import { EventTimeline } from './components/EventTimeline'

export default function App() {
  const [runId, setRunId] = useState<string | null>(null)
  const [continuing, setContinuing] = useState(false)
  const { run, error } = useRunStatus(runId)
  const terminal = run?.run_state === 'completed' || run?.run_state === 'failed'

  const onContinue = async () => {
    if (!runId) return
    setContinuing(true)
    try {
      await api.continueRun(runId)   // poll picks up the transition to running/paused
    } catch (e) {
      alert(String(e))
    } finally {
      setContinuing(false)
    }
  }

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

          {run?.run_state === 'stalled' && (
            <div className="continue-bar">
              <span>This run stopped mid-graph (its worker died). Resume from the last checkpoint.</span>
              <button className="continue" disabled={continuing} onClick={onContinue}>
                {continuing ? 'continuing…' : 'Continue run'}
              </button>
            </div>
          )}

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
