import { useState } from 'react'
import { api } from './api'
import { useRunStatus } from './hooks/useRunStatus'
import { ArtifactsPanel } from './components/ArtifactsPanel'
import { LessonsPanel } from './components/LessonsPanel'
import { PipelineGraph } from './components/PipelineGraph'
import { NewRunPage } from './components/NewRunPage'
import { SettingsPage } from './components/SettingsPage'
import { RunOverview } from './components/RunOverview'
import { RunsHome } from './components/RunsHome'
import { StatusBar } from './components/StatusBar'
import { LogPanel } from './components/LogPanel'
import { HitlPanel } from './components/HitlPanel'
import { EventTimeline } from './components/EventTimeline'

type View = 'home' | 'newrun' | 'settings'

export default function App() {
  const [runId, setRunId] = useState<string | null>(null)
  const [view, setView] = useState<View>('home')
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

  const openRun = (id: string) => { setView('home'); setRunId(id) }

  // Full-page routes (no run open): New run / Settings.
  if (!runId && view === 'settings') return <div className="app"><SettingsPage onClose={() => setView('home')} /></div>
  if (!runId && view === 'newrun') return <div className="app"><NewRunPage onRun={openRun} onCancel={() => setView('home')} /></div>

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>◆</span>
          <span className="brand-name">AgenticDevOps</span>
          <span className="tagline">control plane</span>
        </div>
        {!runId && (
          <div className="topbar-actions">
            <button className="ghost gear" onClick={() => setView('settings')} title="Settings" aria-label="Settings">⚙</button>
            <button className="primary" onClick={() => setView('newrun')}>＋ New run</button>
          </div>
        )}
      </header>

      {error && <div className="error">poll error: {error}</div>}

      {/* Home: all previous runs as sortable boxes. Hidden while a run is open. */}
      {!runId && <RunsHome activeRunId={runId} onOpen={setRunId} />}

      {runId && (
        <>
          <button className="back" onClick={() => setRunId(null)}>← all runs</button>

          <StatusBar run={run} />

          <PipelineGraph runId={runId} run={run} live={!terminal} />

          {run?.run_state === 'stalled' && (
            <div className="continue-bar">
              <span>
                {run.stalled_reason
                  ? <>Stalled on a transient error — <code>{run.stalled_reason}</code>. Continue to retry from the last checkpoint.</>
                  : <>This run stopped mid-graph (its worker died). Resume from the last checkpoint.</>}
              </span>
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

          <RunOverview runId={runId} live={!terminal} />

          <div className="cols">
            {/* live log is in-memory only — for a finished run it's empty noise, so the
                durable history takes the full width instead */}
            {!terminal && <LogPanel runId={runId} active={!terminal} />}
            <EventTimeline runId={runId} stop={terminal} />
          </div>

          <ArtifactsPanel runId={runId} />
          <LessonsPanel runId={runId} />
        </>
      )}
    </div>
  )
}
