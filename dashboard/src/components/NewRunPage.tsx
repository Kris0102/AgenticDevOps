import { useEffect, useRef, useState } from 'react'
import { api, LLM_AGENTS, type AppSettings, type DeployMode, type LessonView, type LLMConfig, type RepoView } from '../api'
import { buildRunRequest } from '../defaultRunRequest'
import { audience } from '../lessonInfo'
import { ModelPicker } from './ModelPicker'

const GATES = ['plan', 'code', 'test', 'deploy'] as const
const AGENT_LABELS = { planner: 'Planner', reviewer: 'Reviewer', coder: 'Coder', tester: 'Tester', reflector: 'Reflector' } as const

// Deploy modes in picker order (ADR 0036). Every mode still passes the deploy gate when
// it's ticked — the mode only decides what an approve DOES.
const DEPLOY_MODES: { mode: DeployMode; label: string; hint: string }[] = [
  { mode: 'dry_run', label: 'dry run', hint: 'nothing leaves the sandbox — review the diff at the gate, then the run just ends. No push token needed.' },
  { mode: 'push', label: 'push branch', hint: 'push the run branch to GitHub, no pull request.' },
  { mode: 'pr', label: 'push + PR', hint: 'push the run branch and open a pull request — the token needs PR access (checked at start).' },
  { mode: 'local_only', label: 'local branch', hint: 'no remote at all — the commit lands as a new branch in a git clone on this machine. No push token needed.' },
]

// The task becomes objective.description, which is injected VERBATIM (never truncated)
// into every planner synthesis attempt and every reviewer pass — so its size is multiplied
// by the synthesis retries and the planner↔reviewer loop. At ~4 chars/token a 50k file is
// ~12k tokens per pass, which a handful of iterations turns into a six-figure token bill or
// a context overflow. Warn early, refuse the truly unusable; never silently truncate — a
// dropped paragraph of a spec is a requirement the agents never see.
const TASK_WARN_CHARS = 20_000
const TASK_MAX_CHARS = 50_000
const TASK_FILE_EXTS = ['.txt', '.md', '.markdown'] as const
// `accept` only filters what the picker dialog SHOWS — its "All files" option walks straight
// past it. It is a convenience, never the check; onTaskFile does the enforcing.
const TASK_FILE_TYPES = '.txt,.md,.markdown,text/plain,text/markdown'

// Reject anything that isn't really text. A picked .png read via file.text() does not throw:
// TextDecoder replaces each undecodable byte with U+FFFD and hands back plausible-looking
// garbage, which would sail straight into the planner's prompt.
//
// Two signals, because they catch different things:
//   NUL bytes    — every ordinary binary (png/pdf/gz/elf) trips this one alone.
//   U+FFFD ratio — for the NUL-free stragglers, and for text in the wrong encoding.
// The 2% cut-off is measured, not guessed: a genuine spec carrying a couple of copy-paste
// artifacts lands at ~0.2%, latin-1 text decoded as UTF-8 at ~12%, a NUL-free binary at
// 100%. Anything above the cut-off is either binary or mis-encoded — and mis-encoded text
// is worth refusing too, since loading it would silently turn every umlaut into a "?" in
// the spec the agents build from. Base64 and other odd-but-valid text score 0%.
const BINARY_REPLACEMENT_RATIO = 0.02

function binaryReason(text: string): string | null {
  if (text.includes('\u0000')) return 'it contains NUL bytes'
  const replacements = (text.match(/\uFFFD/g) ?? []).length
  if (replacements / Math.max(text.length, 1) > BINARY_REPLACEMENT_RATIO) {
    return 'it is binary, or text saved in an encoding other than UTF-8 (re-save it as UTF-8)'
  }
  return null
}

// Per-run settings page: a registered-repo picker (GH #41) + task, with a model/gates
// override pre-filled from the global settings. Starting the run preflights the repo
// AND the provider server-side; a failure is reported inline and no run is created.
export function NewRunPage({ onRun, onCancel, onManageRepos, onManageLessons }: {
  onRun: (id: string) => void
  onCancel: () => void
  onManageRepos: () => void
  onManageLessons: () => void
}) {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [repos, setRepos] = useState<RepoView[] | null>(null)
  const [repoId, setRepoId] = useState('')
  const [branches, setBranches] = useState<string[]>([])
  const [branchWarning, setBranchWarning] = useState<string | null>(null)
  const [baseRef, setBaseRef] = useState('')
  const [baseSha, setBaseSha] = useState('')
  const [runName, setRunName] = useState('')  // New state for run name
  const [task, setTask] = useState('')
  const taskRef = useRef<HTMLTextAreaElement | null>(null)
  // Name of the .txt/.md the task was loaded from — provenance only; the textarea stays
  // the single source of truth, so the loaded text is editable like anything typed.
  const [taskFile, setTaskFile] = useState<string | null>(null)
  const [commitMessage, setCommitMessage] = useState('')
  const [pushBranch, setPushBranch] = useState('')
  // Seeded from the global Settings default (auto_create_pr: on → 'pr', off → 'push'),
  // then overridable for this run only (ADR 0036).
  const [deployMode, setDeployMode] = useState<DeployMode>('push')
  const [localExportPath, setLocalExportPath] = useState('')
  // Per-run, per-agent model override (GH #71), seeded from the global default.
  const [llm, setLlm] = useState<LLMConfig | null>(null)
  const [showModels, setShowModels] = useState(false)
  const [gates, setGates] = useState<Record<string, boolean>>({})
  // Operator rules the picked repo will impose on this run (ADR 0034) — shown here because
  // this is the moment you'd want to know, and because a rule you forgot you wrote is
  // indistinguishable from the agent misbehaving.
  const [rules, setRules] = useState<LessonView[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.getSettings().then((s) => {
      setSettings(s)
      setLlm(s.selected)
      setDeployMode(s.defaults.auto_create_pr ? 'pr' : 'push')
      setGates(Object.fromEntries(GATES.map((g) => [g, s.defaults.hitl_checkpoints.includes(g)])))
    }).catch((e) => setError(String(e)))
    api.listRepos().then(setRepos).catch((e) => setError(String(e)))
  }, [])

  // Load a .txt/.md spec into the task box. Read client-side and dropped into the same
  // textarea — the task is just `objective.description`, so a file needs no server support.
  // Everything here is a real check, not a dialog hint: the picker's `accept` only filters
  // what's shown, and "All files" walks straight past it.
  const onTaskFile = async (file: File) => {
    setError(null)
    const name = file.name.toLowerCase()
    if (!TASK_FILE_EXTS.some((ext) => name.endsWith(ext))) {
      setError(`${file.name} is not a text file — the task is sent to the agents as plain text, so pick a ${TASK_FILE_EXTS.join(', ')} file.`)
      return
    }
    // Size is checked BEFORE reading: file.text() on a multi-GB pick would hang the tab
    // before any length guard could run. Bytes >= chars in UTF-8, so this can only be
    // conservative — the exact char count is re-checked below.
    if (file.size > TASK_MAX_CHARS) {
      setError(`${file.name} is ${file.size.toLocaleString()} bytes — over the ${TASK_MAX_CHARS.toLocaleString()} limit. `
        + `The whole task is re-sent on every planning iteration, so a file this size would exhaust the run's token budget. `
        + `Trim it to the requirements the agents actually need.`)
      return
    }
    try {
      const text = await file.text()
      // The extension can lie (a renamed binary), so verify the CONTENT decoded as text.
      const notText = binaryReason(text)
      if (notText) {
        setError(`${file.name} isn't readable as text — ${notText}. Pick a plain ${TASK_FILE_EXTS.join(', ')} file.`)
        return
      }
      if (!text.trim()) {
        setError(`${file.name} is empty.`)
        return
      }
      setTask(text)
      setTaskFile(file.name)
    } catch (e) {
      setError(`could not read ${file.name}: ${String(e)}`)
    }
  }

  const repo = repos?.find((r) => r.repo_id === repoId)
  // Summary shown on the collapsed model row: one entry per distinct model.
  const modelSummary = llm
    ? [...new Set(LLM_AGENTS.map((a) => llm[a].model).filter(Boolean))].join(', ') || '—'
    : '…'

  // Live branch list per selected repo; a fetch failure only degrades to "default
  // branch" (base_ref = null) with a warning — the run can still start.
  useEffect(() => {
    setBranches([]); setBranchWarning(null); setBaseRef('')
    if (!repoId) return
    let stale = false
    api.getRepoBranches(repoId)
      .then((names) => {
        if (stale) return
        setBranches(names)
        const def = repos?.find((r) => r.repo_id === repoId)?.default_branch
        setBaseRef(def && names.includes(def) ? def : names[0] ?? '')
      })
      .catch((e) => { if (!stale) setBranchWarning(`branch list unavailable (${e}) — using the default branch`) })
    return () => { stale = true }
  }, [repoId, repos])

  // The picked repo's operator rules. Only the ones the server marked `injected` are listed
  // — a rule displaced past the cap is stored but never sent, so promising it here would be
  // a lie (the Lessons page is where that's explained). Informational: a fetch failure just
  // hides the block rather than blocking the run.
  useEffect(() => {
    setRules([])
    if (!repoId) return
    let stale = false
    api.getRepoLessons(repoId)
      .then((ls) => { if (!stale) setRules(ls.filter((l) => l.agent === 'human' && l.injected !== false)) })
      .catch(() => {})
    return () => { stale = true }
  }, [repoId])

  // Auto-grow the task textarea with its content, up to the CSS max-height (then it scrolls).
  useEffect(() => {
    const el = taskRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [task])

  const start = async () => {
    if (!settings || !repoId || !llm) return
    setBusy(true); setError(null)
    try {
      const req = buildRunRequest({
        repoId,
        baseRef: baseRef || null, 
        baseSha,
        task,
        // Every deploy mode passes the deploy gate (ADR 0036) — even a dry-run pauses
        // there so the human sees the final diff before the run ends.
        hitlCheckpoints: GATES.filter((g) => gates[g]),
        runName: runName || null,  // Include the run name in the request optional
        llm,
        maxPlannerReviewerIterations: settings.defaults.max_planner_reviewer_iterations,
        maxMicrotaskAttempts: settings.defaults.max_microtask_attempts,
        maxTotalTokens: settings.defaults.max_total_tokens,
        commitMessage: commitMessage.trim() || null,
        pushBranch: pushBranch.trim() || null,
        deployMode,
        localExportPath: deployMode === 'local_only' ? localExportPath.trim() || null : null,
      })
      const r = await api.createRun(req)
      onRun(r.run_id)
    } catch (e) {
      // repo/provider preflight failures come back as 4xx with the reason in the message
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="settingspage newrunpage">
      <header className="sp-head">
        <div className="sp-title">
          <button className="ghost sp-back" onClick={onCancel}>← back</button>
          <div>
            <h2>New run</h2>
            <p className="sp-sub">Repo + task, with an optional model / gate override for this run.</p>
          </div>
        </div>
      </header>

      <div className="panel">
        <div className="np-grid">
          <label className="fld"><span>repository</span>
            <select value={repoId} onChange={(e) => setRepoId(e.target.value)}>
              <option value="" disabled>{repos === null ? 'loading…' : repos.length ? 'pick a repo…' : 'no repos registered'}</option>
              {(repos ?? []).map((r) => (
                <option key={r.repo_id} value={r.repo_id}>
                  {r.label}{r.last_validation_result?.kind === 'ok_private' ? ' · private' : ''}
                </option>
              ))}
            </select>
            {repos?.length === 0 && (
              <em className="np-hint">no repos yet — <button type="button" className="linklike" onClick={onManageRepos}>add one on the Repos page</button></em>
            )}
          </label>
          <label className="fld"><span>base branch</span>
            <select value={baseRef} onChange={(e) => setBaseRef(e.target.value)} disabled={!repoId || branches.length === 0}>
              {branches.length === 0 && <option value="">{repoId ? (branchWarning ? 'default branch' : 'loading…') : '—'}</option>}
              {branches.map((b) => (
                <option key={b} value={b}>{b}{b === repo?.default_branch ? ' (default)' : ''}</option>
              ))}
            </select>
            {branchWarning && <em className="np-hint">{branchWarning}</em>}
          </label>
           <label className="fld"><span>run name <em>optional</em></span>
            <input placeholder="name for the run" value={runName} onChange={(e) => setRunName(e.target.value)} /></label>
          <label className="fld"><span>base sha <em>optional</em></span>
            <input placeholder="pin a commit on the base branch" value={baseSha} onChange={(e) => setBaseSha(e.target.value)} /></label>
          <div className="fld span2">
            <div className="task-head">
              <span>task</span>
              {/* A spec usually already exists as a file. Loading it just fills the textarea
                  below — which stays editable and remains the value actually submitted. */}
              <label className="task-file">
                <input type="file" accept={TASK_FILE_TYPES}
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) onTaskFile(f); e.target.value = '' }} />
                <span className="task-file-btn">＋ load .txt / .md</span>
              </label>
            </div>
            <textarea ref={taskRef} className="ta-task" placeholder="what should the agents implement?" value={task}
              onChange={(e) => { setTask(e.target.value); setTaskFile(null) }} />
            <div className="task-foot">
              {taskFile && <em className="np-hint">loaded from {taskFile} — edit freely, the box is what gets sent</em>}
              <span className="task-spacer" />
              {task.length > TASK_WARN_CHARS && (
                <em className={`np-hint ${task.length > TASK_MAX_CHARS ? 'bad' : 'warn'}`}>
                  {task.length.toLocaleString()} chars (~{Math.round(task.length / 4).toLocaleString()} tokens)
                  {task.length > TASK_MAX_CHARS
                    ? ' — too long to run'
                    : ' — re-sent on every planning iteration; trim if you can'}
                </em>
              )}
            </div>
          </div>
          {repoId && (
            <div className="fld span2">
              <div className="task-head">
                <span>operator rules <em>{rules.length ? 'this repo imposes on every run' : ''}</em></span>
                <button type="button" className="linklike" onClick={onManageLessons}>
                  {rules.length ? 'edit in Lessons' : 'add rules in Lessons'}
                </button>
              </div>
              {rules.length === 0 ? (
                <em className="np-hint">none — the agents follow only this task and what past runs taught them.</em>
              ) : (
                <ul className="np-rules">
                  {rules.map((r) => (
                    <li key={r.lesson_id}>
                      <span className={`lspill scope-${r.scope}`} title={audience(r.scope)}>{r.scope}</span>
                      <span className="np-rule-text">{r.summary}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <label className="fld span2"><span>deploy commit message <em>optional — editable again at the deploy gate</em></span>
            <input placeholder="built from the objective, microtasks and changed files" value={commitMessage} disabled={deployMode === 'dry_run'}
              onChange={(e) => setCommitMessage(e.target.value)} /></label>
          <label className="fld span2"><span>{deployMode === 'local_only' ? 'branch name' : 'push branch'} <em>optional — overrides this repo's registry default</em></span>
            <input placeholder="agent/run-<run id>" value={pushBranch} disabled={deployMode === 'dry_run'}
              onChange={(e) => setPushBranch(e.target.value)} /></label>
          <div className="fld span2"><span>deployment</span>
            {/* One mode per run (ADR 0036), seeded from Settings → auto_create_pr. */}
            <div className="gatechips">
              {DEPLOY_MODES.map(({ mode, label }) => (
                <label key={mode} className={`chip ${deployMode === mode ? 'on' : ''}`}>
                  <input type="radio" name="deploy-mode" checked={deployMode === mode}
                    onChange={() => setDeployMode(mode)} />
                  {label}
                </label>
              ))}
            </div>
            <em className="np-hint">{DEPLOY_MODES.find((m) => m.mode === deployMode)?.hint}</em>
          </div>
          {deployMode === 'local_only' && (
            <label className="fld span2"><span>local clone path <em>absolute path of a git clone on the machine the API runs on</em></span>
              <input placeholder="/home/you/projects/your-clone" value={localExportPath}
                onChange={(e) => setLocalExportPath(e.target.value)} /></label>
          )}

          <div className="fld span2"><span>model per agent <em>overrides the default for this run</em></span>
            <button type="button" className="expert-toggle" onClick={() => setShowModels((v) => !v)}>
              <span className="chev">{showModels ? '▾' : '▸'}</span> {modelSummary}
            </button>
            {showModels && llm && (
              <div className="agentmodels">
                {LLM_AGENTS.map((agent) => {
                  const sel = llm[agent]
                  const prov = settings?.providers.find((p) => p.id === sel.provider_id)
                  const patch = (p: Partial<typeof sel>) =>
                    setLlm({ ...llm, [agent]: { ...sel, ...p } })
                  return (
                    <div key={agent} className="agentmodel-row">
                      <div className="agentmodel-name">{AGENT_LABELS[agent]}</div>
                      <label className="fld"><span>provider</span>
                        <select value={sel.provider_id} onChange={(e) => patch({ provider_id: e.target.value })}>
                          {(settings?.providers ?? []).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                        </select></label>
                      <label className="fld"><span>model</span>
                        <ModelPicker models={prov?.models ?? []} value={sel.model} onChange={(v) => patch({ model: v })} />
                      </label>
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          <div className="fld span2"><span>human review gates</span>
            <div className="gatechips">
              {GATES.map((g) => (
                <label key={g} className={`chip ${gates[g] ? 'on' : ''}`}>
                  <input type="checkbox" checked={!!gates[g]}
                    onChange={(e) => setGates({ ...gates, [g]: e.target.checked })} />{g}
                </label>
              ))}
            </div>
          </div>
        </div>

        {error && <div className="error">{error}</div>}

        <div className="np-actions">
          <button className="ghost" onClick={onCancel}>Cancel</button>
          {/* task length is guarded here too, not just on file load — a paste reaches the
              same prompts by the same path. */}
          <button className="primary" disabled={busy || !repoId || !task.trim() || !settings || task.length > TASK_MAX_CHARS
            || (deployMode === 'local_only' && !localExportPath.trim())} onClick={start}>
            {busy ? 'preflighting…' : 'Start run'}
          </button>
        </div>
      </div>
    </section>
  )
}
