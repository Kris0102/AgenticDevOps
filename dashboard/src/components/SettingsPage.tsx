import { useEffect, useMemo, useState } from 'react'
import {
  api,
  LLM_AGENTS,
  type AppSettings,
  type LLMAgent,
  type ProviderProfile,
  type ProviderTestResult,
  type SchemaField,
} from '../api'
import { ModelPicker } from './ModelPicker'

const AGENT_LABELS: Record<LLMAgent, string> = {
  planner: 'Planner',
  reviewer: 'Reviewer',
  coder: 'Coder',
  tester: 'Tester',
  reflector: 'Reflector',
}
const AGENT_HINTS: Record<LLMAgent, string> = {
  planner: 'Investigates the repo, drafts the plan. A cheap/fast model is usually enough.',
  reviewer: 'Independently investigates and approves/rejects the plan. Cheap/fast is fine.',
  coder: 'Writes the actual code. The one agent that most wants your strongest model.',
  tester: 'Synthesizes tests from the acceptance criteria. Cheap/fast is fine.',
  reflector: 'Diagnoses a failed microtask and routes the fix. Mid-tier.',
}

// Global settings hub. Providers (endpoint+key), a Basic tier (default provider+model), and
// a collapsed Expert tier rendered from the backend schema so every field carries its own
// info tooltip. Keys are masked on load; leaving one masked keeps the stored value.

// dotted-path get/set into the settings object (keys come from the schema)
function getPath(obj: any, path: string): any {
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj)
}
function setPath(obj: any, path: string, value: any): any {
  const keys = path.split('.')
  const clone = structuredClone(obj)
  let cur = clone
  for (let i = 0; i < keys.length - 1; i++) cur = cur[keys[i]]
  cur[keys[keys.length - 1]] = value
  return clone
}

const GATES = ['plan', 'code', 'test', 'deploy']

function Info({ text }: { text: string }) {
  return (
    <span className="info-wrap" tabIndex={0}>
      <span className="info" aria-hidden="true">i</span>
      <span className="info-tip" role="tooltip">{text}</span>
    </span>
  )
}

export function SettingsPage({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [schema, setSchema] = useState<SchemaField[]>([])
  const [expert, setExpert] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [tests, setTests] = useState<Record<string, ProviderTestResult>>({})
  const [reveal, setReveal] = useState<Record<string, boolean>>({})
  // extra_request_params editing (GH #65): raw text per provider id while typing —
  // only valid JSON objects are written into settings; invalid input shows red.
  const [extraParamsText, setExtraParamsText] = useState<Record<string, string>>({})
  const [extraParamsError, setExtraParamsError] = useState<Record<string, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    api.getSettings().then(setSettings).catch((e) => setError(String(e)))
    api.getSettingsSchema().then(setSchema).catch(() => {})
  }, [])

  const expertFields = useMemo(() => schema.filter((f) => f.group === 'expert'), [schema])

  if (error) return <div className="settingspage"><div className="error">settings error: {error}</div></div>
  if (!settings) return <div className="settingspage"><div className="sp-loading">loading settings…</div></div>

  const s = settings
  const set = (path: string, v: any) => { setSettings(setPath(s, path, v)); setDirty(true) }
  // Providers referenced by at least one agent's selection — drives the "in use" badge.
  const usedProviderIds = new Set(LLM_AGENTS.map((a) => s.selected[a]?.provider_id))

  const save = async () => {
    setSaving(true)
    try { setSettings(await api.putSettings(s)); setDirty(false) }
    catch (e) { alert(`save failed: ${e}`) }
    finally { setSaving(false) }
  }

  const updateProvider = (i: number, patch: Partial<ProviderProfile>) => {
    const providers = s.providers.map((p, j) => (j === i ? { ...p, ...patch } : p))
    setSettings({ ...s, providers }); setDirty(true)
  }
  const addProvider = () => {
    const id = `provider-${s.providers.length + 1}`
    setSettings({ ...s, providers: [...s.providers, { id, label: 'New provider', type: 'openai_compatible', base_url: '', api_key: '', models: [] }] })
    setDirty(true)
  }
  const removeProvider = (i: number) => {
    setSettings({ ...s, providers: s.providers.filter((_, j) => j !== i) }); setDirty(true)
  }
  const test = async (id: string) => {
    setTests((t) => ({ ...t, [id]: { ok: false, reason: 'testing…' } }))
    try {
      const res = await api.testProvider(id)
      setTests((t) => ({ ...t, [id]: res }))
    } catch (e) {
      setTests((t) => ({ ...t, [id]: { ok: false, reason: String(e) } }))
    }
  }

  const renderField = (f: SchemaField) => {
    const val = getPath(s, f.key)
    if (f.type === 'gates') {
      const on: string[] = val || []
      return (
        <div className="gatechips">
          {GATES.map((g) => (
            <label key={g} className={`chip ${on.includes(g) ? 'on' : ''}`}>
              <input type="checkbox" checked={on.includes(g)}
                onChange={(e) => set(f.key, e.target.checked ? [...on, g] : on.filter((x) => x !== g))} />
              {g}
            </label>
          ))}
        </div>
      )
    }
    if (f.type === 'bool') {
      return (
        <label className={`chip ${val ? 'on' : ''}`}>
          <input type="checkbox" checked={!!val} onChange={(e) => set(f.key, e.target.checked)} />
          {val ? 'enabled' : 'disabled'}
        </label>
      )
    }
    const scale = f.scale ?? 1
    const displayVal = val == null ? '' : val / scale
    return (
      <div className="fld-numwrap">
        <input
          type="number" step={f.type === 'float' ? '0.1' : '1'} min={0} value={displayVal}
          onChange={(e) => {
            const raw = e.target.value
            if (f.nullable && raw === '') {
              set(f.key, null)
              setFieldErrors((fe) => ({ ...fe, [f.key]: '' }))
              return
            }
            const parsed = f.type === 'float' ? parseFloat(raw) : parseInt(raw, 10)
            if (raw.trim() === '' || !Number.isFinite(parsed) || parsed < 0) {
              setFieldErrors((fe) => ({ ...fe, [f.key]: 'enter a positive number' }))
              return
            }
            setFieldErrors((fe) => ({ ...fe, [f.key]: '' }))
            set(f.key, Math.round(parsed * scale))
          }}
        />
        {scale > 1 && <span className="fld-unit">× 1,000 tokens — e.g. 200 = 200,000</span>}
        {fieldErrors[f.key] && <span className="fld-error">{fieldErrors[f.key]}</span>}
      </div>
    )
  }

  return (
    <section className="settingspage">
      <header className="sp-head">
        <div className="sp-title">
          <button className="ghost sp-back" onClick={onClose}>← back</button>
          <div>
            <h2>Settings</h2>
            <p className="sp-sub">Providers, default model, and tuning knobs. Applies to new runs.</p>
          </div>
        </div>
        <div className="sp-actions">
          {dirty && <span className="sp-dirty">unsaved changes</span>}
          <button className="primary" disabled={!dirty || saving} onClick={save}>
            {saving ? 'saving…' : dirty ? 'Save changes' : '✓ Saved'}
          </button>
        </div>
      </header>

      {/* Providers */}
      <div className="sp-section">
        <div className="sp-section-head">
          <h3>Providers</h3>
          <p>Each provider has its own endpoint + API key. Add Claude or any OpenAI-compatible endpoint.</p>
        </div>

        {s.providers.map((p, i) => {
          const res = tests[p.id]
          return (
            <div key={i} className={`provcard type-${p.type} ${usedProviderIds.has(p.id) ? 'is-selected' : ''}`}>
              <div className="provcard-head">
                <span className="provbadge">{p.type === 'anthropic' ? 'Anthropic' : 'OpenAI-compatible'}</span>
                {usedProviderIds.has(p.id) && <span className="provdefault">● in use</span>}
                <div className="provcard-actions">
                  <button className="ghost sm" onClick={() => test(p.id)}>Test connection</button>
                  {s.providers.length > 1 && <button className="ghost sm danger" onClick={() => removeProvider(i)}>remove</button>}
                </div>
              </div>

              <div className="np-grid">
                <label className="fld"><span>id</span>
                  <input value={p.id} onChange={(e) => updateProvider(i, { id: e.target.value })} /></label>
                <label className="fld"><span>label</span>
                  <input value={p.label} onChange={(e) => updateProvider(i, { label: e.target.value })} /></label>
                <label className="fld"><span>type</span>
                  <select value={p.type} onChange={(e) => updateProvider(i, { type: e.target.value as any })}>
                    <option value="openai_compatible">openai_compatible</option>
                    <option value="anthropic">anthropic</option>
                  </select>
                </label>
                <label className="fld"><span>api key</span>
                  <div className="keyfield">
                    <input type={reveal[p.id] ? 'text' : 'password'} placeholder="leave masked to keep"
                      value={p.api_key ?? ''} onChange={(e) => updateProvider(i, { api_key: e.target.value })} />
                    <button type="button" className="reveal" title={reveal[p.id] ? 'hide' : 'show'}
                      onClick={() => setReveal((r) => ({ ...r, [p.id]: !r[p.id] }))}>{reveal[p.id] ? '🙈' : '👁'}</button>
                  </div>
                </label>
                {p.type === 'openai_compatible' && (
                  <label className="fld span2"><span>base url</span>
                    <input value={p.base_url ?? ''} onChange={(e) => updateProvider(i, { base_url: e.target.value })} /></label>
                )}
                {p.type === 'anthropic' && (
                  <label className="fld span2 checkfld" title="Extended (adaptive) thinking. On: the model reasons before answering — better plans/code, but slower and pricier per call. Off: faster and cheaper, weaker reasoning. Note: cannot be turned off on Fable 5.">
                    <input type="checkbox" checked={p.thinking !== false}
                      onChange={(e) => updateProvider(i, { thinking: e.target.checked })} />
                    <span>extended thinking <em>on = better reasoning, slower/pricier · off = faster/cheaper</em></span>
                  </label>
                )}
                <label className="fld" title="How many times the client retries a failed request (timeout / 429 / 5xx). Cluster: 2-3 (transient overloads). Slow-thinking Claude: 1 — a timeout there is deterministic, so extra retries just multiply the wait.">
                  <span>max retries</span>
                  <input type="number" min={0} max={5} value={p.max_retries ?? 2}
                    onChange={(e) => updateProvider(i, { max_retries: Math.max(0, Math.min(5, Number(e.target.value) || 0)) })} />
                </label>
                <div className="fld span2"><span>suggested models <em>optional — populate the model dropdowns</em></span>
                  <div className="modeltags">
                    {p.models.map((m, mi) => (
                      <span key={mi} className="modeltag">{m}
                        <button type="button" title="remove"
                          onClick={() => updateProvider(i, { models: p.models.filter((_, k) => k !== mi) })}>✕</button>
                      </span>
                    ))}
                    <input className="modeltag-add" placeholder="add a model, press Enter"
                      onKeyDown={(e) => {
                        if (e.key !== 'Enter') return
                        e.preventDefault()
                        const el = e.target as HTMLInputElement
                        const v = el.value.trim()
                        if (v && !p.models.includes(v)) updateProvider(i, { models: [...p.models, v] })
                        el.value = ''
                      }} />
                  </div>
                </div>
                <label className="fld span2"><span>extra request params <em>optional JSON, e.g. {'{"prompt_cache_key": "agentic"}'} — never secrets</em></span>
                  <input
                    className={extraParamsError[p.id] ? 'input-invalid' : ''}
                    placeholder="{}"
                    value={extraParamsText[p.id] ?? JSON.stringify(p.extra_request_params ?? {})}
                    onChange={(e) => {
                      const text = e.target.value
                      setExtraParamsText((t) => ({ ...t, [p.id]: text }))
                      try {
                        const parsed = text.trim() === '' ? {} : JSON.parse(text)
                        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                          updateProvider(i, { extra_request_params: parsed })
                          setExtraParamsError((er) => ({ ...er, [p.id]: false }))
                        } else {
                          setExtraParamsError((er) => ({ ...er, [p.id]: true }))
                        }
                      } catch {
                        setExtraParamsError((er) => ({ ...er, [p.id]: true }))  // keep typing; not saved until valid
                      }
                    }}
                  />
                </label>
              </div>

              {res && (
                <div className={`provtest ${res.reason === 'testing…' ? 'pending' : res.ok ? 'ok' : 'bad'}`}>
                  {res.reason === 'testing…'
                    ? <><span className="dot" /> testing…</>
                    : res.ok
                      ? <>
                          ✓ reachable · {res.latency_ms}ms
                          {res.cache
                            ? res.cache.active
                              ? <> · caching active ({(res.cache.read_tokens / 1000).toFixed(1)}k read)</>
                              : <span title="self-hosted endpoint? ask the operator to enable prefix caching (vLLM: --enable-prefix-caching)"> · caching not reported</span>
                            : <> · cache probe unavailable</>}
                        </>
                      : <>✗ {res.kind ? `${res.kind}: ` : ''}{res.reason}</>}
                </div>
              )}
            </div>
          )
        })}
        <button className="ghost addprov" onClick={addProvider}>+ add provider</button>
      </div>

      {/* GitHub */}
      <div className="sp-section">
        <div className="sp-section-head">
          <h3>GitHub</h3>
          <p>Token used to clone private repos and push the run branch. Without one, only public repos work.</p>
        </div>
        <div className="provcard type-github">
          <div className="np-grid">
            <label className="fld"><span>personal access token <em>repo scope</em></span>
              <div className="keyfield">
                <input type={reveal['__gh'] ? 'text' : 'password'} placeholder="leave masked to keep"
                  value={s.github_token ?? ''} onChange={(e) => set('github_token', e.target.value)} />
                <button type="button" className="reveal" title={reveal['__gh'] ? 'hide' : 'show'}
                  onClick={() => setReveal((r) => ({ ...r, __gh: !r['__gh'] }))}>{reveal['__gh'] ? '🙈' : '👁'}</button>
              </div>
            </label>
          </div>
          <div className="provactions-gh">
            <button className="ghost sm" onClick={async () => {
              setTests((t) => ({ ...t, __gh: { ok: false, reason: 'testing…' } }))
              try { const res = await api.testGithub(); setTests((t) => ({ ...t, __gh: res })) }
              catch (e) { setTests((t) => ({ ...t, __gh: { ok: false, reason: String(e) } })) }
            }}>Test connection</button>
            {dirty && <span className="gh-hint">save first — the test uses the stored token</span>}
          </div>
          {tests['__gh'] && (
            <div className={`provtest ${tests['__gh'].reason === 'testing…' ? 'pending' : tests['__gh'].ok ? 'ok' : 'bad'}`}>
              {tests['__gh'].reason === 'testing…'
                ? <><span className="dot" /> testing…</>
                : tests['__gh'].ok
                  ? <>✓ {tests['__gh'].reason} · {tests['__gh'].latency_ms}ms</>
                  : <>✗ {tests['__gh'].kind ? `${tests['__gh'].kind}: ` : ''}{tests['__gh'].reason}</>}
            </div>
          )}
        </div>
      </div>

      {/* Default model — per agent (GH #71) */}
      <div className="sp-section">
        <div className="sp-section-head">
          <h3>Default model per agent</h3>
          <p>What new runs use unless overridden on the run page. Point cheap/fast models at the
            investigation agents (planner / reviewer / tester) and your strongest at the Coder to
            cut cost without hurting code quality.</p>
        </div>
        <div className="agentmodels">
          {LLM_AGENTS.map((agent) => {
            const sel = s.selected[agent]
            const prov = s.providers.find((p) => p.id === sel.provider_id)
            return (
              <div key={agent} className="agentmodel-row">
                <div className="agentmodel-name">
                  {AGENT_LABELS[agent]} <Info text={AGENT_HINTS[agent]} />
                </div>
                <label className="fld"><span>provider</span>
                  <select value={sel.provider_id}
                    onChange={(e) => set(`selected.${agent}.provider_id`, e.target.value)}>
                    {s.providers.map((p) => <option key={p.id} value={p.id}>{p.label} ({p.id})</option>)}
                  </select>
                </label>
                <label className="fld"><span>model</span>
                  <ModelPicker models={prov?.models ?? []} value={sel.model}
                    onChange={(v) => set(`selected.${agent}.model`, v)} />
                </label>
              </div>
            )
          })}
        </div>
      </div>

      {/* Expert */}
      <div className="sp-section">
        <button className="expert-toggle" onClick={() => setExpert((v) => !v)}>
          <span className="chev">{expert ? '▾' : '▸'}</span> Expert settings
          <span className="expert-count">{expertFields.length}</span>
        </button>
        <p className="expert-hint">Per-agent tool-step / timeout limits, retry caps, and run defaults. Safe to leave alone.</p>
        {expert && (
          <div className="expert-grid">
            {expertFields.map((f) => (
              <label key={f.key} className="fld setfld">
                <span>{f.label} <Info text={f.description} /></span>
                {renderField(f)}
              </label>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}
