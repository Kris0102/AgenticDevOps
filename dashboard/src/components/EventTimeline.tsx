import { useEffect, useRef, useState } from 'react'
import { api, type EventView } from '../api'
import { actorCat, BAD_EVENTS, eventLabel, eventSummary } from '../eventInfo'

// Durable history (poll GET /events?after=<seq>). Survives restarts — the audit record,
// vs the ephemeral live log. Renders the STORY: friendly label + payload summary per
// event, actor-colored. Tool calls (the bulk of rows) are folded behind a toggle so the
// domain events stay readable.
export function EventTimeline({ runId, stop }: { runId: string; stop: boolean }) {
  const [events, setEvents] = useState<EventView[]>([])
  const [showTools, setShowTools] = useState(false)
  const cursor = useRef(0)

  // Reset when the run changes.
  useEffect(() => {
    setEvents([])
    cursor.current = 0
  }, [runId])

  useEffect(() => {
    let dead = false
    const tick = async () => {
      try {
        // Drain everything available (old runs have full history waiting).
        for (;;) {
          const batch = await api.getEvents(runId, cursor.current)
          if (dead || batch.length === 0) return
          cursor.current = batch[batch.length - 1].seq
          setEvents((prev) => [...prev, ...batch])
          if (batch.length < 200) return
        }
      } catch {
        /* transient — next tick retries */
      }
    }
    tick()
    if (stop) return
    const handle = setInterval(tick, 2000)
    return () => {
      dead = true
      clearInterval(handle)
    }
  }, [runId, stop])

  const isNoisy = (e: EventView) => e.event_type.startsWith('tool.') || e.event_type === 'llm.usage'
  const toolCount = events.filter(isNoisy).length
  const visible = showTools ? events : events.filter((e) => !isNoisy(e))

  return (
    <div className="panel events">
      <div className="ev-head">
        <h3>history</h3>
        {toolCount > 0 && (
          <label className="ev-toggle">
            <input type="checkbox" checked={showTools} onChange={(e) => setShowTools(e.target.checked)} />
            tool calls / llm usage ({toolCount})
          </label>
        )}
      </div>
      <div className="eventfeed">
        {visible.map((e) => (
          <div key={e.seq} className={`eventline cat-${actorCat(e.actor)} ${BAD_EVENTS.has(e.event_type) ? 'bad' : ''}`}>
            <span className="ets">{e.ts.slice(11, 19)}</span>
            <span className="eactor">{e.actor}</span>
            <span className="ebody">
              <span className="etype">{eventLabel(e)}</span>
              {eventSummary(e) && <span className="esummary">{eventSummary(e)}</span>}
            </span>
          </div>
        ))}
        {visible.length === 0 && <div className="loading">no events</div>}
      </div>
    </div>
  )
}
