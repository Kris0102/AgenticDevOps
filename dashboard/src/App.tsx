import { useState } from 'react'
import { api } from './api'
import { useRunStatus } from './hooks/useRunStatus'
import { ArtifactsPanel } from './components/ArtifactsPanel'
import { MicrotasksPanel } from './components/MicrotasksPanel'
import { LessonsPanel } from './components/LessonsPanel'
import { PipelineGraph } from './components/PipelineGraph'
import { TokensByAgent } from './components/TokensByAgent'
import { CostBreakdown } from './components/CostBreakdown'
import { NewRunPage } from './components/NewRunPage'
import { ReposPage } from './components/ReposPage'
import { SettingsPage } from './components/SettingsPage'
import { RunOverview } from './components/RunOverview'
import { RunSettingsPanel } from './components/RunSettingsPanel'
import { ResizableCols } from './components/ResizableCols'
import { RunsHome } from './components/RunsHome'
import { StatusBar } from './components/StatusBar'
import { LogPanel } from './components/LogPanel'
import { HitlPanel } from './components/HitlPanel'
import { EventTimeline } from './components/EventTimeline'

type View = 'home' | 'newrun' | 'settings' | 'repos'

// Grafana runs as its own page (compose service `grafana`, :3000). We link out to it
// rather than embedding — override the host with VITE_GRAFANA_URL if the port moved.
const GRAFANA_URL =
  (import.meta.env.VITE_GRAFANA_URL as string | undefined)?.replace(/\/$/, '') ?? 'http://localhost:3000'
const GRAFANA_DASHBOARD = `${GRAFANA_URL}/d/agenticdevops-overview`
type Tab = 'activity' | 'artifacts' | 'microtasks' | 'lessons'

export default function App() {
  const [runId, setRunId] = useState<string | null>(null)
  const [view, setView] = useState<View>('home')
  const [tab, setTab] = useState<Tab>('activity')
  const [continuing, setContinuing] = useState(false)
  const [stopping, setStopping] = useState(false)
  // Entered in thousands, same convention as the Settings "max tokens per run" field —
  // "200" here means "+200,000 tokens". This ADDS to the current cap, it does not replace
  // it (ADR 0025 amendment): a run capped at 200k that stalls and gets "200" entered again
  // ends up with a 400k cap.
  const [addTokensK, setAddTokensK] = useState('')
  const [capVersion, setCapVersion] = useState(0)
  const { run, error } = useRunStatus(runId)
  const terminal = run?.run_state === 'completed' || run?.run_state === 'failed' || run?.run_state === 'stopped'
  // A token-budget stall can't usefully be retried unchanged — it just re-trips the same
  // check and re-stalls — so an amount to add is mandatory for this stall kind (not for a
  // plain transient/crash stall, where blank = retry unchanged).
  const capRequired = run?.stalled_kind === 'token_budget'
  const addTokensKNum = Number(addTokensK.trim())
  const addTokensValid = addTokensK.trim() !== '' && Number.isFinite(addTokensKNum) && addTokensKNum > 0
  const capValid = !capRequired || addTokensValid

  const onContinue = async () => {
    if (!runId || !capValid) return
    setContinuing(true)
    try {
      // blank = today's plain retry (no override); a value ADDS to the checkpointed cap
      // before resuming (ADR 0025 amendment) — required for a token-budget stall.
      const additionalTokens = addTokensK.trim() === '' ? undefined : Math.round(addTokensKNum * 1000)
      await api.continueRun(runId, additionalTokens)   // poll picks up the transition to running/paused
      if (additionalTokens != null) setCapVersion((v) => v + 1)   // let RunOverview refetch its cap immediately
      setAddTokensK('')
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

  // Hard-stop a live run (ADR 0033). Blocks server-side until the driver thread has
  // actually exited, so this call can take a moment — the button shows a busy state.
  const onStop = async () => {
    if (!runId) return
    if (!window.confirm('Stop this run? It will end early and cannot be resumed.')) return
    const note = window.prompt('Optional note (why stop it?)') ?? undefined
    setStopping(true)
    try {
      await api.stopRun(runId, note || undefined)   // poll flips to the gray "stopped" banner
    } catch (e) {
      alert(String(e))
    } finally {
      setStopping(false)
    }
  }

  // Permanently remove a terminal run and its data — no undo, so confirm plainly.
  const onDelete = async () => {
    if (!runId) return
    if (!window.confirm('Delete this run permanently? Its history cannot be recovered.')) return
    try {
      await api.deleteRun(runId)
      setRunId(null)   // nothing left to poll — back to the run list
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
            <a className="ghost" href={GRAFANA_DASHBOARD} target="_blank" rel="noreferrer" title="Cross-run metrics (Grafana)">Metrics ↗</a>
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

          {(run?.run_state === 'running' || run?.run_state === 'paused_hitl' || terminal) && (
            <div className="run-actions">
              {(run?.run_state === 'running' || run?.run_state === 'paused_hitl') && (
                <button className="stop" disabled={stopping} onClick={onStop} title="hard-stop this run — ends it early, cannot be resumed">
                  {stopping ? 'stopping…' : '■ Stop run'}
                </button>
              )}
              {terminal && (
                <button className="delete" onClick={onDelete} title="permanently delete this run and its data">
                  🗑 Delete run
                </button>
              )}
            </div>
          )}

          <PipelineGraph runId={runId} run={run} live={!terminal} />

          {run?.run_state === 'failed' && (
            <div className="failbanner">
              <span className="fb-badge">run failed</span>
              <span className="fb-reason">{run.failure_reason ?? 'no failure reason recorded'}</span>
            </div>
          )}

          {run?.run_state === 'stopped' && (
            <div className="stopbanner">
              <span className="sb-badge">manually stopped</span>
              <span className="sb-reason">{run.stopped_reason ?? 'no reason recorded'}</span>
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
                  tokens to add, in thousands (required) — adds to the current cap, e.g. 200 = +200,000
                  <input
                    type="number" min={1} step="1" placeholder="e.g. 200 = +200,000 tokens"
                    value={addTokensK} onChange={(e) => setAddTokensK(e.target.value)}
                  />
                  {addTokensK.trim() !== '' && !addTokensValid && (
                    <span className="fld-error">enter a positive number (thousands of tokens)</span>
                  )}
                </label>
              )}
              <div className="cb-actions">
                <button className="continue" disabled={continuing || !capValid} onClick={onContinue}>
                  {continuing ? 'continuing…' : capRequired ? 'Add tokens & continue' : addTokensK.trim() !== '' ? 'Add tokens & continue' : 'Continue run'}
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

          <RunOverview runId={runId} live={!terminal} capVersion={capVersion} progress={run?.progress} />

          <RunSettingsPanel runId={runId} live={!terminal} />

          <TokensByAgent runId={runId} live={!terminal} />

          <CostBreakdown runId={runId} live={!terminal} />

          {/* ONE content area below the overview — tabs instead of four stacked panels
              each with its own scrollbar. Live feeds keep an internal scroll (they must
              stick to bottom without moving the page); everything else page-scrolls. */}
          <nav className="tabbar" role="tablist">
            {(['activity', 'artifacts', 'microtasks', 'lessons'] as Tab[]).map((t) => (
              <button key={t} role="tab" aria-selected={tab === t}
                className={`tabbtn ${tab === t ? 'on' : ''}`} onClick={() => setTab(t)}>
                {t}
              </button>
            ))}
          </nav>

          {tab === 'activity' && (
            // live log is in-memory only — for a finished run it's empty noise, so the durable
            // history takes the full width; otherwise the two boxes share a drag-resizable split.
            terminal ? (
              <EventTimeline runId={runId} stop={terminal} />
            ) : (
              <ResizableCols
                left={<LogPanel runId={runId} active={!terminal} />}
                right={<EventTimeline runId={runId} stop={terminal} />}
              />
            )
          )}
          {tab === 'artifacts' && <ArtifactsPanel runId={runId} live={!terminal} />}
          {tab === 'microtasks' && <MicrotasksPanel runId={runId} live={!terminal} />}
          {tab === 'lessons' && <LessonsPanel runId={runId} live={!terminal} />}
        </>
      )}
    </div>
  )
}
