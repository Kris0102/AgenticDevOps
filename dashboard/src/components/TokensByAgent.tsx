import { useEffect, useMemo, useState } from 'react'
import { api, type EventView } from '../api'

// Per-agent slice of the run-wide "tokens used" total (RunOverview shows the aggregate).
// Bootstrap/deploy/push make no LLM calls and never emit llm.usage, so only the five
// LLM-driven agents appear here — fixed order, always shown (even at 0) so the panel
// never looks broken before an agent has had its first turn.

const AGENTS = ['planner', 'reviewer', 'coder', 'tester', 'reflector'] as const

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

export function TokensByAgent({ runId, live }: { runId: string; live: boolean }) {
  const [events, setEvents] = useState<EventView[]>([])

  useEffect(() => {
    let alive = true
    let cursor = 0
    let acc: EventView[] = []
    const load = async () => {
      try {
        for (;;) {
          const batch = await api.getEvents(runId, cursor)
          if (!alive || batch.length === 0) break
          cursor = batch[batch.length - 1].seq
          acc = [...acc, ...batch]
          if (batch.length < 200) break
        }
        if (alive) setEvents(acc)
      } catch {
        /* transient — next tick retries */
      }
    }
    setEvents([]); acc = []
    load()
    if (!live) return () => { alive = false }
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [runId, live])

  const byAgent = useMemo(() => {
    const totals: Record<string, number> = Object.fromEntries(AGENTS.map((a) => [a, 0]))
    // Cache-read subset (GH #65) — informational, already inside total_tokens' input
    // share; 0 for providers that don't report caching (the cluster today).
    const cached: Record<string, number> = Object.fromEntries(AGENTS.map((a) => [a, 0]))
    for (const e of events) {
      if (e.event_type !== 'llm.usage') continue
      const agent = String(e.payload?.agent ?? '')
      if (agent in totals && typeof e.payload?.total_tokens === 'number') {
        totals[agent] += e.payload.total_tokens as number
        if (typeof e.payload?.cache_read_tokens === 'number') {
          cached[agent] += e.payload.cache_read_tokens as number
        }
      }
    }
    return { totals, cached }
  }, [events])

  // Bar length is relative to the largest agent so far — a stable 1 keeps a brand-new
  // run's all-zero bars from divide-by-zero, not from faking a nonzero width.
  const max = Math.max(1, ...AGENTS.map((a) => byAgent.totals[a]))

  return (
    <div className="panel tokensbyagent">
      <h3>tokens by agent</h3>
      <div className="tbchart">
        {AGENTS.map((a) => (
          <div key={a} className="tbrow">
            <span className="tblabel">{a}</span>
            <div className="tbtrack">
              <div className={`tbfill cat-${a}`} style={{ width: `${(byAgent.totals[a] / max) * 100}%` }} />
            </div>
            <span className="tbvalue">
              {fmtTokens(byAgent.totals[a])}
              {byAgent.cached[a] > 0 && (
                <span className="tbcached" title="input tokens served from the provider's prompt cache (~10% of normal input price)">
                  {' '}· {fmtTokens(byAgent.cached[a])} cached
                </span>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
