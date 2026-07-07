import { useState } from 'react'
import { api } from './api'
import { useRunStatus } from './hooks/useRunStatus'
import { ArtifactsPanel } from './components/ArtifactsPanel'
import { LessonsPanel } from './components/LessonsPanel'
import { PipelineGraph } from './components/PipelineGraph'
import { NewRunPage } from './components/NewRunPage'
import { ReposPage } from './components/ReposPage'
import { SettingsPage } from './components/SettingsPage'
import { RunOverview } from './components/RunOverview'
import { RunsHome } from './components/RunsHome'
import { StatusBar } from './components/StatusBar'
import { LogPanel } from './components/LogPanel'
import { HitlPanel } from './components/HitlPanel'
import { EventTimeline } from './components/EventTimeline'

type View = 'home' | 'newrun' | 'settings' | 'repos'
type Tab = 'activity' | 'artifacts' | 'lessons'

export default function App() {
  const [runId, setRunId] = useState<string | null>(null)
  const [view, setView] = useState<View>('home')
  const [tab, setTab] = useState<Tab>('activity')
  const [continuing, setContinuing] = useState(false)
  const [newCap, setNewCap] = useState('')
  const { run, error } = useRunStatus(runId)
  const terminal = run?.run_state === 'completed' || run?.run_state === 'failed'

  const onContinue = async () => {
    if (!runId) return
    setContinuing(true)
    try {
      // blank = today's plain retry (no override); a value raises the checkpointed cap
      // before resuming (ADR 0026) — only meaningful for a token-budget stall.
      const cap = newCap.trim() === '' ? undefined : parseInt(newCap, 10)
      await api.continueRun(runId, cap)   // poll picks up the transition to running/paused
      setNewCap('')
    } catch (e) {
      alert(String(e))
    } finally {
      setContinuing(false)
    }
  }

  // Human closes the case on a stalled run — the deliberate counterpart to Continue.
  const onGiveUp = async () => {
    if (!runId) return
    if (!window.confirm('Mark this run as failed? It can no longer be continued.')) return
    const note = window.prompt('Optional note (why is it hopeless?)') ?? undefined
    try {
      await api.failRun(runId, note || undefined)   // poll flips to the failed banner
    } catch (e) {
      alert(String(e))
    }
  }

  const openRun = (id: string) => { setView('home'); setTab('activity'); setRunId(id) }

  // Full-page routes (no run open): New run / Settings / Repos.
  if (!runId && view === 'settings') return <div className="app"><SettingsPage onClose={() => setView('home')} /></div>
  if (!runId && view === 'repos') return <div className="app"><ReposPage onClose={() => setView('home')} /></div>
  if (!runId && view === 'newrun') return <div className="app"><NewRunPage onRun={openRun} onCancel={() => setView('home')} onManageRepos={() => setView('repos')} /></div>

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
            <button className="ghost" onClick={() => setView('repos')} title="Repositories">Repos</button>
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

          {run?.run_state === 'failed' && (
            <div className="failbanner">
              <span className="fb-badge">run failed</span>
              <span className="fb-reason">{run.failure_reason ?? 'no failure reason recorded'}</span>
            </div>
          )}

          {run?.run_state === 'stalled' && (
            <div className="continue-bar">
              <div className="cb-head">
                <span className="cb-badge">run stalled</span>
                <span className="cb-hint">
                  {run.stalled_reason
                    ? 'stopped on the error below — recoverable from the last checkpoint'
                    : 'stopped mid-graph (its worker died) — recoverable from the last checkpoint'}
                </span>
              </div>
              {run.stalled_reason && <div className="cb-reason">{run.stalled_reason}</div>}
              {run.stalled_kind === 'token_budget' && (
                <label className="cb-cap">
                  new token cap
                  <input
                    type="number" min={1} placeholder="leave blank to retry unchanged"
                    value={newCap} onChange={(e) => setNewCap(e.target.value)}
                  />
                </label>
              )}
              <div className="cb-actions">
                <button className="continue" disabled={continuing} onClick={onContinue}>
                  {continuing ? 'continuing…' : newCap.trim() !== '' ? 'Raise cap & continue' : 'Continue run'}
                </button>
                <button className="giveup" disabled={continuing} onClick={onGiveUp} title="close the case — the run becomes terminal FAILED and can no longer be continued">
                  Mark as failed
                </button>
              </div>
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

          {/* ONE content area below the overview — tabs instead of four stacked panels
              each with its own scrollbar. Live feeds keep an internal scroll (they must
              stick to bottom without moving the page); everything else page-scrolls. */}
          <nav className="tabbar" role="tablist">
            {(['activity', 'artifacts', 'lessons'] as Tab[]).map((t) => (
              <button key={t} role="tab" aria-selected={tab === t}
                className={`tabbtn ${tab === t ? 'on' : ''}`} onClick={() => setTab(t)}>
                {t}
              </button>
            ))}
          </nav>

          {tab === 'activity' && (
            <div className="cols">
              {/* live log is in-memory only — for a finished run it's empty noise, so the
                  durable history takes the full width instead */}
              {!terminal && <LogPanel runId={runId} active={!terminal} />}
              <EventTimeline runId={runId} stop={terminal} />
            </div>
          )}
          {tab === 'artifacts' && <ArtifactsPanel runId={runId} />}
          {tab === 'lessons' && <LessonsPanel runId={runId} />}
        </>
      )}
    </div>
  )
}
