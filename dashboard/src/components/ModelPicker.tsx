// Pick a model for an agent from the provider's suggested models (a real dropdown), with a
// free-text fallback for anything not in the list. `models` is optional (GH #71): an empty
// list collapses to a plain text input. Stateless — "custom mode" is derived from whether the
// current value is one of the suggestions, so there is no local state to fall out of sync when
// the provider (and its model list) changes.
const CUSTOM = '__custom__'

export function ModelPicker({ models, value, onChange }: {
  models: string[]
  value: string
  onChange: (v: string) => void
}) {
  if (models.length === 0) {
    return <input placeholder="model id…" value={value} onChange={(e) => onChange(e.target.value)} />
  }
  const selectVal = models.includes(value) ? value : CUSTOM
  return (
    <div className="modelpick">
      <select value={selectVal}
        onChange={(e) => onChange(e.target.value === CUSTOM ? '' : e.target.value)}>
        {models.map((m) => <option key={m} value={m}>{m}</option>)}
        <option value={CUSTOM}>custom…</option>
      </select>
      {selectVal === CUSTOM && (
        <input placeholder="model id…" value={value} autoFocus
          onChange={(e) => onChange(e.target.value)} />
      )}
    </div>
  )
}
