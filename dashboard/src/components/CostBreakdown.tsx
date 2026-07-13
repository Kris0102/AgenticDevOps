import { useEffect, useState } from 'react'
import { api, type RunCostView } from '../api'

// Detailed USD cost view (GH #62), open/close on demand. The headline total is always
// visible; expanding reveals the per-phase/per-model breakdown with the token split and
// where the money went (input / output / cache). Backed by GET /runs/{id}/cost, which
// derives cost from the durable llm.usage events — so it's correct live, after a clean
// finish, AND after a crash/stall (unlike anything computed in the terminal deploy node).

const PHASES = ['planner', 'reviewer', 'coder', 'tester', 'reflector']

function usd(n: number): string {
  if (n === 0) return '$0.00'
  if (n < 0.01) return `$${n.toFixed(4)}`     // sub-cent: show enough digits to be non-zero
  return `$${n.toFixed(n < 1 ? 3 : 2)}`
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

export function CostBreakdown({ runId, live }: { runId: string; live: boolean }) {
  const [cost, setCost] = useState<RunCostView | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const c = await api.getCost(runId)
        if (alive) setCost(c)
      } catch {
        /* transient — next tick retries */
      }
    }
    setCost(null)
    load()
    if (!live) return () => { alive = false }
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [runId, live])

  const total = cost?.total_cost ?? 0
  const lines = cost?.lines ?? []

  return (
    <div className="panel costbreakdown">
      <button className="cost-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="cost-caret">{open ? '▾' : '▸'}</span>
        <h3>estimated cost</h3>
        <span className="cost-total">
          {cost?.has_unpriced ? '≥ ' : ''}{usd(total)}
        </span>
      </button>

      {cost?.has_unpriced && (
        <p className="cost-note">
          Some models have no list price (self-hosted) — their tokens are shown but not
          costed, so the total is a lower bound.
        </p>
      )}

      {open && (
        lines.length === 0 ? (
          <p className="cost-empty">No token usage recorded yet.</p>
        ) : (
          <div className="cost-table">
            <div className="cost-row cost-hrow">
              <span className="cx-phase">phase</span>
              <span className="cx-model">model</span>
              <span className="cx-num">tokens</span>
              <span className="cx-num">input</span>
              <span className="cx-num">output</span>
              <span className="cx-num">cache</span>
              <span className="cx-num cx-tot">cost</span>
            </div>
            {lines.map((ln, i) => {
              const catPhase = PHASES.includes(ln.phase) ? ln.phase : 'planner'
              const cacheCost = (ln.cost?.cache_read ?? 0) + (ln.cost?.cache_write ?? 0)
              return (
                <div className="cost-row" key={`${ln.phase}-${ln.model}-${i}`}>
                  <span className="cx-phase">
                    <span className={`cost-dot cat-${catPhase}`} /> {ln.phase}
                  </span>
                  <span className="cx-model" title={ln.model}>{ln.model}</span>
                  <span className="cx-num">{fmtTokens(ln.total_tokens)}</span>
                  <span className="cx-num">{ln.priced ? usd(ln.cost!.input) : '—'}</span>
                  <span className="cx-num">{ln.priced ? usd(ln.cost!.output) : '—'}</span>
                  <span className="cx-num">{ln.priced ? usd(cacheCost) : '—'}</span>
                  <span className="cx-num cx-tot">{ln.priced ? usd(ln.cost!.total) : 'unpriced'}</span>
                </div>
              )
            })}
          </div>
        )
      )}
    </div>
  )
}
