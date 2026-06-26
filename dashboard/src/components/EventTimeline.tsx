import { useEffect, useRef, useState } from 'react'
import { api, type EventView } from '../api'

// Durable history (poll GET /events?after=<seq>). Survives restarts — the audit record,
// vs the ephemeral live log. Paginates forward by the last seq seen.
export function EventTimeline({ runId, stop }: { runId: string; stop: boolean }) {
  const [events, setEvents] = useState<EventView[]>([])
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
        const batch = await api.getEvents(runId, cursor.current)
        if (dead || batch.length === 0) return
        cursor.current = batch[batch.length - 1].seq
        setEvents((prev) => [...prev, ...batch])
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

  return (
    <div className="panel events">
      <h3>history</h3>
      <div className="eventfeed">
        {events.map((e) => (
          <div key={e.seq} className="eventline">
            <span className="ets">{e.ts.slice(11, 19)}</span>
            <span className="eactor">{e.actor}</span>
            <span className="etype">{e.event_type}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
