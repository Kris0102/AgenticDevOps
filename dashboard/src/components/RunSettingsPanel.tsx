import { useEffect, useState } from 'react'
import { api, type RunConfigView, type SchemaField } from '../api'
import { ModelPicker } from './ModelPicker'

// Run-settings view (GH #64): the effective config THIS run is executing under. Everything is
// read-only (greyed) EXCEPT the per-agent model, which can be changed mid-run while the run is
// not terminal — the change takes effect on that agent's next phase.

const AGENT_LABELS: Record<string, string> = {
  planner: 'Planner', reviewer: 'Reviewer', coder: 'Coder', tester: 'Tester', reflector: 'Reflector',
}

// Which run value backs each expert schema key (knobs.* / a few defaults.*). Fields not
// listed (teardown, auto-PR) aren't fixed on the run, so they're skipped.
function valueFor(cfg: RunConfigView, key: string): { value: unknown; scale?: number } | null {
  if (key.startsWith('knobs.')) return { value: cfg.knobs[key.slice(6)] }
  if (key === 'defaults.max_total_iterations') return { value: cfg.constraints.max_total_iterations }
  if (key === 'defaults.max_microtask_attempts') return { value: cfg.constraints.max_microtask_attempts }
  if (key === 'defaults.max_total_tokens') return { value: cfg.constraints.max_total_tokens, scale: 1000 }
  if (key === 'defaults.hitl_checkpoints') return { value: cfg.hitl_checkpoints }
  return null
}

export function RunSettingsPanel({ runId, live }: { runId: string; live: boolean }) {
  const [cfg, setCfg] = useState<RunConfigView | null>(null)
  const [schema, setSchema] = useState<SchemaField[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)   // agent currently saving
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.getSettingsSchema().then(setSchema).catch(() => {})
  }, [])

  // The run's config only exists once the graph writes its first checkpoint — a just-created
  // run 404s here for a beat (POST /runs drives the graph in the background). Treat a failed
  // load as "not ready yet", NOT an error: poll silently until it lands, then (while live)
  // keep `editable` + models fresh. Stop once loaded on a terminal run (nothing changes).
  useEffect(() => {
    if (cfg && !live) return
    let cancelled = false
    const load = () => api.getRunConfig(runId).then((c) => { if (!cancelled) setCfg(c) }).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { cancelled = true; clearInterval(t) }
  }, [cfg, live, runId])

  if (!cfg) return null   // config not checkpointed yet — render nothing (no error banner)

  const patch = async (agent: string, providerId: string, model: string) => {
    if (!model.trim()) return
    setBusy(agent); setError(null)
    try { setCfg(await api.patchRunModel(runId, agent, providerId, model)) }
    catch (e) { setError(String(e)) }
    finally { setBusy(null) }
  }

  const expert = schema.filter((f) => f.group === 'expert')

  return (
    <div className="panel runsettings">
      <button className="expert-toggle rs-head" onClick={() => setOpen((v) => !v)}>
        <span className="chev">{open ? '▾' : '▸'}</span> Run settings
        <span className="rs-sub">{cfg.editable ? 'models editable during the run' : 'run is terminal — read-only'}</span>
      </button>

      {open && (
        <div className="rs-body">
          {error && <div className="error rs-error">{error}</div>}
          {/* Per-agent model — the ONLY editable part */}
          <div className="rs-section-head">
            <h4>Model per agent</h4>
            <p>Change a model mid-run — it takes effect on that agent's next phase. Point a stronger
              model at the Coder, cheaper ones at investigation, without restarting.</p>
          </div>
          <div className="agentmodels">
            {cfg.models.map((m) => {
              const prov = cfg.providers.find((p) => p.id === m.provider_id)
              return (
                <div key={m.agent} className="agentmodel-row">
                  <div className="agentmodel-name">
                    {AGENT_LABELS[m.agent] ?? m.agent}
                    {busy === m.agent && <span className="rs-saving"> saving…</span>}
                  </div>
                  <label className="fld"><span>provider</span>
                    <select value={m.provider_id} disabled={!cfg.editable || busy === m.agent}
                      onChange={(e) => patch(m.agent, e.target.value, m.model)}>
                      {cfg.providers.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                    </select>
                  </label>
                  <label className="fld"><span>model</span>
                    {cfg.editable ? (
                      <ModelPicker models={prov?.models ?? []} value={m.model}
                        onChange={(v) => patch(m.agent, m.provider_id, v)} />
                    ) : (
                      <input value={m.model} disabled />
                    )}
                  </label>
                </div>
              )
            })}
          </div>

          {/* Everything else — fixed for this run, greyed */}
          <div className="rs-section-head">
            <h4>Fixed for this run <span className="rs-lock">read-only</span></h4>
            <p>Tuning knobs and limits are locked once a run starts. Change them in Settings for future runs.</p>
          </div>
          <div className="expert-grid">
            {expert.map((f) => {
              const resolved = valueFor(cfg, f.key)
              if (!resolved) return null
              const { value, scale } = resolved
              let display: string
              if (Array.isArray(value)) display = value.length ? value.join(', ') : '—'
              else if (value == null) display = 'none'
              else if (scale && typeof value === 'number') display = `${value / scale} × 1,000`
              else display = String(value)
              return (
                <label key={f.key} className="fld setfld" title={f.description}>
                  <span>{f.label}</span>
                  <input value={display} disabled />
                </label>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
